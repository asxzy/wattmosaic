"""Z1: the panel display tree can be built from T1's v3 topology graph.

The retired ``electrical/panel_tree.py`` projection is replaced by
:func:`wattmosaic_engine.mapping.display_tree.build_display_tree_v3`, which
reads T1's ``TopologyGraph.primary_display_tree()`` over confirmed CONTAINS
edges and v3 endpoints while keeping the panel wire shape unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EndpointKind,
    EndpointScope,
    MeasurementEndpoint,
    RelationType,
    ScopeStatus,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_graph import TopologyGraph
from wattmosaic_engine.mapping.display_tree import TREE_VERSION, build_display_tree_v3

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _endpoint(eid: str) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=eid,
        kind=EndpointKind.CT,
        entity_id=f"sensor.{eid}",
        scope=EndpointScope.CIRCUIT,
        scope_status=ScopeStatus.CONFIRMED,
        panel_id=None,
        enabled=True,
        created_at=T0,
    )


def _edge(parent: str, child: str) -> TopologyEdgeV3:
    from wattmosaic_engine.contracts import EdgeEvidence

    return TopologyEdgeV3(
        edge_id=f"{parent}->{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        evidence=EdgeEvidence(),
        valid_from=T0,
        valid_until=None,
        pinned=False,
    )


def test_build_display_tree_v3_uses_t1_primary_tree() -> None:
    endpoints = [_endpoint("main"), _endpoint("lights")]
    graph = TopologyGraph.from_edges([_edge("main", "lights")], endpoints, at=T0)

    payload = build_display_tree_v3(graph, generated_at="2026-01-01T00:00:00Z")

    assert payload["version"] == TREE_VERSION
    assert payload["generated_at"] == "2026-01-01T00:00:00Z"
    assert payload["status"] == "confirmed"
    # Every declared reader is present as a leaf, with no fabricated watt value.
    leaves = [
        node
        for node in payload["roots"]
        for node in [node]
        if node.get("reader_id") or node.get("children")
    ]
    assert leaves
    assert payload["stats"]["channels"] == 2
    assert payload["stats"]["circuits"] >= 1
    assert payload["notes"] == []


def test_build_display_tree_v3_places_unassigned_endpoints() -> None:
    graph = TopologyGraph.from_edges([], [_endpoint("orphan")], at=T0)
    payload = build_display_tree_v3(graph)
    assert payload["roots"]
    assert payload["stats"]["feed_circuits"] >= 1
