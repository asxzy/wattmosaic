"""The ``discover_placements`` orchestration, end to end inside the engine.

``correlation`` decides *which CT row a switch is on* and ``auto_placement``
decides *whether that may become a device*; both have thorough unit tests.  What
had none was the step that joins them to the recorder and the configuration
store -- ``EngineCore.discover_placements``.  That gap mattered: while reviewing
this feature a hand-written replay returned ``accepted: []`` and looked exactly
like a regression, when the real fault was a malformed fixture.  The tests below
exercise the real method against a real ``Database``, so the wiring is pinned by
something that cannot be mis-typed into agreement.

Two fixture traps this file deliberately documents, because both silently defeat
correlation while looking like data:

* **A reading stamped at the transition is the new value.**  ``correlation``
  uses a strict ``<`` for the "before" sample.  A fixture that polls on the same
  cadence as it toggles therefore emits a sample *exactly* on each transition
  carrying the *old* value, and every step measures as zero.
* **The row must move *at* the transition, not before it.**  A helper that fills
  the "off" interval with "on" watts makes the row elevated throughout, so the
  transition shows no delta at all.

Both produce ``unresolved: no_row_responded`` and nothing else, which is why the
helpers below are written to keep readings strictly inside each interval.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    ServiceCommand,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.auto_placement import auto_device_id
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

BASELINE_W = 8.35
FIXTURE_W = 19.57
SECOND_W = 13.39

#: Total cycles recorded.  Three independent on/off pairs is the default
#: corroboration minimum, so this is the smallest history that can be accepted.
CYCLES = 3


# --------------------------------------------------------------------------- #
# A recorded history that looks like continuous polling
# --------------------------------------------------------------------------- #


def _sample(at: datetime, value: Any, attributes: dict[str, Any] | None = None) -> dict:
    row: dict[str, Any] = {"s": str(value), "lu": at.timestamp()}
    if attributes is not None:
        row["a"] = attributes
    return row


def _fill(
    rows: list[dict],
    start: datetime,
    end: datetime,
    watts: float,
    *,
    jitter: bool = True,
) -> None:
    """Poll one row every 30 s, strictly *inside* ``(start, end)``.

    Staying off the endpoints is the whole point: a sample on a transition
    instant would be read as the post-transition value and the step would
    disappear.
    """
    moment = start + timedelta(seconds=20)
    index = 0
    while moment < end - timedelta(seconds=5):
        value = watts + ((0.004 if index % 2 else -0.004) if jitter else 0.0)
        rows.append(_sample(moment, round(value, 3)))
        moment += timedelta(seconds=30)
        index += 1


def _history(
    start: datetime,
    *,
    levels: tuple[float, ...] = (),
) -> dict[str, list[dict]]:
    """One CT row with two fixtures, and optional per-cycle dimmer levels.

    ``levels`` is a per-cycle brightness fraction for the *first* fixture; an
    empty tuple means the fixture reported no level at all (a plain switch).
    """
    ct: list[dict] = []
    first: list[dict] = []
    second: list[dict] = []
    ct.append(_sample(start, BASELINE_W))
    first.append(_sample(start, "off"))
    second.append(_sample(start, "off"))

    moment = start
    for cycle in range(CYCLES):
        on_first = moment + timedelta(minutes=10)
        off_first = on_first + timedelta(minutes=3)
        _fill(ct, moment, on_first, BASELINE_W)
        fraction = levels[cycle] if levels else None
        attrs = {"brightness": int(round(fraction * 255))} if fraction else None
        first.append(_sample(on_first, "on", attrs))
        _fill(ct, on_first, off_first, BASELINE_W + FIXTURE_W * (fraction or 1.0))
        first.append(_sample(off_first, "off", attrs))

        on_second = off_first + timedelta(minutes=6)
        off_second = on_second + timedelta(minutes=3)
        _fill(ct, off_first, on_second, BASELINE_W)
        second.append(_sample(on_second, "on"))
        _fill(ct, on_second, off_second, BASELINE_W + SECOND_W)
        second.append(_sample(off_second, "off"))
        moment = off_second

    # A trailing baseline so the final falling edge has a reading after it.
    _fill(ct, moment, moment + timedelta(minutes=5), BASELINE_W)

    for rows in (ct, first, second):
        rows.sort(key=lambda item: item["lu"])
    return {
        "sensor.ct1_power": ct,
        "light.first": first,
        "light.second": second,
    }


class _FakeSource:
    """Stands in for the HA WebSocket source; serves states, registries, history."""

    connected = True

    def __init__(
        self,
        payload: dict[str, list[dict]],
        *,
        leaky: bool = False,
        states: list[dict[str, Any]] | None = None,
        registries: dict[str, Any] | None = None,
        fail_history: bool = False,
    ) -> None:
        self.payload = payload
        self.leaky = leaky
        self.fail_history = fail_history
        #: Set to make ``history`` fail the way a timeout or a dropped socket does.
        #: ``history`` returns ``{}`` for a failure by design, so the only way a
        #: caller can tell "the recorder is empty" from "the query did not finish"
        #: is this attribute -- mirroring ``HASource.last_history_error``.
        self.last_history_error: str | None = None
        self.calls: list[dict[str, Any]] = []
        # Candidate population now comes from the *state snapshot* (contract §2),
        # not from the topology.  Default to the two fixtures the topology used
        # to declare so the historical assertions still describe the same run.
        self._states = (
            states
            if states is not None
            else [
                {"entity_id": "light.first", "state": "off", "attributes": {}},
                {"entity_id": "light.second", "state": "off", "attributes": {}},
            ]
        )
        self._registries = registries or {"entities": [], "devices": []}

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return list(self._states)

    async def registries(self) -> dict[str, Any]:
        return dict(self._registries)

    async def history(
        self,
        entity_ids: Any,
        *,
        start: datetime,
        end: datetime | None = None,
        include_attributes: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, list[dict]]:
        self.calls.append(
            {
                "entity_ids": list(entity_ids),
                "include_attributes": include_attributes,
                "timeout_s": timeout_s,
            }
        )
        if self.fail_history:
            self.last_history_error = "TimeoutError: history query exceeded 30.0s"
            return {}
        self.last_history_error = None
        if self.leaky:
            # A source that ignores the request and returns everything it has.
            # ``discover_placements`` must still reason only about the entities
            # the state snapshot offered as candidates.
            return dict(self.payload)
        wanted = set(entity_ids)
        return {key: rows for key, rows in self.payload.items() if key in wanted}


# --------------------------------------------------------------------------- #
# Topology and core
# --------------------------------------------------------------------------- #


def _topology() -> Topology:
    """A confirmed topology with a main feed and one branch lighting circuit.

    The devices deliberately declare **no** state entities: contract §2 makes a
    declared entity ineligible for auto placement, and these tests are about the
    fixtures the *state snapshot* offers as candidates, not about re-placing an
    entity a person already linked.
    """
    return Topology(
        status=TopologyStatus.CONFIRMED,
        version=1,
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main_power",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct1",
                entity_id="sensor.ct1_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            ),
        ],
        devices=[],  # type: ignore[arg-type]
    )


def _core(
    tmp_path: Path,
    *,
    payload: dict[str, list[dict]] | None = None,
    auto_placement: bool = False,
    topology: Topology | None = None,
    leaky: bool = False,
    states: list[dict[str, Any]] | None = None,
    fail_history: bool = False,
) -> tuple[EngineCore, Database, _FakeSource]:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(
        database_path=str(tmp_path / "wm.db"),
        ha_token="not-used",
        auto_placement=auto_placement,
    )
    core = EngineCore(config, db)
    source = _FakeSource(
        payload if payload is not None else _history(_start()),
        leaky=leaky,
        states=states,
        fail_history=fail_history,
    )
    core.hasource = source  # type: ignore[assignment]
    stored = topology if topology is not None else _topology()
    db.save_topology(stored, note="test")
    core.topology_cache = stored
    return core, db, source


def _start() -> datetime:
    # Comfortably inside the default lookback; the age horizon is measured from
    # the request time, which is ``utcnow()``.
    return datetime.now(UTC) - timedelta(hours=4)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_analysis_alone_writes_nothing(tmp_path: Path) -> None:
    """An explicit ``apply=False`` is a decision, not "unset".

    The stored opt-in is on, so only the request can keep this read-only --
    exactly the case where a caller asks "just tell me what you would do".
    """
    core, db, _ = _core(tmp_path, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert result["applied"] is False
    assert db.load_device_configurations().devices == []
    assert {item["entity_id"] for item in result["accepted"]} == {
        "light.first",
        "light.second",
    }
    # The frozen §3.3 shape always reports the version it analysed against; what
    # must *not* change is that nothing was written.
    assert result["topology_version"] == 1
    assert all(item["applied"] is False for item in result["accepted"])


def test_apply_none_is_read_only_even_when_the_opt_in_is_on(tmp_path: Path) -> None:
    """``apply`` defaults to false: only an explicit ``True`` may write.

    The stored opt-in is a *permission*, not an instruction (contract §3.1).  A
    caller that omits ``apply`` asked for an analysis, and an analysis writes
    nothing -- otherwise a status poll would silently create devices.
    """
    core, db, _ = _core(tmp_path, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=None))

    assert result["applied"] is False
    assert db.load_device_configurations().devices == []
    assert "apply" in result["detail"].lower()


def test_a_disabled_opt_in_is_reported_rather_than_silent(tmp_path: Path) -> None:
    """The plan is returned with a reason, so the UI can offer the switch."""
    core, db, _ = _core(tmp_path, auto_placement=False)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=None))

    assert result["accepted"], "the analysis itself must still run"
    assert result["applied"] is False
    assert "disabled" in result["detail"]
    assert db.load_device_configurations().devices == []


def test_an_explicit_apply_writes_the_devices(tmp_path: Path) -> None:
    """Writing needs the request's consent *and* the stored opt-in (contract §3.1)."""
    core, db, _ = _core(tmp_path, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["applied"] is True
    assert result["topology_version"] == 2
    configurations = db.load_device_configurations().devices
    assert {config.device_id for config in configurations} == {
        auto_device_id("light.first"),
        auto_device_id("light.second"),
    }
    assert all(config.configured_by == "auto" for config in configurations)
    # The measured steps, not one shared average: the fixtures differ.  The
    # small tolerance is the poll jitter the fixture adds, which a real meter
    # also has.
    steps = sorted(config.expected.on_w for config in configurations)
    assert steps == pytest.approx([SECOND_W, FIXTURE_W], abs=0.05)


def test_the_created_device_is_materialised_and_inherits_the_row_phase(
    tmp_path: Path,
) -> None:
    """A fixture device absent from ``topology.devices`` must be created.

    Defect #54: it was saved and silently ignored.  The phase must also come
    from the CT row, because the fallback coefficient would double the row
    (see PROJECT_STATUS §7r.6c).
    """
    core, db, _ = _core(tmp_path, auto_placement=True)

    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    projected = db.load_topology()
    assert projected is not None
    created = {
        device.device_id: device
        for device in projected.devices
        if device.device_id.startswith("dev_auto_")
    }
    assert set(created) == {
        auto_device_id("light.first"),
        auto_device_id("light.second"),
    }
    assert all(device.phase == Phase.L1 for device in created.values())
    assert all(device.branch_ct_ids == ["ct1"] for device in created.values())


def test_a_second_run_does_not_duplicate_or_withdraw(tmp_path: Path) -> None:
    core, db, _ = _core(tmp_path, auto_placement=True)
    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))
    before = db.load_device_configurations().devices

    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))
    after = db.load_device_configurations().devices

    assert {c.device_id for c in after} == {c.device_id for c in before}
    assert len(after) == len(before)


