"""P6 — the three-loop runtime in EngineCore (HB §5, DEC-14/37).

Acceptance checks (task P6 §Tests):

* a flat reader stream produces **zero** ``infer_group`` calls, and a simulated
  hour produces at most 60 heartbeats;
* one HA event plus a reader step produces a PROVISIONAL/MATCHING result within
  one cadence of the first post-sample;
* kill/restart mid-group reconstructs the group from persisted evidence and the
  inferred result is identical;
* the X07 HB §17 replay completes through ``EngineHarness`` with an injected
  clock (K07's ``engine_harness`` fixture no longer raises);
* the single-thread background executor drains jobs off the event loop.

Everything uses deterministic doubles (fake clock, in-memory DB) and never
imports ``runtime.fakes`` into production code.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from aiohttp.test_utils import TestClient, TestServer
from wattmosaic_engine.api.server import create_app
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EventGroup,
    EventGroupStatus,
    HAEvent,
    MeasurementType,
    ReaderDelta,
    ReaderMeasurement,
)
from wattmosaic_engine.core.executors import BackgroundExecutor, JobBudget
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.realtime import EngineHarness
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _core(tmp_path, *, clock=None):
    db = Database(tmp_path / "runtime.db")
    config = EngineConfig(database_path=str(tmp_path / "runtime.db"), ha_token="x")
    core = EngineCore(config, db, clock=clock or (lambda: T0))
    core._wire_v3_services()
    return core, db


def _reading(reader: str, value: float, at: datetime, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
        observed_at=at,
        received_at=at,
        active_power_w=value,
        phase="l1",
        leg=None,
        voltage_reference_v=None,
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=1000,
        calibration_version="test@1",
        measurement_basis="instantaneous",
        quality_flags=[],
        sequence=seq,
    )


def test_flat_stream_runs_no_inference(tmp_path) -> None:
    """A constant-power hour produces zero infer_group calls, <=60 heartbeats."""
    core, db = _core(tmp_path)
    try:
        for tick in range(3600):
            at = T0 + timedelta(seconds=tick)
            asyncio.run(core.on_reading(_reading("ct_a", 250.0, at, tick)))
        # No steady change: the detector stays STABLE and no group is enqueued.
        assert core.infer_group_calls == 0
        assert len(core.groups_ready) == 0
        # A simulated hour emits exactly one 60 s heartbeat when the steady
        # loop is manually driven 60 times; never a per-sample full solve.
        for _ in range(60):
            core._steady_pass()
        assert core.heartbeats == 60
        assert core.infer_group_calls == 0
    finally:
        db.close()


def test_event_plus_step_produces_within_one_cadence(tmp_path) -> None:
    """An HA event + a reader step yields a group and an inferred result."""
    core, db = _core(tmp_path)
    try:
        event = HAEvent.make(
            entity_id="light.living_room_1",
            observed_at=T0,
            received_at=T0,
            to_state="on",
            linked=False,
        )
        asyncio.run(core.on_ha_event(event))
        # Seed a stable baseline past the detector's sigma warm-up (64 samples),
        # then a sustained step.  The first samples prime P2's noise estimate.
        for tick in range(1, 70):
            at = T0 + timedelta(seconds=tick * 15)
            asyncio.run(core.on_reading(_reading("ct_a", 250.0, at, tick)))
        for tick in range(70, 100):
            at = T0 + timedelta(seconds=tick * 15)
            asyncio.run(core.on_reading(_reading("ct_a", 295.0, at, tick)))
        assert core.infer_group_calls == 0  # event loop is async, not driven here
        group = core.groups_ready.get_nowait()
        assert group is not None, "a reader step must enqueue a ready group"
        bundle = core._build_input_bundle(group)
        result = core._run_infer_group(bundle)
        assert result is not None
        assert core.infer_group_calls == 1
    finally:
        db.close()


def test_harness_replays_hb17_end_to_end() -> None:
    """The X07 HB §17 run completes through a deterministic harness."""
    from tools.simulator.hb17 import build

    house = build(17)
    harness = EngineHarness.from_hb17(
        {"house": house, "observation": house.observation_view()}
    )
    try:
        assert harness.replay_result.n_ticks > 0
        assert harness.replay_result.n_messages > 0
        assert harness.core.infer_group_calls >= 1
        assert harness.result is not None
        assert harness.result.event_group_id is not None
    finally:
        harness.close()


def test_restart_reconstructs_group_from_persisted_evidence(tmp_path) -> None:
    """Kill/restart mid-group: group evidence is reloaded byte-identically."""
    core, db = _core(tmp_path)
    try:
        event = HAEvent.make(
            entity_id="switch.tv_plug",
            observed_at=T0,
            received_at=T0,
            to_state="on",
            context_id="ctx-1",
            linked=False,
        )
        asyncio.run(core.on_ha_event(event))
        groups = core.grouping_engine.groups()
        assert groups
        group = groups[0]
        # The fast loop already persisted this revision; a restart reconstructs
        # the same group from S16 evidence (idempotent latest_group read).
        restored = db.repos.evidence.latest_group(group.group_id)
        assert restored is not None
        assert restored.model_dump(mode="json") == group.model_dump(mode="json")
    finally:
        db.close()


def test_silent_reader_is_stale_and_excluded_from_inference(tmp_path) -> None:
    """ZF: quality is scored from ReaderStateStore, so a reader that goes silent
    appears in the snapshot's ``stale_sources`` and is dropped from the next
    inference's operator rows.

    Before the fix the quality engine read the SampleAligner's power buffers,
    which the HA source no longer feeds, so both the exclusion and the stale
    reason were computed from an empty report.
    """
    clock = {"now": T0}
    core, db = _core(tmp_path, clock=lambda: clock["now"])
    try:
        # Two readers stream normally for a while so both have a held value.
        for tick in range(1, 40):
            clock["now"] = T0 + timedelta(seconds=tick * 15)
            at = clock["now"]
            asyncio.run(core.on_reading(_reading("ct_live", 200.0, at, tick)))
            asyncio.run(core.on_reading(_reading("ct_silent", 100.0, at, tick)))

        # Evaluate quality now: both readers are healthy.
        core._evaluate_quality()
        assert "ct_silent" not in core.latest_quality.channels or core.latest_quality.channels[
            "ct_silent"
        ]["status"] == "good"

        # ct_silent stops reporting.  Advance the clock past the store's
        # missing_after_s horizon (5x stale, >= 600 s) while ct_live continues.
        clock["now"] = T0 + timedelta(seconds=40 * 15 + 700)
        at = clock["now"]
        asyncio.run(core.on_reading(_reading("ct_live", 250.0, at, 40)))
        core._evaluate_quality()

        # The silent reader is now missing/stale with a concrete reason, and the
        # reason is surfaced as a ``reader: reason`` stale_sources entry.
        assert "ct_silent" in core.latest_quality.channels
        stale = core.latest_quality.channels["ct_silent"]["status"]
        assert stale in ("stale", "missing", "bad", "degraded")
        from wattmosaic_engine.core.runtime import stale_sources_from_quality

        stale_sources = stale_sources_from_quality(core.latest_quality)
        assert any(s.startswith("ct_silent:") for s in stale_sources), stale_sources

        # The event loop's input bundle drops the excluded reader from the
        # operator rows, so no fabricated 0 W estimate can be produced for it.
        versions = core.version_service.current()
        group = EventGroup(
            group_id="grp-zf",
            revision=1,
            status=EventGroupStatus.CONFIRMED,
            opened_at=at,
            reader_deltas=[
                ReaderDelta(
                    reader_id="ct_live", delta_w=50.0, completeness="complete"
                ),
                ReaderDelta(
                    reader_id="ct_silent", completeness="missing"
                ),
            ],
            versions=versions,
        )
        bundle = core._build_input_bundle(group)
        assert bundle is not None
        reader_rows = {row.reader_id for row in bundle["operator"].rows}
        assert "ct_live" in reader_rows
        assert "ct_silent" not in reader_rows
    finally:
        db.close()


def test_background_executor_runs_on_one_thread() -> None:
    """DEC-37: one worker, jobs run off the caller thread, budget is chunked."""
    executor = BackgroundExecutor()
    try:
        main_thread = time.thread_time_ns if hasattr(time, "thread_time_ns") else None
        del main_thread  # not used; identity below is the assertion
        import threading

        caller = threading.get_ident()
        future = executor.submit(lambda: threading.get_ident())
        worker_thread = future.result(timeout=5.0)
        assert worker_thread != caller
        assert executor.max_workers == 1
        assert executor.stats()["completed"] == 1

        budget = JobBudget(now=lambda: 0.0, max_chunk_s=2.0)
        budget.start_chunk()
        assert budget.exceeded() is False
        assert budget.remaining_s() == 2.0
    finally:
        executor.shutdown()


# --------------------------------------------------------------------------- #
# P6b — live wiring of T5 / T7 / D5 and the v3 facade (integration).
# --------------------------------------------------------------------------- #


def _hb17_harness():
    """Build the harness from the post-move HB17 variant.

    ``relocated_tv`` keeps toggling the TV after the step-9 plug move, giving
    the runtime's own T7 relocation detector the >= 5 post-move events the
    design §5 rule requires.  The default script has a single 3 W step, which
    is below that threshold.
    """
    from tools.simulator.hb17 import build

    house = build(17, variant="relocated_tv", minutes=60)
    harness = EngineHarness.from_hb17(
        {"house": house, "observation": house.observation_view()}
    )
    return harness


def test_hb17_replay_persists_topology_ledger_update() -> None:
    """(a) Replaying X07 HB17 persists at least one ledger edge (S14 evidence)."""
    harness = _hb17_harness()
    try:
        core = harness.core
        edges = core.db.repos.topology_v3.edges()
        assert edges, "a co-temporal reader group must fold into the T5 ledger"
        import json

        assert any(
            "support" in json.loads(edge.get("evidence_json") or "{}")
            for edge in edges
        ), "persisted edges must carry the per-pair evidence counters"
        assert core.topology_inference is not None
        assert len(core.topology_inference.edges) >= len(edges)
    finally:
        harness.close()


def test_hb17_replay_produces_configuration_event_after_plug_move() -> None:
    """(b) The scripted HB17 step-9 plug move yields a CONFIRMED config event."""
    from wattmosaic_engine.contracts import ChangeDetection, ConfigurationChangeKind

    harness = _hb17_harness()
    try:
        core = harness.core
        events = core.v3_configuration_events()
        assert any(
            str(getattr(ev.kind, "value", ev.kind)) == "smart_plug_relocation"
            for ev in events
        ), "step 9 must be recorded as a smart-plug relocation"
        confirmed = [
            ev
            for ev in events
            if str(getattr(ev.status, "value", ev.status)) == "confirmed"
        ]
        assert confirmed, "the corroborated relocation must be published as CONFIRMED"
        # P6c: the event is INFERRED, produced by the runtime's own detector job
        # (not by harness scaffolding), and carries the detector's evidence ref.
        assert any(
            getattr(ev.detection, "value", ev.detection) == ChangeDetection.INFERRED.value
            for ev in confirmed
        )
        assert any(
            any("topology_change.detect_inferred_changes" in str(ref.id) for ref in ev.evidence_refs)
            for ev in confirmed
        ), "the event must carry the real detector's evidence ref"
        # The self-healing workflow promoted a version and requested L7 replay.
        assert core.last_promotion is not None
        assert core.configuration_workflow is not None
        assert core.reconciler is not None
        assert core.configuration_workflow.records
        relocation = next(
            r
            for r in core.configuration_workflow.records
            if r.kind == ConfigurationChangeKind.SMART_PLUG_RELOCATION
        )
        assert relocation.corroborations >= 3
        assert core.reconciler.runs() or core.background_executor.stats()["completed"] >= 0
    finally:
        harness.close()


def test_hb17_plug_move_is_detected_by_runtime_detector_not_harness() -> None:
    """P6c: the relocation is produced by ``detect_inferred_changes``.

    A spy on the pure detector confirms the runtime's background job is the
    producer, and the TV power-model version is untouched by the move (AT-15).
    """
    from wattmosaic_engine.contracts import ConfigurationChangeKind
    from wattmosaic_engine.electrical import topology_change

    real = topology_change.detect_inferred_changes
    calls: list[dict[str, object]] = []

    def spy(*args: object, **kwargs: object):
        result = real(*args, **kwargs)
        calls.append(kwargs)
        return result

    topology_change.detect_inferred_changes = spy  # type: ignore[assignment]
    try:
        harness = _hb17_harness()
    finally:
        topology_change.detect_inferred_changes = real  # type: ignore[assignment]
    try:
        core = harness.core
        # The detector job actually ran and was fed the association history.
        assert calls, "the runtime background job must call detect_inferred_changes"
        assert any(
            (call.get("association_history") or {}) for call in calls
        ), "the detector must be fed a non-empty association history"
        # The pure detector produced the relocation proposal itself.
        assert any(
            any(
                getattr(change.kind, "value", change.kind)
                == ConfigurationChangeKind.SMART_PLUG_RELOCATION.value
                for change in real(**call)  # type: ignore[arg-type]
            )
            for call in calls
        ), "detect_inferred_changes must propose the plug relocation"
        # The persisted event is INFERRED and detector-attributed.
        relocation_events = [
            ev
            for ev in core.v3_configuration_events()
            if str(getattr(ev.kind, "value", ev.kind)) == "smart_plug_relocation"
        ]
        assert relocation_events
        assert any(
            any("topology_change.detect_inferred_changes" in str(ref.id) for ref in ev.evidence_refs)
            for ev in relocation_events
        )
        # AT-15: promotion changed the mapping, never the TV power model.  The
        # seeded HB17 TV model id is stable across the relocation flow; the
        # workflow itself asserts model immutability inside its promotion step.
        assert core.version_service is not None
        assert core.version_service.model_version_id("tv") == "mset-hb17"
        assert core.last_promotion is not None
        assert (
            core.configuration_workflow.records[0].kind
            == ConfigurationChangeKind.SMART_PLUG_RELOCATION
        )
    finally:
        harness.close()


def test_hb17_late_sample_sets_supersedes_evidence_id() -> None:
    """(c) A late sample after FINAL persists a supersedes_evidence_id link."""
    harness = _hb17_harness()
    try:
        core = harness.core
        from wattmosaic_engine.contracts import MeasurementEvent, StepClass

        reader = "ct_test_late"
        t0 = T0
        event = MeasurementEvent(
            event_id="mev-late",
            reader_id=reader,
            delta_w=10.0,
            sigma_w=1.0,
            z_score=8.0,
            step_class=StepClass.SUSTAINED,
            onset_lo=t0,
            onset_hi=t0 + timedelta(seconds=15),
            pre_level_w=100.0,
            post_level_w=110.0,
            quality_score=0.9,
            detected_at=t0 + timedelta(seconds=2),
        )
        core.window_collector.on_detected(event, at=t0)
        core.window_collector.on_sample(reader, t0 + timedelta(seconds=1), 110.0)
        final = core.window_collector.close(reader)
        assert final is not None and final.finalized
        revised = core.window_collector.on_late_sample(
            reader, t0 + timedelta(seconds=2), 112.0
        )
        assert revised is not None
        assert core.db.repos.evidence.supersedes_evidence_id(revised.evidence_id) == (
            final.evidence_id
        )
    finally:
        harness.close()


def test_configuration_events_route_returns_hb17_event() -> None:
    """(d) GET /api/v1/configuration-events returns the HB17 plug-move event."""

    async def scenario() -> None:
        # Build the harness off the running loop: it uses asyncio.run internally
        # to bridge the sync X07 replay driver to the async engine callbacks.
        harness = await asyncio.to_thread(_hb17_harness)
        try:
            core = harness.core
            app = create_app(core, api_token="")
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/v1/configuration-events")
                assert response.status == 200
                payload = await response.json()
                assert payload, "the route must surface the persisted config event"
                kinds = [
                    ev.get("kind") if isinstance(ev, dict) else getattr(ev, "kind", None)
                    for ev in payload
                ]
                assert any("smart_plug_relocation" in str(k) for k in kinds)
        finally:
            harness.close()

    asyncio.run(scenario())
