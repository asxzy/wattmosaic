"""ADR-0046: persisted user declarations override inferred placements at read time.

``apply_user_placements`` is pure: it overlays the event-sourced CONTAINS rows
on an inference ledger's ``PlacementReport`` without mutating it or the live
ledger.  These tests all exercise the real function; they fail before the change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    Placement,
    PlacementReport,
    apply_user_placements,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(
    edge_id: str,
    parent: str,
    child: str,
    *,
    lifecycle: TopologyEdgeState = TopologyEdgeState.STRUCTURAL,
    declared_by: str = "user",
    pinned: bool = True,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by=declared_by,  # type: ignore[arg-type]
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=valid_from or T0,
        valid_until=valid_until,
        pinned=pinned,
    )


def _report(placement: Placement) -> PlacementReport:
    report = PlacementReport()
    report.by_child[placement.child] = placement
    return report


def test_user_parent_beats_a_higher_scoring_inferred_parent() -> None:
    placement = Placement(
        child="ct_oven",
        parent="ct_feed",
        band=BAND_HIGH,
        score=0.9,
        alternatives=(("ct_feed", 0.9), ("ct_sub", 0.8)),
    )
    out = apply_user_placements(
        _report(placement),
        [_edge("u1", "ct_sub", "ct_oven")],
    )
    result = out.by_child["ct_oven"]
    assert result.parent == "ct_sub"
    assert result.score == 1.0
    assert result.band == BAND_HIGH
    assert result.reason == "user"
    # The chosen parent is removed from the alternatives; the rest are kept.
    assert result.alternatives == (("ct_feed", 0.9),)


def test_latest_user_edge_wins_among_two() -> None:
    older = _edge("u-old", "ct_sub", "ct_oven", valid_from=T0)
    newer = _edge("u-new", "ct_main", "ct_oven", valid_from=T0 + timedelta(minutes=5))
    # Deliberately pass the older first; latest valid_from must still win.
    out = apply_user_placements(
        _report(Placement(child="ct_oven", parent="ct_feed")),
        [older, newer],
    )
    assert out.by_child["ct_oven"].parent == "ct_main"


def test_largest_edge_id_breaks_a_valid_from_tie() -> None:
    first = _edge("u-a", "ct_sub", "ct_oven", valid_from=T0)
    second = _edge("u-b", "ct_main", "ct_oven", valid_from=T0)
    out = apply_user_placements(
        _report(Placement(child="ct_oven", parent="ct_feed")),
        [first, second],
    )
    assert out.by_child["ct_oven"].parent == "ct_main"


def test_pinned_inferred_edge_is_a_user_declaration() -> None:
    pinned = _edge("inf-1", "ct_sub", "ct_oven", declared_by="inference", pinned=True)
    out = apply_user_placements(
        _report(Placement(child="ct_oven", parent="ct_feed")),
        [pinned],
    )
    assert out.by_child["ct_oven"].reason == "user"
    assert out.by_child["ct_oven"].parent == "ct_sub"


def test_closed_user_edge_does_not_override() -> None:
    closed = _edge("u-old", "ct_sub", "ct_oven", valid_until=T0 + timedelta(minutes=1))
    placement = Placement(child="ct_oven", parent="ct_feed")
    out = apply_user_placements(_report(placement), [closed])
    assert out.by_child["ct_oven"].parent == "ct_feed"


def test_rejected_pair_falls_back_to_next_alternative() -> None:
    placement = Placement(
        child="ct_oven",
        parent="ct_feed",
        band=BAND_HIGH,
        score=0.9,
        alternatives=(("ct_sub", 0.12), ("ct_lights", 0.02)),
    )
    rejected = _edge(
        "r1", "ct_feed", "ct_oven",
        lifecycle=TopologyEdgeState.REJECTED,
        declared_by="inference",
        pinned=False,
        valid_until=T0,
    )
    out = apply_user_placements(_report(placement), [rejected])
    result = out.by_child["ct_oven"]
    assert result.parent == "ct_sub"
    assert result.score == 0.12
    assert result.band == BAND_MEDIUM
    assert result.reason is None


def test_fallback_bands_follow_the_score_thresholds() -> None:
    rejected = _edge(
        "r1", "ct_feed", "ct_oven",
        lifecycle=TopologyEdgeState.REJECTED,
        declared_by="inference",
        pinned=False,
        valid_until=T0,
    )
    for score, band in ((0.2, BAND_HIGH), (0.06, BAND_MEDIUM), (0.01, BAND_LOW)):
        placement = Placement(
            child="ct_oven",
            parent="ct_feed",
            alternatives=(("ct_sub", score),),
        )
        out = apply_user_placements(_report(placement), [rejected])
        assert out.by_child["ct_oven"].band == band


def test_no_usable_alternative_becomes_user_root() -> None:
    placement = Placement(
        child="ct_oven",
        parent="ct_feed",
        alternatives=(("ct_sub", 0.5),),
    )
    rejected_feed = _edge(
        "r1", "ct_feed", "ct_oven",
        lifecycle=TopologyEdgeState.REJECTED,
        declared_by="inference",
        pinned=False,
        valid_until=T0,
    )
    rejected_sub = _edge(
        "r2", "ct_sub", "ct_oven",
        lifecycle=TopologyEdgeState.REJECTED,
        declared_by="inference",
        pinned=False,
        valid_until=T0,
    )
    out = apply_user_placements(_report(placement), [rejected_feed, rejected_sub])
    result = out.by_child["ct_oven"]
    assert result.parent is None
    assert result.reason == "user_root"


def test_both_legs_cleared_when_a_user_parent_exists() -> None:
    placement = Placement(
        child="ct_oven",
        parent=None,
        band=BAND_MEDIUM,
        reason="leg_unidentified",
        both_legs=("ct_a", "ct_b"),
    )
    out = apply_user_placements(
        _report(placement), [_edge("u1", "ct_sub", "ct_oven")]
    )
    result = out.by_child["ct_oven"]
    assert result.parent == "ct_sub"
    assert result.both_legs == ()


def test_input_report_is_not_mutated() -> None:
    placement = Placement(child="ct_oven", parent="ct_feed", alternatives=(("ct_sub", 0.5),))
    report = _report(placement)
    edges = [
        _edge("u1", "ct_sub", "ct_oven"),
        _edge("r1", "ct_feed", "ct_oven", lifecycle=TopologyEdgeState.REJECTED,
              declared_by="inference", pinned=False, valid_until=T0),
    ]
    out = apply_user_placements(report, edges)
    assert out is not report
    assert report.by_child["ct_oven"] is placement
    assert placement.parent == "ct_feed"
    assert out.by_child["ct_oven"].parent == "ct_sub"


def test_uncovered_children_are_copied_unchanged() -> None:
    a = Placement(child="ct_a", parent="ct_feed")
    b = Placement(child="ct_b", parent="ct_feed", reason="low_confidence")
    report = PlacementReport(by_child={"ct_a": a, "ct_b": b})
    out = apply_user_placements(report, [])
    assert out.by_child == {"ct_a": a, "ct_b": b}
