"""Dynamic discovery and disaggregation run on a live-shaped stream (HB §6, §7, T5).

Regression for the live install where readings flowed and P3 detected steps,
yet no group ever carried a reader step, ``infer_group`` never ran and T5 never
proposed an edge.  The stream reproduces that install's shape synthetically
(no live data):

* 40 enabled CT endpoints, all ``candidate``, no topology edges, no devices;
* a flood of unlinked numeric ``sensor.*`` telemetry and ``image`` refreshes,
  plus HA ``switch`` changes at the real load changes;
* a parent CT that steps together with one of two child CTs, repeatedly.

Everything goes through the production path: HA ``state_changed`` frames into
the engine's own ``HASource`` (P1), ``EngineCore.on_reading`` /
``on_ha_event`` (P2-P5), the event loop's bundle -> ``infer_group`` -> publish,
the steady pass (group lifecycle ticks) and the background T5 ledger fold.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import TopologyEdgeState
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 18, 3, 0, 0, tzinfo=UTC)
N_CT = 40
PARENT, CHILD_A, CHILD_B = "ct_00", "ct_01", "ct_02"
CT_CADENCE_S = 10
TELEMETRY_CADENCE_S = 3
#: P3 estimates each reader's noise from its first 64 samples before it may
#: open a step (``DetectorConfig.sigma_warmup_samples``): ~11 min at 10 s.
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


def _frame(entity_id: str, state: str, at: datetime, **attributes: Any) -> dict[str, Any]:
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": at.isoformat(),
            "data": {
                "entity_id": entity_id,
                "old_state": None,
                "new_state": {
                    "entity_id": entity_id,
                    "state": state,
                    "last_updated": at.isoformat(),
                    "attributes": attributes,
                    "context": {"id": f"ctx-{entity_id}-{at.timestamp():.0f}"},
                },
            },
        },
    }


def _build_core(now: list[datetime], path: str = ":memory:", *, declare: bool = True) -> EngineCore:
    db = Database(path)
    config = EngineConfig(
        database_path=path, ha_token="replay", api_token="replay", learning_enabled=False
    )
    core = EngineCore(config, db, clock=lambda: now[0])
    entities = db.repos.entities
    for i in range(N_CT if declare else 0):
        endpoint_id = f"ct_{i:02d}"
        entities.upsert_endpoint(
            {
                "endpoint_id": endpoint_id,
                "kind": "ct",
                "entity_id": f"sensor.panel_{endpoint_id}_power",
                "display_name": endpoint_id,
                "scope": "panel_feed" if i < 2 else "circuit",
                "scope_status": "candidate",
                "panel_id": "panel_a" if i < 20 else "panel_b",
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
                "calibration_version": "discovery",
                "declared_by": "discovery",
            }
        )
    core._wire_v3_services()
    source = core.hasource
    source._clock = lambda: now[0]  # noqa: SLF001 - injected clock for replay
    source._on_reading = core.on_reading  # noqa: SLF001 - what HASource.start binds
    source._on_ha_event = core.on_ha_event  # noqa: SLF001
    return core


def _stream(
    rng: random.Random,
    load_changes: list[tuple[int, str, float, str]] = LOAD_CHANGES,
    end_s: int = END_S,
) -> list[tuple[float, dict[str, Any]]]:
    base = {f"ct_{i:02d}": 20.0 + 11.0 * i for i in range(N_CT)}
    base[PARENT] = 1000.0
    on = {CHILD_A: 0.0, CHILD_B: 0.0}
    changes = {start: (child, delta, switch) for start, child, delta, switch in load_changes}
    frames: list[tuple[float, dict[str, Any]]] = []
    for t in range(end_s + 1):
        at = T0 + timedelta(seconds=t)
        if t in changes:
            child, delta, switch = changes[t]
            on[child] += delta
            frames.append((t - 0.5, _frame(switch, "on" if delta > 0 else "off", at - timedelta(seconds=0.5))))
        if t % TELEMETRY_CADENCE_S == 0:
            # Unlinked numeric telemetry and image refreshes: measurements and
            # noise, never device events.
            for k in range(6):
                frames.append(
                    (t + 0.01 * k, _frame(f"sensor.plug_{k}_current", f"{rng.uniform(0.0, 0.05):.3f}",
                                          at, unit_of_measurement="A"))
                )
            frames.append((t + 0.07, _frame("sensor.plug_0_power_factor", f"{rng.uniform(0.3, 0.6):.2f}", at)))
            frames.append((t + 0.08, _frame("image.doorbell", at.isoformat(), at)))
        if t % CT_CADENCE_S == 0:
            for i in range(N_CT):
                reader = f"ct_{i:02d}"
                value = base[reader] + on.get(reader, 0.0) + rng.gauss(0.0, 2.0)
                if reader == PARENT:
                    value += on[CHILD_A] + on[CHILD_B]
                frames.append(
                    (t + 0.1 + 0.001 * i,
                     _frame(f"sensor.panel_{reader}_power", f"{value:.1f}", at + timedelta(milliseconds=i),
                            unit_of_measurement="W", device_class="power"))
                )
    frames.sort(key=lambda item: item[0])
    return frames


def _pump(core: EngineCore) -> None:
    while True:
        group = core.groups_ready.get_nowait()
        if group is None:
            break
        bundle = core._build_input_bundle(group)
        if bundle is None:
            continue
        result = core._run_infer_group(bundle)
        core._publish_group_result(bundle["group"], result, bundle)
        core.drain_background()
    core.drain_background()


def test_discovery_and_disaggregation_run_on_live_shaped_stream() -> None:
    now = [T0]
    core = _build_core(now)
    results: list[Any] = []
    run_infer = core._run_infer_group

    def _capture(bundle: dict[str, Any]) -> Any:
        result = run_infer(bundle)
        results.append(result)
        return result

    core._run_infer_group = _capture  # type: ignore[method-assign]
    last_heartbeat = T0
    switch_frames = 0
    try:
        for offset, frame in _stream(random.Random(7)):
            now[0] = max(now[0], T0 + timedelta(seconds=offset))
            if frame["event"]["data"]["entity_id"].startswith("switch."):
                switch_frames += 1
            asyncio.run(core.hasource._dispatch(frame))  # noqa: SLF001 - P1 entry
            _pump(core)
            if (now[0] - last_heartbeat).total_seconds() >= 60:
                core._steady_pass()
                _pump(core)
                last_heartbeat = now[0]

        # P1: telemetry is not an HA event; the switch changes are.
        assert core.events_ingested == switch_frames
        assert core.hasource.stats.get("ha_events_filtered", 0) > 0

        # P5: reader-step groups reach disk with their deltas.
        rows = core.db._conn.execute(  # noqa: SLF001 - storage inspection
            "SELECT status, sub_events_json, reader_deltas_json FROM event_groups"
        ).fetchall()
        stored = [
            (row[0], json.loads(row[1]), json.loads(row[2])) for row in rows
        ]
        with_steps = [
            deltas
            for _, subs, deltas in stored
            if any(sub["kind"] == "reader_step" for sub in subs) and deltas
        ]
        assert with_steps, "no stored group carries a reader step and its deltas"
        co_steps = [
            deltas for deltas in with_steps if {PARENT, CHILD_A} <= {d["reader_id"] for d in deltas}
        ]
        assert co_steps, "the parent and child co-step never landed in one stored group"
        evidence_ids = {
            row[0] for row in core.db._conn.execute("SELECT evidence_id FROM event_evidence")  # noqa: SLF001
        }
        assert all(d["evidence_ref"]["id"] in evidence_ids for deltas in with_steps for d in deltas)
        assert any(status == "confirmed" for status, _, deltas in stored if deltas)

        # Event loop: inference runs and publishes explicit unknown estimates
        # (no device or mapping is declared, so nothing is identifiable; the
        # unexplained delta stays an unknown, never a fabricated device).
        assert core.infer_group_calls > 0
        assert results
        unknown_scopes = {u.scope_reader_id for r in results for u in r.unknown_estimates}
        assert {PARENT, CHILD_A, CHILD_B} <= unknown_scopes
        snapshot = core._v3_snapshot()
        assert snapshot is not None
        assert len(snapshot.reader_measurements) == N_CT

        # E6 §2 / ADR-0045: clean co-steps from >=2 causes now reach STRUCTURAL
        # at support_w >= 3 (was PROVISIONAL at the old bar of 8).  One event
        # still never promotes past CANDIDATE.
        view = core.topology_inference
        edge = view.edge(PARENT, CHILD_A)
        assert edge is not None, "T5 never saw the parent/child co-steps"
        assert edge.lifecycle == TopologyEdgeState.STRUCTURAL, edge.evidence
        assert view.edge(PARENT, CHILD_B) is not None
        # CHILD_B has fewer clean co-steps: it stays soft.
        assert view.edge(PARENT, CHILD_B).lifecycle == TopologyEdgeState.PROVISIONAL
        persisted = {
            (str(row["source_endpoint_id"]), str(row["target_endpoint_id"])): str(row["lifecycle"])
            for row in core.db.repos.topology_v3.edges()
        }
        assert persisted.get((PARENT, CHILD_A)) == TopologyEdgeState.STRUCTURAL.value
        # One event never promotes past CANDIDATE: the noise readers never
        # stepped and have no edge at all.
        assert all("ct_1" not in pair[0] for pair in persisted)
    finally:
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        if core.inference_executor is not None:
            core.inference_executor.shutdown(wait=False)
        core.db.close()


def _replay(core: EngineCore, now: list[datetime], frames: list[tuple[float, dict[str, Any]]]) -> None:
    last_heartbeat = now[0]
    for offset, frame in frames:
        now[0] = max(now[0], T0 + timedelta(seconds=offset))
        asyncio.run(core.hasource._dispatch(frame))  # noqa: SLF001 - P1 entry
        _pump(core)
        if (now[0] - last_heartbeat).total_seconds() >= 60:
            core._steady_pass()
            _pump(core)
            last_heartbeat = now[0]


def _close(core: EngineCore) -> None:
    if core.background_executor is not None:
        core.background_executor.shutdown(wait=False)
    if core.inference_executor is not None:
        core.inference_executor.shutdown(wait=False)
    core.db.close()


def test_restart_rederiving_groups_continues_the_record_chain(tmp_path: Any, caplog: Any) -> None:
    """A restart that re-derives the same groups never re-inserts a record.

    Group ids are content-derived, so replaying the same stream after a
    restart re-derives the same group ids.  The live install logged
    ``UNIQUE constraint failed: event_inference_records.event_group_id,
    event_inference_records.interpretation_revision`` because the new
    process's record store restarted every chain at revision 1.  The stored
    head is adopted instead: interpretations continue at ``head + 1``.
    """
    changes = [(WARMUP_S, CHILD_A, 300.0, "switch.kettle")]
    end_s = WARMUP_S + 300
    path = str(tmp_path / "wm.db")
    frames = _stream(random.Random(3), changes, end_s)

    now = [T0]
    first = _build_core(now, path)
    try:
        _replay(first, now, frames)
    finally:
        _close(first)

    caplog.clear()
    now = [T0]
    second = _build_core(now, path, declare=False)
    try:
        _replay(second, now, frames)
        assert second.infer_group_calls > 0
        assert "failed to persist an inference record" not in caplog.text
        rows = second.db._conn.execute(  # noqa: SLF001 - storage inspection
            "SELECT event_group_id, interpretation_revision, previous_record_id, record_id "
            "FROM event_inference_records WHERE event_group_id LIKE 'grp-%' "
            "ORDER BY event_group_id, interpretation_revision"
        ).fetchall()
        by_group: dict[str, list[tuple[int, str | None, str]]] = {}
        for group_id, revision, previous, record_id in rows:
            by_group.setdefault(group_id, []).append((revision, previous, record_id))
        rederived = [chain for chain in by_group.values() if len(chain) > 1]
        assert rederived, "the second process must re-infer at least one stored group"
        for chain in by_group.values():
            assert [rev for rev, _, _ in chain] == list(range(1, len(chain) + 1))
            for (_, _, prior_id), (_, previous, _) in zip(chain, chain[1:], strict=False):
                assert previous == prior_id
    finally:
        _close(second)
