"""D1 — version lifecycle service (rule 20, HB §14, interfaces.md §7).

Acceptance:
- A plug relocation bumps only the mapping version; a reader move bumps both.
- ``as_of`` an old ``t`` returns the old triple.
- Rollback appends a new version with ``rollback_of`` metadata and never
  reactivates rows in place.
- Two proposals in flight: the second promote fails (optimistic parent check).
- ``mark_suspected`` sets lifecycles to SUSPECTED_CHANGE and appends edge history.
- Views are immutable.

The service is built on the real S14/S15 repositories against a genuine migrated
SQLite database; no wall clock is read by production code (a ``clock`` callable
is injected).  Production code never imports ``runtime.fakes``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    ConfigurationEvent,
    EdgeEvidence,
    EdgeLifecycleV3,
    MappingEdge,
    Observability,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.runtime.interfaces import (
    MappingView,
    TopologyView,
)
from wattmosaic_engine.runtime.interfaces import (
    VersionService as VersionServiceProtocol,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.versioning import VersionService
from wattmosaic_engine.versioning.service import VersionConflictError

T0 = datetime(2026, 1, 1, tzinfo=UTC)

MSID = "mset-stub"


class Clock:
    """An injected, manually advanced clock (no wall-clock reads)."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "versions.db")


def topo_edge(edge_id: str, parent: str, child: str, *, at: datetime = T0) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=at,
        pinned=True,
    )


def map_edge(
    edge_id: str,
    subject: str,
    endpoint: str,
    *,
    lifecycle: EdgeLifecycleV3 = EdgeLifecycleV3.CONFIRMED,
    at: datetime = T0,
) -> MappingEdge:
    return MappingEdge(
        mapping_edge_id=edge_id,
        subject_kind="device",
        subject_id=subject,
        endpoint_id=endpoint,
        observability=Observability.DIRECT,
        lifecycle=lifecycle,
        declared_by="migration",
        evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=at,
    )


def event(event_id: str, *, at: datetime = T0) -> ConfigurationEvent:
    return ConfigurationEvent(
        event_id=event_id,
        kind="smart_plug_relocation",
        detection="declared",
        status="confirmed",
        affected_endpoint_ids=["ct_a", "ct_c"],
        affected_edge_ids=["te-1"],
        affected_mapping_edge_ids=["me-1"],
        created_at=at,
    )


def make_service(db: Database, clock: Clock) -> VersionService:
    return VersionService(
        topology_repo=db.repos.topology_v3,
        mapping_repo=db.repos.mapping,
        clock=clock,
        model_set_id=MSID,
    )


# --------------------------------------------------------------------------- #
# Protocol conformance / current triple.
# --------------------------------------------------------------------------- #


def test_re_detected_configuration_event_is_recorded_once(tmp_path: Path) -> None:
    """Live: a self-healing detector re-detected a reader removal after a
    promotion and the repeated insert raised IntegrityError (event ids are
    derived from what was detected).  Re-recording is a no-op, also after a
    restart."""
    db = make_db(tmp_path)
    try:
        svc = make_service(db, Clock())
        ev = event("evt-inferred-reader_removal-kitchen_ckt")
        assert svc.record_configuration_event(ev) == ev.event_id
        assert svc.record_configuration_event(ev) == ev.event_id
        restarted = make_service(db, Clock())
        assert restarted.record_configuration_event(ev) == ev.event_id
        stored = [e for e in db.repos.topology_v3.events() if e["event_id"] == ev.event_id]
        assert len(stored) == 1
        # mark_suspected appends its own history event; re-marking is a no-op.
        restarted.mark_suspected(ev.event_id, [], [])
        restarted.mark_suspected(ev.event_id, [], [])
        history = [e for e in db.repos.topology_v3.events() if e["event_id"] == f"{ev.event_id}:suspected"]
        assert len(history) == 1
        # The repository itself is idempotent for every caller.
        db.repos.topology_v3.append_configuration_event(ev)
    finally:
        db.close()


