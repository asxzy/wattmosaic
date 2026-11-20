"""Live dev-server findings (2026-11-20): the panel showed one branch, rest unassigned.

* A PROVISIONAL edge gave a CT a parent in the hierarchy index, hiding a whole
  CONFIRMED subtree in "Unassigned measurements".
* A CT with two CONFIRMED parents (a feed and its grandparent) kept the
  alphabetically first one, not the direct parent.
* Every inferred version carried the whole ledger (candidates, conflicted).
* Several panels' CTs share a channel name ("l2"), so the tree was unreadable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.contracts.entities import EndpointKind, EndpointScope, ScopeStatus
from wattmosaic_engine.electrical.topology_graph import TopologyGraph
from wattmosaic_engine.electrical.topology_inference import TopologyInferenceView

T0 = datetime(2026, 11, 20, tzinfo=UTC)


def _edge(source: str, target: str, lifecycle: EdgeLifecycleV3, *, support: int = 8,
          parent_only: int = 0) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{source}::{target}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by="inference",
        evidence=EdgeEvidence(support=support, parent_only=parent_only, independent_causes=2),
        valid_from=T0,
        pinned=False,
    )


def _endpoint(endpoint_id: str, name: str) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=endpoint_id, kind=EndpointKind.CT, entity_id=None, display_name=name,
        scope=EndpointScope.CIRCUIT, scope_status=ScopeStatus.CANDIDATE, enabled=True,
        created_at=T0,
    )


def _tree_readers(nodes: Any) -> dict[str, list[str]]:
    """parent reader -> child readers, from the display tree."""
    out: dict[str, list[str]] = {}

    def walk(node: Any) -> None:
        leaf = next((c.reader_id for c in node.children if c.reader_id and c.level == "L4"), None)
        for child in node.children:
            if child.level != "L4":
                child_leaf = next((c.reader_id for c in child.children if c.level == "L4"), None)
                out.setdefault(leaf, []).append(child_leaf)
                walk(child)

    for node in nodes:
        if node.kind != "unassigned":
            walk(node)
    return out


def test_a_provisional_parent_does_not_hide_a_confirmed_subtree() -> None:
    edges = [
        _edge("main_l1", "sub_l2", EdgeLifecycleV3.PROVISIONAL),
        _edge("sub_l2", "hrv", EdgeLifecycleV3.CONFIRMED),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    assert _tree_readers(graph.primary_display_tree()).get("sub_l2") == ["hrv"]


def test_the_direct_parent_wins_over_a_busier_grandparent_feed() -> None:
    # main_l2 also sees the plug (transitive) but steps on its own far more often.
    edges = [
        _edge("main_l2", "plug", EdgeLifecycleV3.CONFIRMED, support=9, parent_only=40),
        _edge("sub_l1", "plug", EdgeLifecycleV3.CONFIRMED, support=8, parent_only=3),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    assert graph.parent("plug") == "sub_l1"


def test_duplicate_channel_names_get_readable_unique_labels() -> None:
    endpoints = [
        _endpoint("refoss_main_panel_l2", "l2"),
        _endpoint("refoss_sub_panel_l2", "l2"),
        _endpoint("refoss_sub_panel_upper_floor_hrv", "upper floor hrv"),
    ]
    edges = [_edge("refoss_sub_panel_l2", "refoss_sub_panel_upper_floor_hrv", EdgeLifecycleV3.CONFIRMED)]
    graph = TopologyGraph.from_edges(edges, endpoints, T0)
    labels = {graph._label(e.endpoint_id) for e in endpoints}  # noqa: SLF001
    assert labels == {"Refoss main panel L2", "Refoss sub panel L2", "upper floor hrv"}


def test_the_lower_level_parent_is_the_direct_one_and_the_grandparent_is_demoted() -> None:
    from wattmosaic_engine.electrical.topology_inference import DeltaObservation

    edges = (
        _edge("main_l2", "plug", EdgeLifecycleV3.CONFIRMED, support=14, parent_only=21),
        _edge("sub_l1", "plug", EdgeLifecycleV3.CONFIRMED, support=19, parent_only=33),
    )
    causes = {
        ("main_l2", "plug", "contains"): frozenset({"a", "b"}),
        ("sub_l1", "plug", "contains"): frozenset({"a", "b"}),
    }
    view = TopologyInferenceView(edges=edges, clock=lambda: T0, causes_by_edge=causes)
    view = view.observe_group(
        DeltaObservation(group_id="g", deltas={"other": (40.0, 2.0)}, at=T0, cause_id="c"),
        levels={"main_l2": 3100.0, "sub_l1": 820.0, "plug": 60.0},
    )
    assert view.edge("sub_l1", "plug").lifecycle == EdgeLifecycleV3.CONFIRMED
    assert view.edge("main_l2", "plug").lifecycle == EdgeLifecycleV3.PROVISIONAL
    # Without levels nothing is decided.
    same = TopologyInferenceView(edges=edges, clock=lambda: T0, causes_by_edge=causes).observe_group(
        DeltaObservation(group_id="g2", deltas={"other": (40.0, 2.0)}, at=T0, cause_id="c2")
    )
    assert same.edge("main_l2", "plug").lifecycle == EdgeLifecycleV3.CONFIRMED


class _RecordingVersions:
    def __init__(self, active: set[str]) -> None:
        self.active = active
        self.proposed: list[list[str]] = []

    def current(self) -> Any:
        return type("T", (), {"topology_version_id": "topo-x"})()

    def topology_view(self, _version_id: str) -> Any:
        ids = self.active

        class _View:
            def edges(self) -> list[Any]:
                return [type("E", (), {"edge_id": i})() for i in ids]

        return _View()

    def propose_topology(self, edges: list[TopologyEdgeV3], _event: Any) -> str:
        self.proposed.append(sorted(e.edge_id for e in edges))
        self.active = {e.edge_id for e in edges}
        return f"topo-{len(self.proposed) + 1}"

    def promote_topology(self, _version_id: str, _reason: str) -> None:
        return None


def test_a_version_holds_only_structure_and_is_not_republished() -> None:
    service = _RecordingVersions(active=set())
    edges = (
        _edge("feed", "kitchen", EdgeLifecycleV3.CONFIRMED),
        _edge("feed", "laundry", EdgeLifecycleV3.CANDIDATE, support=1),
        _edge("kitchen", "feed", EdgeLifecycleV3.CONFLICTED),
        _edge("feed", "garage", EdgeLifecycleV3.PROVISIONAL, support=3),
    )
    view = TopologyInferenceView(edges=edges, clock=lambda: T0, versions=service)
    view = view._propose_version(["te-inf::feed::kitchen: provisional -> confirmed"])  # noqa: SLF001
    assert service.proposed == [["te-inf::feed::kitchen"]]
    view._propose_version(["again"])  # noqa: SLF001 - identical structure
    assert len(service.proposed) == 1
