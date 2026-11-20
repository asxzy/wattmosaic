"""T5 discovery survives a restart, and evidence from the old rule is discarded.

* The ledger used to be seeded only from the active topology version, so every
  restart threw away accumulated discovery evidence.
* Stored inferred edges from before the corrected conflict rule carry wrong
  counts; migration 27 removes the unpinned, unversioned ones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import DeltaObservation
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import v027_reset_inferred_edges as v027

T0 = datetime(2026, 11, 20, 7, 0, 0, tzinfo=UTC)


def _edge(edge_id: str, parent: str, child: str, *, declared_by: str = "inference",
          pinned: bool = False, support: int = 5, causes: int = 2,
          lifecycle: EdgeLifecycleV3 = EdgeLifecycleV3.PROVISIONAL) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by=declared_by,
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, independent_causes=causes),
        valid_from=T0,
        pinned=pinned,
    )


def test_restart_resumes_inferred_edges_with_their_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "resume.db")
    db.repos.topology_v3.upsert_edge(_edge("te-inf::feed_l1::kitchen_ckt", "feed_l1", "kitchen_ckt"))
    core = EngineCore(EngineConfig(database_path=str(tmp_path / "resume.db"), ha_token="x"),
                      db, clock=lambda: T0)
    core._wire_v3_services()  # noqa: SLF001 - production start path
    view = core.topology_inference
    assert view is not None
    edge = view.edge("feed_l1", "kitchen_ckt")
    assert edge is not None and edge.evidence.support == 5

    # A new matching co-step adds to the resumed evidence rather than restarting it.
    view = view.observe_group(
        DeltaObservation(group_id="g-new", deltas={"feed_l1": (40.0, 2.0), "kitchen_ckt": (40.0, 2.0)},
                         at=T0, cause_id="cause-new")
    )
    edge = view.edge("feed_l1", "kitchen_ckt")
    assert edge is not None
    assert edge.evidence.support == 6
    assert edge.evidence.independent_causes == 3


def test_version_edges_keep_their_lifecycle_after_a_restart_and_a_fold(tmp_path: Path) -> None:
    """A CONFIRMED edge seeded from the active version must survive the next fold.

    Live regression: causes were restored only for edges outside the version, so
    the first fold after a restart recomputed every version edge with zero
    independent causes and demoted all CONFIRMED/PROVISIONAL edges to CANDIDATE.
    """
    db = Database(tmp_path / "resume2.db")
    edge = _edge("te-inf::feed_l1::kitchen_ckt", "feed_l1", "kitchen_ckt",
                 support=9, causes=3, lifecycle=EdgeLifecycleV3.CONFIRMED)
    db.repos.topology_v3.upsert_edge(edge)
    core = EngineCore(EngineConfig(database_path=str(tmp_path / "resume2.db"), ha_token="x"),
                      db, clock=lambda: T0)
    core._wire_v3_services()  # noqa: SLF001
    versions = core.version_service
    vid = versions.propose_topology([edge], None)
    versions.promote_topology(vid, "test: version holds the confirmed edge")
    core.topology_inference = None
    core._wire_self_healing()  # noqa: SLF001 - a restart re-seeds from the version
    view = core.topology_inference.observe_group(
        DeltaObservation(group_id="g-other", deltas={"garage_ckt": (30.0, 2.0)}, at=T0, cause_id="x")
    )
    resumed = view.edge("feed_l1", "kitchen_ckt")
    assert resumed is not None
    assert resumed.lifecycle == EdgeLifecycleV3.CONFIRMED
    assert resumed.evidence.independent_causes == 3


def test_migration_27_drops_only_unpinned_unversioned_inferred_edges(tmp_path: Path) -> None:
    db = Database(tmp_path / "m27.db")
    repo = db.repos.topology_v3
    repo.upsert_edge(_edge("stale", "feed_l1", "kitchen_ckt"))
    repo.upsert_edge(_edge("pinned", "feed_l1", "laundry_ckt", pinned=True))
    repo.upsert_edge(_edge("declared", "feed_l2", "garage_ckt", declared_by="user"))
    repo.upsert_edge(_edge("versioned", "feed_l2", "office_ckt"))
    conn = db._conn  # noqa: SLF001 - drive the migration body directly
    conn.execute(
        "UPDATE topology_versions_v3 SET edge_ids_json = ? WHERE status = 'active'",
        (json.dumps(["versioned"]),),
    )
    if conn.execute("SELECT count(*) FROM topology_versions_v3 WHERE status='active'").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO topology_versions_v3(version_id, number, status, edge_ids_json, created_at) "
            "VALUES('topo-test', 1, 'active', ?, ?)",
            (json.dumps(["versioned"]), T0.isoformat()),
        )
    v027._apply(conn)  # noqa: SLF001
    v027._apply(conn)  # noqa: SLF001 - idempotent
    remaining = {row[0] for row in conn.execute("SELECT edge_id FROM topology_edges")}
    assert remaining == {"pinned", "declared", "versioned"}
