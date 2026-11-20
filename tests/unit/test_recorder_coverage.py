"""Tests for the read-only HA recorder coverage probe (raw-capture T05).

Everything here is offline: HA compressed-history payloads are built by hand and
the client layer is patched.  No network, no Home Assistant, no live install.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import asynccontextmanager
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
        {"s": "nan", "lu": (START + timedelta(minutes=4)).timestamp()},
        {"s": "inf", "lu": (START + timedelta(minutes=5)).timestamp()},
    ]
    report = mod.coverage_report({"sensor.ct1": rows}, ["sensor.ct1"], start=START, end=END)
    assert report[0].rows == 1
    assert report[0].status == "ok"
    assert mod.non_numeric_counts({"sensor.ct1": rows}, ["sensor.ct1"]) == {"sensor.ct1": 5}


# --- streaming fold --------------------------------------------------------


def test_streaming_fold_matches_pure_report() -> None:
    mod = _mod()
    ids = ["sensor.ct1", "sensor.ct2"]
    all_times = [START + timedelta(minutes=20 * i) for i in range(9)]
    payload = {
        # Include duplicate timestamps, a row exactly at END and a row after END.
        "sensor.ct1": [
            _row(t, value=float(i))
            for i, t in enumerate(all_times)
        ]
        + [_row(all_times[3], value=99.0), _row(END, value=5.0), _row(END + timedelta(hours=1), value=6.0)],
        "sensor.ct2": [_row(t, value=float(i), key="lc") for i, t in enumerate(all_times[:5])],
    }
    pure = mod.coverage_report(payload, ids, start=START, end=END)

    accumulators = {eid: mod._Accumulator(eid) for eid in ids}
    # Fold every row, split into chronological chunks.  The duplicate, the row
    # exactly at END and the row after END are all folded exactly once; the
    # shared _summarize rule must ignore the after-END row for both paths.
    ct1 = payload["sensor.ct1"]
    ct2 = payload["sensor.ct2"]
    for lo1, hi1, lo2, hi2 in ((0, 4, 0, 2), (4, 8, 2, 4), (8, 12, 4, 5)):
        mod.fold_chunk(
            accumulators,
            {"sensor.ct1": ct1[lo1:hi1], "sensor.ct2": ct2[lo2:hi2]},
        )
    streamed = [acc.finish(start=START, end=END) for acc in accumulators.values()]
    assert streamed == pure


# --- stream_coverage against a fake client ----------------------------------


def test_stream_coverage_chunks_and_real_history(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _mod()
    calls: list[dict[str, Any]] = []

    # Two real rows only in the first hour; nothing after, so the recorder
    # stopped mid-window and a large max_gap_s / early `last` must be reported.
    real_rows = [_row(START), _row(START + timedelta(minutes=30))]

    class FakeClient:
        async def ws_command(self, msg_type: str, **params: Any) -> Any:
            calls.append({"type": msg_type, **params})
            start = datetime.fromisoformat(params["start_time"])
            include = bool(params["include_start_time_state"])
            rows: dict[str, list[dict[str, Any]]] = {}
            if start == START:
                rows["sensor.ct1"] = list(real_rows)
                rows["sensor.ct2"] = [_row(START)]
            if include:
                # HA prepends a synthetic start-time row per entity with history.
                for entity_rows in rows.values():
                    entity_rows.insert(0, _row(start, value=0.0))
            return rows

    @asynccontextmanager
    async def fake_from_env():
        yield FakeClient()

    monkeypatch.setattr(mod.HAClient, "from_env", staticmethod(fake_from_env))

    ids = ["sensor.ct1", "sensor.ct2"]
    accs = asyncio.run(
        mod.stream_coverage(
            ids,
            start=START,
            end=START + timedelta(hours=3),
            chunk=timedelta(hours=1),
        )
    )

    assert len(calls) == 3
    bounds = [(c["start_time"], c["end_time"]) for c in calls]
    assert bounds == [
        (START.isoformat(), (START + timedelta(hours=1)).isoformat()),
        ((START + timedelta(hours=1)).isoformat(), (START + timedelta(hours=2)).isoformat()),
        ((START + timedelta(hours=2)).isoformat(), (START + timedelta(hours=3)).isoformat()),
    ]
    assert [c["include_start_time_state"] for c in calls] == [True, False, False]
    for call in calls:
        assert call["type"] == "history/history_during_period"
        assert call["minimal_response"] is True
        assert call["no_attributes"] is True
        assert call["significant_changes_only"] is False
        assert call["entity_ids"] == ids

    cov = [acc.finish(start=START, end=START + timedelta(hours=3)) for acc in accs.values()]
    ct1 = next(c for c in cov if c.entity_id == "sensor.ct1")
    # Only the synthetic row (START) + two real rows are folded; no per-chunk
    # synthetic rows inflate the gap.  The recorder stops after 30 minutes.
    assert ct1.last == START + timedelta(minutes=30)
    assert ct1.max_gap_s == pytest.approx(2.5 * 3600.0)
    assert ct1.status == "ok"


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


# --- CLI behaviour ----------------------------------------------------------


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


def test_cli_rejects_bad_dates_and_days() -> None:
    mod = _mod()
    with pytest.raises(SystemExit) as exc:
        mod.main(["--entities", "sensor.ct1", "--end", "not-a-date"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        mod.main(["--entities", "sensor.ct1", "--days", "0"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        mod.main(["--entities", "sensor.ct1", "--days", "-1"])
    assert exc.value.code == 2


def test_cli_missing_db_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _mod()
    missing = tmp_path / "absent.db"
    code = mod.main(["--db", str(missing)])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_cli_corrupt_topology_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _mod()
    db = tmp_path / "bad.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE topology_versions (payload_json TEXT, version INTEGER)")
        conn.execute(
            "INSERT INTO topology_versions (payload_json, version) VALUES (?, ?)",
            ("{not json", 1),
        )
        conn.commit()
    finally:
        conn.close()
    code = mod.main(["--db", str(db)])
    assert code == 2
    assert "cannot read CT entities" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("no credentials"),
        OSError("network down"),
        TimeoutError("timed out"),
    ],
)
def test_cli_ha_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    mod = _mod()

    async def failing_stream(*args: Any, **kwargs: Any):
        raise error

    monkeypatch.setattr(mod, "stream_coverage", failing_stream)
    code = mod.main(["--entities", "sensor.ct1"])
    assert code == 2
    assert "Home Assistant history request failed" in capsys.readouterr().err


def test_cli_non_numeric_in_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _mod()
    payload = {
        "sensor.ct1": [_row(START), {"s": "unavailable", "lu": START.timestamp()}],
    }

    async def fake_stream(entity_ids, *, start, end, accumulators, chunk=None):
        mod.fold_chunk(accumulators, payload)

    monkeypatch.setattr(mod, "stream_coverage", fake_stream)
    assert mod.main(["--entities", "sensor.ct1", "--json", "--end", END.isoformat()]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["entities"][0]["non_numeric"] == 1
