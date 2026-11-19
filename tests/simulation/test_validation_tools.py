"""The two documented reproduction commands must actually work.

``docs/real_ha_validation.md`` ends with:

```bash
.venv/bin/python tools/dev/make_reference_house.py --passive-validation
.venv/bin/python tools/audit/passive_validation.py \\
    --from data/examples/reference_house/passive_validation.json
```

For a long time both commands were documentation-only: ``make_reference_house``
had no ``--passive-validation`` flag and ``passive_validation`` had no ``--from``,
so the only way to rebuild the report was a live Home Assistant. That is the
worst kind of doc bug — the command looks runnable, and the failure appears only
when someone tries to reproduce a published number.

These tests pin the contract instead of the prose:

* the flag exists and produces the *committed* file byte-for-byte;
* ``--from`` renders the *committed* report byte-for-byte;
* the two tools therefore cannot drift apart, because one generates the input of
  the other and both are compared against the same checked-in artifacts.

No network, no Home Assistant, no credentials, and nothing is written outside
``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "data" / "examples" / "reference_house"
CAPTURE = EXAMPLES / "passive_validation.json"
REPORT = REPO_ROOT / "docs" / "real_ha_validation.md"
TOOLS = REPO_ROOT / "tools"
PYTHON = sys.executable


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module")
def passive_capture() -> dict:
    if not CAPTURE.exists():  # pragma: no cover - committed file
        pytest.skip(f"{CAPTURE} is not present in this checkout")
    return json.loads(CAPTURE.read_text())


def test_make_reference_house_writes_the_committed_capture(tmp_path, passive_capture) -> None:
    """``--passive-validation`` must reproduce the committed JSON exactly."""
    sys.path.insert(0, str(TOOLS / "dev"))
    sys.path.insert(0, str(TOOLS / "audit"))
    sys.path.insert(0, str(REPO_ROOT))
    import make_reference_house

    produced = make_reference_house.build_passive_validation()
    assert produced == passive_capture, (
        "make_reference_house --passive-validation no longer matches the "
        "committed data/examples/reference_house/passive_validation.json; "
        "regenerate it with tools/dev/render_validation_docs.py"
    )
    # And the shape the docs promise downstream.
    assert set(produced) >= {
        "generated_at",
        "minutes",
        "synthetic",
        "power_entity_count",
        "states",
        "result",
    }
    assert produced["synthetic"] is True
    assert produced["result"]["channels_observed"] > 0

    # Written form round-trips through the same serialisation the CLI uses.
    path = tmp_path / "passive_validation.json"
    path.write_text(json.dumps(produced, indent=2) + "\n")
    assert json.loads(path.read_text()) == passive_capture


def test_passive_validation_from_renders_the_committed_report(
    tmp_path, passive_capture
) -> None:
    """``--from`` must render ``docs/real_ha_validation.md`` exactly, offline."""
    if not REPORT.exists():  # pragma: no cover - committed file
        pytest.skip(f"{REPORT} is not present in this checkout")
    out = tmp_path / "real_ha_validation.md"
    result = _run(
        [
            str(TOOLS / "audit" / "passive_validation.py"),
            "--from",
            str(CAPTURE),
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    produced = out.read_text()
    committed = REPORT.read_text()
    assert produced.strip() == committed.strip(), (
        "passive_validation --from produced a different report than the "
        "committed docs/real_ha_validation.md"
    )
    # The offline path must never claim to have talked to a live install.
    assert "Passive Validation — synthetic reference household" in produced
    assert "Live HA reachable and authenticated" not in produced
    payload = json.loads(result.stdout)
    assert payload["source"] == str(CAPTURE)
    assert payload["channels_observed"] == passive_capture["result"]["channels_observed"]


def test_passive_validation_from_handles_a_raw_series_capture(tmp_path) -> None:
    """A file holding only observations is still renderable (result recomputed)."""
    sys.path.insert(0, str(TOOLS / "audit"))
    import passive_validation

    raw = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "minutes": 20.0,
        "series": {
            "sensor.panel_a_ct1_power": [
                {"value": 100.0, "last_updated": "2026-01-01T00:00:15+00:00"},
                {"value": 140.0, "last_updated": "2026-01-01T00:00:30+00:00"},
            ]
        },
        "states": [
            {
                "entity_id": "sensor.panel_a_ct1_power",
                "state": "140.0",
                "attributes": {"device_class": "power"},
            }
        ],
    }
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(raw))

    capture = passive_validation.load_capture(path)
    assert capture["result"]["channels_observed"] == 1
    stats = capture["result"]["channels"]["sensor.panel_a_ct1_power"]
    assert stats["observations"] == 2
    assert stats["mean"] == pytest.approx(120.0)
    assert capture["power_entity_count"] == 1


def test_passive_validation_from_rejects_a_non_capture(tmp_path) -> None:
    """A JSON file that is not a capture must fail loudly, not render nonsense."""
    sys.path.insert(0, str(TOOLS / "audit"))
    import passive_validation

    path = tmp_path / "unrelated.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(SystemExit) as exc:
        passive_validation.load_capture(path)
    assert "not a passive-validation capture" in str(exc.value)

    missing = tmp_path / "missing.json"
    with pytest.raises(SystemExit) as exc:
        passive_validation.load_capture(missing)
    assert "no capture at" in str(exc.value)


# --------------------------------------------------------------------------- #
# z2c: the ported v3 nesting replay, exercised over the HB §17 simulator data.
# No real household database or path is used -- the SQLite file is built in
# tmp_path from the synthetic simulator's observation frame.
# --------------------------------------------------------------------------- #
def _hb17_db(tmp_path, *, seed: int = 7, minutes: float = 25.0) -> Path:
    """Write a minimal engine-shaped DB from the synthetic HB §17 house."""
    import sqlite3

    from tools.simulator import build_hb17

    house = build_hb17(seed=seed, minutes=minutes, interval_s=10.0)
    path = tmp_path / "hb17.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE topology_versions (version INTEGER, payload_json TEXT)"
    )
    # The declared parents are the scoring ground truth: ct_sub -> ct_main,
    # ct_a/ct_b -> ct_sub, ct_c -> ct_main (HB §17).
    topology = {
        "version": 1,
        "status": "candidate",
        "channels": [
            {"ct_id": rid, "entity_id": sample["entity_id"], "name": rid,
             "ct_type": "main", "phase": "l1", "enabled": True,
             "parent_ct_id": parent}
            for rid, parent, sample in [
                ("ct_main", None, {"entity_id": "sensor.ct_main_power"}),
                ("ct_sub", "ct_main", {"entity_id": "sensor.ct_sub_power"}),
                ("ct_a", "ct_sub", {"entity_id": "sensor.ct_a_power"}),
                ("ct_b", "ct_sub", {"entity_id": "sensor.ct_b_power"}),
                ("ct_c", "ct_main", {"entity_id": "sensor.ct_c_power"}),
            ]
        ],
        "devices": [],
    }
    conn.execute(
        "INSERT INTO topology_versions (version, payload_json) VALUES (?, ?)",
        (1, json.dumps(topology)),
    )
    conn.execute(
        "CREATE TABLE ct_samples (id INTEGER PRIMARY KEY, ct_id TEXT, entity_id TEXT, "
        "value_w REAL, state_timestamp TEXT, received_at TEXT, quality TEXT)"
    )
    for record in house.records:
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            conn.execute(
                "INSERT INTO ct_samples (ct_id, entity_id, value_w, state_timestamp, "
                "received_at, quality) VALUES (?, ?, ?, ?, ?, ?)",
                (reader_id, sample["entity_id"], sample["value_w"],
                 sample["state_timestamp"], sample["received_at"], sample["quality"]),
            )
    conn.commit()
    conn.close()
    return path


def test_replay_nesting_port_scores_hb17_declared_parents(tmp_path) -> None:
    """``--score`` port: inferred CONTAINS edges reproduce declared parents.

    The v3 engine sees the synthetic HB §17 reader history through
    :func:`wattmosaic_engine.electrical.topology_inference.infer_topology` and
    the declared parents come from the synthetic topology -- never a real DB.
    """
    sys.path.insert(0, str(TOOLS / "dev"))
    sys.path.insert(0, str(REPO_ROOT / "engine"))
    import replay_nesting

    path = _hb17_db(tmp_path)
    topology, observation = replay_nesting.load_observation(path, hours=2.0)
    assert topology.version == 1
    assert observation, "the simulator DB must yield reader frames"

    from wattmosaic_engine.electrical.topology_inference import infer_topology

    view = infer_topology(observation)
    # Scoring must run without error and report the measurable children.
    assert replay_nesting.score(topology, view) == 0


def test_replay_nesting_requires_an_explicit_db(monkeypatch, capsys) -> None:
    """No checkout path is hard-coded: --db is required and never defaulted."""
    sys.path.insert(0, str(TOOLS / "dev"))
    import replay_nesting

    # With no --db the parser exits with an error rather than falling back to a
    # checkout-relative database path (the old tool defaulted to ROOT/data/...).
    monkeypatch.setattr(sys, "argv", ["replay_nesting.py", "--hours", "1"])
    with pytest.raises(SystemExit) as exc:
        replay_nesting.main()
    assert exc.value.code == 2
    assert "--db is required" in capsys.readouterr().err
