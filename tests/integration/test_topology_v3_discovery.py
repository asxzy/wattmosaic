"""TV — discovered topology edges are visible to the panel.

Regression for the live-dev defect: T5's ledger writes inferred CONTAINS edges
to ``topology_edges`` at lifecycle CANDIDATE/PROVISIONAL with
``declared_by='inference'``, but a version's membership is
``topology_versions_v3.edge_ids_json``.  GET /topology/v3 previously returned
only the active version's edges (0 suggestions), so the panel never saw a
PROVISIONAL edge and could not offer Accept/Pin/Reject, even though the view
renders those controls.

These tests drive a real EngineCore over a real migrated SQLite database:
- a stored PROVISIONAL inferred edge outside the version is listed with
  ``in_version: false``;
- version edges are unchanged and marked ``in_version: true``;
- a CANDIDATE is hidden unless ``lifecycles`` contains ``candidate``;
- the discovery summary counts both and exposes the thresholds;
- the read is pure (no edge/version rows are written by the read).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import EdgeEvidence, RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _inferred(
    edge_id: str,
    parent: str,
    child: str,
    lifecycle: TopologyEdgeState,
    *,
    support: int = 0,
    conflict: int = 0,
    independent_causes: int = 0,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(
            support=support,
            conflict=conflict,
            independent_causes=independent_causes,
        ),
        valid_from=T0,
        pinned=False,
    )


@pytest.fixture()
def core(tmp_path: Path):
    db = Database(tmp_path / "discovery.db")
    engine = EngineCore(
        EngineConfig(database_path=str(tmp_path / "discovery.db"), ha_token="x"),
        db,
        clock=lambda: T0,
    )
    try:
        yield engine
    finally:
        db.close()


def _promote(engine: EngineCore, edges: list[TopologyEdgeV3]) -> str:
    versions = engine._v3_topology_service().versions
    vid = versions.propose_topology(edges, None)
    versions.promote_topology(vid, "seed")
    return vid


def _version_edge(edge_id: str, parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=False,
    )


def test_inferred_edges_outside_version_are_suggestions(core: EngineCore) -> None:
    repo = core.db.repos.topology_v3
    # One declared edge in the active version, and two inferred edges stored
    # outside it (the live-ledger behaviour).
    version_edge = _version_edge("v1", "ct_main", "ct_a")
    provisional = _inferred(
        "inf-prov", "ct_main", "ct_b", TopologyEdgeState.PROVISIONAL,
        support=4, conflict=0, independent_causes=2,
    )
    candidate = _inferred(
        "inf-cand", "ct_main", "ct_c", TopologyEdgeState.CANDIDATE,
        support=1, conflict=0, independent_causes=1,
    )
    repo.upsert_edge(version_edge)
    repo.upsert_edge(provisional)
    repo.upsert_edge(candidate)
    vid = _promote(core, [version_edge])

    payload = core.topology_v3()
    by_id = {e["edge_id"]: e for e in payload["edges"]}
    # Version edges are unchanged and marked as in the version.
    assert by_id["v1"]["in_version"] is True
    assert by_id["v1"]["lifecycle"] == "structural"
    # The PROVISIONAL suggestion appears, marked as not in the version.
    assert by_id["inf-prov"]["in_version"] is False
    assert by_id["inf-prov"]["lifecycle"] == "provisional"
    assert by_id["inf-prov"]["source_endpoint_id"] == "ct_main"
    assert by_id["inf-prov"]["target_endpoint_id"] == "ct_b"
    # The single-event CANDIDATE stays hidden by default.
    assert "inf-cand" not in by_id
    assert payload["version_id"] == vid

    discovery = payload["discovery"]
    assert discovery["counts"] == {
        "candidate": 1, "provisional": 1, "structural": 0, "conflicted": 0,
    }
    # E6 §2 / ADR-0045: calibrated thresholds (3 and 2, down from 8 and 3).
    assert discovery["thresholds"]["MIN_SUPPORT_PROVISIONAL"] == 2
    assert discovery["thresholds"]["MIN_INDEPENDENT_CAUSES"] == 2
    assert discovery["thresholds"]["MIN_SUPPORT_STRUCTURAL"] == 3
    top_pairs = discovery["top_pairs"]
    assert len(top_pairs) == 2
    assert top_pairs[0]["edge_id"] == "inf-prov"
    assert top_pairs[0]["support"] == 4
    assert top_pairs[0]["conflict"] == 0
    assert top_pairs[0]["independent_causes"] == 2
    assert top_pairs[0]["needed"]["structural"]["support"] == 0  # E6: 3, support 4
    cand = next(p for p in top_pairs if p["edge_id"] == "inf-cand")
    assert cand["needed"]["provisional"]["support"] == 1  # E6: 2, support 1
    assert cand["needed"]["provisional"]["independent_causes"] == 1


def test_candidate_visible_only_when_lifecycles_requested(core: EngineCore) -> None:
    repo = core.db.repos.topology_v3
    version_edge = _version_edge("v1", "ct_main", "ct_a")
    candidate = _inferred(
        "inf-cand", "ct_main", "ct_c", TopologyEdgeState.CANDIDATE,
        support=1, independent_causes=1,
    )
    provisional = _inferred(
        "inf-prov", "ct_main", "ct_b", TopologyEdgeState.PROVISIONAL,
        support=3, independent_causes=2,
    )
    repo.upsert_edge(version_edge)
    repo.upsert_edge(candidate)
    repo.upsert_edge(provisional)
    _promote(core, [version_edge])

    hidden = core.topology_v3()
    assert "inf-cand" not in {e["edge_id"] for e in hidden["edges"]}
    # The filter is uniform across version edges and suggestions: asking for
    # 'candidate' opts the candidate suggestion in and narrows everything else.
    shown = core.topology_v3(lifecycles=["candidate"])
    by_id = {e["edge_id"]: e for e in shown["edges"]}
    assert by_id["inf-cand"]["in_version"] is False
    assert "inf-prov" not in by_id
    # Requesting both lifecycles shows both suggestions.
    both = core.topology_v3(lifecycles=["candidate", "provisional"])
    by_id_both = {e["edge_id"]: e for e in both["edges"]}
    assert "inf-cand" in by_id_both and "inf-prov" in by_id_both
    # Summary still counts both regardless of the filter.
    assert shown["discovery"]["counts"]["candidate"] == 1


def test_read_is_pure(core: EngineCore) -> None:
    repo = core.db.repos.topology_v3
    version_edge = _version_edge("v1", "ct_main", "ct_a")
    provisional = _inferred(
        "inf-prov", "ct_main", "ct_b", TopologyEdgeState.PROVISIONAL,
        support=3, independent_causes=2,
    )
    repo.upsert_edge(version_edge)
    repo.upsert_edge(provisional)
    _promote(core, [version_edge])

    before_edges = repo.edges()
    before_versions = repo.versions()
    core.topology_v3()
    assert repo.edges() == before_edges
    assert repo.versions() == before_versions


def test_version_edges_lifecycle_filter_unchanged(core: EngineCore) -> None:
    """The caller's lifecycles filter still narrows the active version's edges."""
    repo = core.db.repos.topology_v3
    confirmed = _version_edge("v1", "ct_main", "ct_a")
    provisional_in_version = _inferred(
        "v2", "ct_main", "ct_b", TopologyEdgeState.PROVISIONAL, support=3,
    )
    repo.upsert_edge(confirmed)
    repo.upsert_edge(provisional_in_version)
    _promote(core, [confirmed, provisional_in_version])

    payload = core.topology_v3(lifecycles=["structural"])
    version_edges = [e for e in payload["edges"] if e["in_version"]]
    assert [e["edge_id"] for e in version_edges] == ["v1"]
