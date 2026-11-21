"""E6 §1: exactly one placement per child at any confidence.

The tests use neutral reader ids; placement comes only from the declared edges,
evidence counts and lift -- never from a name or id pattern.  Placement is
display/diagnostic only and does not touch H (STRUCTURAL edges).
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_placement import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MEDIUM,
    BAND_UNPLACED,
    compute_placements,
)

T0 = datetime(2026, 11, 21, tzinfo=UTC)


def _edge(parent: str, child: str, lifecycle: TopologyEdgeState, support: int = 4, conflict: int = 0) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, conflict=conflict, support_w=float(support)),
        valid_from=T0,
        pinned=False,
    )


def test_each_child_gets_exactly_one_parent_and_a_band() -> None:
    edges = [
        _edge("feed", "sub", TopologyEdgeState.PROVISIONAL, support=20),
        _edge("sub", "oven", TopologyEdgeState.PROVISIONAL, support=8),
        _edge("feed", "oven", TopologyEdgeState.PROVISIONAL, support=1),  # grandparent rival
    ]
    report = compute_placements(
        edges,
        readers=["feed", "sub", "oven"],
        wlift_by_pair={("feed", "sub"): 0.5, ("sub", "oven"): 0.4},
        n_by_child={"sub": 12, "oven": 12},
    )
    sub = report.by_child["sub"]
    assert sub.parent == "feed"
    assert sub.band == BAND_HIGH  # score >= 0.15
    oven = report.by_child["oven"]
    assert oven.parent == "sub"  # deeper parent wins over grandparent
    assert oven.band == BAND_HIGH
    assert report.by_child["feed"].band == BAND_UNPLACED  # root


def test_bands_follow_score_thresholds() -> None:
    edges = [
        _edge("feed", "a", TopologyEdgeState.PROVISIONAL, support=10),
        _edge("feed", "b", TopologyEdgeState.PROVISIONAL, support=10),
        _edge("feed", "c", TopologyEdgeState.PROVISIONAL, support=10),
    ]
    report = compute_placements(
        edges,
        readers=["feed", "a", "b", "c"],
        wlift_by_pair={("feed", "a"): 0.3, ("feed", "b"): 0.07, ("feed", "c"): 0.001},
        n_by_child={"a": 10, "b": 10, "c": 10},
    )
    assert report.by_child["a"].band == BAND_HIGH
    assert report.by_child["b"].band == BAND_MEDIUM
    assert report.by_child["c"].band == BAND_LOW
    assert report.by_child["c"].reason == "low_confidence"


def test_low_wlift_plateau_when_fewer_than_eight_steps() -> None:
    edges = [_edge("feed", "lamp", TopologyEdgeState.PROVISIONAL, support=10)]
    report = compute_placements(edges, readers=["feed", "lamp"], n_by_child={"lamp": 3})
    lamp = report.by_child["lamp"]
    # 0.05 * (10 + 0.5) / (10 + 0 + 1) = 0.0477 < 0.05 -> low band.
    assert lamp.band == BAND_LOW
    assert lamp.score > 0


def test_structural_edge_is_high_even_with_low_score() -> None:
    edges = [_edge("feed", "hvac", TopologyEdgeState.STRUCTURAL, support=2)]
    report = compute_placements(edges, readers=["feed", "hvac"], n_by_child={"hvac": 3})
    assert report.by_child["hvac"].band == BAND_HIGH


def test_conflicted_and_declared_pair_are_never_candidates() -> None:
    edges = [
        _edge("feed", "x", TopologyEdgeState.CONFLICTED, support=20),
        _edge("twin", "x", TopologyEdgeState.PROVISIONAL, support=8),
    ]
    report = compute_placements(
        edges,
        readers=["feed", "twin", "x"],
        declared_pairs=frozenset({frozenset({"twin", "x"})}),
    )
    assert report.by_child["x"].parent is None
    assert report.by_child["x"].band == BAND_UNPLACED


def test_placement_is_acyclic() -> None:
    edges = [
        _edge("feed", "sub", TopologyEdgeState.PROVISIONAL, support=20),
        _edge("sub", "lamp", TopologyEdgeState.PROVISIONAL, support=20),
        # A cycle: lamp cannot be placed under its own descendant.
        _edge("lamp", "feed", TopologyEdgeState.PROVISIONAL, support=30),
    ]
    report = compute_placements(edges, readers=["feed", "sub", "lamp"])
    assert report.by_child["feed"].parent is None


def test_leg_unidentified_child_gets_both_legs() -> None:
    edges = [
        _edge("main_l", "leg", TopologyEdgeState.PROVISIONAL, support=20),
        _edge("main_r", "leg", TopologyEdgeState.PROVISIONAL, support=20),
    ]
    report = compute_placements(
        edges, readers=["main_l", "main_r", "leg"], leg_unidentified=frozenset({"leg"})
    )
    leg = report.by_child["leg"]
    assert leg.parent is None
    assert leg.band == BAND_MEDIUM
    assert leg.reason == "leg_unidentified"
    assert leg.both_legs == ("main_l", "main_r")
