"""T08 — runtime wiring for raw CT capture (ADR-0042).

The runtime must honour ``raw_capture_days``: no persistence when it is 0, and a
single off-loop deletion path (``_purge_raw_capture``) for ``ct_samples``.  AC1–AC5
from ``design.md`` are pinned here.  The job-level tests use the light
``object.__new__(EngineCore)`` construction from ``test_derived_retention.py``;
only the two end-to-end tests drive a real harness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import MeasurementBasis, MeasurementType, ReaderMeasurement
from wattmosaic_engine.main import EngineCore

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)


def _reading(reader_id: str, power: float, at: datetime, seq: int = 1) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader_id,
        observed_at=at,
        received_at=at,
        active_power_w=power,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED,
        sequence=seq,
    )


def _count(db: Any) -> int:
    with db._lock:
        return int(db._conn.execute("SELECT COUNT(*) FROM ct_samples").fetchone()[0])


def _job_core(config: EngineConfig, db: Any) -> EngineCore:
    """A minimal core with only the attributes the capture jobs read."""
    core = object.__new__(EngineCore)
    core.config = config
    core.db = db
    core._clock = lambda: NOW
    core.raw_capture_purged = 0
    return core


# --------------------------------------------------------------------------- #
# AC1 / AC2 — end to end through a real EngineCore
# --------------------------------------------------------------------------- #


def test_default_does_not_persist_readings() -> None:
    """AC1: default config leaves ct_samples empty, samples_persisted == 0, yet
    snapshots and inference results are still produced."""
    from tests.support.hb17_seed import start_hb17_harness
    from tools.simulator.hb17 import build

    house = build(17)
    harness = start_hb17_harness({"house": house, "observation": house.observation_view()})
    try:
        asyncio.run(harness.core._drain_persist_queue())
        assert _count(harness.db) == 0
        assert harness.core.samples_persisted == 0
        assert harness.result is not None, "inference must still run with capture off"
        assert harness.snapshot is not None, "a snapshot must still be published"
    finally:
        harness.close()


def test_capture_persists_readings() -> None:
    """AC2: raw_capture_days=2 writes readings to ct_samples."""
    from wattmosaic_engine.realtime import EngineHarness

    def configure(core: Any) -> None:
        core.config.raw_capture_days = 2

    harness = EngineHarness.start([], start_time=NOW, configure=configure)
    try:
        core = harness.core
        for seq in range(5):
            asyncio.run(
                core.on_reading(_reading("ct_a", 100.0 + seq, NOW + timedelta(seconds=seq), seq))
            )
        asyncio.run(core._drain_persist_queue())
        assert _count(harness.db) == 5
        assert core.samples_persisted == 5
    finally:
        harness.close()


# --------------------------------------------------------------------------- #
# AC2 / AC3 — the purge job
# --------------------------------------------------------------------------- #


def test_purge_job_respects_horizon(tmp_db_path: Any) -> None:
    """AC2: only rows older than ``raw_capture_days`` are removed; the counter
    grows by the number deleted."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        core = _job_core(EngineConfig(raw_capture_days=2), db)
        db.repos.samples.insert_readings(
            [
                _reading("ct", 1.0, NOW - timedelta(days=3), 1),
                _reading("ct", 2.0, NOW - timedelta(days=1), 2),
            ]
        )
        core._purge_raw_capture()
        assert _count(db) == 1
        remaining = db._conn.execute("SELECT received_at FROM ct_samples").fetchall()
        assert remaining[0][0].startswith((NOW - timedelta(days=1)).date().isoformat())
        assert core.raw_capture_purged == 1
    finally:
        db.close()