# --------------------------------------------------------------------------- #
# Dimmer watts per level
# --------------------------------------------------------------------------- #


def test_a_dimmer_records_a_curve_from_its_recorded_levels(tmp_path: Path) -> None:
    """Brightness in history must become watts per level, not one number.

    The row moves by ``19.57 W × level``, so each cycle is a genuine point.
    """
    levels = (0.4, 0.7, 1.0)
    core, db, _ = _core(
        tmp_path, payload=_history(_start(), levels=levels), auto_placement=True
    )

    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    configuration = next(
        c
        for c in db.load_device_configurations().devices
        if c.device_id == auto_device_id("light.first")
    )
    assert configuration.light is not None
    curve = configuration.light.dimming_curve
    assert len(curve) == len(levels)
    # Stored as a fraction of full-on; the watts are reconstructed once.
    assert [round(level, 2) for level, _ in curve] == pytest.approx([0.4, 0.7, 1.0])
    assert configuration.light.full_on_w() == pytest.approx(FIXTURE_W, abs=0.1)


def test_a_switch_without_levels_gets_no_curve(tmp_path: Path) -> None:
    """No reported level is "no dimming evidence", never "full brightness"."""
    core, db, _ = _core(tmp_path, auto_placement=True)

    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    configuration = next(
        c
        for c in db.load_device_configurations().devices
        if c.device_id == auto_device_id("light.first")
    )
    assert configuration.light is None or not configuration.light.dimming_curve


