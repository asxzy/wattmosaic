"""The HA-recorder replay source (``tools/audit/replay_capture.py``).

No network is used: the history payloads are canned and the fetch layer is
faked. What is exercised here is the pure conversion from a compressed HA
recorder payload into a ``Capture`` that the production v3 ``replay()`` accepts,
plus the chunking/dedup of the fetch helper and the CLI's flag validation.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest


def _replay_module():
    """Import ``tools.audit.replay_capture`` (the packages have no ``__init__``)."""
    from tests.conftest import REPO_ROOT

    audit = str(REPO_ROOT / "tools" / "audit")
    if audit not in sys.path:
        sys.path.insert(0, audit)
    import replay_capture

    return replay_capture


START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _row(ts: datetime, value: str, *, key: str = "lu") -> dict[str, Any]:
    return {key: ts.timestamp(), "s": value}


def test_loader_maps_entities_and_parses_rows() -> None:
    """Two mapped entities, lu/lc mix: ct ids, values and UTC timestamps are right."""
    module = _replay_module()
    start = START
    end = start + timedelta(hours=1)
    t1 = start + timedelta(minutes=1)
    t2 = start + timedelta(minutes=2)
    t3 = start + timedelta(minutes=3)
    payload = {
        "sensor.ct_a": [_row(t1, "100.0"), _row(t2, "150.0", key="lc")],
        "sensor.ct_b": [_row(t3, "200.0")],
    }
    entity_to_ct = {"sensor.ct_a": "ct_a", "sensor.ct_b": "ct_b"}

    capture = module.load_capture_ha_history(
        payload, entity_to_ct=entity_to_ct, start=start, end=end
    )

    assert [(s.ct_id, s.value_w) for s in capture.samples] == [
        ("ct_a", 100.0),
        ("ct_a", 150.0),
        ("ct_b", 200.0),
    ]
    assert [s.received_at for s in capture.samples] == [t1, t2, t3]
    assert all(s.state_timestamp == s.received_at for s in capture.samples)
    assert all(s.received_at.tzinfo is UTC for s in capture.samples)
    assert all(s.quality == "good" and s.repeated is False for s in capture.samples)
    assert [s.sequence for s in capture.samples] == [0, 1, 2]
    assert capture.source == "ha-history"
    assert capture.window_start == start and capture.window_end == end
    assert any("arrival jitter" in note for note in capture.notes)


def test_loader_skips_non_numeric_states_with_note() -> None:
    """unavailable/unknown/empty states are skipped and counted explicitly."""
    module = _replay_module()
    end = START + timedelta(hours=1)
    payload = {
        "sensor.ct_a": [
            _row(START + timedelta(minutes=1), "unavailable"),
            _row(START + timedelta(minutes=2), "unknown"),
            _row(START + timedelta(minutes=3), ""),
            _row(START + timedelta(minutes=4), "12.5"),
        ]
    }
    capture = module.load_capture_ha_history(
        payload, entity_to_ct={"sensor.ct_a": "ct_a"}, start=START, end=end
    )
    assert [s.value_w for s in capture.samples] == [12.5]
    assert any("non-numeric" in note for note in capture.notes)


def test_loader_notes_missing_entity() -> None:
    """A mapped entity present with [] gets an explicit no-history note."""
    module = _replay_module()
    end = START + timedelta(hours=1)
    payload = {
        "sensor.ct_a": [_row(START + timedelta(minutes=1), "10")],
        "sensor.ct_b": [],
    }
    capture = module.load_capture_ha_history(
        payload,
        entity_to_ct={"sensor.ct_a": "ct_a", "sensor.ct_b": "ct_b"},
        start=START,
        end=end,
    )
    assert any(
        "sensor.ct_b: no recorder history" in note for note in capture.notes
    )


def test_loader_notes_late_recorder_start() -> None:
    """A first row >1h after start is called out as a possible purge_keep_days gap."""
    module = _replay_module()
    end = START + timedelta(hours=3)
    first = START + timedelta(hours=2)
    payload = {"sensor.ct_a": [_row(first, "10")]}
    capture = module.load_capture_ha_history(
        payload, entity_to_ct={"sensor.ct_a": "ct_a"}, start=START, end=end
    )
    assert any(
        "sensor.ct_a: recorder history starts at" in note
        and "purge_keep_days" in note
        for note in capture.notes
    )


def test_loader_drops_rows_outside_window() -> None:
    """Rows before start and after end are dropped (bounds are inclusive)."""
    module = _replay_module()
    end = START + timedelta(hours=1)
    payload = {
        "sensor.ct_a": [
            _row(START - timedelta(seconds=1), "1"),
            _row(START, "2"),
            _row(end, "3"),
            _row(end + timedelta(seconds=1), "4"),
        ]
    }
    capture = module.load_capture_ha_history(
        payload, entity_to_ct={"sensor.ct_a": "ct_a"}, start=START, end=end
    )
    assert [s.value_w for s in capture.samples] == [2.0, 3.0]


def test_loader_accepts_aligner_key_alias() -> None:
    """A topology key stored as a bare aligner key still maps a full entity id."""
    module = _replay_module()
    end = START + timedelta(hours=1)
    payload = {"sensor.ct_a": [_row(START + timedelta(minutes=1), "10")]}
    capture = module.load_capture_ha_history(
        payload, entity_to_ct={"ct_a": "ct_a"}, start=START, end=end
    )
    assert [s.ct_id for s in capture.samples] == ["ct_a"]


def test_loader_with_no_samples_exits() -> None:
    """A payload with nothing usable fails loudly, like the other loaders."""
    module = _replay_module()
    end = START + timedelta(hours=1)
    with pytest.raises(SystemExit):
        module.load_capture_ha_history(
            {"sensor.ct_a": []},
            entity_to_ct={"sensor.ct_a": "ct_a"},
            start=START,
            end=end,
        )


class _FakeClient:
    """Minimal ws_command stand-in used by ``fetch_history``."""

    def __init__(self, rows_by_start: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self.rows_by_start = rows_by_start
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def ws_command(self, msg_type: str, **params: Any) -> Any:
        self.calls.append((msg_type, dict(params)))
        return self.rows_by_start.get(str(params.get("start_time")), {})


async def test_fetch_chunks_and_dedupes() -> None:
    """A 3h window with 1h chunks makes 3 contiguous calls and drops a boundary dup."""
    module = _replay_module()
    start = START
    end = start + timedelta(hours=3)
    c1 = start + timedelta(hours=1)
    c2 = start + timedelta(hours=2)
    t0 = (start + timedelta(minutes=5)).timestamp()
    tb = c1.timestamp()
    row0 = {"s": "10", "lu": t0}
    rowb = {"s": "20", "lu": tb}
    client = _FakeClient(
        {
            start.isoformat(): {"sensor.ct_a": [row0]},
            c1.isoformat(): {"sensor.ct_a": [rowb]},
            c2.isoformat(): {"sensor.ct_a": [dict(rowb)]},  # duplicate at boundary
        }
    )

    payload = await module.fetch_ha_history_payload(
        client, ["sensor.ct_a"], start=start, end=end
    )

    assert [msg for msg, _ in client.calls] == ["history/history_during_period"] * 3
    starts = [params["start_time"] for _, params in client.calls]
    ends = [params["end_time"] for _, params in client.calls]
    assert starts == [start.isoformat(), c1.isoformat(), c2.isoformat()]
    assert ends == [c1.isoformat(), c2.isoformat(), end.isoformat()]
    assert all(params["minimal_response"] is True for _, params in client.calls)
    assert payload == {"sensor.ct_a": [row0, rowb]}


def test_capture_feeds_replay() -> None:
    """A loader-produced Capture is accepted by the real production replay()."""
    module = _replay_module()
    from tests.simulation.test_replay_capture import _capture_from_house
    from tools.simulator import build_hb17

    house = build_hb17(seed=7, minutes=10, interval_s=10.0)
    original = _capture_from_house(house)
    entity_to_ct: dict[str, str] = {}
    payload: dict[str, list[dict[str, Any]]] = {}
    for sample in original.samples:
        entity_to_ct[sample.entity_id] = sample.ct_id
        payload.setdefault(sample.entity_id, []).append(
            _row(sample.received_at, str(sample.value_w))
        )

    capture = module.load_capture_ha_history(
        payload,
        entity_to_ct=entity_to_ct,
        start=original.window_start,
        end=original.window_end,
    )
    run = module.replay(capture, tick_s=10.0, models="v1")
    assert run.derived_run_id.startswith("derived-")
    assert run.chunks >= 1


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Run ``main()`` with patched argv and return its code (or SystemExit code)."""
    module = _replay_module()
    monkeypatch.setattr(sys, "argv", ["replay_capture.py", *argv])
    try:
        return module.main()
    except SystemExit as exc:
        return int(exc.code or 0)


