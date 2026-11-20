"""Tests for the read-only HA recorder coverage probe (raw-capture T05).

Everything here is offline: HA compressed-history payloads are built by hand and
the fetch layer is patched.  No network, no Home Assistant, no live install.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "audit"))

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _row(ts: datetime, value: float = 1.0, *, key: str = "lu") -> dict[str, Any]:
    return {"s": str(value), key: ts.timestamp()}


def _mod():
    import check_recorder_coverage as mod

    return mod


# --- pure coverage_report ---------------------------------------------------


def test_ok_entity() -> None:
    mod = _mod()
    rows = [_row(START), _row(START + timedelta(hours=1)), _row(START + timedelta(hours=2))]
    report = mod.coverage_report({"sensor.ct1": rows}, ["sensor.ct1"], start=START, end=END)
    assert len(report) == 1
    cov = report[0]
    assert cov.entity_id == "sensor.ct1"
    assert cov.status == "ok"
    assert cov.rows == 3
    assert cov.first == START
    assert cov.last == START + timedelta(hours=2)


def test_missing_entity() -> None:
    mod = _mod()
    report = mod.coverage_report(
        {"sensor.ct1": [_row(START)], "sensor.ct2": []},
        ["sensor.ct2", "sensor.ct3"],
        start=START,
        end=END,
    )
    assert [c.status for c in report] == ["missing", "missing"]
    for cov in report:
        assert cov.rows == 0
        assert cov.first is None
        assert cov.last is None
        assert cov.max_gap_s is None


def test_short_entity() -> None:
    mod = _mod()
    start = START
    end = START + timedelta(days=3)
    late = start + timedelta(days=2)
    report = mod.coverage_report(
        {"sensor.ct1": [_row(late)]}, ["sensor.ct1"], start=start, end=end
    )
    assert report[0].status == "short"
    assert report[0].first == late


def test_lc_fallback_timestamp() -> None:
    mod = _mod()
    ts = START + timedelta(minutes=5)
    report = mod.coverage_report(
        {"sensor.ct1": [_row(ts, key="lc")]}, ["sensor.ct1"], start=START, end=END
    )
    assert report[0].first == ts
    assert report[0].last == ts
    assert report[0].rows == 1


def test_max_gap_includes_tail_to_end() -> None:
    mod = _mod()
    rows = [_row(START), _row(START + timedelta(hours=1))]
    report = mod.coverage_report({"sensor.ct1": rows}, ["sensor.ct1"], start=START, end=END)
    # 1 h between the two rows, 23 h from the last row to END.
    assert report[0].max_gap_s == pytest.approx(23 * 3600.0)


def test_non_numeric_states_skipped() -> None:
    mod = _mod()
    rows = [
        _row(START, value=10),
        {"s": "unavailable", "lu": (START + timedelta(minutes=1)).timestamp()},
        {"s": "unknown", "lu": (START + timedelta(minutes=2)).timestamp()},
        {"s": "", "lu": (START + timedelta(minutes=3)).timestamp()},
    ]
    report = mod.coverage_report({"sensor.ct1": rows}, ["sensor.ct1"], start=START, end=END)
    assert report[0].rows == 1
    assert report[0].status == "ok"


# --- streaming fold ---------------------------------------------------------


def test_streaming_fold_matches_pure_report() -> None:
    mod = _mod()
    ids = ["sensor.ct1", "sensor.ct2"]
    all_times = [START + timedelta(minutes=20 * i) for i in range(9)]
    payload = {
        "sensor.ct1": [_row(t, value=float(i)) for i, t in enumerate(all_times)],
        "sensor.ct2": [_row(t, value=float(i), key="lc") for i, t in enumerate(all_times[:5])],
    }
    pure = mod.coverage_report(payload, ids, start=START, end=END)

    accumulators = {eid: mod._Accumulator(eid) for eid in ids}
    # Fold the same rows in three chronological chunks per entity.  Use the
    # values from the one-shot payload so the two paths see identical rows.
    for lo, hi in ((0, 3), (3, 6), (6, 9)):
        chunk = {
            "sensor.ct1": payload["sensor.ct1"][lo:hi],
            "sensor.ct2": payload["sensor.ct2"][max(0, min(lo, 5)) : min(hi, 5)],
        }
        mod.fold_chunk(accumulators, chunk)
    streamed = [acc.finish(start=START, end=END) for acc in accumulators.values()]
    assert streamed == pure


# --- topology DB entity ids -------------------------------------------------


def test_entities_from_topology_db(tmp_path: Path) -> None:
    mod = _mod()
    db = tmp_path / "topology.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE topology_versions (payload_json TEXT, version INTEGER)")
        payload = {
            "channels": [
                {"entity_id": "sensor.ct1_power", "ct_id": "ct1"},
                {"entity_id": "sensor.ct2_power", "ct_id": "ct2"},
                {"entity_id": "sensor.unassigned"},
            ]
        }
        conn.execute(
            "INSERT INTO topology_versions (payload_json, version) VALUES (?, ?)",
            (json.dumps(payload), 7),
        )
        conn.commit()
    finally:
        conn.close()
    assert mod.entity_ids_from_topology_db(db) == ["sensor.ct1_power", "sensor.ct2_power"]


# --- CLI exit code ----------------------------------------------------------


def test_cli_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _mod()
    ids = ["sensor.ct1", "sensor.ct2"]
    canned_missing = {"sensor.ct1": [_row(START)], "sensor.ct2": []}
    canned_ok = {
        "sensor.ct1": [_row(START), _row(END)],
        "sensor.ct2": [_row(START), _row(END)],
    }

    active_payload: list[dict[str, list[dict[str, Any]]]] = []

    async def fake_stream(entity_ids, *, start, end, accumulators, chunk=None):
        mod.fold_chunk(accumulators, active_payload[0])

    monkeypatch.setattr(mod, "stream_coverage", fake_stream)

    argv = ["--entities", *ids, "--days", "1", "--end", END.isoformat()]
    active_payload.append(canned_missing)
    assert mod.main(argv) == 1
    active_payload[0] = canned_ok
    assert mod.main(argv) == 0
