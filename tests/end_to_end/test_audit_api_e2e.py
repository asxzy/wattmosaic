"""HTTP surface for the audit artifacts: additive, read-only, honest when empty.

Every route here answers from what an analysis pass already derived.  None of
them triggers analysis and none of them writes: a read endpoint that could start
work would make a UI refresh an unbounded operation on the event loop.

The payload of an *empty* database is asserted explicitly.  An endpoint that
invents a plausible-looking zero is worse than one that says "nothing analysed
yet": the first is indistinguishable from a real all-clear.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import aiohttp
import pytest

TOKEN = "audit-token"
TOKEN_HEADERS = {"X-API-Token": TOKEN}


async def _server(core, *, token: str = TOKEN):
    from wattmosaic_engine.api.server import APIServer

    server = APIServer(core, host="127.0.0.1", port=0, api_token=token)
    await server.start()
    return server


async def _get(session: aiohttp.ClientSession, server, path: str):
    url = f"http://127.0.0.1:{server.port}/api/v1{path}"
    async with session.get(url, headers=TOKEN_HEADERS) as resp:
        text = await resp.text()
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text
        return resp.status, body


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def core(tmp_db_path):
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore, load_inference_backend
    from wattmosaic_engine.storage.database import Database

    from tools.simulator import REAL_HOME_TOPOLOGY

    db = Database(tmp_db_path)
    topology = REAL_HOME_TOPOLOGY.to_contract_topology()
    db.save_topology(topology)
    config = EngineConfig(
        database_path=str(tmp_db_path), ha_token="not-used", api_token=TOKEN
    )
    engine = EngineCore(config, db)
    engine.topology_cache = db.load_topology()
    engine.inference_backend = load_inference_backend(config, db)
    assert engine.inference_backend is not None
    try:
        yield engine
    finally:
        db.close()


def test_circuits_is_empty_until_a_pass_has_evidence(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                status, body = await _get(session, server, "/circuits")
        finally:
            await server.stop()
        return status, body

    status, body = _run(scenario())
    assert status == 200
    # No pass has run, so there is no circuit -- not a fabricated empty circuit.
    assert body == []


def test_relations_reports_kinds_separately(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                status, body = await _get(session, server, "/relations")
                filtered_status, filtered = await _get(
                    session, server, "/relations?kind=device_ct_association"
                )
        finally:
            await server.stop()
        return (status, body), (filtered_status, filtered)

    (status, body), (filtered_status, filtered) = _run(scenario())
    assert status == 200
    assert "kinds" in body and "counts" in body
    assert "requires_confirmation" in body
    assert filtered_status == 200
    assert set(filtered["kinds"]) <= {"device_ct_association"}


def test_inference_records_route_is_append_only_and_oldest_first(core) -> None:

    from wattmosaic_engine.contracts import (
        InferenceResult,
        MeasurementBasis,
        Source,
    )
    from wattmosaic_engine.disaggregation.audit import AuditConfig, AuditPass

    device_id = core.topology_cache.devices[0].device_id
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = AuditPass(core.db, config=AuditConfig())
    for revision, power in enumerate((10.0, 900.0), start=1):
        moment = at + dt.timedelta(hours=revision)
        pass_.analyze(
            _window(moment),
            topology=core.topology_cache,
            results=[
                InferenceResult(
                    device_id=device_id,
                    timestamp=moment,
                    power_w=power,
                    source=Source.DIRECT_MEASURED,
                    confidence=0.7,
                    measurement_basis=MeasurementBasis.CT_MEASURED,
                )
            ],
            events=[],
            now=moment,
        )

    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                status, body = await _get(
                    session, server, f"/devices/{device_id}/records"
                )
        finally:
            await server.stop()
        return status, body

    status, body = _run(scenario())
    assert status == 200
    assert [row["revision"] for row in body] == [1, 2]
    assert body[1]["previous_record_id"] == body[0]["record_id"]
    assert body[0]["power_w"] == pytest.approx(10.0)


def test_inference_records_for_an_unknown_device_is_empty_not_an_error(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                return await _get(session, server, "/devices/no_such_device/records")
        finally:
            await server.stop()

    status, body = _run(scenario())
    assert status == 200
    assert body == []


def test_inference_records_rejects_a_bad_limit(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                return await _get(
                    session, server, "/devices/anything/records?limit=not-a-number"
                )
        finally:
            await server.stop()

    status, body = _run(scenario())
    assert status == 400
    assert body["error"] == "bad_request"


def test_residuals_are_signed_and_never_zeroed(core) -> None:
    from wattmosaic_engine.contracts import ResidualKind, ResidualPower

    core.db.insert_residual_power(
        ResidualPower(
            residual_w=-125.0,
            kind=ResidualKind.NEGATIVE,
            basis="ct_measured",
            quality_status="good",
        ),
        timestamp=dt.datetime.now(dt.UTC),
    )

    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                return await _get(session, server, "/residuals")
        finally:
            await server.stop()

    status, body = _run(scenario())
    assert status == 200
    assert body["count"] >= 1
    values = [row["residual_w"] for row in body["components"]]
    assert -125.0 in values, "a negative residual must survive storage and the API"


def test_isolation_status_says_no_pass_yet_rather_than_all_clear(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                return await _get(session, server, "/isolation")
        finally:
            await server.stop()

    status, body = _run(scenario())
    assert status == 200
    assert body["source"] in {"no_analysis_pass", "no_analysis_pass_yet"}
    assert body["counts"]["analysed"] == 0
    assert body["verdicts"] == []


def test_isolation_status_reflects_a_real_pass(core) -> None:
    from wattmosaic_engine.contracts import (
        InferenceResult,
        MeasurementBasis,
        Source,
    )

    device = core.topology_cache.devices[0]
    channel = core.topology_cache.channels[0]
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = core.inference_backend.audit
    pass_.analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=core.topology_cache,
        results=[
            InferenceResult(
                device_id=device.device_id,
                timestamp=at,
                power_w=50.0,
                source=Source.DIRECT_MEASURED,
                confidence=0.6,
                measurement_basis=MeasurementBasis.CT_MEASURED,
            )
        ],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
        ],
        now=at + dt.timedelta(minutes=5),
    )

    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                return await _get(session, server, "/isolation")
        finally:
            await server.stop()

    status, body = _run(scenario())
    assert status == 200
    assert body["source"] == "last_analysis_pass"
    assert body["counts"]["analysed"] == 1
    assert body["algorithm_version"]


def test_new_routes_preserve_the_contract_header(core) -> None:
    async def scenario():
        server = await _server(core)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{server.port}/api/v1/circuits"
                async with session.get(url, headers=TOKEN_HEADERS) as resp:
                    return dict(resp.headers)
        finally:
            await server.stop()

    headers = _run(scenario())
    assert "X-WattMosaic-Contract-Version" in headers


def _window(at: dt.datetime):
    from wattmosaic_engine.models.protocols import SimpleWindow

    return SimpleWindow(at=at, values={"ct_l1": 100.0})


def test_null_facade_still_answers_the_new_routes_with_503() -> None:
    from wattmosaic_engine.api.facade import NullFacade
    from wattmosaic_engine.api.server import APIServer

    async def scenario():
        server = APIServer(NullFacade("no engine"), host="127.0.0.1", port=0, api_token=TOKEN)
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                out = []
                for path in (
                    "/circuits",
                    "/relations",
                    "/residuals",
                    "/isolation",
                    "/devices/x/records",
                ):
                    url = f"http://127.0.0.1:{server.port}/api/v1{path}"
                    async with session.get(url, headers=TOKEN_HEADERS) as resp:
                        out.append((path, resp.status))
                return out
        finally:
            await server.stop()

    for path, status in _run(scenario()):
        assert status == 503, path
