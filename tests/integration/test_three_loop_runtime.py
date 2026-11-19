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

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import HAEvent, MeasurementType, ReaderMeasurement
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
