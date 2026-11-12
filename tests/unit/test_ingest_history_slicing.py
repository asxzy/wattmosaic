"""Unit tests for time-sliced recorder history.

Why these exist: the recorder answers one ``history/history_during_period``
request with one websocket frame, and ``HASource`` caps a frame at
``MAX_FRAME_BYTES`` (64 MiB).  Measured on the reference install with
``include_attributes=True`` that frame was 39.7 MB at 24 h, 59.8 MB at 36 h,
79.6 MB at 48 h, 119.2 MB at 72 h and 282.5 MB at 168 h -- so every window past
~40 h had the connection dropped, and discovery reported "history query did not
complete".  Slicing by *time* bounds the frame (12 h slices peaked at 20.1 MB);
slicing by entity count does not, because a handful of dense power rows dominate
the bytes (64-entity batches still produced a 266.8 MB frame).

These tests pin the three properties that make slicing safe:

* one request for a short window, several for a long one;
* seam-exact merging, because a duplicated boundary row is a phantom on/off
  event and the correlation/group code counts transitions;
* a failed slice fails the call, because a truncated window that looks complete
  would let the engine reason about a period it never read.

No network: the recorder is an in-memory fake that applies Home Assistant's own
SQL bounds (``last_updated_ts > start`` and ``< end``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.ingestion.correlation import parse_history_payload
from wattmosaic_engine.ingestion.ha_source import (
    HISTORY_SLICE_HOURS,
    HISTORY_SLICE_THRESHOLD_HOURS,
    ConnectionState,
    HASource,
    history_window_slices,
    merge_history_slices,
)

START = datetime(2026, 1, 1, tzinfo=UTC)

#: One CT row every minute and one switch that toggles.  The switch is the
#: transition counter, so the seam is measured on the thing the correlation code
#: actually counts.
CT_ENTITY = "sensor.ct1_power"
SWITCH_ENTITY = "light.fixture"


class _FakeRecorder:
    """A recorder that answers with Home Assistant's real window semantics.

    ``get_significant_states`` filters ``last_updated_ts > start`` and
    ``< end`` (strict on both sides), and with ``include_start_time_state`` it
    prepends a synthetic row stamped at the *query start* carrying the last state
    before that instant.  Both details matter for the seam: a strict ``< end``
    means a row exactly on the boundary belongs to nobody unless the next slice
    asks from one microsecond earlier, and the synthetic start row is the row most
    likely to be duplicated by a naive merge.
    """

    def __init__(
        self,
        rows: dict[str, list[tuple[float, str]]],
        *,
        fail_at: int | None = None,
        inclusive: bool = False,
    ) -> None:
        self.rows = {entity: sorted(values) for entity, values in rows.items()}
        self.fail_at = fail_at
        #: Emit the boundary row in *both* neighbours, the defect a naive
        #: inclusive-both-ends implementation would produce.
        self.inclusive = inclusive
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, msg_type: str, *, _timeout_s: float | None = None, **kwargs: Any):
        assert msg_type == "history/history_during_period"
        start = datetime.fromisoformat(kwargs["start_time"]).timestamp()
        end = datetime.fromisoformat(kwargs["end_time"]).timestamp()
        self.requests.append(
            {
                "start": start,
                "end": end,
                "include_start_time_state": kwargs.get("include_start_time_state"),
                "no_attributes": kwargs.get("no_attributes"),
                "timeout_s": _timeout_s,
            }
        )
        if self.fail_at is not None and len(self.requests) == self.fail_at:
            raise TimeoutError("recorder query exceeded its slice budget")
        out: dict[str, list[dict[str, Any]]] = {}
        for entity, values in self.rows.items():
            selected: list[dict[str, Any]] = []
            if kwargs.get("include_start_time_state"):
                prior = [row for row in values if row[0] < start]
                if prior:
                    selected.append({"s": prior[-1][1], "lu": start})
            for stamp, state in values:
                inside = (
                    start <= stamp <= end
                    if self.inclusive
                    else start < stamp < end
                )
                if inside:
                    selected.append({"s": state, "lu": stamp})
            if selected:
                out[entity] = selected
        return out


def _source(recorder: _FakeRecorder) -> HASource:
    """A connected source whose transport is the fake recorder."""
    source = HASource("http://ha.local:8123", "token")
    source._state = ConnectionState.CONNECTED
    source._request = recorder  # type: ignore[method-assign]
    return source


def _rows_hours(hours: int) -> dict[str, list[tuple[float, str]]]:
    """A CT row every minute plus a switch toggling exactly on the hour."""
    end = START + timedelta(hours=hours)
    ct: list[tuple[float, str]] = []
    moment = START
    while moment <= end:
        ct.append((moment.timestamp(), "100.0"))
        moment += timedelta(minutes=1)
    # Toggle at every whole hour: with 12 h slices, every interior boundary is
    # also a transition, which is the worst case for a duplicated seam row.
    # The first row sits just inside the window (the recorder drops a row exactly
    # at ``start``, which is the same rule for a sliced and an unsliced call).
    switch: list[tuple[float, str]] = [
        ((START + timedelta(seconds=30)).timestamp(), "off")
    ]
    for hour in range(1, hours):
        state = "on" if hour % 2 else "off"
        switch.append(((START + timedelta(hours=hour)).timestamp(), state))
    return {CT_ENTITY: ct, SWITCH_ENTITY: switch}


def _transitions(payload: dict[str, Any]) -> int:
    """Count on/off transitions the way ``observations_from_history`` sees them."""
    states, _, _ = parse_history_payload(payload)
    total = 0
    for rows in states.values():
        previous: str | None = None
        for _, text in rows:
            if previous is not None and text != previous:
                total += 1
            previous = text
    return total


def _count_at(payload: dict[str, Any], entity: str, stamp: float) -> int:
    return sum(1 for row in payload.get(entity, ()) if float(row["lu"]) == stamp)


# --------------------------------------------------------------------------- #
# Slicing only when needed
# --------------------------------------------------------------------------- #


def test_a_short_window_stays_one_request() -> None:
    """24 h measured 39.7 MB -- one frame, under the cap, and already ~4 s.

    Splitting it would double the round trips for no protection, so the
    threshold is deliberate: at or below 24 h the request is not cut.
    """
    slices = history_window_slices(START, START + timedelta(hours=24))
    assert slices == [(START, START + timedelta(hours=24))]
    assert HISTORY_SLICE_THRESHOLD_HOURS == 24.0


def test_a_week_is_sliced_into_bounded_windows() -> None:
    """168 h produced a 282.5 MB frame and a dropped connection.

    12 h slices peaked at 20.1 MB on the same data (3.2x headroom under the
    64 MiB cap, against 1.5x for the 24 h slice that already reached 41.5 MB).
    """
    end = START + timedelta(hours=168)
    slices = history_window_slices(START, end)
    assert len(slices) == 14
    assert HISTORY_SLICE_HOURS == 12.0
    assert all(stop - begin == timedelta(hours=12) for begin, stop in slices)
    # Consecutive, non-overlapping, and covering exactly the requested window.
    assert slices[0][0] == START
    assert slices[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(slices, slices[1:], strict=False))


@pytest.mark.asyncio
async def test_a_24_hour_request_issues_one_call_and_a_week_several() -> None:
    """The threshold must reach the transport, not just the helper."""
    recorder = _FakeRecorder(_rows_hours(168))
    source = _source(recorder)

    await source.history(
        [CT_ENTITY],
        start=START,
        end=START + timedelta(hours=24),
        include_attributes=True,
    )
    assert len(recorder.requests) == 1, "a 24 h request must not be split"

    await source.history(
        [CT_ENTITY],
        start=START,
        end=START + timedelta(hours=168),
        include_attributes=True,
    )
    assert len(recorder.requests) == 15, "a 7-day request must be sliced (1 + 14)"
    # Attributes are what makes the payload large; discovery still asks for them.
    assert all(request["no_attributes"] is False for request in recorder.requests)
    # Only the first slice carries the start-time state: a later slice's
    # synthesised start row would replay a stale value behind the previous
    # slice's rows and turn one boundary transition into two.  (Index 0 is the
    # 24 h request above; the 14 slices start at index 1.)
    assert [request["include_start_time_state"] for request in recorder.requests[1:]] == [
        True,
        *([False] * 13),
    ]


# --------------------------------------------------------------------------- #
# Seam-exact merging
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_boundary_row_is_merged_exactly_once() -> None:
    """A duplicated boundary row is a phantom on/off event.

    The recorder returns a row when ``start < last_updated_ts < end``, so a row
    exactly on a slice boundary belongs to neither neighbour unless the later
    slice asks from one microsecond earlier.  The fixture toggles at every whole
    hour and the slices are 12 h, so every interior boundary is simultaneously a
    transition -- the exact case where an inclusive-both-ends implementation
    would count each of them twice.
    """
    hours = 48
    recorder = _FakeRecorder(_rows_hours(hours))
    source = _source(recorder)

    merged = await source.history(
        [CT_ENTITY, SWITCH_ENTITY], start=START, end=START + timedelta(hours=hours)
    )

    assert len(recorder.requests) == 4
    # The row stamped at each interior boundary appears once, not twice.
    for boundary_hour in (12, 24, 36):
        stamp = (START + timedelta(hours=boundary_hour)).timestamp()
        assert _count_at(merged, SWITCH_ENTITY, stamp) == 1, (
            f"boundary row at +{boundary_hour}h was duplicated or dropped"
        )
        assert _count_at(merged, CT_ENTITY, stamp) == 1
    # ...and the merged transition count is what one unsliced fetch produces.
    unsliced = await _FakeRecorder(_rows_hours(hours))(
        "history/history_during_period",
        start_time=START.isoformat(),
        end_time=(START + timedelta(hours=hours)).isoformat(),
        entity_ids=[CT_ENTITY, SWITCH_ENTITY],
        include_start_time_state=True,
    )
    assert _transitions(merged) == _transitions(unsliced)
    assert _transitions(merged) == hours - 1
    # Every entity's rows are still time-ordered, as a single answer would be.
    for rows in merged.values():
        stamps = [row["lu"] for row in rows]
        assert stamps == sorted(stamps)


@pytest.mark.asyncio
async def test_an_inclusive_recorder_cannot_duplicate_a_seam_row() -> None:
    """Defence in depth: even a recorder that returns the boundary twice is safe.

    The structural half-open split is the primary guarantee, but a library change
    (or a future backend) that treats both bounds as inclusive must not be able to
    manufacture a transition.  The merge drops a later slice's row when it is not
    strictly newer than the youngest row already admitted for that entity.
    """
    hours = 48
    recorder = _FakeRecorder(_rows_hours(hours), inclusive=True)
    source = _source(recorder)

    merged = await source.history(
        [CT_ENTITY, SWITCH_ENTITY], start=START, end=START + timedelta(hours=hours)
    )

    for boundary_hour in (12, 24, 36):
        stamp = (START + timedelta(hours=boundary_hour)).timestamp()
        assert _count_at(merged, SWITCH_ENTITY, stamp) == 1
    assert _transitions(merged) == hours - 1


def test_merging_keeps_distinct_observations_inside_one_slice() -> None:
    """Only the *seam* is de-duplicated, never real history.

    Two rows at the same instant inside one answer are two observations the
    recorder really made (a state change and an attribute update coalesced into
    one timestamp); collapsing them would delete evidence, so the filter is
    strictly a slice-boundary rule.
    """
    first = {"light.fixture": [{"s": "on", "lu": 10.0}, {"s": "on", "lu": 20.0, "a": {"b": 1}}]}
    second = {"light.fixture": [{"s": "off", "lu": 20.0, "a": {"b": 2}}, {"s": "on", "lu": 30.0}]}

    merged = merge_history_slices([first, second])

    assert [row["lu"] for row in merged["light.fixture"]] == [10.0, 20.0, 30.0]
    assert merged["light.fixture"][-1]["s"] == "on"


# --------------------------------------------------------------------------- #
# Partial failure is a failure
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_slice_returns_no_partial_window() -> None:
    """Returning slices 1 and 3 of 3 would silently truncate the window.

    Discovery would then reason about a period it believes is complete.  The
    contract is therefore all-or-nothing, and the error names which slice failed
    so the user can see how much of the window could not be read.
    """
    recorder = _FakeRecorder(_rows_hours(36), fail_at=2)
    source = _source(recorder)

    payload = await source.history(
        [CT_ENTITY, SWITCH_ENTITY], start=START, end=START + timedelta(hours=36)
    )

    assert payload == {}, "a partial answer must never be returned as the window"
    assert len(recorder.requests) == 2, "the call must stop at the failed slice"
    assert source.last_history_error is not None
    assert "slice 2 of 3" in source.last_history_error
    assert "TimeoutError" in source.last_history_error


@pytest.mark.asyncio
async def test_a_successful_multi_slice_fetch_clears_the_error() -> None:
    """``last_history_error`` describes the most recent call, not the last bad one."""
    source = _source(_FakeRecorder(_rows_hours(48)))
    source.last_history_error = "slice 2 of 3 failed: TimeoutError: stale"

    payload = await source.history(
        [CT_ENTITY], start=START, end=START + timedelta(hours=48)
    )

    assert payload
    assert source.last_history_error is None


@pytest.mark.asyncio
async def test_a_failed_slice_is_reported_with_its_window() -> None:
    """The diagnostic must locate the failure, not just name an exception.

    "slice 3 of 4 [2026-01-02T00:00:00+00:00, 2026-01-02T12:00:00+00:00) failed"
    tells a user which part of the week could not be read; a bare
    ``TimeoutError`` says nothing about how much of the window is missing.
    """
    recorder = _FakeRecorder(_rows_hours(48), fail_at=3)
    source = _source(recorder)

    await source.history([CT_ENTITY], start=START, end=START + timedelta(hours=48))

    error = source.last_history_error or ""
    assert "slice 3 of 4" in error
    # The reported window is the *logical* slice, not the microsecond-nudged
    # query bound: one microsecond is the seam mechanism, not something a user
    # should have to reason about.
    assert "[2026-01-02T00:00:00+00:00, 2026-01-02T12:00:00+00:00)" in error
    assert datetime.fromtimestamp(recorder.requests[2]["end"], tz=UTC).isoformat() in error


@pytest.mark.asyncio
async def test_an_unsliced_failure_names_the_whole_window() -> None:
    """An unsliced request is not "slice 1 of 1" to the user.

    The slicing is an implementation detail; what the caller asked for is one
    window, so that is what the diagnostic must describe.
    """
    recorder = _FakeRecorder(_rows_hours(24), fail_at=1)
    source = _source(recorder)

    await source.history([CT_ENTITY], start=START, end=START + timedelta(hours=24))

    error = source.last_history_error or ""
    assert "the whole window" in error
    assert "slice 1 of 1" not in error
    assert "TimeoutError" in error
