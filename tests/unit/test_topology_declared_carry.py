"""A T5 promotion must keep the user's declarations in the new topology version.

Regression (real-history replay, 2026-11-20): the ledger proposed a version
from its own edges only.  User-declared PAIRED_LEG / SAME_CONDUCTOR edges live
in the active version, not in the ledger, so the first inferred promotion
dropped every declaration and the fold lost its declared-pair/alias
constraints (P4) for the rest of the run.  Names are arbitrary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(
    relation: RelationType,
    source: str,
    target: str,
    lifecycle: TopologyEdgeState,
    declared_by: str,
    *,
    support: int = 0,
) -> TopologyEdgeV3:
    prefix = "decl" if declared_by == "user" else "te-inf"
    return TopologyEdgeV3(
        edge_id=f"{prefix}::{relation.value}::{source}::{target}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by=declared_by,
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, independent_causes=2 if support else 0),
        valid_from=T0,
        pinned=False,
    )


class _Versions:
    def __init__(self, active: list[TopologyEdgeV3]) -> None:
        self.active = list(active)
        self.proposed: list[list[TopologyEdgeV3]] = []

    def current(self) -> Any:
        return type("T", (), {"topology_version_id": f"topo-{len(self.proposed)}"})()

    def topology_view(self, _version_id: str) -> Any:
        edges = list(self.active)

        class _View:
            def edges(self) -> list[TopologyEdgeV3]:
                return edges

        return _View()

    def propose_topology(self, edges: list[TopologyEdgeV3], _event: Any) -> str:
        self.proposed.append(list(edges))
        self.active = list(edges)
        return f"topo-{len(self.proposed)}"

    def promote_topology(self, _version_id: str, _reason: str) -> None:
        return None


def test_inferred_promotion_keeps_declared_pairs_and_aliases() -> None:
    declared = [
        _edge(RelationType.PAIRED_LEG, "leg_a", "leg_b", TopologyEdgeState.STRUCTURAL, "user"),
        _edge(RelationType.SAME_CONDUCTOR, "alias_r", "feed", TopologyEdgeState.STRUCTURAL, "user"),
    ]
    service = _Versions(declared)
    inferred = _edge(
        RelationType.CONTAINS, "feed", "load", TopologyEdgeState.STRUCTURAL, "inference", support=20
    )
    view = TopologyInferenceView(edges=(inferred,), clock=lambda: T0, versions=service)
    view._propose_version(["te-inf::feed::load: provisional -> structural"])  # noqa: SLF001
    assert len(service.proposed) == 1
    ids = {edge.edge_id for edge in service.proposed[0]}
    assert inferred.edge_id in ids
    assert {edge.edge_id for edge in declared} <= ids


def test_inferred_edges_of_the_old_version_are_not_carried() -> None:
    """Only declarations are carried; a demoted inferred edge leaves the version."""
    stale = _edge(
        RelationType.CONTAINS, "feed", "old", TopologyEdgeState.STRUCTURAL, "inference", support=20
    )
    declared = _edge(
        RelationType.PAIRED_LEG, "leg_a", "leg_b", TopologyEdgeState.STRUCTURAL, "user"
    )
    service = _Versions([stale, declared])
    demoted = stale.model_copy(update={"lifecycle": TopologyEdgeState.PROVISIONAL})
    view = TopologyInferenceView(edges=(demoted,), clock=lambda: T0, versions=service)
    view._propose_version(["te-inf::feed::old: structural -> provisional"])  # noqa: SLF001
    ids = {edge.edge_id for edge in service.proposed[-1]}
    assert declared.edge_id in ids
    assert stale.edge_id not in ids