# --------------------------------------------------------------------------- #
# Refusals that must reach the caller
# --------------------------------------------------------------------------- #


def test_a_main_row_is_never_a_placement_target(tmp_path: Path) -> None:
    """A feed measures the whole installation; crediting one switch is wrong."""
    topology = _topology().model_copy(
        update={
            "channels": [
                CTChannel(
                    ct_id="ct1",
                    entity_id="sensor.ct1_power",
                    ct_type=CTType.MAIN,
                    phase=Phase.BOTH,
                )
            ]
        }
    )
    core, db, _ = _core(tmp_path, topology=topology, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["accepted"] == []
    assert db.load_device_configurations().devices == []


def test_a_row_owned_by_a_dedicated_measurement_is_refused_with_the_remedy(
    tmp_path: Path,
) -> None:
    """The whole row already belongs to one device; a second would double it.

    The message must name the conversion, because the user's only action is to
    turn that channel into a shared circuit.
    """
    topology = _topology().model_copy(
        update={
            "devices": [
                *(_topology().devices),
                DeviceTopology(
                    device_id="dev_lights",
                    name="lights",
                    phase=Phase.L1,
                    dedicated_ct_ids=["ct1"],
                ),
            ]
        }
    )
    core, db, _ = _core(tmp_path, topology=topology, auto_placement=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["accepted"] == []
    assert result["refused"], "the refusal reason was not reported"
    assert all(
        "dedicated" in reason.lower() for reason in result["refused"].values()
    )
    assert db.load_device_configurations().devices == []


def test_no_history_is_reported_not_treated_as_a_clean_result(tmp_path: Path) -> None:
    """An empty recorder answer must not look like "nothing correlated"."""
    core, _, _ = _core(tmp_path, payload={})

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=None))

    assert result["accepted"] == []
    assert result["skipped"].get("reason") == "no recorded history"


