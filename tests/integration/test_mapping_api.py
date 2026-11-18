"""Integration test for the derived Device<->Reader Mapping Matrix read path.

B3b of the completion sprint.  This exercises the real HTTP surface (a live
``APIServer`` on an ephemeral port) with an in-memory ``StubFacade`` — the same
transport pattern the sibling API tests use — so the route is proven to be
wired, authenticated and shaped like every other ``/api/v1`` read endpoint:

* ``GET /api/v1/mapping`` returns 200, the standard contract envelope and a
  matrix with cells plus a coverage block carrying honest, derived counts;
* an empty topology yields an empty matrix with zeroed counts (no fabrication);
* auth and error shape match the sibling routes (401 + ``ErrorResponse``).

The matrix is projected from current state on request; nothing here writes
storage or snapshots a sample.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from wattmosaic_engine.api.facade import StubFacade
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.contracts import CONTRACT_VERSION, ErrorResponse

TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}


async def _with_server(facade: Any, fn: Any, *, token: str = TOKEN) -> Any:
    server = APIServer(facade, host="127.0.0.1", port=0, api_token=token)
    await server.start()
    try:
        base = f"http://127.0.0.1:{server.port}/api/v1"
        async with aiohttp.ClientSession() as session:
            return await fn(session, base)
    finally:
        await server.stop()


def _empty_topology() -> dict[str, Any]:
    return {
        "version": 1,
        "status": "candidate",
        "channels": [],
        "devices": [],
    }


def test_mapping_route_returns_matrix_with_coverage_counts() -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/mapping", headers=HEADERS) as resp:
            assert resp.status == 200
            # Standard envelope: every response carries the contract header.
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
            body = await resp.json()
        assert isinstance(body, dict)
        assert "cells" in body
        assert "coverage" in body
        cells = body["cells"]
        assert isinstance(cells, list)
        assert len(cells) == 1
        cell = cells[0]
        assert cell["device_id"] == "stub_device"
        assert cell["reader_id"] == "ct_l1"
        assert "status" in cell
        assert "relation_type" in cell

        # Device-major axes are additive and present without breaking the cell envelope.
        assert body["devices"] == ["stub_device"]
        assert body["readers"] == ["ct_l1"]

        coverage = body["coverage"]
        assert isinstance(coverage, dict)
        counts = coverage["counts"]
        assert counts["cell_count"] == len(cells)
        assert counts["by_status"] == {"unknown": 1}
        assert counts["by_cardinality"] == {"one_to_one": 1}
        assert counts["unknown_load_count"] == 0

    asyncio.run(_with_server(StubFacade(), fn))


def test_mapping_route_surfaces_ha_registry_devices_as_rows() -> None:
    """A facade exposing HA registry data gets real devices on the row axis."""

    class RegistryFacade(StubFacade):
        def ha_device_registry(self) -> dict[str, Any]:
            return {
                "devices": [
                    {"id": "ha_kitchen", "name_by_user": "Kitchen light"},
                    {"id": "ha_washer", "name": "Washer"},
                    {"name": "no id"},  # skipped: no device id, so nothing fabricated
                ]
            }

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/mapping", headers=HEADERS) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert "ha_kitchen" in body["devices"]
        assert "ha_washer" in body["devices"]
        assert "stub_device" in body["devices"]
        assert body["readers"] == ["ct_l1"]
        # Cell envelope stays valid: existing stub cell is unchanged.
        assert any(c["device_id"] == "stub_device" for c in body["cells"])

    asyncio.run(_with_server(RegistryFacade(), fn))


def test_mapping_route_empty_state_is_honest() -> None:
    facade = StubFacade()
    facade.set_topology(_empty_topology())

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/mapping", headers=HEADERS) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["cells"] == []
        counts = body["coverage"]["counts"]
        assert counts["cell_count"] == 0
        assert counts["by_status"] == {}
        assert counts["by_cardinality"] == {}
        assert counts["unknown_load_count"] == 0

    asyncio.run(_with_server(facade, fn))


def test_mapping_route_auth_and_error_shape_match_siblings() -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        # Missing token is rejected with the same shaped error as other reads.
        async with session.get(f"{base}/mapping") as resp:
            assert resp.status == 401
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
            ErrorResponse.model_validate(await resp.json())
        # A wrong token is rejected identically.
        async with session.get(
            f"{base}/mapping", headers={"X-API-Token": "nope"}
        ) as resp:
            assert resp.status == 401
            ErrorResponse.model_validate(await resp.json())

    asyncio.run(_with_server(StubFacade(), fn))


def test_mapping_route_allows_anonymous_when_no_token_configured() -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/mapping") as resp:
            assert resp.status == 200
            body = await resp.json()
            assert "cells" in body
            assert "coverage" in body

    asyncio.run(_with_server(StubFacade(), fn, token=""))
