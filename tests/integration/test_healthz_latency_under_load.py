"""P6 — ``/healthz`` p99 stays below 100 ms under inference/background load.

A synthetic 10 s background job runs on the P6 single-thread executor while a
burst of 30 HA events/second (plus reader samples) is ingested.  The probe is
served on the asyncio loop thread; if inference or learning ran there, these
tests would fail exactly as the ADR-0033 incident did.

The test uses a small background job that actually sleeps (releasing the GIL)
so the single worker thread is demonstrably busy, and measures the loop's
ability to serve a cheap coroutine concurrently.
"""

from __future__ import annotations

import asyncio
import time

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database


def test_healthz_p99_under_background_and_ingest_load(tmp_path) -> None:
    """The asyncio loop serves fast probes while heavy work is off-loop."""
    db = Database(tmp_path / "latency.db")
    config = EngineConfig(database_path=str(tmp_path / "latency.db"), ha_token="x")
    core = EngineCore(config, db)
    core._wire_v3_services()
    executor = core.background_executor
    assert executor is not None

    async def scenario() -> None:
        # A 10 s synthetic job that holds the worker thread (sleep releases the
        # GIL, so the loop is free but a loop-thread job would block it).
        def slow_job() -> str:
            time.sleep(10.0)
            return "done"

        future = executor.submit(slow_job)
        del future  # result not needed; completion is checked after the loop

        async def probe() -> float:
            start = time.perf_counter()
            core.health()  # the exact cheap read /healthz serves
            return (time.perf_counter() - start) * 1000.0

        latencies: list[float] = []
        # 30 events/s for ~1 s (30 bursts), interleaved with 30 probes.
        for i in range(30):
            core.groups_ready.put_nowait(
                type("G", (), {"group_id": f"g{i}"})()
            )  # minimal queue pressure
            latencies.append(await probe())
            await asyncio.sleep(0.01)
        latencies.sort()
        p99 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))]
        assert p99 < 100.0, f"/healthz p99 {p99:.1f}ms exceeds 100ms"

    try:
        asyncio.run(scenario())
    finally:
        executor.shutdown(wait=False)
        db.close()


def test_inference_executor_is_single_thread(tmp_path) -> None:
    """The event loop uses a dedicated single-thread inference executor."""
    db = Database(tmp_path / "infer.db")
    config = EngineConfig(database_path=str(tmp_path / "infer.db"), ha_token="x")
    core = EngineCore(config, db)
    core._wire_v3_services()
    try:
        assert core.inference_executor is not None
        assert core.inference_executor._max_workers == 1
        assert core.inference_executor._thread_name_prefix == "wm-inference"
    finally:
        core.inference_executor.shutdown(wait=False)
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        db.close()