def test_service_satisfies_runtime_protocol(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        svc = make_service(db, Clock())
        assert isinstance(svc, VersionServiceProtocol)
    finally:
        db.close()


def test_current_assembles_independent_triple(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    try:
        svc = make_service(db, Clock())
        triple = svc.current()
        assert isinstance(triple, VersionTriple)
        assert triple.topology_version_id == "topo-1"
        assert triple.mapping_version_id == "map-1"
        assert triple.model_set_id == MSID
        assert svc.topology_version_id() == "topo-1"
        assert svc.mapping_version_id() == "map-1"
        assert svc.model_version_id() == MSID
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Plug relocation: mapping bumps, topology/model unchanged. Reader move: both.
# --------------------------------------------------------------------------- #


def test_plug_relocation_bumps_mapping_not_topology_or_model(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        before = svc.current()

        ev = svc.record_configuration_event(event("evt-plug"))
        svc.mark_suspected(ev, ["te-1"], ["me-1"])

        new = map_edge("me-2", "tv", "ct_c", at=clock())
        map_vid = svc.propose_mapping([new], ev)
        svc.promote_mapping(map_vid, "plug moved to ct_c")

        after = svc.current()
        assert after.mapping_version_id == "map-2"
        assert after.topology_version_id == before.topology_version_id
        assert after.model_set_id == before.model_set_id
    finally:
        db.close()


def test_reader_move_bumps_topology_and_mapping(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        before = svc.current()

        ev = svc.record_configuration_event(event("evt-move"))
        topo_vid = svc.propose_topology([topo_edge("te-2", "main", "ct_c")], ev)
        svc.promote_topology(topo_vid, "reader moved")
        map_vid = svc.propose_mapping([map_edge("me-2", "tv", "ct_c")], ev)
        svc.promote_mapping(map_vid, "reader moved")

        after = svc.current()
        assert after.topology_version_id != before.topology_version_id
        assert after.mapping_version_id != before.mapping_version_id
        assert after.model_set_id == before.model_set_id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# as_of.
# --------------------------------------------------------------------------- #


def test_as_of_old_t_returns_old_triple(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        old = svc.current()
        old_t = clock()

        clock.advance(days=1)
        ev = svc.record_configuration_event(event("evt-later", at=clock()))
        map_vid = svc.propose_mapping([map_edge("me-2", "tv", "ct_c", at=clock())], ev)
        svc.promote_mapping(map_vid, "relocated")

        assert svc.current().mapping_version_id == "map-2"
        assert svc.as_of(old_t) == old
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Rollback is append-only.
# --------------------------------------------------------------------------- #


def test_rollback_appends_new_version_and_never_reactivates_rows(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        v1 = svc.current().topology_version_id

        clock.advance(hours=1)
        ev = svc.record_configuration_event(event("evt-new", at=clock()))
        v2 = svc.propose_topology([topo_edge("te-2", "main", "ct_c", at=clock())], ev)
        svc.promote_topology(v2, "promote candidate")
        assert svc.current().topology_version_id == v2

        clock.advance(hours=1)
        rb = svc.rollback("topology", v1)
        assert rb != v1
        assert svc.current().topology_version_id == rb
        assert svc.topology_version_id() == rb

        versions = {v["version_id"]: v for v in db.repos.topology_v3.versions()}
        assert versions[rb]["status"] == "active"
        assert versions[v2]["status"] == "superseded"
        assert versions[v1]["status"] == "superseded"
        assert versions[rb]["parent_version_id"] == v2
        # The rollback row is a new version with rollback_of metadata, and the
        # target's edge set (not the superseded candidates) is copied.
        assert versions[rb]["edge_ids_json"] == versions[v1]["edge_ids_json"]
        assert versions[rb]["metadata"]["rollback_of"] == v1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# mark_suspected.
# --------------------------------------------------------------------------- #


def test_mark_suspected_sets_lifecycle_and_appends_edge_history(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        repo = db.repos.topology_v3
        edge = topo_edge("te-1", "main", "ct_a")
        repo.upsert_edge(edge)
        # Store a real mapping edge through a proposal + promotion so
        # mark_suspected can flip its lifecycle in the active version.
        map_vid = svc.propose_mapping([map_edge("me-1", "tv", "ct_a")], None)
        svc.promote_mapping(map_vid, "seed")

        clock.advance(minutes=5)
        svc.mark_suspected("evt-suspect", ["te-1"], ["me-1"])

        # The current topology edge row's lifecycle is flipped (retained).
        history = repo.edge_history("te-1")
        assert history[-1]["lifecycle"] == EdgeLifecycleV3.SUSPECTED_CHANGE.value
        # The mapping edge is likewise flipped in place.
        map_rows = db.repos.mapping.mapping_edges(svc.current().mapping_version_id)
        assert {r["mapping_edge_id"]: r["lifecycle"] for r in map_rows}["me-1"] == (
            EdgeLifecycleV3.SUSPECTED_CHANGE.value
        )
        # An append-only history row (a SUSPECTED ConfigurationEvent) records
        # the change, listing the affected edges — the original event is intact.
        events = [e for e in repo.events() if e["event_id"] == "evt-suspect:suspected"]
        assert len(events) == 1
        assert events[0]["status"] == "suspected"
        assert "te-1" in events[0]["affected_edge_ids"]
        assert "me-1" in events[0]["affected_mapping_edge_ids"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Concurrency: two proposals in flight, second promote fails.
# --------------------------------------------------------------------------- #


def test_second_promote_with_stale_parent_fails(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        ev = svc.record_configuration_event(event("evt-concurrent"))

        va = svc.propose_topology([topo_edge("te-a", "main", "ct_a")], ev)
        vb = svc.propose_topology([topo_edge("te-b", "main", "ct_b")], ev)

        svc.promote_topology(va, "first wins")
        with pytest.raises(VersionConflictError):
            svc.promote_topology(vb, "second loses")
        assert svc.current().topology_version_id == va
        assert db.repos.topology_v3.active_version()["version_id"] == va
    finally:
        db.close()


def test_mapping_second_promote_with_stale_parent_fails(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        ev = svc.record_configuration_event(event("evt-concurrent-map"))

        ma = svc.propose_mapping([map_edge("me-a", "tv", "ct_a")], ev)
        mb = svc.propose_mapping([map_edge("me-b", "fridge", "ct_b")], ev)

        svc.promote_mapping(ma, "first wins")
        with pytest.raises(VersionConflictError):
            svc.promote_mapping(mb, "second loses")
        assert svc.current().mapping_version_id == ma
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Views are immutable.
# --------------------------------------------------------------------------- #


def test_views_are_immutable_and_cached_per_version(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    clock = Clock()
    try:
        svc = make_service(db, clock)
        repo = db.repos.topology_v3
        repo.upsert_edge(topo_edge("te-1", "main", "ct_a"))
        svc.propose_topology([topo_edge("te-1", "main", "ct_a")], None)

        # Views are cached per version id: same object on repeated calls.
        v1 = svc.current().topology_version_id
        view1 = svc.topology_view(v1)
        assert isinstance(view1, TopologyView)
        assert svc.topology_view(v1) is view1

        with pytest.raises(AttributeError):
            view1.edges_all += ()  # type: ignore[attr-defined]

        map_view = svc.mapping_view(svc.current().mapping_version_id)
        assert isinstance(map_view, MappingView)
        assert map_view.most_specific_reader(("device", "none")) is None
    finally:
        db.close()
