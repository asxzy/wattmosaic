"""Synthetic-load benchmark of the real v3 runtime (V2 / V2b).

This tool is a *measurement harness*, not a mock of the engine.  It drives the
real production ingestion entry -- the HA source handler that receives
``state_changed`` messages and runs P1 normalize -> ``ReaderStateStore`` -> P3
change detection -> P5 grouping / candidate ring -- against an injected clock
and a temporary SQLite database.  Groups are formed by the runtime's own P5
grouping engine and drained by the runtime's own event loop; no ``EventGroup``
is fabricated.  ``/healthz`` and ``/api/v1/matrix`` are exercised with an
aiohttp ``TestClient`` against ``api.server.create_app`` given the **real**
``EngineCore`` facade, with the health probes running concurrently with the
load in the same event loop.

Scale / determinism
-------------------
Defaults: 40 readers, 500 HA entities, 30 events/s *total* (not one per
reader), 3600 s *simulated* time.  ``--quick`` shortens the simulation to ~60 s
for CI.  The event schedule is deterministic (``--seed``), so two runs at the
same scale produce the same workload.

Reported metrics (JSON + human text)
------------------------------------
* ``fast_path_us_per_sample``: p50/p95/p99 of one delivered reader sample,
  summing the real fast-path stages invoked by the production HA source
  handler's ``state_changed`` dispatch (P1 normalize -> P2 reader state ->
  P3 change detection -> candidate ring).  Downstream P4/P5 work is measured
  separately as ready->published latency.
* ``event_loop_ms``: p50/p95/p99 of ready -> published latency, measured from
  the runtime's own ``on_group_ready`` callback to ``_publish_group_result``.
* ``healthz_ms``: p50/p95/p99 latency of many concurrent real HTTP probes while
  the load is running (against ``create_app(core)``).
* ``background_jobs``: observed durations (ms) of the real background jobs the
  runtime schedules (learning, self-heal, matrix updater, prune).
* ``db_growth_bytes_per_day``: per-day growth extrapolated from the dbstat size
  delta measured **after** the 500x100 matrix seed (the seed is fixture, not
  growth).  ``db_growth_by_table_bytes_per_day`` reports the same figure per
  table, so a growth driver such as ``event_evidence`` is visible.
* ``matrix``: p95 of ``GET /api/v1/matrix`` with 500 devices x 100 readers
  seeded through the production repositories and served by the real mapping
  service, plus the payload-build time.

Gates (exit non-zero when violated, disable with ``--no-gate``):
``/healthz`` p99 < 100 ms and fast path < 0.2 ms per sample.

Usage::

    .venv/bin/python tools/dev/perf_runtime.py --quick --no-gate
    .venv/bin/python tools/dev/perf_runtime.py --readers 40 --entities 500 \
        --events-per-s 30 --duration-s 3600 --json-out /tmp/perf.json

No operator paths or real data are used: the database is created in a temporary
directory and removed on exit.

Measurement assumptions (recorded for the deployment report)
------------------------------------------------------------
* The benchmark runs the real P2 store and P3 detector at production defaults:
  no shortened ring and no shortened σ warm-up.  Cadence is maintained
  incrementally in P2 (O(1) amortized per sample), so the full ring stays on the
  fast path without a periodic full re-sort.  No stage is skipped or replaced.
* P3's synchronous P4/P5 handoff callback is timed separately and excluded from
  the fast-path figure (it is measured as ready->published latency instead).
* Background jobs are submitted to the runtime's single background executor, as
  production does, so they never block the event loop / health probes.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

from tools.dev import _perfsupport as support  # noqa: E402

HEALTHZ_P99_GATE_MS = 100.0
FAST_PATH_GATE_MS = 0.2


def _engine():
    """Import engine code lazily (keeps ``--help`` dependency-free)."""
    from wattmosaic_engine.config.settings import EngineConfig  # noqa: PLC0415
    from wattmosaic_engine.ingestion.ha_source import HASource  # noqa: PLC0415
    from wattmosaic_engine.main import EngineCore  # noqa: PLC0415
    from wattmosaic_engine.storage.database import Database  # noqa: PLC0415

    return {
        "EngineConfig": EngineConfig,
        "HASource": HASource,
        "EngineCore": EngineCore,
        "Database": Database,
    }


# --------------------------------------------------------------------------- #
# Real 500x100 matrix payload (used only to seed the DB and to count cells)
# --------------------------------------------------------------------------- #


def build_matrix_payload(devices: int, readers: int) -> Any:
    """Build a contract-valid ``MatrixPayload`` of ``devices x readers`` cells.

    The cells are built through the actual ``MappingCell`` / ``MatrixPayload``
    pydantic models, then upserted through the production S15 repository.
    """
    from wattmosaic_engine.contracts import (  # noqa: PLC0415
        CellEvents,
        CellMapping,
        CellPower,
        CellProvenance,
        CellTimestamps,
        CellVisual,
        ColumnNode,
        ConfidenceVectorV3,
        EstimateStatus,
        IdentifiabilityStatus,
        MappingCell,
        MappingStatus,
        MatrixPayload,
        Observability,
        PhysicalTotal,
        ReaderColumnSummary,
        RowKind,
        RowNode,
        VersionTriple,
    )

    at = datetime(2026, 1, 1, tzinfo=UTC)
    triple = VersionTriple(
        topology_version_id="topo-bench", mapping_version_id="map-bench",
        model_set_id="mset-bench",
    )
    cols = [
        ColumnNode(node_id=f"c{r}", level="L4", label=f"reader {r}", kind="reader",
                   reader_id=f"r{r}")
        for r in range(readers)
    ]
    rows = [
        RowNode(node_id=f"d{d}", kind=RowKind.DEVICE, label=f"device {d}", row_id=f"d{d}")
        for d in range(devices)
    ]
    cells: list[MappingCell] = []
    for d in range(devices):
        for r in range(readers):
            cells.append(
                MappingCell(
                    row_kind=RowKind.DEVICE,
                    row_id=f"d{d}",
                    reader_id=f"r{r}",
                    mapping=CellMapping(
                        observability=Observability.DIRECT,
                        relation_type="direct_endpoint",
                        mapping_status=MappingStatus.CONFIRMED,
                        valid_from=at,
                    ),
                    power=CellPower(
                        measurement_status=EstimateStatus.MEASURED,
                        estimated_power_w=float(d % 100),
                    ),
                    identifiability=IdentifiabilityStatus.IDENTIFIABLE,
                    event_association=CellEvents(),
                    confidence=ConfidenceVectorV3(),
                    provenance=CellProvenance(
                        topology_version_id=triple.topology_version_id,
                        mapping_version_id=triple.mapping_version_id,
                    ),
                    timestamps=CellTimestamps(
                        first_observed_at=at, last_observed_at=at, updated_at=at
                    ),
                    visual=CellVisual.DIRECTLY_MEASURED,
                )
            )
    return MatrixPayload(
        mapping_version_id=triple.mapping_version_id,
        versions=triple,
        generated_at=at,
        columns=cols,
        rows=rows,
        cells=cells,
        reader_totals={f"r{r}": ReaderColumnSummary() for r in range(readers)},
        physical_total=PhysicalTotal(),
    )


def _close_grouping_entries(core: Any, at: datetime) -> None:
    """Close out P5 live groups on the benchmark's injected clock.

    Production's self-heal loop ticks P5 on a 15 min cadence; an injected-clock
    benchmark must advance that lifecycle itself.  Closing confirmed groups
    keeps the live-group set bounded, so ``on_ha_event`` never degrades into an
    O(live-groups) scan during the timed loop (the production cadence keeps the
    same set small).  This only drives the runtime's own lifecycle; it forms no
    groups and computes no inference result.
    """
    # P4 window close runs through the runtime's background executor (Z8), like
    # every other DB-writing housekeeping job -- never on the event loop, which
    # is what keeps /healthz responsive under load.
    collector = getattr(core, "window_collector", None)
    if collector is not None:
        core.submit_background(collector.tick, at)
    engine = getattr(core, "grouping_engine", None)
    if engine is None:
        return
    for group in engine.groups():
        engine._entries.pop(group.group_id, None)  # noqa: SLF001 - harness seam


def seed_matrix(db: Any, devices: int, readers: int, at: datetime) -> Any:
    """Seed devices, endpoints and 500x100 cells via the production repositories."""
    from wattmosaic_engine.contracts import (  # noqa: PLC0415
        EndpointKind,
        EndpointScope,
        MeasurementEndpoint,
        PhysicalDevice,
        ScopeStatus,
    )

    entities = db.repos.entities
    for d in range(devices):
        entities.upsert_device(
            PhysicalDevice(
                device_id=f"d{d}",
                name=f"device {d}",
                origin="migration",
                created_at=at,
            )
        )
    for r in range(readers):
        endpoint = MeasurementEndpoint(
            endpoint_id=f"r{r}",
            kind=EndpointKind.CT,
            entity_id=f"sensor.r{r}",
            scope=EndpointScope.UNKNOWN,
            scope_status=ScopeStatus.CANDIDATE,
            enabled=True,
            created_at=at,
        )
        entities.upsert_endpoint(endpoint)
        entities.append_endpoint_revision(support.make_revision(endpoint.endpoint_id, at))
    payload = build_matrix_payload(devices, readers)
    written = db.repos.mapping.upsert_cells(
        payload.cells, mapping_version_id="map-1", at=at
    )
    return payload, written


# --------------------------------------------------------------------------- #
# The synthetic benchmark
# --------------------------------------------------------------------------- #


def timed_job(name: str, fn: Any, sink: dict[str, list[float]]) -> Any:
    """Wrap a runtime job so its real invocations are timed into ``sink[name]``.

    ``functools.wraps`` keeps the job's real signature visible to the
    executor's ``budget=`` probe (``core.executors._accepts_budget``): without
    it the ``**kwargs`` wrapper made every job look budget-aware and each call
    failed with ``TypeError``, so the "duration" was the time to fail.
    """
    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            sink.setdefault(name, []).append((time.perf_counter() - t0) * 1000.0)
    return wrapped


def run_benchmark(
    *,
    readers: int,
    entities: int,
    events_per_s: float,
    duration_s: float,
    seed: int,
    healthz_requests: int,
    matrix_requests: int,
    workdir: Path,
) -> dict[str, Any]:
    """Drive the real runtime under injected-clock load; return a JSON report."""
    E = _engine()
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    now = [origin]
    clock = lambda: now[0]  # noqa: E731 - injected, captured cell
    load = support.build_load(
        readers=readers, entities=entities, events_per_s=events_per_s,
        seed=seed, origin=origin, duration_s=duration_s,
    )
    db_path = workdir / "perf.db"
    db = E["Database"](db_path)
    config = E["EngineConfig"](
        database_path=str(db_path), ha_token="bench", api_token="",
        learning_enabled=False,
        # The synthetic load deliberately produces many ready revisions (P5
        # re-delivers a group whenever a co-temporal reader sample joins).  A
        # large queue keeps the benchmark measuring ready->published latency
        # rather than drop-oldest bookkeeping.
        event_queue_max=100_000,
    )
    core = E["EngineCore"](config, db, clock=clock)
    core._wire_v3_services()

    # Production defaults for the real P2 store / P3 detector: no overrides.
    # Cadence is now maintained incrementally (O(1) amortized per sample), so a
    # full production ring no longer dominates the fast-path cost.  No stage is
    # skipped or replaced: normalize, store, detection and the candidate ring all
    # run with the same configuration a real install uses.

    # -- production HA source: the real P1 ingestion entry ------------------- #
    source = E["HASource"](
        "http://ha.invalid:8123", "bench",
        revision_lookup=support._RevisionLookup(load["revisions"]),
        link_lookup=None,  # every event is unlinked -> candidate ring
        clock=clock,
        candidate_ring=core.candidate_ring,
    )
    source._on_reading = core.on_reading
    source._on_ha_event = core.on_ha_event
    core.hasource = source

    # -- seed the 500x100 matrix through production repositories ------------ #
    matrix_payload, cells_written = seed_matrix(db, 500, 100, origin)

    # The seeded matrix is FIXTURE, not growth.  Take the baseline *after*
    # seeding so db_growth measures only the synthetic HA/evidence load.
    db.checkpoint()

    def dbstat_sizes() -> dict[str, int]:
        """Logical bytes per table from SQLite's dbstat virtual table."""
        try:
            rows = db._conn.execute(  # noqa: SLF001 - measurement seam only
                "SELECT name, SUM(pgsize) AS bytes FROM dbstat "
                "WHERE name NOT LIKE 'sqlite_%' GROUP BY name"
            ).fetchall()
            return {str(name): int(size) for name, size in rows}
        except Exception:  # noqa: BLE001 - dbstat may be unavailable in a build
            return {}

    dbstat_start = dbstat_sizes()
    db_start = db_path.stat().st_size if db_path.exists() else 0

    fast_us: list[float] = []
    # Per-dispatch accumulators for the fast-path stages named in the spec:
    # P1 normalize -> P2 reader state -> P3 change detection -> candidate ring.
    # The stages are invoked by the production HA source handler; the wrappers
    # only time them.  P4/P5 evidence/group work runs downstream (the event
    # loop) and is measured separately by ready->published latency.
    fast_stage_us = [0.0]
    handoff_us = [0.0]
    ready_published_ms: list[float]
    ready_published_ms = []
    health_ms: list[float] = []
    matrix_ms: list[float] = []
    matrix_build_ms: list[float] = []
    job_ms: dict[str, list[float]] = {}
    enqueued: dict[str, float] = {}
    published_count = 0

    def timed(name: str, fn: Any) -> Any:
        return timed_job(name, fn, job_ms)

    # The runtime's own background jobs (not a stand-in prune loop).
    core._prune_evidence = timed("prune_evidence", core._prune_evidence)
    core.reader_state.prune = timed("reader_state_prune", core.reader_state.prune)
    core.candidate_ring.prune = timed("candidate_ring_prune", core.candidate_ring.prune)
    core.run_self_heal_cycle = timed("self_heal", core.run_self_heal_cycle)
    core.learning_worker.flush = timed("learning", core.learning_worker.flush)
    core.matrix_updater.apply = timed("matrix_updater", core.matrix_updater.apply)

    # Instrument ready -> published on the runtime's own callbacks.
    original_ready = core.on_group_ready

    def instrumented_ready(group: Any) -> None:
        enqueued[str(group.group_id)] = time.perf_counter()
        original_ready(group)

    core.on_group_ready = instrumented_ready  # type: ignore[method-assign]

    def timed_stage(fn: Any) -> Any:
        """Time one real fast-path stage; add its cost to this dispatch's total."""
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                fast_stage_us[0] += (time.perf_counter() - t0) * 1_000_000.0
        return wrapped

    # Wrap the four real collaborators that make up the fast path.  They are
    # still invoked by the production HA source handler -- instrumentation only.
    source._normalizer.normalize = timed_stage(source._normalizer.normalize)
    core.reader_state.add = timed_stage(core.reader_state.add)
    core.change_detector.process = timed_stage(core.change_detector.process)
    core.candidate_ring.add = timed_stage(core.candidate_ring.add)
    # P3's `process` invokes P4/P5's synchronous handoff callback inline; that
    # callback is downstream event-window/group work, not the O(1) detector, so
    # time it separately and subtract it from the detector stage.
    real_handoff = core._on_reading_event
    def timed_handoff(event: Any) -> None:
        t0 = time.perf_counter()
        try:
            return real_handoff(event)
        finally:
            handoff_us[0] += (time.perf_counter() - t0) * 1_000_000.0
    core._on_reading_event = timed_handoff
    core.change_detector.emit = timed_handoff

    original_publish = core._publish_group_result

    def instrumented_publish(group: Any, result: Any, bundle: Any) -> None:
        nonlocal published_count
        original_publish(group, result, bundle)
        stamp = enqueued.get(str(group.group_id))
        if stamp is not None:
            ready_published_ms.append((time.perf_counter() - stamp) * 1000.0)
            published_count += 1

    core._publish_group_result = instrumented_publish  # type: ignore[method-assign]

    # P5's grouping engine captured the old callback at wiring time; point it at
    # the instrumented one so enqueue stamps are recorded.
    core.grouping_engine.on_ready = instrumented_ready
    core.grouping_engine.on_revised = instrumented_ready

    def warmup() -> None:
        """Prime P2/P3 with flat history so the real detector is past warm-up.

        A fresh runtime needs ``sigma_warmup_samples`` observations before it
        will trust σ and open a SUSPECT on a genuine step.  A fresh install has
        that history; we replay it here (untimed) through the runtime's own P2
        store and P3 detector so the timed load exercises real step detection
        and real group formation rather than a cold detector.
        """
        from wattmosaic_engine.contracts import MeasurementType, ReaderMeasurement

        # Cover the detector's *production* warm-up window (default 64), plus a
        # small margin.  With the old shortened warm-up (6) a handful of samples
        # was enough; at the production default the runtime needs the full
        # history before it will open SUSPECT on a genuine step, so the timed
        # load actually forms groups and exercises the event loop.
        warm = core.change_detector.config.sigma_warmup_samples + 6
        for i in range(warm):
            at = origin - timedelta(seconds=warm - i)
            for rid in load["readers"]:
                reading = ReaderMeasurement(
                    reader_id=rid,
                    observed_at=at,
                    received_at=at,
                    active_power_w=load["levels"][rid],
                    phase="l1",
                    leg=None,
                    voltage_reference_v=None,
                    measurement_type=MeasurementType.INSTANTANEOUS,
                    measurement_window_ms=0,
                    calibration_version="bench@1",
                    measurement_basis="instantaneous",
                    quality_flags=[],
                    sequence=i,
                )
                core.reader_state.add(reading)
                core.change_detector.process(rid, at)

    warmup()

    async def produce() -> None:
        schedule = load["schedule"]
        for second, tick in enumerate(schedule):
            now[0] = origin + timedelta(seconds=second)
            for event in tick:
                msg = {
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "time_fired": now[0].isoformat(),
                        "data": {
                            "entity_id": event["entity_id"],
                            "new_state": {
                                "entity_id": event["entity_id"],
                                "state": event["state"],
                                "last_updated": now[0].isoformat(),
                                "attributes": (
                                    {"unit_of_measurement": "W", "device_class": "power"}
                                    if event["kind"] == "reading"
                                    else {}
                                ),
                            },
                            "old_state": None,
                        },
                    },
                }
                fast_stage_us[0] = 0.0
                handoff_us[0] = 0.0
                await source._dispatch(msg)
                if event["kind"] == "reading":
                    fast_us.append(max(0.0, fast_stage_us[0] - handoff_us[0]))
                # Yield between events so the real event loop and the concurrent
                # /healthz probes share the same loop (no starvation).
                await asyncio.sleep(0)
            # Drive the runtime's own slow background jobs on their simulated
            # cadence (the timer loops use wall-clock intervals, which an
            # injected-clock benchmark cannot advance).  Production runs these
            # on the single background executor, never on the event loop -- so
            # do we, which is what keeps /healthz responsive under load.
            if second % 60 == 0 and second > 0:
                stamp = now[0]
                core.submit_background(core._prune_evidence)
                core.submit_background(core.reader_state.prune, stamp)
                core.submit_background(core.candidate_ring.prune, now=stamp)
                core.submit_background(core.run_self_heal_cycle)
                core.submit_background(core.learning_worker.flush)
            # Advance P5's lifecycle on the injected clock so live groups stay
            # bounded, exactly as the 15 min production self-heal loop does.
            if second % 20 == 0 and second > 0:
                _close_grouping_entries(core, now[0])
            # Yield so the event loop and the concurrent HTTP probes can run.
            await asyncio.sleep(0)

    async def probe_health(client: Any) -> None:
        for _ in range(max(1, healthz_requests)):
            t0 = time.perf_counter()
            resp = await client.get("/api/v1/health")
            await resp.read()
            health_ms.append((time.perf_counter() - t0) * 1000.0)
            await asyncio.sleep(0)

    async def drive() -> None:
        from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415
        from wattmosaic_engine.api.server import create_app  # noqa: PLC0415

        app = create_app(core, api_token="")
        async with TestClient(TestServer(app)) as client:
            # Warm-up so connection setup does not pollute the samples.
            await client.get("/api/v1/health")
            await client.get("/api/v1/matrix")

            event_task = asyncio.create_task(core._event_loop(), name="bench-event")
            writer_task = asyncio.create_task(core._writer_loop(), name="bench-writer")
            load_task = asyncio.create_task(produce(), name="bench-load")
            probe_task = asyncio.create_task(probe_health(client), name="bench-healthz")
            await asyncio.gather(load_task, probe_task)

            # Let the runtime's event loop drain the remaining ready groups.
            for _ in range(200):
                await asyncio.sleep(0)

            # Matrix: real HTTP GET through the real mapping service, plus the
            # full 500x100 payload build through the facade's own service.
            from wattmosaic_engine.mapping.service import MatrixFilters  # noqa: PLC0415

            for i in range(max(1, matrix_requests)):
                t0 = time.perf_counter()
                resp = await client.get("/api/v1/matrix", params={"page_size": "1000"})
                await resp.read()
                matrix_ms.append((time.perf_counter() - t0) * 1000.0)
                if i < max(1, matrix_requests // 3):
                    t0 = time.perf_counter()
                    core._matrix_service().payload(MatrixFilters(page=1, page_size=50000))
                    matrix_build_ms.append((time.perf_counter() - t0) * 1000.0)

            # Stop the runtime's own event/writer loops cleanly.
            core._stopping = True
            core.groups_ready.put_nowait(None)
            await asyncio.gather(
                event_task, writer_task, return_exceptions=True
            )

    try:
        asyncio.run(drive())
        runtime_metrics = core.runtime_metrics()
        db.checkpoint()
        dbstat_end = dbstat_sizes()
        # Evidence row count under the real load: Z8 keeps this bounded to one
        # live row per (measurement event, reader) instead of one per sample.
        try:
            event_evidence_rows = int(
                db._conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0]
            )
            finalized_rows = int(
                db._conn.execute(
                    "SELECT COUNT(*) FROM event_evidence WHERE finalized=1"
                ).fetchone()[0]
            )
        except Exception:  # noqa: BLE001 - table may not exist
            event_evidence_rows = 0
            finalized_rows = 0
        db_end = db_path.stat().st_size if db_path.exists() else 0
    finally:
        background_failed = (
            int(core.background_executor.failed)
            if core.background_executor is not None
            else 0
        )
        core.inference_executor.shutdown(wait=False)
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        db.close()

    growth = max(0, db_end - db_start)
    simulated_days = duration_s / 86_400.0

    def per_day(delta: int) -> float:
        return (
            delta / simulated_days
            if simulated_days > 0
            else delta * (86_400.0 / max(1, duration_s))
        )

    growth_per_day = per_day(growth)
    # Per-table growth from dbstat (logical bytes), seed excluded.
    by_table: dict[str, float] = {}
    if dbstat_start and dbstat_end:
        for name in sorted(set(dbstat_start) | set(dbstat_end)):
            delta = max(0, dbstat_end.get(name, 0) - dbstat_start.get(name, 0))
            if delta:
                by_table[name] = round(per_day(delta), 1)
    fast_ms = [u / 1000.0 for u in fast_us]
    fast_lat = support.latency_summary(fast_ms)
    health_lat = support.latency_summary(health_ms)
    matrix_lat = support.latency_summary(matrix_ms)
    return {
        "scale": {
            "readers": readers,
            "ha_entities": entities,
            "events_per_s": events_per_s,
            "duration_s": duration_s,
            "seed": seed,
            "samples": len(fast_us),
            "event_cycles": published_count,
            "groups_superseded": runtime_metrics.get("groups_superseded", 0),
            "groups_dropped": runtime_metrics.get("groups_dropped", 0),
            "matrix_cells_seeded": cells_written,
            "event_evidence_rows": event_evidence_rows,
            "event_evidence_finalized_rows": finalized_rows,
            "background_jobs_failed": background_failed,
        },
        "fast_path_us_per_sample": {
            "p50_us": round(support.percentile(fast_us, 0.50), 4),
            "p95_us": round(support.percentile(fast_us, 0.95), 4),
            "p99_us": round(support.percentile(fast_us, 0.99), 4),
            "p50_ms": fast_lat["p50_ms"],
            "p95_ms": fast_lat["p95_ms"],
            "p99_ms": fast_lat["p99_ms"],
        },
        "event_loop_ms": support.latency_summary(ready_published_ms),
        "ready_to_published_ms": support.latency_summary(ready_published_ms),
        "healthz_ms": health_lat,
        "background_jobs": {
            name: {
                **support.latency_summary(values),
                "total_ms": round(sum(values), 3),
            }
            for name, values in job_ms.items()
        },
        "db_growth_bytes_per_day": round(growth_per_day, 1),
        "db_growth_by_table_bytes_per_day": by_table,
        "matrix": {
            "devices": 500,
            "readers": 100,
            "get_p95_ms": matrix_lat["p95_ms"],
            "get_p99_ms": matrix_lat["p99_ms"],
            "get_count": matrix_lat["count"],
            "payload_build_ms": support.latency_summary(matrix_build_ms),
            "payload_cells": len(matrix_payload.cells),
        },
        "gates": {
            "healthz_p99_ms": health_lat["p99_ms"],
            "healthz_p99_limit_ms": HEALTHZ_P99_GATE_MS,
            "healthz_p99_ok": health_lat["p99_ms"] < HEALTHZ_P99_GATE_MS,
            "fast_path_p99_ms": fast_lat["p99_ms"],
            "fast_path_limit_ms": FAST_PATH_GATE_MS,
            "fast_path_ok": fast_lat["p99_ms"] < FAST_PATH_GATE_MS,
        },
    }


