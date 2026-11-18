"""M1: L0–L4 column tree (design §8, HB §9.1–9.2, AT-13)."""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    ColumnAnnotation,
    EdgeEvidence,
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.mapping import build_columns
from wattmosaic_engine.runtime.fakes import FakeTopology

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(
    relation: RelationType,
    source: str,
    target: str,
    lifecycle: EdgeLifecycleV3 = EdgeLifecycleV3.CONFIRMED,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"e::{source}->{target}::{relation.value}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by="user",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=True,
        valid_until=T0 if lifecycle is EdgeLifecycleV3.RETIRED else None,
    )


class TreeTopology(FakeTopology):
    """K08 fake topology with T1's ``primary_display_tree`` seam added."""

    def __init__(self, tree: object) -> None:
        super().__init__(())
        self._tree = tree

    def primary_display_tree(self) -> object:
        return self._tree


def _leaves(nodes: list[object]) -> list[object]:
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


def test_hb17_column_tree_has_depth_five() -> None:
    # HB §17/N §9.1 full hierarchy: L1 panel -> L2 subpanel -> L3 circuit ->
    # L4 reader.  Explicit level nodes are the T1 primary_display_tree shape.
    tree = [
        {
            "label": "Main Panel",
            "level": "L1",
            "kind": "panel",
            "children": [
                {
                    "label": "Subpanel A",
                    "level": "L2",
                    "kind": "subpanel",
                    "children": [
                        {
                            "label": "Circuit A",
                            "level": "L3",
                            "kind": "circuit",
                            "children": [{"label": "CT-A", "reader_id": "Circuit A"}],
                        }
                    ],
                }
            ],
        }
    ]
    topology = TreeTopology(tree)
    endpoints = ["Circuit A"]
    columns = build_columns(topology, endpoints)
    assert len(columns) == 1  # one L0 domain root
    assert columns[0].level == "L0"
    assert _max_depth(columns[0]) == 5  # L0..L4 inclusive


def test_leaf_count_equals_endpoint_count() -> None:
    tree = {"Main": {"Subpanel": {"Circuit A": {}, "Circuit B": {}}, "Circuit C": {}}}
    topology = TreeTopology(tree)
    endpoints = ["Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"]
    columns = build_columns(topology, endpoints)
    leaves = _leaves(columns)
    assert len(leaves) == len(endpoints)
    assert {leaf.reader_id for leaf in leaves} == set(endpoints)


def test_at13_secondary_relations_are_annotations_not_extra_leaves() -> None:
    edges = [
        _edge(RelationType.SHARED_LOAD, "Circuit A", "Circuit B"),
        _edge(RelationType.PAIRED_LEG, "Circuit A", "Circuit C"),
        _edge(RelationType.SAME_CONDUCTOR, "Subpanel", "Circuit C"),
        _edge(RelationType.CORRELATED, "Main", "Subpanel"),
        _edge(RelationType.CONTAINS, "Main", "Circuit B", EdgeLifecycleV3.PROVISIONAL),
    ]
    topology = FakeTopology.from_edges(edges)
    endpoints = ["Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"]
    columns = build_columns(topology, endpoints)
    leaves = _leaves(columns)
    assert len(leaves) == len(endpoints)

    kinds = {a.kind for leaf in leaves for a in leaf.annotations}
    assert {
        "SHARED_LOAD", "PAIRED_LEG", "SAME_CONDUCTOR", "CORRELATED", "CONTAINS"
    } <= kinds
    # An annotation never becomes a reader leaf itself.
    assert all(
        not any(a.kind == "reader" for a in leaf.annotations) for leaf in leaves
    )


def test_unlinked_endpoint_goes_under_unassigned() -> None:
    tree = {"Main": {"Circuit A": {}}}
    topology = TreeTopology(tree)
    endpoints = [
        {"endpoint_id": "Main", "display_name": "Main"},
        {"endpoint_id": "Circuit A", "display_name": "Circuit A"},
        {"endpoint_id": "Plug-TV", "display_name": "TV plug", "panel_id": None},
    ]
    columns = build_columns(topology, endpoints)
    unassigned = next(
        n for n in columns[0].children if n.kind == "unassigned"
    )
    assert unassigned.label == "Unassigned measurements"
    assert [leaf.reader_id for leaf in _leaves([unassigned])] == ["Plug-TV"]


def test_column_annotation_contract_shape() -> None:
    ann = ColumnAnnotation(kind="SHARED_LOAD", related_reader_id="Circuit B")
    assert ann.kind == "SHARED_LOAD"
