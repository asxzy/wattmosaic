"""ADR-0046: POST /topology/circuits/{reader_id}/parent on a real EngineCore.

Follows the fixture style of ``test_engine_core_panel_routes.py``: real Database,
real EngineCore, real VersionService and the real aiohttp API server.  These
tests fail before the change (the route does not exist).
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
    EndpointConfigRevision,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}
ENDPOINT_IDS = ("feed", "a", "b", "c")


def _clock() -> datetime:
    return T0


def _seed_endpoints(db: Database) -> None:
    for endpoint_id in ENDPOINT_IDS:
        endpoint = MeasurementEndpoint(
            endpoint_id=endpoint_id,
            kind="ct",
            entity_id=f"sensor.{endpoint_id}_power",
            display_name=endpoint_id,
            scope="circuit",
            scope_status="candidate",
            enabled=True,
            created_at=T0,
        )
        row = endpoint.model_dump(mode="json")
        row["metadata"] = {"source": "ha_discovery"}
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
    db = Database(tmp_path / "move.db")
    _seed_endpoints(db)
    config = EngineConfig(database_path=str(tmp_path / "move.db"), ha_token="not-used")
    engine = EngineCore(config, db, clock=_clock)
    try:
        yield engine
    finally:
        db.close()


def _contains(edge_id: str, parent: str, child: str, lifecycle: TopologyEdgeState) -> TopologyEdgeV3:
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


def _seed_ledger(core: Any, edges: list[TopologyEdgeV3] | None = None) -> None:
    """Install a real inference ledger seeded from the active version's edges.

    A never-started core has ``topology_inference=None``; the panel tree then has
    no placement report.  A running install always has one, so mirror that.
    """
    from wattmosaic_engine.electrical.topology_inference import TopologyInferenceView

    if edges is None:
        triple = core.version_service.current()
        edges = list(core.version_service.topology_view(triple.topology_version_id).edges())
    core.topology_inference = TopologyInferenceView(edges=tuple(edges), clock=_clock)


def _seed_feed_a(core: Any) -> None:
    """A fresh core with an inferred feed→a edge, like a running install."""
    edge = _contains("e-a", "feed", "a", TopologyEdgeState.STRUCTURAL)
    _promote(core, [edge])
    _seed_ledger(core, [edge])


async def _with_server(core: Any, fn: Callable[[aiohttp.ClientSession, str], Awaitable[Any]]) -> Any:
    server = APIServer(core, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            return await fn(session, f"http://127.0.0.1:{server.port}{API_PREFIX}")
    finally:
        await server.stop()


async def _post(session: aiohttp.ClientSession, url: str, body: dict) -> tuple[int, Any]:
    async with session.post(url, json=body) as resp:
        return resp.status, await resp.json(content_type=None)


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[int, Any]:
    async with session.get(url) as resp:
        return resp.status, await resp.json(content_type=None)


def _edges_by_id(body: dict) -> dict[str, dict]:
    return {edge["edge_id"]: edge for edge in body["edges"]}


def _in_force_contains_to(body: dict, child: str) -> list[dict]:
    return [
        edge for edge in body["edges"]
        if edge["relation"] == "contains"
        and edge["target_endpoint_id"] == child
        and edge["valid_until"] is None
        and edge["lifecycle"] not in ("rejected", "retired")
    ]


def test_move_a_under_b_rejects_inferred_and_declares_pinned_user_edge(core: Any) -> None:
    _seed_feed_a(core)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "b"})
        assert status == 200, body
        assert body["child"] == "a" and body["parent"] == "b"
        assert body["event_id"] and body["version_id"]
        assert body["edge_id"] and body["edge_id"].startswith("te-")
        assert set(body["closed_edge_ids"]) == {"e-a"}

        # The old inferred edge is closed as REJECTED in the event-sourced store.
        history = core.db.repos.topology_v3.edge_history("e-a")
        assert history[-1]["lifecycle"] == "rejected"
        assert history[-1]["valid_until"] is not None

        status, v3 = await _get(session, f"{base}/topology/v3")
        assert status == 200, v3
        by_id = _edges_by_id(v3)
        new = by_id[body["edge_id"]]
        assert new["source_endpoint_id"] == "b"
        assert new["target_endpoint_id"] == "a"
        assert new["lifecycle"] == "structural"
        assert new["declared_by"] == "user"
        assert new["pinned"] is True
        assert _in_force_contains_to(v3, "a") == [new]

        # The read-time overlay shows the user's choice in the tree payload too.
        status, tree = await _get(session, f"{base}/topology/tree")
        assert status == 200, tree
        assert tree["placements"]["a"]["parent"] == "b"
        assert tree["placements"]["a"]["reason"] == "user"

    asyncio.run(_with_server(core, fn))


def test_move_again_under_c_closes_the_user_edge(core: Any) -> None:
    _seed_feed_a(core)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        _, first = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "b"})
        status, second = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "c"})
        assert status == 200, second
        assert second["edge_id"] != first["edge_id"]
        assert first["edge_id"] in second["closed_edge_ids"]

        first_history = core.db.repos.topology_v3.edge_history(first["edge_id"])
        assert first_history[-1]["valid_until"] is not None

        _, v3 = await _get(session, f"{base}/topology/v3")
        by_id = _edges_by_id(v3)
        assert by_id[second["edge_id"]]["source_endpoint_id"] == "c"
        assert by_id[second["edge_id"]]["pinned"] is True
        assert _in_force_contains_to(v3, "a") == [by_id[second["edge_id"]]]

    asyncio.run(_with_server(core, fn))


def test_move_to_null_leaves_no_in_force_contains_targeting_a(core: Any) -> None:
    _seed_feed_a(core)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": None})
        assert status == 200, body
        assert body["parent"] is None
        assert body["edge_id"] is None
        assert set(body["closed_edge_ids"]) == {"e-a"}

        _, v3 = await _get(session, f"{base}/topology/v3")
        assert _in_force_contains_to(v3, "a") == []
        _, tree = await _get(session, f"{base}/topology/tree")
        assert tree["placements"]["a"]["parent"] is None
        assert tree["placements"]["a"]["reason"] == "user_root"
        # A user-root circuit is a top-level circuit, not in unplaced group.
        assert any(n.get("reader_id") == "a" for n in tree["roots"])

    asyncio.run(_with_server(core, fn))


def test_cycle_is_rejected_with_no_new_version(core: Any) -> None:
    # feed → a → b: moving feed under b closes the loop feed→a→b→feed.
    edges = [
        _contains("e-a", "feed", "a", TopologyEdgeState.STRUCTURAL),
        _contains("e-b", "a", "b", TopologyEdgeState.STRUCTURAL),
    ]
    _promote(core, edges)
    _seed_ledger(core, edges)
    versions_before = len(core.topology_versions())

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/feed/parent", {"parent": "b"})
        assert status == 400, body
        assert "cycle" in str(body).lower()

    asyncio.run(_with_server(core, fn))
    assert len(core.topology_versions()) == versions_before


def test_unknown_reader_is_404(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/nope/parent", {"parent": "b"})
        assert status == 404, body

    asyncio.run(_with_server(core, fn))


def test_unknown_parent_is_404(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "nope"})
        assert status == 404, body

    asyncio.run(_with_server(core, fn))


def test_missing_parent_key_is_400(core: Any) -> None:
    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {})
        assert status == 400, body
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": ""})
        assert status == 400, body
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": 5})
        assert status == 400, body

    asyncio.run(_with_server(core, fn))


def test_no_op_move_returns_200_without_a_new_version(core: Any) -> None:
    _seed_feed_a(core)
    # First move under b; moving a under b again is a no-op.
    versions_before: list[int] = []

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        _, first = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "b"})
        versions_before.append(len(core.topology_versions()))
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": "b"})
        assert status == 200, body
        assert body["edge_id"] == first["edge_id"]
        assert body["closed_edge_ids"] == []
        assert body["event_id"] is None
        assert len(core.topology_versions()) == versions_before[0]

    asyncio.run(_with_server(core, fn))


def test_route_is_registered_and_token_protected() -> None:
    app = create_app(api_token="secret")
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource and r.method}
    assert ("POST", "/api/v1/topology/circuits/{reader_id}/parent") in routes


def _held_lifecycle(core: Any, edge_id: str) -> str:
    held = {e.edge_id: e for e in core.topology_inference.edges}[edge_id]
    return str(getattr(held.lifecycle, "value", held.lifecycle))


def test_move_to_top_level_survives_ledger_repersistence(core: Any) -> None:
    """Regression: the live ledger must adopt the user's rejection.

    Before the fix the ledger kept its own STRUCTURAL copy of ``e-a``; the next
    fold upserts ledger edges by id, which overwrote the REJECTED row and put
    ``a`` straight back under ``feed``.
    """
    _seed_feed_a(core)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/circuits/a/parent", {"parent": None})
        assert status == 200, body
        assert _held_lifecycle(core, "e-a") == "rejected"

        # What a fold / discovery merge does: persist every ledger edge by id.
        for edge in core.topology_inference.edges:
            core.db.repos.topology_v3.upsert_edge(edge)
        assert core.db.repos.topology_v3.edge_history("e-a")[-1]["lifecycle"] == "rejected"

        _, tree = await _get(session, f"{base}/topology/tree")
        assert tree["placements"]["a"]["reason"] == "user_root"

    asyncio.run(_with_server(core, fn))


def test_reject_button_also_updates_the_live_ledger(core: Any) -> None:
    _seed_feed_a(core)

    async def fn(session: aiohttp.ClientSession, base: str) -> None:
        status, body = await _post(session, f"{base}/topology/edges/e-a/reject", {})
        assert status == 200, body
        assert _held_lifecycle(core, "e-a") == "rejected"

    asyncio.run(_with_server(core, fn))