def test_a_failed_history_query_is_not_reported_as_an_empty_recorder(
    tmp_path: Path,
) -> None:
    """A timeout is a fact about the query, not about the recorder.

    ``HASource.history`` returns ``{}`` for *any* failure -- by design, because a
    caller must never read failure as "no change".  The consequence was that a
    timeout and a genuinely empty recorder were indistinguishable, and the engine
    reported "no recorded history" for a query that had simply not finished.  That
    is not hypothetical: 7 days of history across 485 entities takes longer than
    the 30 s default on the reference install, so asking for a week produced a
    confident, wrong statement that the recorder held nothing.

    The distinction is a separate reason *and* the underlying cause, so a user can
    tell "your recorder is empty" from "this window is too big to answer in time".
    """
    core, _, source = _core(tmp_path, fail_history=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=None))

    assert result["accepted"] == []
    assert result["skipped"].get("reason") == "history query did not complete"
    assert result["skipped"].get("reason") != "no recorded history"
    assert "TimeoutError" in (result["skipped"].get("detail") or "")


def test_the_history_timeout_grows_with_the_requested_window(tmp_path: Path) -> None:
    """A week of history must be given more time than a day.

    The query cost grows with both the window and the entity count, so a fixed
    timeout cannot serve both: one tight enough to notice a dead socket on a 24 h
    request is far too tight for 7 days (measured 7 s at 24 h against 17 s at
    72 h on the reference install, with the 7-day whole-house request exceeding
    30 s).  The budget is therefore a function of ``lookback_hours``, and this pins
    that it actually reaches the source.
    """
    core, _, source = _core(tmp_path)

    asyncio.run(core.discover_placements(lookback_hours=24, apply=None))
    short = source.calls[-1]["timeout_s"]
    asyncio.run(core.discover_placements(lookback_hours=168, apply=None))
    long = source.calls[-1]["timeout_s"]

    assert short is not None and long is not None
    assert long > short, "a 7-day query must be given a larger budget than a 24 h one"
    # The base keeps a short window from waiting as long as a week: 24 h asks for
    # far less than the linear term for 168 h alone would suggest.
    assert short < long / 3, "the budget must be dominated by the window, not a constant"


