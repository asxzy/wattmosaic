"""L6 — background learner worker off the event loop (DEC-37, F6, HB §15).

Three acceptance checks:

1. ``/healthz`` latency stays < 100 ms while a forced 2 s training runs.  The
   worker drains on an injected executor; a slow training job therefore never
   runs on the probe's event-loop thread (modelled on the ADR-0033 tests
   ``test_discovery_analysis_runs_off_the_event_loop`` and
   ``test_nesting_runs_off_the_event_loop``).
2. A cycle with no dirty subjects performs zero learning DB queries — the old
   per-cycle scan of every topology device is gone.
3. A worker restart loses no evidence: the dirty set is re-derived from the
   S18 ``dirty_subjects()`` view.

Everything is driven directly — no P6 executor is required.  The production
code depends only on Protocols/contracts; the doubles here are local.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from wattmosaic_engine.api.facade import StubFacade
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.contracts import (
    EvidenceRef,
    IdentifiabilityStatus,
    LearningObservation,
    ModelBasis,
)
from wattmosaic_engine.learning.worker import (
    InlineExecutor,
    LearningWorker,
)
from wattmosaic_engine.models.registry import DatabaseModelRepository, Registry
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Test doubles / fixtures
# --------------------------------------------------------------------------- #


def _obs(index: int, *, subject: str = "dev-a", at: datetime = T0) -> LearningObservation:
    return LearningObservation(
        observation_id=f"obs-{index:04d}",
        subject_kind="device",
        subject_id=subject,
        endpoint_id="ct-row",
        event_group_ref=EvidenceRef(kind="event_group", id=f"grp-{index // 2}"),
        transition="active",
        attributes={},
        delta_w=60.0 + (index % 3) * 0.3,
        level_w=None,
        status="confirmed",
        rejection_reason=None,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        basis=ModelBasis.DELTA,
        created_at=at,
    )


class _EvidenceSource:
    """S18-shaped source: confirmed observations + dirty_subjects + id lookup.

    Every read is counted so the zero-query assertion is exact.  In production
    this is the ``LearningRepository``; here it is a deterministic in-memory
    double with the identical Protocol surface the worker consumes.
    """

    def __init__(self, observations: list[LearningObservation]) -> None:
        self._by_id = {o.observation_id: o for o in observations}
        self._by_subject: dict[str, list[LearningObservation]] = {}
        for observation in observations:
            self._by_subject.setdefault(observation.subject_id, []).append(observation)
        self.reads = 0

    def observation_by_id(self, observation_id: str) -> LearningObservation | None:
        self.reads += 1
        return self._by_id.get(observation_id)

    def observations(self, subject_id: str, status: str = "confirmed") -> list[dict[str, Any]]:
        self.reads += 1
        rows = self._by_subject.get(subject_id, [])
        return [o.model_dump(mode="json") for o in rows if o.status == status]

    def dirty_subjects(self) -> list[tuple[str, str]]:
        self.reads += 1
        return sorted({("device", sid) for sid in self._by_subject})


def _registry() -> Registry:
    """A real v3 Registry over an in-memory repository (no wall clock use)."""
    db = Database(":memory:")
    repo = DatabaseModelRepository(db)
    return Registry(repo, clock=lambda: T0)


# --------------------------------------------------------------------------- #
# 1. /healthz stays responsive while a 2 s training runs on the worker
# --------------------------------------------------------------------------- #


def test_training_runs_off_the_event_loop() -> None:
    """A forced 2 s training must not delay a concurrent /healthz probe."""
    observations = [_obs(i) for i in range(8)]
    source = _EvidenceSource(observations)
    registry = _registry()

    # Force a slow 2 s training *inside* the worker's job.  We wrap the worker's
    # subject job with a blocking sleep performed on the executor thread only.
    worker = LearningWorker(
        source,
        registry,
        executor=ThreadPoolExecutor(max_workers=1),
        clock=lambda: T0,
    )
    real_train = worker._train_subject  # noqa: SLF001 - test drives the seam

    def slow_train(subject_id: str, entry: Any) -> Any:
        time.sleep(2.0)  # exactly the DEC-37 2 s CPU job
        return real_train(subject_id, entry)

    worker._train_subject = slow_train  # type: ignore[method-assign]  # noqa: SLF001

    try:
        worker.submit_observations(observations)
        server = APIServer(StubFacade(), host="127.0.0.1", port=0)

        async def probe_while_training() -> tuple[float, int, int]:
            loop_thread = threading.get_ident()
            train_threads: list[int] = []
            original = worker._train_subject  # noqa: SLF001

            def recording_train(subject_id: str, entry: Any) -> Any:
                train_threads.append(threading.get_ident())
                return original(subject_id, entry)

            worker._train_subject = recording_train  # type: ignore[method-assign]  # noqa: SLF001

            await server.start()
            try:
                # Kick the worker off on its background thread.
                worker.schedule()
                # Wait until the worker has actually entered the slow job, so
                # the probe overlaps the full 2 s training window.
                for _ in range(200):
                    if train_threads:
                        break
                    await asyncio.sleep(0.01)
                assert train_threads, "the worker never entered the training job"
                ticks = 0
                stop = asyncio.Event()

                async def ticker() -> None:
                    nonlocal ticks
                    while not stop.is_set():
                        ticks += 1
                        await asyncio.sleep(0.01)

                async with aiohttp.ClientSession() as session:
                    task = asyncio.create_task(ticker())
                    started = time.perf_counter()
                    # ``/health`` is the engine's watchdog-equivalent liveness
                    # probe (the HA supervisor's ``/healthz`` maps to it here).
                    async with session.get(f"http://127.0.0.1:{server.port}/api/v1/health") as response:
                        assert response.status == 200
                        await response.read()
                    elapsed = time.perf_counter() - started
                    # Keep the 2 s training running while the probe's ticks are
                    # counted: the worker thread must be independent of the loop.
                    await asyncio.sleep(2.1)
                    stop.set()
                    await task
                worker.flush(timeout=10.0)
                return elapsed, ticks, loop_thread, train_threads  # type: ignore[return-value]
            finally:
                await server.stop()

        elapsed, ticks, loop_thread, train_threads = asyncio.run(probe_while_training())

        assert elapsed < 0.1, f"health probe took {elapsed:.3f}s (> 100 ms) during training"
        assert ticks >= 3, f"only {ticks} loop ticks during the probe: loop was starved"
        assert train_threads, "the training job never ran"
        assert train_threads[0] != loop_thread, "training ran on the event-loop thread"
    finally:
        executor = worker.executor
        shutdown = getattr(executor, "shutdown", None)
        if callable(shutdown):
            shutdown(wait=True)


# --------------------------------------------------------------------------- #
# 2. Idle cycle costs zero learning DB queries
# --------------------------------------------------------------------------- #


def test_no_queries_when_nothing_is_dirty() -> None:
    source = _EvidenceSource([])  # no confirmed observations anywhere
    # Driven directly with the inline executor: P6's note 4 (no real thread).
    worker = LearningWorker(
        source, _registry(), executor=InlineExecutor(), clock=lambda: T0
    )
    worker.start()
    reads_after_start = source.reads

    report = worker.drain()

    assert report.trained == ()
    assert source.reads == reads_after_start, (
        f"an idle cycle performed {source.reads - reads_after_start} learning DB queries"
    )
    assert worker.jobs_run == 0
    assert worker.observations_consumed == 0


def test_submit_is_a_pure_fast_path_without_training() -> None:
    observations = [_obs(i) for i in range(6)]
    source = _EvidenceSource(observations)
    worker = LearningWorker(source, _registry(), clock=lambda: T0)

    added = worker.submit([o.observation_id for o in observations])

    # All six observations belong to one subject; the first dirties it, the
    # rest are folded into the same dirty entry (dedup is by subject).
    assert added == 1
    assert len(worker.dirty_subjects) == 1
    assert worker.jobs_run == 0, "submit trained inline instead of only dirtying"
    # Re-submitting the same ids is a no-op (deduplicated by observation id).
    assert worker.submit([o.observation_id for o in observations]) == 0


# --------------------------------------------------------------------------- #
# 3. Restart safety: no evidence is lost
# --------------------------------------------------------------------------- #


def test_worker_restart_loses_no_work() -> None:
    observations = [_obs(i, at=T0 + timedelta(seconds=i)) for i in range(6)]
    source = _EvidenceSource(observations)

    # Worker A receives the evidence but is stopped before draining it.
    first = LearningWorker(source, _registry(), clock=lambda: T0)
    first.submit_observations(observations)
    assert first.dirty_subjects

    # Worker B starts fresh (empty in-memory dirty set) and re-derives its work
    # from the S18 dirty_subjects() view — no hidden in-memory state.
    second = LearningWorker(source, _registry(), clock=lambda: T0)
    assert second.dirty_subjects == ()
    second.start()
    assert second.dirty_subjects == (("device", "dev-a"),)

    report = second.drain()
    assert report.trained and report.trained[0].observations_used == len(observations)
    assert first.jobs_run == 0


def test_restart_requeues_a_subject_without_any_local_state() -> None:
    observations = [_obs(i) for i in range(6)]
    source = _EvidenceSource(observations)
    worker = LearningWorker(source, _registry(), clock=lambda: T0)
    worker.start()
    report = worker.drain()
    assert report.trained[0].observations_used == len(observations)
    assert worker.dirty_subjects == (), "a drained subject must leave the dirty set"


# --------------------------------------------------------------------------- #
# Wiring: the fast-path hook is present and only enqueues
# --------------------------------------------------------------------------- #


def test_backend_confirmed_evidence_hook_only_enqueues() -> None:
    from wattmosaic_engine.backend.learning import LearningMixin

    class Backend(LearningMixin):
        pass

    backend = Backend()
    assert backend.on_confirmed_evidence(["obs-0000"]) == 0, "no worker wired is a no-op"

    class _Worker:
        def __init__(self) -> None:
            self.submitted: list[Any] = []
            self.scheduled = 0

        def submit(self, ids: Any) -> int:
            self.submitted.append(ids)
            return 1

        def schedule(self) -> None:
            self.scheduled += 1

    worker = _Worker()
    backend.learning_worker = worker  # type: ignore[attr-defined]
    assert backend.on_confirmed_evidence(["obs-0000"]) == 1
    assert worker.submitted == [["obs-0000"]]
    assert worker.scheduled == 1


def test_worker_module_does_not_import_runtime_fakes() -> None:
    """Production code must never reach the deterministic doubles."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2]
        / "engine"
        / "wattmosaic_engine"
        / "learning"
        / "worker.py"
    ).read_text(encoding="utf-8")
    assert "import runtime.fakes" not in source
    assert "from wattmosaic_engine.runtime import fakes" not in source