def test_purge_job_drains_when_off(tmp_db_path: Any, monkeypatch: Any) -> None:
    """AC3: with capture off, repeated bounded runs drain a legacy table and no
    single run deletes more than its batch budget."""
    import wattmosaic_engine.storage.capture as capture
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        core = _job_core(EngineConfig(raw_capture_days=0), db)
        db.repos.samples.insert_readings(
            [_reading("ct", 1.0, NOW - timedelta(days=i + 1), i) for i in range(12)]
        )
        real = capture.purge_ct_samples
        results: list[Any] = []

        def bounded(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("batch_rows", 5)
            kwargs["max_batches"] = 1  # one short transaction per job run
            kwargs["time_budget_s"] = None
            result = real(*args, **kwargs)
            results.append(result)
            return result

        monkeypatch.setattr(capture, "purge_ct_samples", bounded)
        for _ in range(10):
            if _count(db) == 0:
                break
            core._purge_raw_capture()
        assert _count(db) == 0
        assert results and all(r.deleted <= 5 for r in results)
        assert len(results) >= 3, "the drain must span several budgeted runs"
        assert core.raw_capture_purged == 12
    finally:
        db.close()


def test_purge_job_failure_is_degraded(tmp_db_path: Any, monkeypatch: Any) -> None:
    """A purge failure is logged and swallowed — never a crash — and the
    counter is left unchanged."""
    import wattmosaic_engine.storage.capture as capture
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        core = _job_core(EngineConfig(raw_capture_days=2), db)

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("purge exploded")

        monkeypatch.setattr(capture, "purge_ct_samples", boom)
        core._purge_raw_capture()  # must not raise
        assert core.raw_capture_purged == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# AC5 and maintenance plumbing
# --------------------------------------------------------------------------- #


def test_purge_job_maps_budget_remaining_to_time_budget(
    tmp_db_path: Any, monkeypatch: Any
) -> None:
    """A budget exposing remaining_s() always caps the pass via time_budget_s;
    even a spent allowance is forwarded (the purge still runs one batch)."""
    import wattmosaic_engine.storage.capture as capture
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        core = _job_core(EngineConfig(raw_capture_days=2), db)
        budget = SimpleNamespace(remaining_s=lambda: 0.5)
        seen: dict[str, Any] = {}

        def spy(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return capture.PurgeResult(0, 1, True)

        monkeypatch.setattr(capture, "purge_ct_samples", spy)
        core._purge_raw_capture(budget=budget)
        assert seen["time_budget_s"] == 0.5

        seen.clear()
        core._purge_raw_capture(budget=SimpleNamespace(remaining_s=lambda: 0.0))
        assert seen["time_budget_s"] == 0.0
    finally:
        db.close()


def test_prune_evidence_never_touches_ct_samples(tmp_db_path: Any) -> None:
    """AC5: the evidence prune no longer issues a DELETE against ct_samples —
    an old row survives even with a tiny evidence-raw horizon."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        core = _job_core(
            EngineConfig(raw_capture_days=0, evidence_raw_retention_days=1), db
        )
        old = NOW - timedelta(days=5)
        db.repos.samples.insert_readings([_reading("ct", 1.0, old, 1)])
        core._prune_evidence()
        assert _count(db) == 1
    finally:
        db.close()


def test_maintenance_passes_capture_days() -> None:
    """The on-loop maintenance prune hands capture_days to db.prune and submits
    the off-loop purge job."""
    core = object.__new__(EngineCore)
    core.config = EngineConfig(raw_capture_days=7)
    # Stay "running" so the loop executes one cycle; the already-set stop event
    # makes the trailing wait_for return immediately, ending the loop.
    core._stopping = False
    core._stop_event = asyncio.Event()
    core._stop_event.set()
    core.aligner = SimpleNamespace(prune=lambda: None)
    core._raw_capture_purge_future = None

    seen: dict[str, Any] = {}

    def fake_prune(raw: int, agg: int, *, capture_days: int | None = None) -> dict[str, int]:
        seen["args"] = (raw, agg)
        seen["capture_days"] = capture_days
        return {}

    core.db = SimpleNamespace(prune=fake_prune)
    submitted: list[Any] = []
    core.submit_background = lambda fn, *a, **kw: submitted.append(fn)  # type: ignore[method-assign]

    async def noop() -> None:
        return None

    core.maybe_bootstrap_topology = noop  # type: ignore[method-assign]
    asyncio.run(core._maintenance_loop())

    assert seen["capture_days"] == 7
    assert seen["args"] == (core.config.retention_days_raw, core.config.retention_days_aggregates)
    assert core._purge_raw_capture in submitted


class _NeverSet:
    """A stop event whose wait() always times out, driving another loop cycle."""

    async def wait(self) -> None:
        raise TimeoutError


class _Future:
    def __init__(self) -> None:
        self._done = False

    def done(self) -> bool:
        return self._done


def test_maintenance_skips_overlapping_purge() -> None:
    """A still-running purge is not resubmitted on the next maintenance tick;
    once its future is done, a later tick submits again."""
    core = object.__new__(EngineCore)
    core.config = EngineConfig(raw_capture_days=2)
    core._stopping = False
    core._stop_event = _NeverSet()
    core.aligner = SimpleNamespace(prune=lambda: None)
    core.db = SimpleNamespace(prune=lambda *a, **k: {})
    core._raw_capture_purge_future = None

    purge_future = _Future()
    submitted: list[Any] = []

    def submit(fn: Any, *a: Any, **k: Any) -> Any:
        submitted.append(fn)
        # Bound methods are fresh objects on each access, so compare by name.
        return purge_future if getattr(fn, "__name__", "") == "_purge_raw_capture" else None

    core.submit_background = submit  # type: ignore[method-assign]

    calls = [0]

    async def bootstrap() -> None:
        calls[0] += 1
        if calls[0] == 2:
            # First purge finished after cycle 2 skipped it.
            purge_future._done = True
        elif calls[0] >= 3:
            core._stopping = True

    core.maybe_bootstrap_topology = bootstrap  # type: ignore[method-assign]
    asyncio.run(core._maintenance_loop())

    assert calls[0] == 3
    assert submitted.count(core._purge_raw_capture) == 2  # cycles 1 and 3, not 2
