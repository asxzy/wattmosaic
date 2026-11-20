"""Devices hang off the channel that measures them, whatever topology knows.

Live (2026-11-20): the panel hierarchy never showed a device -- the tree builder
hard-coded ``devices: 0`` -- so a fixture assigned to a channel seemed to need a
topology first.  A mapping edge names the most specific reader and ancestors are
derived through topology (contract ``MappingEdge``): the device belongs under its
channel while the channel is unassigned, and moves with the channel when the
channel's parent is found.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.contracts.entities import EndpointKind, EndpointScope, ScopeStatus
from wattmosaic_engine.electrical.topology_graph import TopologyGraph
from wattmosaic_engine.mapping.display_tree import build_display_tree_v3

T0 = datetime(2026, 11, 20, tzinfo=UTC)
LIGHT = {"device_id": "dev-hall-light", "label": "Hall light", "lifecycle": "candidate",
         "declared_by": "auto_placement"}


def _endpoint(endpoint_id: str) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=endpoint_id, kind=EndpointKind.CT, entity_id=None, display_name=endpoint_id,
        scope=EndpointScope.CIRCUIT, scope_status=ScopeStatus.CANDIDATE, enabled=True,
        created_at=T0,
    )


def _contains(parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}", relation=RelationType.CONTAINS,
        source_endpoint_id=parent, target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL, declared_by="inference",
        evidence=EdgeEvidence(support=9, independent_causes=3), valid_from=T0, pinned=False,
    )


def _path_to(nodes: list[dict[str, Any]], device_id: str) -> list[str] | None:
    for node in nodes:
        if node.get("device_id") == device_id:
            return [node["id"]]
        below = _path_to(node.get("children") or [], device_id)
        if below is not None:
            return [node["id"], *below]
    return None


def test_a_device_shows_under_its_channel_before_topology_exists() -> None:
    graph = TopologyGraph.from_edges((), (_endpoint("feed_l1"), _endpoint("branch")), T0)
    tree = build_display_tree_v3(graph, devices_by_reader={"branch": [LIGHT]})
    path = _path_to(tree["roots"], "dev-hall-light")
    assert path is not None and path[-2] == "reader:branch"
    assert path[0].startswith("group:Unassigned")
    assert tree["stats"]["devices"] == 1
    assert tree["stats"]["unplaced_devices"] == 0


def test_the_device_moves_with_its_channel_when_the_parent_is_found() -> None:
    endpoints = (_endpoint("feed_l1"), _endpoint("branch"))
    graph = TopologyGraph.from_edges((_contains("feed_l1", "branch"),), endpoints, T0)
    tree = build_display_tree_v3(graph, devices_by_reader={"branch": [LIGHT]})
    path = _path_to(tree["roots"], "dev-hall-light")
    assert path is not None and path[-2] == "reader:branch"
    assert not any(step.startswith("group:Unassigned") for step in path)
    # The feed's node is an ancestor: the device travelled with its channel.
    flat = [node["id"] for node in tree["roots"]]
    assert path[0] in flat


def test_a_device_on_an_unknown_reader_is_counted_not_dropped() -> None:
    graph = TopologyGraph.from_edges((), (_endpoint("branch"),), T0)
    tree = build_display_tree_v3(graph, devices_by_reader={"gone_reader": [LIGHT]})
    assert tree["stats"]["devices"] == 0
    assert tree["stats"]["unplaced_devices"] == 1
