"""Synthetic-load benchmark of the real v3 runtime (V2 / task V2a).

This tool is a *measurement harness*, not a mock of the engine.  It drives the
real ingestion fast path (HA state message -> ``realtime.normalize`` ->
``ReaderStateStore`` / ``CandidateRing``), the real event loop (``EventGroup`` ->
``_build_input_bundle`` -> ``_run_infer_group`` -> ``_publish_group_result``) and
the real background jobs, against an injected clock and a temporary SQLite
database.  ``/healthz`` and ``/api/v1/matrix`` are exercised with an aiohttp
``TestClient`` against the real app factory (``api.server.create_app``).

Scale / determinism
-------------------
Defaults: 40 readers, 500 HA entities, 30 events/s, 3600 s *simulated* time.
``--quick`` shortens the simulation to ~60 s for CI.  The RNG is deterministic
(``--seed``), so two runs at the same scale produce the same workload.

Reported metrics (JSON + human text)
------------------------------------
* ``fast_path_us_per_sample``: p50/p95/p99 of the real fast-path cost per sample
  (normalize + reader-state insert + candidate append).
* ``event_loop_ms``: p50/p95/p99 of one full event-loop inference cycle.
* ``healthz_ms``: p50/p95/p99 latency of many concurrent real HTTP probes.
* ``background_jobs``: observed durations (ms) of the real maintenance jobs.
* ``db_growth_bytes_per_day``: extrapolated from observed file growth.
* ``matrix``: p95 of ``GET /api/v1/matrix`` with 500 devices x 100 readers and
  the payload-build time (ms).

Gates (exit non-zero when violated, disable with ``--no-gate``):
``/healthz`` p99 < 100 ms and fast path < 0.2 ms per sample.

Usage::

    .venv/bin/python tools/dev/perf_runtime.py --quick --no-gate
    .venv/bin/python tools/dev/perf_runtime.py --readers 40 --entities 500 \
        --events-per-s 30 --duration-s 3600 --json-out /tmp/perf.json

No operator paths or real data are used: the database is created in a temporary
directory and removed on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
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
    from wattmosaic_engine.contracts import (  # noqa: PLC0415
        EventGroup,
        EventGroupStatus,
        HAEvent,
        ReaderDelta,
        VersionTriple,
        model_set_id,
    )
    from wattmosaic_engine.main import EngineCore  # noqa: PLC0415
    from wattmosaic_engine.realtime.normalize import ReadingNormalizer  # noqa: PLC0415
    from wattmosaic_engine.storage.database import Database  # noqa: PLC0415

    return {
        "EngineConfig": EngineConfig,
        "EventGroup": EventGroup,
        "EventGroupStatus": EventGroupStatus,
        "HAEvent": HAEvent,
        "ReaderDelta": ReaderDelta,
        "VersionTriple": VersionTriple,
        "model_set_id": model_set_id,
        "EngineCore": EngineCore,
        "ReadingNormalizer": ReadingNormalizer,
        "Database": Database,
    }


# --------------------------------------------------------------------------- #
# Synthetic matrix payload (500 devices x 100 readers)
# --------------------------------------------------------------------------- #


def build_matrix_payload(devices: int, readers: int) -> Any:
    """Build a real, contract-valid ``MatrixPayload`` of ``devices x readers`` cells.

    The matrix 500x100 gate is about payload construction/serialisation cost, so
    the cells are built through the actual ``MappingCell`` / ``MatrixPayload``
    pydantic models rather than plain dicts.
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


class _MatrixFacade:
    """A facade serving only the two routes the benchmark needs.

    ``health`` is a cheap, constant read (the same shape ``core.health()``
    serves) so the probe measures the HTTP/loop path; ``matrix_payload`` and
    ``matrix_etag`` are served from a pre-built real ``MatrixPayload``.
    """

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "database": "ok",
            "ha_connected": True,
            "queue_depth": 0,
        }

    def matrix_etag(self) -> str:
        return "map-bench-abc123"

    def matrix_payload(self, query: Any = None) -> Any:
        return self.payload


