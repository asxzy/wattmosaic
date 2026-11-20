"""Offline replay diagnostic reports where the live pipeline stops (T5 discovery).

Regression for the live install where readings flowed and P3 detected steps yet
no group carried a reader step, ``infer_group`` never ran and T5 never proposed
an edge.  The test builds a small **synthetic** database (a parent CT and two
children, repeated co-steps, a few HA switch events) with no live data and checks
that the tool walks the real pipeline far enough to propose PROVISIONAL CONTAINS
edges -- the same outcome ``tests/integration/test_live_discovery_pipeline.py``
asserts for the live-shaped stream.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.dev import replay_pipeline

T0 = datetime(2026, 11, 18, 3, 0, 0, tzinfo=UTC)
PARENT, CHILD_A, CHILD_B = "ct_parent", "ct_child_a", "ct_child_b"
CT_CADENCE_S = 10
TELEMETRY_CADENCE_S = 3
#: P3 estimates each reader's noise from its first 64 samples before it may open
#: a step (DetectorConfig.sigma_warmup_samples): ~11 min at 10 s.
WARMUP_S = 700
#: (start_s, child, delta_w, switch entity): ON/OFF cycles, each a separate event.
LOAD_CHANGES = [
    (WARMUP_S + 0, CHILD_A, 300.0, "switch.kettle"),
    (WARMUP_S + 180, CHILD_A, -300.0, "switch.kettle"),
    (WARMUP_S + 360, CHILD_A, 300.0, "switch.kettle"),
    (WARMUP_S + 540, CHILD_A, -300.0, "switch.kettle"),
    (WARMUP_S + 720, CHILD_B, 500.0, "switch.dryer"),
    (WARMUP_S + 900, CHILD_B, -500.0, "switch.dryer"),
    (WARMUP_S + 1080, CHILD_B, 500.0, "switch.dryer"),
    (WARMUP_S + 1260, CHILD_B, -500.0, "switch.dryer"),
]
END_S = WARMUP_S + 1560


def _iso(value: datetime) -> str:
    return value.isoformat()


def _build_source_db(path: Path) -> None:
    """Write a synthetic source-shaped DB (only declarations + observations)."""
    from wattmosaic_engine.storage.database import Database

    db = Database(str(path))
    try:
        entities = db.repos.entities
        # Three endpoints: one panel feed (parent) and two circuit CTs.  No
        # topology edges exist: T5 must discover them from co-steps.
        for index, endpoint_id in enumerate((PARENT, CHILD_A, CHILD_B)):
            entities.upsert_endpoint(
                {
                    "endpoint_id": endpoint_id,
                    "kind": "ct",
                    "entity_id": f"sensor.panel_{endpoint_id}_power",
                    "display_name": endpoint_id,
                    "scope": "panel_feed" if index == 0 else "circuit",
                    "scope_status": "candidate",
                    "panel_id": "panel_a",
                    "enabled": True,
                    "created_at": T0 - timedelta(hours=1),
                }
            )
            entities.append_endpoint_revision(
                {
                    "endpoint_id": endpoint_id,
                    "revision": 1,
                    "valid_from": T0 - timedelta(hours=1),
                    "multiplier": 1.0,
                    "phase": "l1",
                    "polarity": "normal",
                    "calibration_version": "synthetic",
                    "measurement_type": "instantaneous",
                    "declared_by": "synthetic",
                }
            )
        # Behavioural HA entities the live source would emit as events.
        for switch in ("switch.kettle", "switch.dryer"):
            entities.upsert_ha_entity(
                {
                    "entity_id": switch,
                    "domain": "switch",
                    "first_seen": T0 - timedelta(hours=1),
                    "last_seen": T0 + timedelta(seconds=END_S),
                }
            )
    finally:
        db.close()

    # Append observation rows with plain SQL (raw store shape).  No live data.
    conn = sqlite3.connect(str(path))
    try:
        base = {PARENT: 1000.0, CHILD_A: 20.0, CHILD_B: 40.0}
        on = {CHILD_A: 0.0, CHILD_B: 0.0}
        changes = {start: (child, delta) for start, child, delta, _ in LOAD_CHANGES}
        switches = {start: (delta, sw) for start, _child, delta, sw in LOAD_CHANGES}
        sequence = 0
        for t in range(END_S + 1):
            at = T0 + timedelta(seconds=t)
            if t in changes:
                child, delta = changes[t]
                on[child] += delta
                _delta, switch = switches[t]
                # A few HA switch events (behavioural evidence) with context ids.
                state = "on" if delta > 0 else "off"
                conn.execute(
                    """INSERT INTO ha_events_v3(
                        event_id, entity_id, observed_at, received_at, from_state, to_state,
                        attributes_delta_json, context_id, linked)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        f"hae-synth-{t}",
                        switch,
                        _iso(at - timedelta(seconds=0.5)),
                        _iso(at - timedelta(seconds=0.5)),
                        "off" if delta > 0 else "on",
                        state,
                        "{}",
                        f"ctx-{t}",
                        0,
                    ),
                )
            if t % CT_CADENCE_S == 0:
                for reader in (PARENT, CHILD_A, CHILD_B):
                    value = base[reader] + on.get(reader, 0.0)
                    if reader == PARENT:
                        value += on[CHILD_A] + on[CHILD_B]
                    sequence += 1
                    # A deterministic ±0.5 W dither: enough for P2 to estimate a
                    # small sigma without producing extra step events.
                    value += 0.5 if (t // CT_CADENCE_S + sequence) % 2 else -0.5
                    conn.execute(
                        """INSERT INTO ct_samples(
                            ct_id, entity_id, value_w, raw_value, unit, state_timestamp,
                            received_at, quality, sequence, active_power_v2_w, phase,
                            measurement_type, calibration_version, measurement_basis,
                            observed_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            reader,
                            reader,
                            value,
                            value,
                            "W",
                            _iso(at),
                            _iso(at),
                            "good",
                            sequence,
                            value,
                            "l1",
                            "instantaneous",
                            "synthetic",
                            "ct_measured",
                            _iso(at),
                        ),
                    )
        conn.commit()
    finally:
        conn.close()


def test_replay_pipeline_reports_multi_reader_groups_and_provisional_edges(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _build_source_db(source)

    # Work on a prepared copy, exactly as the CLI does; the source is never
    # modified by the tool.
    work = tmp_path / "work.db"
    before = source.read_bytes()
    replay_pipeline._prepare_work_db(source, work)  # noqa: SLF001 - test seam
    assert source.read_bytes() == before

    report = replay_pipeline.replay(work, observation_source=source)

    # The diagnostic reaches the pipeline stages that the live install missed.
    assert report["readings_fed"] > 0
    assert report["detector"]["step_events"] > 0
    assert report["detector"]["multi_reader_coincidences"] > 0, report["detector"]
    assert report["groups"]["with_at_least_two_reader_deltas"] > 0, report["groups"]
    assert report["edges"]["infer_group_calls"] > 0
    assert report["t5"]["ledger_folds"] > 0

    # T5 ledger accumulated support for the parent/child pairs and proposed soft
    # PROVISIONAL CONTAINS edges (design §5: >= 3 support, >= 2 causes).
    pairs = {(p["parent"], p["child"]): p for p in report["t5"]["pairs"]}
    assert pairs[(PARENT, CHILD_A)]["support"] > 0, report["t5"]["pairs"]
    assert pairs[(PARENT, CHILD_B)]["support"] > 0, report["t5"]["pairs"]
    lifecycles = report["edges"]["proposed_by_lifecycle"]
    assert lifecycles.get("provisional", 0) >= 1, lifecycles

    # Thresholds in force are reported, with the shortfall for the top pairs.
    assert report["t5"]["thresholds"]["MIN_SUPPORT_PROVISIONAL"] > 0
    assert len(report["t5"]["top_pairs"]) <= 10
    assert all("short_of" in pair for pair in report["t5"]["top_pairs"])


def test_replay_pipeline_cli_emits_json_and_text(tmp_path: Path, capsys: Any) -> None:
    source = tmp_path / "source.db"
    _build_source_db(source)
    out = tmp_path / "report.json"

    assert replay_pipeline.main(["--db", str(source), "--json-out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["readings_fed"] > 0
    text = capsys.readouterr().out
    assert "STOP:" in text
    assert "T5 (parent, child) pairs" in text