def test_no_candidate_state_entities_short_circuits_with_a_reason(tmp_path: Path) -> None:
    """An empty snapshot is "nothing to analyse", and must say so.

    Candidate population comes from the state snapshot (contract §2), not from
    the topology, so the absence of *devices* is no longer the trigger: the
    trigger is that Home Assistant offered no entity whose domain could be a
    fixture.
    """
    core, _, _ = _core(tmp_path, states=[])

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=None))

    assert result["accepted"] == []
    assert result["skipped"]["reason"] == "no branch CT rows or no candidate state entities"


# --------------------------------------------------------------------------- #
# The service surface
# --------------------------------------------------------------------------- #


def test_the_command_rejects_a_non_numeric_lookback(tmp_path: Path) -> None:
    core, _, _ = _core(tmp_path)

    from wattmosaic_engine.api.facade import InvalidRequest

    with pytest.raises(InvalidRequest):
        asyncio.run(
            core.command(
                ServiceCommand(
                    command="discover_placements",
                    parameters={"lookback_hours": "yesterday"},
                )
            )
        )


def test_the_command_rejects_an_out_of_range_lookback(tmp_path: Path) -> None:
    core, _, _ = _core(tmp_path)

    from wattmosaic_engine.api.facade import InvalidRequest

    for hours in (0.0, -1, 10_000):
        with pytest.raises(InvalidRequest):
            asyncio.run(
                core.command(
                    ServiceCommand(
                        command="discover_placements",
                        parameters={"lookback_hours": hours},
                    )
                )
            )


# --------------------------------------------------------------------------- #
# Guards that only bite on a deliberately awkward recording
# --------------------------------------------------------------------------- #


def _only_row_moves(
    start: datetime,
    moving_entity: str,
    *,
    extra_entities: tuple[str, ...] = (),
) -> dict[str, list[dict]]:
    """A recording where one row tracks one switch and everything else is flat.

    ``moving_entity`` is the CT row that answers the switch; ``extra_entities``
    are additional state entities that toggle in step with it, for exercising
    the "declared entities only" filter.
    """
    moving: list[dict] = []
    flat: list[dict] = []
    switch: list[dict] = []
    extras = {name: [] for name in extra_entities}
    moving.append(_sample(start, BASELINE_W))
    flat.append(_sample(start, BASELINE_W))
    switch.append(_sample(start, "off"))
    for name in extras:
        extras[name].append(_sample(start, "off"))

    moment = start
    for _ in range(CYCLES):
        on_at = moment + timedelta(minutes=10)
        off_at = on_at + timedelta(minutes=3)
        _fill(moving, moment, on_at, BASELINE_W)
        _fill(flat, moment, on_at, BASELINE_W)
        switch.append(_sample(on_at, "on"))
        for name in extras:
            extras[name].append(_sample(on_at, "on"))
        _fill(moving, on_at, off_at, BASELINE_W + FIXTURE_W)
        _fill(flat, on_at, off_at, BASELINE_W)
        switch.append(_sample(off_at, "off"))
        for name in extras:
            extras[name].append(_sample(off_at, "off"))
        moment = off_at

    _fill(moving, moment, moment + timedelta(minutes=5), BASELINE_W)
    _fill(flat, moment, moment + timedelta(minutes=5), BASELINE_W)
    payload = {
        moving_entity: sorted(moving, key=lambda item: item["lu"]),
        "sensor.ct1_power": sorted(flat, key=lambda item: item["lu"]),
        "light.first": sorted(switch, key=lambda item: item["lu"]),
    }
    for name, rows in extras.items():
        payload[name] = sorted(rows, key=lambda item: item["lu"])
    return payload


