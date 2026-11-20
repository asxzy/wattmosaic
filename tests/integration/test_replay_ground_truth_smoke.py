"""Plumbing smoke test for the ground-truth replay runner (spec §3.4).

Writes a tiny synthetic history (section 1 format) and GT file to ``tmp_path``,
runs the runner's ``main([...])`` and checks the report shape.  It deliberately
does **not** assert that inference got the topology right -- this is a plumbing
test, not an accuracy test.  No live-install data, no network, no HA.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.dev import replay_ground_truth

T0 = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)
MAIN, K1, K2 = "ct_main", "ct_kettle", "ct_dryer"
POWER = {
    MAIN: "sensor.main_power",
    K1: "sensor.kettle_power",
    K2: "sensor.dryer_power",
}


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _write_history(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    sample_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    # 2 h of step loads at a 10 s cadence.  P3 needs ~64 samples of warm-up
    # before it may open a step, so the early minutes are quiet.
    on = {K1: 0.0, K2: 0.0}
    changes = {
        400: (K1, 300.0, "switch.kettle"),
        700: (K1, -300.0, "switch.kettle"),
        1000: (K1, 300.0, "switch.kettle"),
        1300: (K1, -300.0, "switch.kettle"),
        1600: (K2, 500.0, "switch.dryer"),
        1900: (K2, -500.0, "switch.dryer"),
        2200: (K2, 500.0, "switch.dryer"),
        2500: (K2, -500.0, "switch.dryer"),
    }
    for second in range(0, 2 * 3600, 10):
        at = T0 + timedelta(seconds=second)
        if second in changes:
            reader, delta, entity = changes[second]
            on[reader] += delta
            event_rows.append(
                {
                    "entity_id": entity,
                    "t": (at - timedelta(seconds=0.5)).isoformat(),
                    "state": "on" if delta > 0 else "off",
                    "attributes": {"friendly_name": entity},
                }
            )
        for rid, entity in POWER.items():
            value = {MAIN: 1000.0, K1: 20.0, K2: 30.0}[rid] + on.get(rid, 0.0)
            if rid == MAIN:
                value += on.get(K1, 0.0) + on.get(K2, 0.0)
            sample_rows.append({"entity_id": entity, "t": at.isoformat(), "v": float(value)})

    day = T0.date().isoformat()
    _write_jsonl_gz(root / f"samples-{day}.jsonl.gz", sample_rows)
    _write_jsonl_gz(root / f"events-{day}.jsonl.gz", event_rows)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "wattmosaic.ha_history/1",
                "start": T0.isoformat(),
                "end": (T0 + timedelta(hours=2)).isoformat(),
                "days": {day: {"complete": True, "samples": len(sample_rows), "events": len(event_rows)}},
                "sample_entities": list(POWER.values()),
                "event_entities": ["switch.kettle", "switch.dryer"],
                "earliest_row": T0.isoformat(),
                "pulled_at": T0.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    # No factors.json: factors=auto simply stores nothing.


def _write_gt(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "wattmosaic.topology_ground_truth/1",
                "readers": [
                    {"name": "Main", "reader_id": MAIN, "power_entity": POWER[MAIN], "factor_inputs": []},
                    {"name": "Kettle", "reader_id": K1, "power_entity": POWER[K1], "factor_inputs": []},
                    {"name": "Dryer", "reader_id": K2, "power_entity": POWER[K2], "factor_inputs": []},
                ],
                "contains": [
                    {"parent": MAIN, "child": K1},
                    {"parent": MAIN, "child": K2},
                ],
                "paired_leg": [],
                "same_conductor": [],
                "one_leg_240v": [],
                "legs": {"A": [MAIN, K1, K2], "B": []},
            }
        ),
        encoding="utf-8",
    )


def test_replay_ground_truth_smoke(tmp_path: Path, capsys: Any) -> None:
    history = tmp_path / "ha_history"
    gt = tmp_path / "topology_gt.json"
    report_path = tmp_path / "report.json"
    _write_history(history)
    _write_gt(gt)

    exit_code = replay_ground_truth.main(
        [
            "--history",
            str(history),
            "--gt",
            str(gt),
            "--checkpoint-hours",
            "1",
            "--json-out",
            str(report_path),
        ]
    )
    assert exit_code == 0

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(["run", "records", "declarations", "factors_stored", "checkpoints", "final"]) <= set(
        payload
    )
    assert payload["records"]["total"] > 0
    assert payload["records"]["samples"] > 0
    assert payload["records"]["readings_fed"] > 0
    assert "score" in payload["final"]
    assert set(["contains_direct", "contains_closure", "per_child", "violations", "recall_scorable"]) <= set(
        payload["final"]["score"]
    )
    assert len(payload["checkpoints"]) >= 1  # at least one mid-run checkpoint
    assert payload["run"]["declare"] == "pairs+aliases"

    # The human summary uses GT reader names and contains no raw samples.
    text = capsys.readouterr().out
    assert "WattMosaic ground-truth replay" in text
    assert "Main" in text
    assert "Kettle" in text
    assert "Dryer" in text
