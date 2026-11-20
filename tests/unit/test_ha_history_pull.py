"""Synthetic-only tests for the HA history pull tool (no network, no live data).

Covers the pure parsing/helpers, day chunking and batching, the request windows
recorded by a fake client, resume semantics, factor derivation and output-path
safety.  Nothing here reads a real Home Assistant or writes tracked files.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools.dev import ha_history_pull as pull_mod

T0 = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)


def _iso(offset_s: float) -> str:
    return (T0 + timedelta(seconds=offset_s)).isoformat()


def _epoch(offset_s: float) -> float:
    """Epoch seconds relative to 2026-11-11 (the day used by the pull tests)."""
    base = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    return (base + timedelta(seconds=offset_s)).timestamp()


# --------------------------------------------------------------------------- #
# Fake client
# --------------------------------------------------------------------------- #
class FakeClient:
    """Records every history query; returns rows keyed by requested entity."""

    def __init__(self, payload: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = payload or {}

    async def ws_command(self, msg_type: str, **params: Any) -> dict[str, Any]:
        self.calls.append({"type": msg_type, **params})
        result: dict[str, Any] = {}
        for entity_id in params.get("entity_ids", []):
            result[entity_id] = [dict(row) for row in self.payload.get(entity_id, [])]
        return result

    async def states(self) -> list[dict[str, Any]]:
        return []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _make_args(**overrides: Any) -> argparse.Namespace:
    args = argparse.Namespace(
        readers="readers.json",
        days=10.0,
        start=None,
        end=None,
        events="none",
        events_file=None,
        out="data/raw/ha_history",
        refresh=False,
        force_out=False,
        derive_factors_only=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _write_readers(path: Path, readers: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps({"readers": readers}))
    return path


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["unavailable", "unknown", "", "  ", None, True, "nan", "inf"])
def test_non_numeric_states_dropped(bad: object) -> None:
    assert pull_mod.parse_numeric(bad) is None


def test_numeric_strings_become_floats() -> None:
    assert pull_mod.parse_numeric("123.4") == 123.4
    assert pull_mod.parse_numeric(7) == 7.0


def test_sample_rows_drop_non_numeric_and_sort_by_time() -> None:
    entity = "sensor.x_power"
    payload = {
        entity: [
            {"s": "123.4", "lu": _epoch(20)},
            {"s": "unavailable", "lu": _epoch(10)},
            {"s": "5.0", "lu": _epoch(5)},
            {"s": "9.0"},  # no timestamp
        ]
    }
    rows = pull_mod.sample_rows_from_history(payload, [entity])
    assert [row["v"] for row in rows] == [5.0, 123.4]
    assert [row["t"] for row in rows] == sorted(row["t"] for row in rows)
    assert all(row["entity_id"] == entity for row in rows)


def test_event_rows_keep_attributes() -> None:
    entity = "climate.living"
    payload = {
        entity: [
            {"s": "heat", "lu": _epoch(2), "a": {"hvac_action": "heating"}},
            {"s": "off", "lu": _epoch(1), "a": {"hvac_action": "idle"}},
        ]
    }
    rows = pull_mod.event_rows_from_history(payload, [entity])
    assert [row["t"] for row in rows] == sorted(row["t"] for row in rows)
    assert rows[0]["state"] == "off"
    assert "attributes" in rows[0]


# --------------------------------------------------------------------------- #
# Chunking / batching / request windows
# --------------------------------------------------------------------------- #
def test_chunk_days_splits_on_utc_boundaries() -> None:
    start = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
    end = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
    chunks = pull_mod.chunk_days(start, end)
    assert [key for key, _, _ in chunks] == ["2026-11-10", "2026-11-11"]
    # Chunks align to full UTC days (the per-day file is the unit of resume).
    assert chunks[0][1] == datetime(2026, 11, 10, tzinfo=UTC)
    assert chunks[0][2] == datetime(2026, 11, 11, tzinfo=UTC)
    assert chunks[1][1] == datetime(2026, 11, 11, tzinfo=UTC)
    assert chunks[1][2] == datetime(2026, 11, 11, 1, tzinfo=UTC)


def test_batches_at_most_fifty() -> None:
    ids = [f"sensor.x{i}" for i in range(120)]
    result = pull_mod.batches(ids)
    assert [len(batch) for batch in result] == [50, 50, 20]


def test_fetch_window_records_request_windows_and_batches() -> None:
    ids = [f"sensor.x{i}" for i in range(51)]
    client = FakeClient()

    asyncio.run(
        pull_mod.fetch_window(
            client,  # type: ignore[arg-type]
            ids,
            start=T0,
            end=T0 + timedelta(days=1),
            is_event=False,
        )
    )
    calls = [call for call in client.calls if call["type"] == "history/history_during_period"]
    assert len(calls) == 2
    assert [len(call["entity_ids"]) for call in calls] == [50, 1]
    assert calls[0]["start_time"] == pull_mod.iso(T0)
    assert calls[0]["end_time"] == pull_mod.iso(T0 + timedelta(days=1))
    assert calls[0]["minimal_response"] is True
    assert calls[0]["no_attributes"] is True
    assert calls[0]["significant_changes_only"] is False


def test_include_start_time_state_only_on_first_chunk() -> None:
    client = FakeClient()

    ids = [f"sensor.x{i}" for i in range(60)]
    asyncio.run(
        pull_mod.fetch_window(
            client,  # type: ignore[arg-type]
            ids,
            start=T0,
            end=T0 + timedelta(days=1),
            is_event=False,
            first_chunk=True,
        )
    )
    flags = [call["include_start_time_state"] for call in client.calls]
    assert flags == [True, False]


def test_event_history_helper_keeps_attributes() -> None:
    client = FakeClient()

    asyncio.run(
        pull_mod.fetch_history_events(
            client,  # type: ignore[arg-type]
            ["climate.x"],
            start=pull_mod.iso(T0),
            end=pull_mod.iso(T0 + timedelta(hours=1)),
        )
    )
    call = client.calls[0]
    assert call["minimal_response"] is False
    assert call["no_attributes"] is False
    assert call["significant_changes_only"] is False


# --------------------------------------------------------------------------- #
# Auto event selection
# --------------------------------------------------------------------------- #
def test_event_candidates_exclude_own_domain_and_non_domains() -> None:
    states: list[dict[str, Any]] = [
        {"entity_id": "switch.kitchen", "attributes": {}},
        {"entity_id": "climate.hall", "attributes": {}},
        {"entity_id": "sensor.wattmosaic_power", "attributes": {}},
        {"entity_id": "wattmosaic.status", "attributes": {}},
        {"entity_id": "sensor.temperature", "attributes": {}},
        {"entity_id": "light.desk", "attributes": {}},
    ]
    candidates = pull_mod.event_candidates(
        states,
        is_event_domain=lambda eid, attrs: True,
    )
    assert candidates == ["climate.hall", "light.desk", "switch.kitchen"]


def test_event_selection_caps_and_orders_by_change_count() -> None:
    selected = pull_mod.select_event_entities(
        ["switch.a", "switch.b", "switch.c"],
        {"switch.a": 2, "switch.b": 9},
        limit=2,
    )
    assert selected == ["switch.b", "switch.a"]


# --------------------------------------------------------------------------- #
# Output safety
# --------------------------------------------------------------------------- #
def test_out_outside_data_raw_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        pull_mod.resolve_out(tmp_path / "elsewhere")


def test_out_inside_data_raw_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "data" / "raw" / "ha_history"
    monkeypatch.setattr(pull_mod, "ROOT", tmp_path)
    assert pull_mod.resolve_out(target) == target.resolve()
    assert pull_mod.resolve_out(tmp_path / "tracked", force=True) == (tmp_path / "tracked").resolve()


# --------------------------------------------------------------------------- #
# Resume (end-to-end with fake client)
# --------------------------------------------------------------------------- #
def _run_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: FakeClient,
    readers: list[dict[str, Any]],
    now: datetime,
    start: datetime,
    end: datetime,
    complete_days: dict[str, Any] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    out = tmp_path / "data" / "raw" / "ha_history"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pull_mod, "ROOT", tmp_path)
    reader_path = _write_readers(tmp_path / "gt.json", readers)
    if complete_days:
        (out / "manifest.json").write_text(
            json.dumps({"schema": pull_mod.SCHEMA_HISTORY, "days": complete_days})
        )
    args = _make_args(
        readers=str(reader_path),
        out=str(out),
        start=pull_mod.iso(start),
        end=pull_mod.iso(end),
        refresh=refresh,
    )
    return asyncio.run(
        pull_mod.pull(
            args,
            client=client,  # type: ignore[arg-type]
            now=now,
        )
    )


def test_resume_skips_complete_days_and_refetches_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readers = [{"power_entity": "sensor.p", "factor_inputs": []}]
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)  # 12th is a partial day
    start = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    client = FakeClient()
    manifest = _run_pull(
        tmp_path,
        monkeypatch,
        client=client,
        readers=readers,
        now=now,
        start=start,
        end=end,
        complete_days={
            "2026-11-10": {"complete": True, "samples": 1, "events": 0},
            "2026-11-11": {"complete": True, "samples": 1, "events": 0},
        },
    )
    days = [key for key, _, _ in pull_mod.chunk_days(start, end)]
    fetched = {call["start_time"][:10] for call in client.calls}
    # Only the current (partial) day is re-fetched.
    assert fetched == {"2026-11-12"}
    assert manifest["days"]["2026-11-10"]["complete"] is True
    assert manifest["days"]["2026-11-12"]["complete"] is False

    client2 = FakeClient()
    _run_pull(
        tmp_path,
        monkeypatch,
        client=client2,
        readers=readers,
        now=now,
        start=start,
        end=end,
        complete_days={
            "2026-11-10": {"complete": True, "samples": 1, "events": 0},
            "2026-11-11": {"complete": True, "samples": 1, "events": 0},
            "2026-11-12": {"complete": False, "samples": 1, "events": 0},
        },
        refresh=True,
    )
    fetched2 = {call["start_time"][:10] for call in client2.calls}
    assert fetched2 == set(days)


def test_earliest_row_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    readers = [{"power_entity": "sensor.p", "factor_inputs": []}]
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    start = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    client = FakeClient(
        {"sensor.p": [{"s": "10", "lu": _epoch(5)}, {"s": "11", "lu": _epoch(10)}]}
    )
    manifest = _run_pull(tmp_path, monkeypatch, client=client, readers=readers, now=now, start=start, end=now)
    assert manifest["earliest_row"] is not None
    assert manifest["earliest_row"].startswith("2026-11-11")
    sample_file = tmp_path / "data" / "raw" / "ha_history" / "samples-2026-11-11.jsonl.gz"
    with gzip.open(sample_file, "rt") as fh:
        assert len(fh.readlines()) == 2


# --------------------------------------------------------------------------- #
# Factor derivation
# --------------------------------------------------------------------------- #
def _factor_rows(
    *,
    base: datetime = T0,
    factor: float,
    p: float = 1000.0,
    v: float = 230.0,
    i: float = 5.0,
    pf: float = 1.0,
    count: int = 6,
    stride: float = 2.0,
) -> dict[str, list[dict[str, str | float]]]:
    """Build aligned P/V/I/PF rows for one hour with the true factor baked in."""
    actual_i = p / (v * pf * factor)
    rows: dict[str, list[dict[str, str | float]]] = {"P": [], "V": [], "I": [], "PF": []}
    for n in range(count):
        ts = (base + timedelta(seconds=n * stride)).timestamp()
        rows["P"].append({"t": datetime.fromtimestamp(ts, UTC).isoformat(), "v": p})
        rows["V"].append({"t": datetime.fromtimestamp(ts, UTC).isoformat(), "v": v})
        rows["I"].append({"t": datetime.fromtimestamp(ts, UTC).isoformat(), "v": actual_i})
        rows["PF"].append({"t": datetime.fromtimestamp(ts, UTC).isoformat(), "v": pf})
    return rows


def _reader(power: str = "sensor.power", inputs: list[str] | None = None) -> dict[str, Any]:
    return {
        "power_entity": power,
        "factor_inputs": inputs if inputs is not None else ["sensor.voltage", "sensor.current", "sensor.power_factor"],
    }


def _assemble(prefix: str | None, rows: dict[str, list[dict[str, str | float]]]) -> dict[str, list[dict[str, Any]]]:
    """Attach (P, V, I, PF) rows under either the default or a prefixed id set."""
    if prefix is None:
        return {
            "sensor.power": rows["P"],
            "sensor.voltage": rows["V"],
            "sensor.current": rows["I"],
            "sensor.power_factor": rows["PF"],
        }
    return {
        f"sensor.{prefix}_power": rows["P"],
        f"sensor.{prefix}_voltage": rows["V"],
        f"sensor.{prefix}_current": rows["I"],
        f"sensor.{prefix}_power_factor": rows["PF"],
    }


def test_derive_factors_detects_x1_and_x2() -> None:
    one = _assemble("one", _factor_rows(factor=1.0))
    two = _assemble("two", _factor_rows(factor=2.0))
    rows = {**one, **two}
    result = pull_mod.derive_factors(
        rows,
        [
            _reader("sensor.one_power", ["sensor.one_voltage", "sensor.one_current", "sensor.one_power_factor"]),
            _reader("sensor.two_power", ["sensor.two_voltage", "sensor.two_current", "sensor.two_power_factor"]),
        ],
    )
    assert result["factors"]["sensor.one_power"][0]["factor"] == 1.0
    assert result["factors"]["sensor.two_power"][0]["factor"] == 2.0
    assert result["unknown"] == []


def test_derive_factors_change_mid_series_creates_two_intervals() -> None:
    first = _factor_rows(base=T0, factor=1.0)
    second = _factor_rows(base=T0 + timedelta(hours=2), factor=2.0)
    third = _factor_rows(base=T0 + timedelta(hours=3), factor=2.0)
    merged: dict[str, list[dict[str, str | float]]] = {}
    for key in ("P", "V", "I", "PF"):
        merged[key] = first[key] + second[key] + third[key]
    result = pull_mod.derive_factors(_assemble(None, merged), readers=[_reader()])
    intervals = result["factors"]["sensor.power"]
    assert [interval["factor"] for interval in intervals] == [1.0, 2.0]
    change_hour = intervals[1]["from"][11:13]
    assert change_hour in ("02", "03")


def test_derive_factors_single_stray_bucket_does_not_change() -> None:
    h0 = _factor_rows(base=T0, factor=1.0)
    h1 = _factor_rows(base=T0 + timedelta(hours=1), factor=2.0)
    h2 = _factor_rows(base=T0 + timedelta(hours=2), factor=1.0)
    h3 = _factor_rows(base=T0 + timedelta(hours=3), factor=1.0)
    merged: dict[str, list[dict[str, str | float]]] = {}
    for key in ("P", "V", "I", "PF"):
        merged[key] = h0[key] + h1[key] + h2[key] + h3[key]
    result = pull_mod.derive_factors(_assemble(None, merged), readers=[_reader()])
    intervals = result["factors"]["sensor.power"]
    assert len(intervals) == 1
    assert intervals[0]["factor"] == 1.0


def test_derive_factors_ignores_low_current_and_low_pf() -> None:
    # All instants have tiny current and tiny PF; none qualify.
    rows = _factor_rows(factor=1.0, p=1000.0, v=230.0, i=0.1, pf=0.1)
    result = pull_mod.derive_factors(_assemble(None, rows), readers=[_reader()])
    assert result["factors"] == {}
    assert "sensor.power" in result["unknown"]


def test_derive_factors_unknown_without_qualifying_data() -> None:
    result = pull_mod.derive_factors({}, readers=[_reader()])
    assert result["factors"] == {}
    assert result["unknown"] == ["sensor.power"]


def test_derive_factors_drops_numeric_string_states() -> None:
    rows = _factor_rows(factor=1.0)
    # numeric strings are accepted as floats, same rule as sample rows
    rows["P"] = [{"t": row["t"], "v": str(row["v"])} for row in rows["P"]]
    result = pull_mod.derive_factors(_assemble(None, rows), readers=[_reader()])
    assert result["factors"]["sensor.power"][0]["factor"] == 1.0