def test_cli_rejects_ha_history_with_jsonl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--ha-history and --jsonl are mutually exclusive (exit 2)."""
    assert _run_cli(monkeypatch, ["--ha-history", "--jsonl", "x.jsonl"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_cli_rejects_end_without_ha_history(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--end only makes sense with --ha-history (exit 2)."""
    assert _run_cli(monkeypatch, ["--end", "2026-01-01T00:00:00Z"]) == 2
    assert "--end requires --ha-history" in capsys.readouterr().err


def test_cli_ha_history_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A --ha-history run resolves wiring/versions from --db and fetches samples."""
    import json
    import sqlite3

    module = _replay_module()
    from tests.simulation.test_replay_capture import _v3_db

    start = START
    end = start + timedelta(minutes=30)
    db_path, _topo_v, _map_v = _v3_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS topology_versions(payload_json TEXT, version INTEGER)"
    )
    conn.execute(
        "INSERT INTO topology_versions(payload_json, version) VALUES(?, 1)",
        (json.dumps({"channels": [{"entity_id": "sensor.ct_a", "ct_id": "ct_a"}]}),),
    )
    conn.commit()
    conn.close()

    rows: dict[str, list[dict[str, Any]]] = {
        "sensor.ct_a": [_row(start + timedelta(seconds=t), "10") for t in range(0, 31 * 60, 600)]
    }

    async def _fake_fetch(client, entity_ids, *, start, end, chunk=None):
        return rows

    # Patch the fetch helper in the replay module (which the CLI calls directly)
    # and the HAClient factory (so no HA connection is attempted).
    monkeypatch.setattr(module, "fetch_ha_history_payload", _fake_fetch)
    ha_client_mod = __import__("tools.audit.ha_client", fromlist=["HAClient"])

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        ha_client_mod, "HAClient", type("Fact", (), {"from_env": staticmethod(lambda: _Client())})
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_capture.py",
            "--ha-history",
            "--db",
            str(db_path),
            "--minutes",
            "30",
            "--end",
            end.isoformat(),
            "--no-compare",
        ],
    )
    code = module.main()
    assert code == 0, capsys.readouterr().err


def test_cli_ha_history_without_db_missing_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """--ha-history with no --db and no default DB fails clearly (not 'no capture')."""
    module = _replay_module()
    monkeypatch.setattr(module, "DEFAULT_DB", module.Path("/nonexistent/wm.db"))
    assert _run_cli(monkeypatch, ["--ha-history", "--minutes", "5"]) == 2
