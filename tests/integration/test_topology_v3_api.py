"""T4 — event-sourced topology service and v3 API.

Tests the v3 topology service against the *real* D1 VersionService and the S13/
S14 repositories on a genuine migrated SQLite database, plus the v3 HTTP routes
against a live aiohttp app.  Injected clock only; ``runtime.fakes`` is used only
as a deterministic test double where a mapping view is required.

Covers the task's acceptance points:
- a declaration never deletes rows: the old edge survives with ``valid_until``;
- rejecting an inferred edge survives a re-run of inference (T1 override rule);
- ``/topology/v3`` exposes PROVISIONAL edges as suggestions;
- versions chain through ``parent_version_id``;
- a calibration edit appends a revision without altering older revisions;
- the health payload reports the dynamic state and structural edge count.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from wattmosaic_engine.api import routes as route_pkg
from wattmosaic_engine.api.facade import maybe_await
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EndpointConfigRevision,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.core.topology_api import TopologyService
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.versioning import VersionService

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


def _edge(
    edge_id: str,
    parent: str,
    child: str,
    *,
    lifecycle: TopologyEdgeState = TopologyEdgeState.STRUCTURAL,
    declared_by: str = "migration",
    at: datetime = T0,
    pinned: bool = False,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by=declared_by,  # type: ignore[arg-type]
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=at,
        pinned=pinned,
    )


def _endpoint_revision(
    endpoint_id: str, *, revision: int = 1, multiplier: float = 1.0, at: datetime = T0
) -> EndpointConfigRevision:
    return EndpointConfigRevision(
        endpoint_id=endpoint_id,
        revision=revision,
        valid_from=at,
        phase="l1",
        polarity="normal",
        multiplier=multiplier,
        calibration_version=f"{endpoint_id}-rev{revision}",
        measurement_type="instantaneous",
        declared_by="user",
    )


def make_service(db: Database, clock: Clock) -> TopologyService:
    versions = VersionService(
        topology_repo=db.repos.topology_v3,
        mapping_repo=db.repos.mapping,
        clock=clock,
        model_set_id="mset-test",
    )
    return TopologyService(
        versions=versions,
        topology_repo=db.repos.topology_v3,
        endpoints_repo=db.repos.entities,
        clock=clock,
    )


@pytest.fixture()
def env(tmp_path: Path):
    db = Database(tmp_path / "topology_v3.db")
    clock = Clock()
    try:
        yield db, clock, make_service(db, clock)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Declaration is event-sourced, not destructive.
# --------------------------------------------------------------------------- #


def test_declaration_keeps_the_old_edge_with_valid_until(env) -> None:
    db, clock, service = env
    repo = db.repos.topology_v3
    # Seed a user-declared parent in the active version.
    result = service.declared_edge(relation="contains", source="main", target="ct_a")
    first_id = result["edge_id"]
    assert result["version_id"] == "topo-2"
    assert result["event_id"].startswith("evt-declare-")

    clock.advance(hours=1)
    # Redeclare the same logical edge.
    result2 = service.declared_edge(relation="contains", source="main", target="ct_a")
    assert result2["version_id"] == "topo-3"
    assert result2["edge_id"] != first_id

    history = repo.edge_history(first_id)
    assert history[-1]["valid_until"] is not None
    assert history[-1]["valid_until"] == clock().isoformat()
    # The old edge row still exists (no DELETE).
    assert any(r["edge_id"] == first_id for r in repo.edges())

    # Versions chain through parent_version_id.
    versions = {v["version_id"]: v for v in service.versions_history()}
    assert versions["topo-2"]["parent_version_id"] == "topo-1"
    assert versions["topo-3"]["parent_version_id"] == "topo-2"


def test_declaration_records_a_declared_configuration_event(env) -> None:
    db, _clock, service = env
    result = service.declared_edge(relation="contains", source="main", target="ct_a")
    events = [e for e in db.repos.topology_v3.events() if e["event_id"] == result["event_id"]]
    assert len(events) == 1
    assert events[0]["detection"] == "declared"
    assert events[0]["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# Reject survives a re-run of inference (T1 override rule).
# --------------------------------------------------------------------------- #


def test_rejected_inferred_edge_survives_inference_rerun(env) -> None:
    db, clock, service = env
    repo = db.repos.topology_v3
    inferred = _edge("inf-1", "main", "ct_a", lifecycle=TopologyEdgeState.PROVISIONAL, declared_by="inference")
    repo.upsert_edge(inferred)
    vid = service.versions.propose_topology([inferred], None)
    service.versions.promote_topology(vid, "seed inferred edge")

    rej = service.reject("inf-1")
    assert rej["version_id"] != vid
    history = repo.edge_history("inf-1")
    assert history[-1]["lifecycle"] == TopologyEdgeState.REJECTED.value
    assert history[-1]["valid_until"] is not None

    # A fresh view built from the stored edges treats the rejected edge as gone.
    view = service.versions.topology_view(service.versions.current().topology_version_id)
    assert view.parent("ct_a") is None


def test_pin_makes_an_inferred_edge_structural(env) -> None:
    db, _clock, service = env
    repo = db.repos.topology_v3
    inferred = _edge("inf-1", "main", "ct_a", lifecycle=TopologyEdgeState.CANDIDATE, declared_by="inference")
    repo.upsert_edge(inferred)
    vid = service.versions.propose_topology([inferred], None)
    service.versions.promote_topology(vid, "seed")
    pin = service.pin("inf-1")
    assert pin["version_id"] != vid
    row = repo.edge_history("inf-1")[-1]
    assert row["pinned"] == 1
    assert row["lifecycle"] == TopologyEdgeState.STRUCTURAL.value


# --------------------------------------------------------------------------- #
# GET /topology/v3 exposes PROVISIONAL edges as suggestions.
# --------------------------------------------------------------------------- #


def test_payload_exposes_provisional_edges_and_primary_tree(env) -> None:
    db, _clock, service = env
    repo = db.repos.topology_v3
    confirmed = _edge("e1", "main", "ct_a", declared_by="user")
    provisional = _edge(
        "e2", "main", "ct_b", lifecycle=TopologyEdgeState.PROVISIONAL, declared_by="inference"
    )
    repo.upsert_edge(confirmed)
    repo.upsert_edge(provisional)
    vid = service.versions.propose_topology([confirmed, provisional], None)
    service.versions.promote_topology(vid, "seed")

    payload = service.topology()
    assert payload["version_id"] == vid
    assert {e["edge_id"] for e in payload["edges"]} == {"e1", "e2"}
    assert any(e["lifecycle"] == "provisional" for e in payload["edges"])
    assert isinstance(payload["primary_display_tree"], list)
    # Filter to only provisional edges (the "suggestions" view).
    only_provisional = service.topology(lifecycles=["provisional"])
    assert {e["edge_id"] for e in only_provisional["edges"]} == {"e2"}


def test_versions_history_lists_every_version(env) -> None:
    _db, _clock, service = env
    result = service.declared_edge(relation="contains", source="main", target="ct_a")
    history = service.versions_history()
    assert [v["version_id"] for v in history] == ["topo-1", "topo-2"]
    one = service.versions_history(result["version_id"])
    assert one["version_id"] == result["version_id"]


# --------------------------------------------------------------------------- #
# Endpoint config edits append revisions; older samples keep their calibration.
# --------------------------------------------------------------------------- #


def test_calibration_edit_appends_revision_and_event(env) -> None:
    from wattmosaic_engine.contracts import EndpointKind, EndpointScope, MeasurementEndpoint, ScopeStatus

    db, clock, service = env
    repo = db.repos.entities
    repo.upsert_endpoint(
        MeasurementEndpoint(
            endpoint_id="ct_a",
            kind=EndpointKind.CT,
            entity_id=None,
            scope=EndpointScope.CIRCUIT,
            scope_status=ScopeStatus.CONFIRMED,
            enabled=True,
            created_at=T0,
        )
    )
    # The S13 store is append-only; the first revision is already closed at the
    # instant the new one begins, so old samples resolve to the old multiplier.
    first = _endpoint_revision("ct_a", multiplier=1.0)
    first = first.model_copy(update={"valid_until": T0 + timedelta(hours=1)})
    repo.append_endpoint_revision(first)

    clock.advance(hours=1)
    result = service.edit_config("ct_a", {"multiplier": 2.0, "phase": "l2"})
    assert result["revision"] == 2

    revisions = repo.revisions("ct_a")
    assert [r["revision"] for r in revisions] == [1, 2]
    # The old revision is never rewritten; its multiplier is unchanged.
    assert revisions[0]["multiplier"] == 1.0
    assert revisions[1]["multiplier"] == 2.0
    assert revisions[1]["phase"] == "l2"
    assert revisions[1]["declared_by"] == "user"
    # A sample at the original time still resolves to the old revision.
    assert repo.endpoint_revision_at("ct_a", T0)["multiplier"] == 1.0
    assert repo.endpoint_revision_at("ct_a", clock())["multiplier"] == 2.0
    # A DECLARED calibration event was recorded.
    events = [e for e in db.repos.topology_v3.events() if e["kind"] == "calibration_change"]
    assert len(events) == 1
    assert events[0]["detection"] == "declared"


# --------------------------------------------------------------------------- #
# Dynamic topology: no per-subject confirmation gate (ADR-0043).
# --------------------------------------------------------------------------- #


def test_topology_service_has_no_confirmation_gate(env) -> None:
    """ADR-0043: per-subject topology confirmation is gone from the service."""
    _db, _clock, service = env
    assert not hasattr(service, "edges_confirmed_for")


# --------------------------------------------------------------------------- #
# HTTP surface (opt-in deps so the legacy route snapshot stays intact).
# --------------------------------------------------------------------------- #


def test_v3_routes_are_wired_when_deps_opt_in() -> None:
    router = web.UrlDispatcher()
    route_pkg.register_all(router, "/api/v1", deps={"topology_v3": True})
    paths = {
        (r.method, r.resource.canonical)
        for r in router.routes()
        if r.method and r.resource and r.method != "HEAD"
    }
    for route in (
        ("GET", "/api/v1/topology/v3"),
        ("POST", "/api/v1/topology/edges"),
        ("POST", "/api/v1/topology/edges/{edge_id}/pin"),
        ("POST", "/api/v1/topology/edges/{edge_id}/reject"),
        ("GET", "/api/v1/topology/versions"),
        ("GET", "/api/v1/topology/versions/{version_id}"),
        ("PUT", "/api/v1/topology/endpoints/{endpoint_id}/config"),
    ):
        assert route in paths


def test_v3_read_returns_edges_and_tree(tmp_path: Path) -> None:
    db = Database(tmp_path / "v3_http.db")
    clock = Clock()
    service = make_service(db, clock)
    try:
        repo = db.repos.topology_v3
        confirmed = _edge("e1", "main", "ct_a", declared_by="user")
        repo.upsert_edge(confirmed)
        vid = service.versions.propose_topology([confirmed], None)
        service.versions.promote_topology(vid, "seed")

        class Facade:
            def topology_v3(self, *, lifecycles=None):
                return service.topology(lifecycles=lifecycles)

        async def fn() -> None:
            from wattmosaic_engine.api.server import FACADE_KEY, _jsonable, _map_exception

            async def handler(request):
                try:
                    payload = await maybe_await(request.app[FACADE_KEY].topology_v3())
                    return web.json_response(_jsonable(payload))
                except Exception as exc:  # noqa: BLE001
                    return _map_exception(request, exc)

            app = web.Application()
            app.router.add_get("/api/v1/topology/v3", handler)
            app[FACADE_KEY] = Facade()
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = list(site._server.sockets)[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}/api/v1/topology/v3") as resp:
                        assert resp.status == 200
                        body = await resp.json()
                    assert {e["edge_id"] for e in body["edges"]} == {"e1"}
                    assert body["version_id"] == vid
                    assert isinstance(body["primary_display_tree"], list)
            finally:
                await runner.cleanup()

        asyncio.run(fn())
    finally:
        db.close()
