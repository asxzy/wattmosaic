"""The panel's read routes on a real :class:`EngineCore` over a real database.

Regression for two 3.0 cutover defects that unit tests with injected facades
never saw, because no test drove the routes through the production core:

* ``GET /topology/v3`` answered 400 (``metadata_json  Extra inputs are not
  permitted``): ``core/topology_api._topology_graph`` built
  ``MeasurementEndpoint(**row)`` from raw S13 rows, and every stored endpoint
  carries the storage-only ``metadata_json`` column.
* ``GET /endpoints`` answered 500 (``'EngineCore' object has no attribute
  'endpoints'``): the endpoints facade existed only on ``DeviceService``.

The database is seeded the way a fresh install looks after discovery:
measurement endpoints (with metadata) and an empty topology.  Then every
parameter-free GET route registered by ``create_app`` is called, and none may
answer 5xx or an unexpected 4xx.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine.api.server import API_PREFIX, APIServer, create_app
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    EndpointConfigRevision,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}
ENDPOINT_IDS = ("ct_feed", "ct_sub", "ct_oven")

#: Parameter-free GET routes that may legitimately answer a 4xx on a fresh
#: install with endpoints but no inference yet, and the statuses they may use.
EXPECTED_4XX: dict[str, set[int]] = {
    # No v3 inference snapshot has been published yet on a core never started.
    "/snapshot": {404},
}


def _clock() -> datetime:
    return T0


def _seed_endpoints(db: Database) -> None:
    """CT endpoints as discovery stores them: with metadata and a revision."""
    for endpoint_id in ENDPOINT_IDS:
        endpoint = MeasurementEndpoint(
            endpoint_id=endpoint_id,
            kind="ct",
            entity_id=f"sensor.{endpoint_id}_power",
            display_name=endpoint_id.replace("_", " ").title(),
            scope="circuit",
            scope_status="candidate",
            enabled=True,
            created_at=T0,
        )
        row = endpoint.model_dump(mode="json")
        # Storage-only metadata: persisted as ``metadata_json`` on the row.
        row["metadata"] = {"source": "ha_discovery", "unit": "W"}
        db.repos.entities.upsert_endpoint(row)
        db.repos.entities.append_endpoint_revision(
            EndpointConfigRevision(
                endpoint_id=endpoint_id,
                revision=1,
                valid_from=T0,
                phase="unknown",
                polarity="normal",
                multiplier=1.0,
                calibration_version="discovery",
                measurement_type="instantaneous",
                declared_by="discovery",
            )
        )


@pytest.fixture
def core(tmp_path: Path) -> Any:
    db = Database(tmp_path / "panel.db")
    _seed_endpoints(db)
    config = EngineConfig(database_path=str(tmp_path / "panel.db"), ha_token="not-used")
    engine = EngineCore(config, db, clock=_clock)
    try:
        yield engine
    finally:
        db.close()


def _contains(edge_id: str, parent: str, child: str, lifecycle: EdgeLifecycleV3) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=False,
    )


def _promote(core: Any, edges: list[TopologyEdgeV3]) -> None:
    versions = core._v3_topology_service().versions
    versions.promote_topology(versions.propose_topology(edges, None), "test")


async def _with_server(core: Any, fn: Callable[[aiohttp.ClientSession, str], Awaitable[Any]]) -> Any:
    server = APIServer(core, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            return await fn(session, f"http://127.0.0.1:{server.port}{API_PREFIX}")
    finally:
        await server.stop()


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[int, Any]:
    async with session.get(url) as resp:
        body = await resp.json(content_type=None)
        return resp.status, body


def _tree_ids(nodes: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if node.get("reader_id"):
            out.add(node["reader_id"])
        stack.extend(node.get("children") or [])
    return out


# --------------------------------------------------------------------------- #
# /topology/v3
# --------------------------------------------------------------------------- #


def test_topology_v3_with_endpoint_metadata_and_no_edges(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _get(session, f"{base}/topology/v3")
        assert status == 200, body
        assert body["edges"] == []
        assert _tree_ids(body["primary_display_tree"]) == set(ENDPOINT_IDS)

    asyncio.run(_with_server(core, fn))


def test_topology_v3_with_endpoint_metadata_returns_edges_and_tree(core: Any) -> None:
    _promote(
        core,
        [
            _contains("e-sub", "ct_feed", "ct_sub", EdgeLifecycleV3.CONFIRMED),
            _contains("e-oven", "ct_sub", "ct_oven", EdgeLifecycleV3.PROVISIONAL),
        ],
    )

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _get(session, f"{base}/topology/v3")
        assert status == 200, body
        by_id = {edge["edge_id"]: edge for edge in body["edges"]}
        assert set(by_id) == {"e-sub", "e-oven"}
        assert by_id["e-oven"]["lifecycle"] == "provisional"
        tree = body["primary_display_tree"]
        assert _tree_ids(tree) == set(ENDPOINT_IDS)
        # The CONFIRMED CONTAINS edge nests ct_sub under ct_feed; the PROVISIONAL
        # one is a soft suggestion and leaves ct_oven unassigned.
        panels = [node for node in tree if node["kind"] == "panel"]
        assert len(panels) == 1
        assert _tree_ids([panels[0]]) == {"ct_feed", "ct_sub"}
        unassigned = [node for node in tree if node["kind"] == "unassigned"]
        assert _tree_ids(unassigned) == {"ct_oven"}

    asyncio.run(_with_server(core, fn))


def test_topology_v3_skips_an_invalid_endpoint_row(core: Any) -> None:
    """One malformed row is skipped; it must not turn the read into a 4xx."""
    with core.db.repos.entities._lock:
        core.db.repos.entities._conn.execute(
            "INSERT INTO measurement_endpoints(endpoint_id, kind, entity_id, scope, "
            "scope_status, scope_subject_id, enabled, created_at, metadata_json) "
            "VALUES('ct_bad', 'ct', 'sensor.bad', 'single_subject', 'candidate', "
            "'dev_x', 1, '2026-01-01T00:00:00+00:00', '{}')"
        )

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _get(session, f"{base}/topology/v3")
        assert status == 200, body
        assert _tree_ids(body["primary_display_tree"]) == set(ENDPOINT_IDS)
        status, body = await _get(session, f"{base}/endpoints")
        assert status == 200, body
        assert {e["endpoint_id"] for e in body["endpoints"]} == set(ENDPOINT_IDS)

    asyncio.run(_with_server(core, fn))


# --------------------------------------------------------------------------- #
# /endpoints
# --------------------------------------------------------------------------- #


def test_endpoints_list_and_detail_on_engine_core(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _get(session, f"{base}/endpoints")
        assert status == 200, body
        endpoints = body["endpoints"]
        assert [e["endpoint_id"] for e in endpoints] == sorted(ENDPOINT_IDS)
        for endpoint in endpoints:
            # Contract-shaped: validates, and no storage-only column leaks.
            MeasurementEndpoint.model_validate(endpoint)
            assert "metadata_json" not in endpoint
            assert endpoint["revisions"][0]["declared_by"] == "discovery"

        status, body = await _get(session, f"{base}/endpoints/ct_sub")
        assert status == 200, body
        assert MeasurementEndpoint.model_validate(body).endpoint_id == "ct_sub"

        status, body = await _get(session, f"{base}/endpoints/nope")
        assert status == 404, body

    asyncio.run(_with_server(core, fn))


# --------------------------------------------------------------------------- #
# Every parameter-free GET route
# --------------------------------------------------------------------------- #


def _parameter_free_get_paths() -> list[str]:
    paths: set[str] = set()
    for route in create_app().router.routes():
        if route.method != "GET":
            continue
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path")
        if path and path.startswith(API_PREFIX):
            paths.add(path[len(API_PREFIX):])
    return sorted(paths)


def test_route_inventory_is_not_empty() -> None:
    paths = _parameter_free_get_paths()
    assert {"/health", "/endpoints", "/topology/v3", "/topology/tree", "/matrix"} <= set(paths)


def test_every_parameter_free_get_route_answers_on_engine_core(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> dict[str, tuple[int, Any]]:
        results: dict[str, tuple[int, Any]] = {}
        for path in _parameter_free_get_paths():
            async with session.get(f"{base}{path}") as resp:
                text = await resp.text()
                results[path] = (resp.status, text[:300])
        return results

    results = asyncio.run(_with_server(core, fn))
    bad = {
        path: (status, text)
        for path, (status, text) in results.items()
        if status >= 500 or (status >= 400 and status not in EXPECTED_4XX.get(path, set()))
    }
    assert not bad, f"routes failing on a real EngineCore: {bad}"
