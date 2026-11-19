"""D2: the endpoints HTTP surface (``/api/v1/endpoints``).

Exercises the real aiohttp transport with an injected facade over a real SQLite
database: discovery proposals, scope confirm/reject, append-only config
revisions and a display-name/enabled update.  Every change records a
configuration event; nothing is overwritten in place.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import aiohttp
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.contracts import (
    CONTRACT_VERSION,
    EndpointKind,
    EndpointScope,
    MeasurementEndpoint,
    ScopeStatus,
)
from wattmosaic_engine.devices.service import DeviceService
from wattmosaic_engine.storage import Database

TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}
T0 = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def _endpoint(endpoint_id: str, entity_id: str, subject: str | None) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=endpoint_id,
        kind=EndpointKind.SMART_PLUG,
        entity_id=entity_id,
        energy_entity_id=None,
        display_name="Washer plug",
        scope=EndpointScope.SINGLE_SUBJECT,
        scope_status=ScopeStatus.CANDIDATE,
        scope_subject_id=subject,
        panel_id=None,
        enabled=False,
        created_at=T0,
    )


async def _with_server(facade: Any, fn: Any) -> Any:
    server = APIServer(facade, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    try:
        base = f"http://127.0.0.1:{server.port}/api/v1"
        async with aiohttp.ClientSession() as session:
            return await fn(session, base)
    finally:
        await server.stop()


def test_endpoints_get_lists_discovered_endpoints(tmp_path):
    db = Database(tmp_path / "d2.db")
    service = DeviceService(db)
    service.upsert_endpoint(_endpoint("washer_plug", "sensor.washer_power", "pd_1"))
    service.upsert_endpoint(
        _endpoint("other_plug", "sensor.other_power", "pd_2").model_copy(
            update={"enabled": True}
        )
    )

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/endpoints", headers=HEADERS) as resp:
            assert resp.status == 200
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
            body = await resp.json()
        assert {e["endpoint_id"] for e in body["endpoints"]} == {
            "washer_plug",
            "other_plug",
        }

    asyncio.run(_with_server(service.as_facade(), fn))


def test_scope_confirm_records_revision_and_enables_endpoint(tmp_path):
    db = Database(tmp_path / "d2.db")
    service = DeviceService(db)
    service.upsert_endpoint(_endpoint("washer_plug", "sensor.washer_power", "pd_1"))

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/endpoints/washer_plug/scope",
            headers=HEADERS,
            json={"action": "confirm", "subject_id": "pd_1"},
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["endpoint"]["scope_status"] == "confirmed"
        assert body["endpoint"]["enabled"] is True
        assert body["endpoint"]["scope_subject_id"] == "pd_1"
        # Confirmation is append-only: the discovery revision is preserved and
        # a new user-declared revision is appended, plus one config event.
        revisions = body["endpoint"]["revisions"]
        assert len(revisions) == 2
        assert revisions[-1]["declared_by"] == "user"
        assert len(body["configuration_events"]) == 1

    asyncio.run(_with_server(service.as_facade(), fn))
    assert service.get_endpoint("washer_plug").scope_status == ScopeStatus.CONFIRMED


def test_scope_reject_disables_endpoint_and_records_event(tmp_path):
    db = Database(tmp_path / "d2.db")
    service = DeviceService(db)
    service.upsert_endpoint(_endpoint("washer_plug", "sensor.washer_power", "pd_1"))

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/endpoints/washer_plug/scope",
            headers=HEADERS,
            json={"action": "reject"},
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["endpoint"]["scope_status"] == "rejected"
        assert body["endpoint"]["enabled"] is False
        assert len(body["configuration_events"]) == 1

    asyncio.run(_with_server(service.as_facade(), fn))


def test_put_endpoint_updates_name_and_enabled(tmp_path):
    db = Database(tmp_path / "d2.db")
    service = DeviceService(db)
    service.upsert_endpoint(_endpoint("washer_plug", "sensor.washer_power", "pd_1"))

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.put(
            f"{base}/endpoints/washer_plug",
            headers=HEADERS,
            json={"display_name": "Laundry plug", "enabled": True},
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["endpoint"]["display_name"] == "Laundry plug"
        assert body["endpoint"]["enabled"] is True
        assert len(body["configuration_events"]) == 1

    asyncio.run(_with_server(service.as_facade(), fn))


def test_scope_rejects_unknown_action(tmp_path):
    db = Database(tmp_path / "d2.db")
    service = DeviceService(db)
    service.upsert_endpoint(_endpoint("washer_plug", "sensor.washer_power", "pd_1"))

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/endpoints/washer_plug/scope",
            headers=HEADERS,
            json={"action": "maybe"},
        ) as resp:
            assert resp.status == 400

    asyncio.run(_with_server(service.as_facade(), fn))