def test_a_main_row_is_never_correlated_even_when_it_tracks_a_switch(
    tmp_path: Path,
) -> None:
    """A feed answers every switch in the house at once, so it is not evidence.

    Here the *only* row that moves is the main feed.  Excluding mains is what
    keeps the answer empty; without the filter this switch would be placed on
    the whole-installation row, which is the worst possible attribution.
    """
    topology = _topology().model_copy(
        update={
            "channels": [
                CTChannel(
                    ct_id="main",
                    entity_id="sensor.main_power",
                    ct_type=CTType.MAIN,
                    phase=Phase.BOTH,
                ),
                CTChannel(
                    ct_id="ct1",
                    entity_id="sensor.ct1_power",
                    ct_type=CTType.BRANCH,
                    phase=Phase.L1,
                ),
            ]
        }
    )
    payload = _only_row_moves(_start(), "sensor.main_power")
    core, _, _ = _core(tmp_path, topology=topology, payload=payload)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert result["accepted"] == []
    # Refused *at correlation*, not filtered afterwards by the placement rules:
    # a main row is not evidence about one fixture, even though it moved.
    assert result["refused"] == {}
    assert "light.first" in result["unresolved"]


def test_an_entity_outside_the_state_snapshot_is_never_placed(tmp_path: Path) -> None:
    """Evidence about an entity Home Assistant did not report is never collected.

    ``light.rogue`` toggles in step with the moving row in the *recorder* payload,
    so a correlation would happily accept it.  It is absent from the state
    snapshot, which is the population contract §2 defines -- so it was never a
    candidate, and asking the recorder about it would be collecting evidence for
    a device nobody offered.
    """
    payload = _history(_start())
    payload["light.rogue"] = [dict(row) for row in payload["light.first"]]
    payload["light.second"] = [
        {"s": "off", "lu": row["lu"]} for row in payload["sensor.ct1_power"]
    ]
    core, _, source = _core(tmp_path, payload=payload)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert [item["entity_id"] for item in result["accepted"]] == ["light.first"]
    # It was not even requested: asking for it would be collecting evidence
    # about an entity the state snapshot never offered.
    requested = {entity for call in source.calls for entity in call["entity_ids"]}
    assert "light.rogue" not in requested


def test_history_is_requested_with_attributes(tmp_path: Path) -> None:
    """Without attributes the recorder returns no brightness and no curve."""
    core, _, source = _core(tmp_path)

    asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    assert source.calls, "no history was requested at all"
    assert all(call["include_attributes"] is True for call in source.calls)


def test_an_over_returning_source_cannot_widen_the_candidate_set(
    tmp_path: Path,
) -> None:
    """The declared-entity filter must not rely on the source behaving.

    A recorder query names exactly which entities to return, but a source that
    answers with more (a caching bug, a fallback to ``/api/states``, a future
    refactor) must not be able to smuggle an undeclared entity into placement.
    ``light.rogue`` tracks the row perfectly here; only the re-filter keeps it
    out.
    """
    payload = _history(_start())
    payload["light.rogue"] = [dict(row) for row in payload["light.first"]]
    core, _, source = _core(tmp_path, payload=payload, leaky=True)

    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=False))

    accepted = {item["entity_id"] for item in result["accepted"]}
    assert "light.rogue" not in accepted
    assert accepted == {"light.first", "light.second"}
