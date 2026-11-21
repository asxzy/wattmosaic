"""Edge states judge the conflict *rate* with a Wilson bound, not a small-sample point ratio.

Regression (real-history replay, 2026-11-20): true pairs with few events
(4 support / 2 conflict) ended CONFLICTED on a point ratio of 0.33 although
their conflicts were measurement artefacts; a genuinely wrong pair (cross-leg,
> 90 % conflict) must still end CONFLICTED after a handful of events, and one
event never moves an edge past CANDIDATE.  Names are arbitrary.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import (
    DeltaObservation,
    TopologyInferenceView,
    _wilson_bounds,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(support: int, conflict: int, lifecycle: TopologyEdgeState) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id="te-inf::feed::load",
        relation=RelationType.CONTAINS,
        source_endpoint_id="feed",
        target_endpoint_id="load",
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, conflict=conflict, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )


def _settled(support: int, conflict: int, lifecycle: TopologyEdgeState) -> TopologyEdgeState:
    causes = {("feed", "load", "contains"): frozenset({"a", "b", "c"})}
    view = TopologyInferenceView(
        edges=(_edge(support, conflict, lifecycle),), clock=lambda: T0, causes_by_edge=causes
    ).observe_group(DeltaObservation(group_id="g", deltas={"other": (40.0, 2.0)}, at=T0, cause_id="x"))
    return view.edge("feed", "load").lifecycle


def test_wilson_bounds_are_ordered_and_shrink_with_evidence() -> None:
    lo_small, hi_small = _wilson_bounds(2, 6)
    lo_big, hi_big = _wilson_bounds(200, 600)
    assert 0.0 <= lo_small < 2 / 6 < hi_small <= 1.0
    assert hi_big - lo_big < hi_small - lo_small


def test_two_conflicts_in_six_events_is_not_conflicted() -> None:
    assert _settled(4, 2, TopologyEdgeState.CANDIDATE) != TopologyEdgeState.CONFLICTED


def test_a_mostly_conflicting_pair_is_still_conflicted() -> None:
    assert _settled(1, 5, TopologyEdgeState.CANDIDATE) == TopologyEdgeState.CONFLICTED
    assert _settled(30, 400, TopologyEdgeState.CANDIDATE) == TopologyEdgeState.CONFLICTED


def test_one_event_never_passes_candidate() -> None:
    view = TopologyInferenceView(at=T0).observe_group(
        DeltaObservation(
            group_id="g1", deltas={"feed": (150.0, 2.0), "load": (150.0, 2.0)}, at=T0, cause_id="c1"
        )
    )
    edge = view.edge("feed", "load")
    assert edge is not None and edge.lifecycle == TopologyEdgeState.CANDIDATE
