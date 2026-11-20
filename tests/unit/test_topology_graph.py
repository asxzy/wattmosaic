"""T1: typed topology graph built from lifecycle-filtered edges (design §5).

Edges are the authority.  Only CONFIRMED CONTAINS edges are hard structure;
PROVISIONAL edges are soft alternatives; CANDIDATE / CORRELATED edges never
shape the hierarchy.  This module is pure: it takes an injected ``at`` and
never touches a clock or a database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.contracts.entities import (
    EndpointKind,
    EndpointScope,
    ScopeStatus,
)
from wattmosaic_engine.electrical.topology_graph import TopologyGraph
from wattmosaic_engine.mapping import build_columns

from tests.support.fakes import HB17_TREE, FakeTopology

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(
    relation: RelationType,
    source: str,
    target: str,
    *,
    lifecycle: TopologyEdgeState = TopologyEdgeState.STRUCTURAL,
    declared_by: str = "user",
    pinned: bool = False,
    at: datetime = T0,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"e::{source}->{target}::{relation.value}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by=declared_by,  # type: ignore[arg-type]
        evidence=EdgeEvidence(),
        valid_from=at,
        pinned=pinned,
    )


def _endpoint(
    endpoint_id: str,
    *,
    display_name: str | None = None,
    panel_id: str | None = None,
    scope: EndpointScope = EndpointScope.CIRCUIT,
) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=endpoint_id,
        kind=EndpointKind.CT,
        entity_id=None,
        display_name=display_name or endpoint_id,
        scope=scope,
        scope_status=ScopeStatus.CONFIRMED,
        enabled=True,
        panel_id=panel_id,
        created_at=T0,
    )


# --------------------------------------------------------------------------- #
# Lifecycle filtering (objective): edges are the authority.
# --------------------------------------------------------------------------- #
def test_correlated_edge_never_yields_children() -> None:
    edges = [
        _edge(RelationType.CONTAINS, "main", "kitchen"),
        _edge(RelationType.CORRELATED, "kitchen", "fridge"),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    assert graph.children("kitchen") == ()
    assert "fridge" not in graph.ancestors("kitchen")
    # CORRELATED is still visible in the raw edge set (all lifecycles).
    assert any(e.relation == RelationType.CORRELATED for e in graph.edges())


def test_provisional_contains_excluded_from_ancestors_and_is_soft() -> None:
    edges = [
        _edge(RelationType.CONTAINS, "main", "subpanel"),
        _edge(
            RelationType.CONTAINS,
            "subpanel",
            "kitchen",
            lifecycle=TopologyEdgeState.PROVISIONAL,
        ),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    # The PROVISIONAL edge is not applied by default: kitchen has no hard
    # parent, and is therefore not a descendant of subpanel or main.
    assert graph.parent("kitchen") is None
    assert graph.ancestors("kitchen") == ()
    # The soft edge is not applied, but is listed as an alternative.
    soft = graph.soft_alternatives()
    assert len(soft) == 1
    assert soft[0].source_endpoint_id == "subpanel"
    assert soft[0].target_endpoint_id == "kitchen"
    assert soft[0].lifecycle == TopologyEdgeState.PROVISIONAL
    # Explicitly asking for PROVISIONAL does expose it as a parent.
    assert graph.parent("kitchen", (TopologyEdgeState.PROVISIONAL,)) == "subpanel"


def test_candidate_and_correlated_are_never_structural() -> None:
    edges = [
        _edge(
            RelationType.CONTAINS,
            "main",
            "a",
            lifecycle=TopologyEdgeState.CANDIDATE,
        ),
        _edge(RelationType.CORRELATED, "main", "b"),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    assert graph.children("main") == ()
    assert graph.soft_alternatives() == ()


# --------------------------------------------------------------------------- #
# Validation: cycles, self edges, conflicting parents (hierarchy.py §validate).
# --------------------------------------------------------------------------- #
def test_self_edge_is_rejected() -> None:
    # A CONTAINS edge cannot connect a reader to itself (contract validator).
    with pytest.raises(ValueError):
        _edge(RelationType.CONTAINS, "main", "main")


def test_cycle_is_rejected() -> None:
    edges = [
        _edge(RelationType.CONTAINS, "a", "b"),
        _edge(RelationType.CONTAINS, "b", "c"),
        _edge(RelationType.CONTAINS, "c", "a"),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    # No reader has a CONFIRMED parent after a cycle is removed.
    assert graph.parent("b") is None
    assert graph.parent("c") is None
    assert graph.parent("a") is None
    assert any("cycle" in p for p in graph.problems)


def test_two_confirmed_parents_move_weaker_edge_to_conflicted() -> None:
    strong = _edge(RelationType.CONTAINS, "main", "kitchen")
    weak = _edge(
        RelationType.CONTAINS,
        "garage",
        "kitchen",
        lifecycle=TopologyEdgeState.PROVISIONAL,
        declared_by="inference",
    )
    graph = TopologyGraph.from_edges([strong, weak], (), T0)
    # One CONFIRMED parent wins; the weaker edge is not applied.
    assert graph.parent("kitchen") == "main"
    assert [e.edge_id for e in graph.edges()] == [strong.edge_id]
    assert weak.edge_id in graph.moved_to_conflicted


def test_pin_overrides_inference_for_same_pair() -> None:
    inferred = _edge(
        RelationType.CONTAINS,
        "main",
        "kitchen",
        lifecycle=TopologyEdgeState.PROVISIONAL,
        declared_by="inference",
    )
    pinned = _edge(
        RelationType.CONTAINS,
        "main",
        "kitchen",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        pinned=True,
    )
    # Different edge ids, same (parent, child) pair.
    pinned = pinned.model_copy(update={"edge_id": "e::pinned"})
    graph = TopologyGraph.from_edges([inferred, pinned], (), T0)
    assert graph.parent("kitchen") == "main"
    assert graph.ancestors("kitchen") == ("main",)


def test_rejected_user_edge_overrides_inference_for_same_pair() -> None:
    inferred = _edge(
        RelationType.CONTAINS,
        "main",
        "kitchen",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
    )
    rejected = _edge(
        RelationType.CONTAINS,
        "main",
        "kitchen",
        lifecycle=TopologyEdgeState.REJECTED,
        declared_by="user",
    ).model_copy(update={"edge_id": "e::rejected"})
    graph = TopologyGraph.from_edges([inferred, rejected], (), T0)
    assert graph.parent("kitchen") is None
    assert graph.edges() == ()


# --------------------------------------------------------------------------- #
# PAIRED_LEG ownership (port test_electrical.py:1312).
# --------------------------------------------------------------------------- #
def test_paired_leg_requires_same_endpoint_to_be_a_complete_pair() -> None:
    # Two legs of the same device form a complete PAIRED_LEG pair; a leg paired
    # across two different endpoints must never be treated as a complete pair.
    same = [
        _edge(RelationType.PAIRED_LEG, "ct_tub_l1", "ct_tub_l2"),
        _edge(RelationType.PAIRED_LEG, "ct_tub_l2", "ct_tub_l1"),
    ]
    graph = TopologyGraph.from_edges(same, (), T0)
    assert graph.paired_leg("ct_tub_l1") == "ct_tub_l2"
    assert graph.paired_leg("ct_tub_l2") == "ct_tub_l1"
    assert set(graph.complete_leg_pairs()) == {frozenset(("ct_tub_l1", "ct_tub_l2"))}

    # A one-sided pairing is not a verified complete pair — the partner must
    # declare the leg back (the v3 equivalent of double_pole_leg's same-device
    # ownership check).  Otherwise summing the two rows would claim the other
    # reader's power.
    one_sided = [_edge(RelationType.PAIRED_LEG, "ct_a_l1", "ct_b_l2")]
    cross_graph = TopologyGraph.from_edges(one_sided, (), T0)
    assert cross_graph.paired_leg("ct_a_l1") is None
    assert cross_graph.complete_leg_pairs() == ()


def test_paired_leg_coefficient_is_half_only_for_complete_pairs() -> None:
    same = [
        _edge(RelationType.PAIRED_LEG, "ct_tub_l1", "ct_tub_l2"),
        _edge(RelationType.PAIRED_LEG, "ct_tub_l2", "ct_tub_l1"),
    ]
    graph = TopologyGraph.from_edges(same, (), T0)
    # A complete pair is fully observed: each leg carries half the load.
    assert graph.leg_coefficient("ct_tub_l1") == pytest.approx(0.5)
    assert graph.leg_coefficient("ct_tub_l2") == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# SAME_CONDUCTOR alias: fallback, never a row.
# --------------------------------------------------------------------------- #
def test_same_conductor_group_alias_is_fallback_not_row() -> None:
    edges = [
        _edge(RelationType.CONTAINS, "main", "ct_canonical"),
        _edge(RelationType.SAME_CONDUCTOR, "ct_canonical", "ct_alias"),
    ]
    graph = TopologyGraph.from_edges(edges, (), T0)
    assert graph.same_conductor_group("ct_alias") == ("ct_canonical", "ct_alias")
    assert graph.canonical_reader("ct_alias") == "ct_canonical"
    # Aliases never become a structural child of the canonical reader.
    assert graph.children("ct_canonical") == ()


# --------------------------------------------------------------------------- #
# HB §17 tree equivalence and primary display tree (M1 seam).
# --------------------------------------------------------------------------- #
def test_hb17_tree_matches_fake_operator_tree() -> None:
    fake = FakeTopology.from_tree(HB17_TREE)
    edges = list(fake.edges_all)
    graph = TopologyGraph.from_edges(edges, (), T0)
    for reader in ("Circuit A", "Circuit B", "Circuit C", "Subpanel", "Main"):
        assert set(graph.ancestors(reader)) == set(fake.ancestors(reader))
        assert set(graph.children(reader)) == set(fake.children(reader))
    assert graph.ancestors("Circuit A") == ("Subpanel", "Main")
    assert graph.ancestors("Main") == ()


def test_primary_display_tree_places_unlocated_endpoints_under_unassigned() -> None:
    edges = [_edge(RelationType.CONTAINS, "main", "kitchen")]
    endpoints = [_endpoint("main", display_name="Main"), _endpoint("kitchen")]
    unlocated = _endpoint("plug", display_name="Loose plug")
    graph = TopologyGraph.from_edges(edges, [*endpoints, unlocated], T0)

    tree = graph.primary_display_tree()
    _assert_has_unassigned(tree)

    columns = build_columns(graph, [*endpoints, unlocated])
    leaves = _reader_leaves(columns)
    # Every endpoint is exactly one leaf, including the unlocated one.
    assert len(leaves) == 3
    assert {leaf.reader_id for leaf in leaves} == {"main", "kitchen", "plug"}
    # The unlocated endpoint sits under an "Unassigned measurements" branch.
    assert _under_unassigned(columns, "plug")


def test_build_columns_on_primary_display_tree_has_depth_five_on_hb17() -> None:
    """M1 consumes primary_display_tree(); on HB §17 it yields depth 5."""
    fake = FakeTopology.from_tree(HB17_TREE)
    endpoints = [
        _endpoint("Main"),
        _endpoint("Subpanel"),
        _endpoint("Circuit A"),
        _endpoint("Circuit B"),
        _endpoint("Circuit C"),
    ]
    graph = TopologyGraph.from_edges(list(fake.edges_all), endpoints, T0)

    # build_columns reads primary_display_tree() from the view ...
    columns = build_columns(graph, endpoints)
    assert _max_depth(columns[0]) == 5
    leaves = _reader_leaves(columns)
    assert len(leaves) == 5

    # ... and the tree alone, fed through a minimal view, is enough for M1.
    tree = graph.primary_display_tree()

    class _TreeView:
        def primary_display_tree(self) -> object:
            return tree

        def edges(self) -> tuple[object, ...]:
            return ()

    columns_from_tree = build_columns(_TreeView(), endpoints)
    assert _max_depth(columns_from_tree[0]) == 5
    assert len(_reader_leaves(columns_from_tree)) == 5


def test_implements_topology_view_protocol() -> None:
    from wattmosaic_engine.runtime.interfaces import TopologyView

    graph = TopologyGraph.from_edges((), (), T0)
    assert isinstance(graph, TopologyView)


# --------------------------------------------------------------------------- #
# Helpers over built ColumnNode trees.
# --------------------------------------------------------------------------- #
def _reader_leaves(nodes: list[object]) -> list[object]:
    out: list[object] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if getattr(node, "kind", None) == "reader":
            out.append(node)
        else:
            stack.extend(getattr(node, "children", ()))
    return out


def _max_depth(node: object, depth: int = 1) -> int:
    children = getattr(node, "children", ())
    if not children:
        return depth
    return max(_max_depth(child, depth + 1) for child in children)


def _assert_has_unassigned(tree: object) -> None:
    assert any(getattr(node, "kind", None) == "unassigned" for node in tree)


def _under_unassigned(nodes: list[object], reader_id: str) -> bool:
    for node in nodes:
        is_unassigned = getattr(node, "kind", None) == "unassigned"
        leaves_have_reader = any(
            getattr(leaf, "reader_id", None) == reader_id
            for leaf in _reader_leaves([node])
        )
        if is_unassigned and leaves_have_reader:
            return True
        if _under_unassigned(getattr(node, "children", ()), reader_id):
            return True
    return False


def test_primary_display_tree_explicit_level_node_form() -> None:
    """primary_display_tree returns explicit level nodes accepted by build_columns."""
    edges = [
        _edge(RelationType.CONTAINS, "main", "subpanel"),
        _edge(RelationType.CONTAINS, "subpanel", "kitchen"),
    ]
    endpoints = [_endpoint("main"), _endpoint("subpanel"), _endpoint("kitchen")]
    graph = TopologyGraph.from_edges(edges, endpoints, T0)
    tree = graph.primary_display_tree()
    # Every node carries an explicit L1..L4 level; build_columns consumes these.
    levels: set[str] = set()

    def walk(nodes: object) -> None:
        for n in nodes:
            levels.add(str(n.level))
            walk(getattr(n, "children", ()))

    walk(tree)
    # Explicit level nodes are present (never an untyped nested map), and the
    # L4 reader leaf carries a reader_id.
    assert levels <= {"L1", "L2", "L3", "L4"}
    assert "L1" in levels and "L4" in levels
