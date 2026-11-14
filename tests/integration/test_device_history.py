"""Regression: the device history read path returned ``[]`` when rows existed.

``EngineCore._device_history`` passed ``since=utcnow()`` to
``Database.inference_history``, whose SQL is ``WHERE timestamp >= ?``.  Every
stored row has a ``timestamp`` in the past, so the predicate matched nothing and
``GET /api/v1/devices/{device_id}`` advertised an empty history forever
(``docs/assessment_gap_analysis.md`` G17).

The tests below assert the *observable* behaviour of the HTTP payload, not the
private helper: a caller reads ``history`` off ``/devices/{device_id}``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import aiohttp
import pytest
from wattmosaic_engine.contracts import InferenceResult, Source, TopologyStatus
from wattmosaic_engine.storage.database import Database

TOKEN = "history-token"
HEADERS = {"X-API-Token": TOKEN}
#: Anchored to *now*, not a calendar literal: the endpoint filters with the
#: configured lookback (24 h by default), so a fixed date silently fell out of
#: the window the day after it was written and the history came back empty.
#: ``test_device_history_excludes_rows_older_than_the_lookback`` already worked
#: this way; this makes the rest of the file agree with it.
BASE = dt.datetime.now(dt.UTC).replace(microsecond=0)


def _core(tmp_path, *, history_lookback_s: float | None = None):
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore, load_inference_backend

    from tools.simulator import REAL_HOME_TOPOLOGY

    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=False).model_copy(
        update={"status": TopologyStatus.CONFIRMED}
    )
    path = tmp_path / "history.db"
    db = Database(path)
    db.save_topology(topology)
    kwargs: dict[str, object] = {}
    if history_lookback_s is not None:
        kwargs["history_lookback_s"] = history_lookback_s
    config = EngineConfig(
        database_path=str(path),
        ha_token="unused",
        api_token=TOKEN,
        **kwargs,
    )
    core = EngineCore(config, db)
    core.topology_cache = db.load_topology()
    core.inference_backend = load_inference_backend(config, db)
    return core, db


def _store_result(db, device_id: str, *, timestamp: dt.datetime, power_w: float) -> None:
    db.insert_inference_results(
        [
            InferenceResult(
                device_id=device_id,
                timestamp=timestamp,
                power_w=power_w,
                source=Source.DIRECT_MEASURED,
                confidence=0.8,
                supporting_ct_channels=["ct_main_l1"],
            )
        ]
    )


async def _get(server, path: str):
    url = f"http://127.0.0.1:{server.port}/api/v1{path}"
    async with aiohttp.ClientSession() as session, session.get(url, headers=HEADERS) as resp:
        return resp.status, json.loads(await resp.text())


def _serve(core, fn):
    from wattmosaic_engine.api.server import APIServer

    async def run():
        server = APIServer(core, host="127.0.0.1", port=0, api_token=TOKEN)
        await server.start()
        try:
            return await fn(server)
        finally:
            await server.stop()

    return asyncio.run(run())


def test_device_history_returns_rows_that_exist(tmp_path) -> None:
    """The defect: a stored result was invisible because ``since=now``."""
    core, db = _core(tmp_path)
    try:
        device_id = core.topology_cache.devices[0].device_id
        stored = BASE - dt.timedelta(minutes=5)
        _store_result(db, device_id, timestamp=stored, power_w=123.0)

        status, body = _serve(core, lambda server: _get(server, f"/devices/{device_id}"))

        assert status == 200
        history = body["history"]
        # Exactly the stored row: not empty, and not invented.
        assert history, "a stored inference result must appear in the device history"
        assert len(history) == 1
        assert history[0]["power_w"] == pytest.approx(123.0)
        assert history[0]["source"] == Source.DIRECT_MEASURED.value
        assert dt.datetime.fromisoformat(history[0]["timestamp"]) == stored
    finally:
        db.close()


def test_device_history_is_newest_first_and_bounded(tmp_path) -> None:
    core, db = _core(tmp_path)
    try:
        device_id = core.topology_cache.devices[0].device_id
        for index in range(5):
            _store_result(
                db,
                device_id,
                timestamp=BASE - dt.timedelta(minutes=10 * (index + 1)),
                power_w=float(100 + index),
            )

        status, body = _serve(core, lambda server: _get(server, f"/devices/{device_id}"))

        assert status == 200
        powers = [row["power_w"] for row in body["history"]]
        assert powers == [100.0, 101.0, 102.0, 103.0, 104.0]
    finally:
        db.close()


def test_device_history_excludes_rows_older_than_the_lookback(tmp_path) -> None:
    """The lookback is explicit and configurable, not "everything ever"."""
    core, db = _core(tmp_path, history_lookback_s=3600.0)
    try:
        device_id = core.topology_cache.devices[0].device_id
        recent = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        _store_result(db, device_id, timestamp=recent, power_w=50.0)
        _store_result(db, device_id, timestamp=old, power_w=999.0)

        status, body = _serve(core, lambda server: _get(server, f"/devices/{device_id}"))

        assert status == 200
        powers = [row["power_w"] for row in body["history"]]
        assert powers == [50.0]
    finally:
        db.close()


def test_history_lookback_default_is_one_day() -> None:
    from wattmosaic_engine.config.settings import EngineConfig

    assert EngineConfig().history_lookback_s == pytest.approx(86400.0)