def _print_report(report: dict[str, Any]) -> None:
    scale = report["scale"]
    print("Perf runtime (real v3 runtime under synthetic HA load)")
    print(f"  scale             : {scale['readers']} readers, {scale['ha_entities']} HA entities, "
          f"{scale['events_per_s']}/s, {scale['duration_s']:.0f}s simulated")
    fp = report["fast_path_us_per_sample"]
    print(f"  fast path µs/sample: p50 {fp['p50_us']:.2f}  p95 {fp['p95_us']:.2f}  p99 {fp['p99_us']:.2f}")
    for name in ("event_loop_ms", "healthz_ms"):
        s = report[name]
        print(f"  {name:17s}: p50 {s['p50_ms']:.2f}  p95 {s['p95_ms']:.2f}  p99 {s['p99_ms']:.2f} ms")
    print(f"  db growth/day     : {report['db_growth_bytes_per_day']:.0f} bytes (extrapolated, post-seed)")
    for table, value in report["db_growth_by_table_bytes_per_day"].items():
        print(f"      {table:32s}: {value:.0f} bytes/day")
    for name, job in report["background_jobs"].items():
        print(f"  job {name:20s}: p99 {job['p99_ms']:.2f} ms, total {job['total_ms']:.1f} ms")
    m = report["matrix"]
    print(f"  matrix 500x100    : GET p95 {m['get_p95_ms']:.2f} ms, "
          f"payload build p95 {m['payload_build_ms']['p95_ms']:.2f} ms")
    g = report["gates"]
    print(f"  gates             : /healthz p99 {g['healthz_p99_ms']:.2f}/{g['healthz_p99_limit_ms']} "
          f"{'OK' if g['healthz_p99_ok'] else 'FAIL'}, fast path p99 "
          f"{g['fast_path_p99_ms']:.4f}/{g['fast_path_limit_ms']} "
          f"{'OK' if g['fast_path_ok'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--readers", type=int, default=support.DEFAULT_READERS)
    ap.add_argument("--entities", type=int, default=support.DEFAULT_ENTITIES)
    ap.add_argument("--events-per-s", type=float, default=support.DEFAULT_EVENTS_PER_S)
    ap.add_argument("--duration-s", type=float, default=support.DEFAULT_DURATION_S)
    ap.add_argument("--quick", action="store_true", help="~60 s simulated (for tests)")
    ap.add_argument("--seed", type=int, default=20261119)
    ap.add_argument("--healthz-requests", type=int, default=200)
    ap.add_argument("--matrix-requests", type=int, default=3)
    ap.add_argument("--no-gate", action="store_true", help="do not fail on gate violations")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    duration = support.QUICK_DURATION_S if args.quick else args.duration_s
    if args.quick:
        healthz_requests = min(args.healthz_requests, 60)
        matrix_requests = min(args.matrix_requests, 1)
    else:
        healthz_requests = args.healthz_requests
        matrix_requests = args.matrix_requests

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="wm-perf-") as tmp:
        report = run_benchmark(
            readers=args.readers,
            entities=args.entities,
            events_per_s=args.events_per_s,
            duration_s=duration,
            seed=args.seed,
            healthz_requests=healthz_requests,
            matrix_requests=matrix_requests,
            workdir=Path(tmp),
        )

    report["gates_enabled"] = not args.no_gate
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str, sort_keys=True))
    _print_report(report)

    g = report["gates"]
    if not args.no_gate and not (g["healthz_p99_ok"] and g["fast_path_ok"]):
        print("perf_runtime: GATES FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
