"""AT-13 — reader columns are not duplicated for secondary relationships (HB §21).

Closes: M1. SHARED_LOAD, PAIRED_LEG and SAME_CONDUCTOR edges become annotations,
never extra leaves; leaf count equals endpoint count.

The invented ``project_matrix_payload(hb17["observation"], secondary_edges=...)``
surface named by the original K07 sketch never landed.  This closed test drives
M1's real column builder: it constructs the K08 topology view with the three
secondary edges and calls ``build_columns`` with the HB §17 reader endpoints.
The fixture plumbing is rewritten; the AT-13 assertions are unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    ColumnAnnotation,
    ColumnNode,
    EdgeEvidence,
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.mapping import build_columns  # real M1 surface
from wattmosaic_engine.runtime.fakes import FakeTopology

CLOSED_BY = "M1"

pytestmark = [pytest.mark.acceptance]

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ENDPOINTS = ("ct_main", "ct_sub", "ct_a", "ct_b", "ct_c")


def _leaves(columns):
    for c in columns:
        if not c.children:
            yield c
        else:
            yield from _leaves(c.children)


def _edge(relation: RelationType, source: str, target: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"e::{source}->{target}::{relation.value}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=True,
    )


def test_secondary_edges_are_annotations_not_leaves(hb17) -> None:
    """Secondary relations annotate existing readers without minting columns."""
    house = hb17["house"]
    assert {s["reader_id"] for s in house.reader_samples} >= set(ENDPOINTS)

    edges = [
        _edge(RelationType.SHARED_LOAD, "ct_a", "ct_b"),
        _edge(RelationType.PAIRED_LEG, "ct_a", "ct_c"),
        _edge(RelationType.SAME_CONDUCTOR, "ct_sub", "ct_c"),
    ]
    topology = FakeTopology.from_edges(edges)
    payload_columns: list[ColumnNode] = build_columns(topology, list(ENDPOINTS))

    leaves = list(_leaves(payload_columns))
    assert len(leaves) == 5
    assert len({leaf.reader_id for leaf in leaves}) == 5

    annotations: list[ColumnAnnotation] = [
        a for c in payload_columns for a in _all_annotations(c)
    ]
    kinds = {a.kind for a in annotations}
    assert kinds >= {"SHARED_LOAD", "PAIRED_LEG", "SAME_CONDUCTOR"}


def _all_annotations(node: ColumnNode):
    """Yield annotations across the whole column tree (leaves included)."""
    yield from node.annotations
    for child in node.children:
        yield from _all_annotations(child)