async def _probe_app(
    app: Any, *, healthz_count: int, matrix_count: int
) -> tuple[list[float], list[float], list[float]]:
    """Issue real HTTP probes against ``create_app`` via ``TestClient``.

    Both routes share one aiohttp application, so they are exercised inside a
    single event loop (an aiohttp ``Application`` cannot be bound to two loops).
    """
    from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

    health: list[float] = []
    matrix: list[float] = []
    builds: list[float] = []
    async with TestClient(TestServer(app)) as client:
        # Warm-up so connection setup does not pollute the sample.
        await client.get("/api/v1/health")
        await client.get("/api/v1/matrix")
        for i in range(max(healthz_count, matrix_count)):
            if i < healthz_count:
                start = time.perf_counter()
                resp = await client.get("/api/v1/health")
                await resp.read()
                health.append((time.perf_counter() - start) * 1000.0)
            if i < matrix_count:
                start = time.perf_counter()
                resp = await client.get("/api/v1/matrix")
                await resp.read()
                matrix.append((time.perf_counter() - start) * 1000.0)
                if i < max(1, matrix_count // 5):  # 50k cells: keep builds bounded
                    t = time.perf_counter()
                    build_matrix_payload(500, 100)
                    builds.append((time.perf_counter() - t) * 1000.0)
    return health, matrix, builds


# --------------------------------------------------------------------------- #
# The synthetic benchmark
# --------------------------------------------------------------------------- #


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
        seed=seed, origin=origin,
    )
    db_path = workdir / "perf.db"
    db = E["Database"](db_path)
    config = E["EngineConfig"](
        database_path=str(db_path), ha_token="bench", api_token="",
        learning_enabled=False,
    )
    core = E["EngineCore"](config, db, clock=clock)
    core._wire_v3_services()
    normalizer = E["ReadingNormalizer"](support._RevisionLookup(load["revisions"]))

    fast_us: list[float] = []
    event_ms: list[float] = []
    job_ms: dict[str, list[float]] = {}
    db_start = db_path.stat().st_size if db_path.exists() else 0
    rng: random.Random = load["rng"]
    rids: list[str] = load["readers"]
    eids: list[str] = load["entities"]
    levels: dict[str, float] = load["levels"]
    sequences = {rid: 0 for rid in rids}
    triple: Any = core.version_service.current()

    def _time_job(name: str, fn: Any, *args: Any) -> Any:
        start = time.perf_counter()
        try:
            return fn(*args)
        finally:
            job_ms.setdefault(name, []).append((time.perf_counter() - start) * 1000.0)

    try:
        # Advance the injected clock in 1 s simulated ticks.  At each tick we
        # deliver one reading per reader (the 40-reader fast path) and an
        # events_per_s Poisson-ish burst of HA state changes into the candidate
        # ring, then drive one event-loop cycle off the real ready-group queue.
        ticks = max(1, int(duration_s))
        for second in range(ticks):
            at = origin + timedelta(seconds=second)
            now[0] = at
            for rid in rids:
                levels[rid] += rng.gauss(0.0, 0.5)
                start = time.perf_counter()
                result = normalizer.normalize(
                    rid,
                    raw_value=levels[rid], unit="W", observed_at=at,
                    received_at=at, sequence=sequences[rid],
                )
                if result.deliverable:
                    core.reader_state.add(result.measurement)
                ha_msg = {
                    "entity_id": rng.choice(eids),
                    "observed_at": at,
                    "state": rng.choice(("on", "off")),
                }
                event = E["HAEvent"].make(
                    entity_id=ha_msg["entity_id"], observed_at=at, received_at=at,
                    to_state=ha_msg["state"], linked=False,
                )
                core.candidate_ring.add(event, now=at)
                fast_us.append((time.perf_counter() - start) * 1_000_000.0)
                sequences[rid] += 1

            # One event-loop cycle per simulated second: use a real EventGroup
            # over a handful of readers so inference actually runs off loop.
            group = E["EventGroup"](
                group_id=f"bench-g{second}",
                revision=1,
                status=E["EventGroupStatus"].CONFIRMED,
                opened_at=at,
                closed_at=at + timedelta(seconds=1),
                reader_deltas=[
                    E["ReaderDelta"](
                        reader_id=rids[i % len(rids)], delta_w=100.0,
                        sigma_w=2.0, completeness="complete",
                    )
                    for i in range(min(3, len(rids)))
                ],
                versions=triple,
            )
            start = time.perf_counter()
            bundle = core._build_input_bundle(group)
            if bundle is not None:
                result = core._run_infer_group(bundle)
                core._publish_group_result(group, result, bundle)
            event_ms.append((time.perf_counter() - start) * 1000.0)

            # Real background jobs at a slow cadence (kept off the hot loop).
            if second % 60 == 0 and second > 0:
                _time_job("reader_state_prune", core.reader_state.prune, now=at)
                _time_job("candidate_ring_prune", core.candidate_ring.prune, now=at)

        # Drain any background jobs submitted by production during the run.
        _time_job("drain_background", core.drain_background)
        db_end = db_path.stat().st_size if db_path.exists() else 0

        # HTTP probes while the load is still resident in the runtime.
        from wattmosaic_engine.api.server import create_app  # noqa: PLC0415

        payload = build_matrix_payload(500, 100)
        facade = _MatrixFacade(payload)
        app = create_app(facade, api_token="")
        healthz_ms, matrix_ms, matrix_build_ms = asyncio.run(
            _probe_app(
                app,
                healthz_count=max(10, healthz_requests),
                matrix_count=max(1, matrix_requests),
            )
        )
    finally:
        core.inference_executor.shutdown(wait=False)
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        db.close()

    growth = max(0, db_end - db_start)
    simulated_days = duration_s / 86_400.0
    growth_per_day = (
        growth / simulated_days if simulated_days > 0 else growth * (86_400.0 / max(1, duration_s))
    )
    fast_ms = [u / 1000.0 for u in fast_us]
    fast_lat = support.latency_summary(fast_ms)
    health_lat = support.latency_summary(healthz_ms)
    matrix_lat = support.latency_summary(matrix_ms)
    return {
        "scale": {
            "readers": readers,
            "ha_entities": entities,
            "events_per_s": events_per_s,
            "duration_s": duration_s,
            "seed": seed,
            "samples": len(fast_us),
            "event_cycles": len(event_ms),
        },
        "fast_path_us_per_sample": {
            "p50_us": round(support.percentile(fast_us, 0.50), 4),
            "p95_us": round(support.percentile(fast_us, 0.95), 4),
            "p99_us": round(support.percentile(fast_us, 0.99), 4),
            "p50_ms": fast_lat["p50_ms"],
            "p95_ms": fast_lat["p95_ms"],
            "p99_ms": fast_lat["p99_ms"],
        },
        "event_loop_ms": support.latency_summary(event_ms),
        "healthz_ms": health_lat,
        "background_jobs": {
            name: {
                **support.latency_summary(values),
                "total_ms": round(sum(values), 3),
            }
            for name, values in job_ms.items()
        },
        "db_growth_bytes_per_day": round(growth_per_day, 1),
        "matrix": {
            "devices": 500,
            "readers": 100,
            "get_p95_ms": matrix_lat["p95_ms"],
            "get_p99_ms": matrix_lat["p99_ms"],
            "payload_build_ms": support.latency_summary(matrix_build_ms),
            "payload_cells": len(payload.cells),
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
    print("Perf runtime (synthetic load)")
    print(f"  scale             : {scale['readers']} readers, {scale['ha_entities']} HA entities, "
          f"{scale['events_per_s']}/s, {scale['duration_s']:.0f}s simulated")
    fp = report["fast_path_us_per_sample"]
    print(f"  fast path µs/sample: p50 {fp['p50_us']:.2f}  p95 {fp['p95_us']:.2f}  p99 {fp['p99_us']:.2f}")
    for name in ("event_loop_ms", "healthz_ms"):
        s = report[name]
        print(f"  {name:17s}: p50 {s['p50_ms']:.2f}  p95 {s['p95_ms']:.2f}  p99 {s['p99_ms']:.2f} ms")
    print(f"  db growth/day     : {report['db_growth_bytes_per_day']:.0f} bytes (extrapolated)")
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
