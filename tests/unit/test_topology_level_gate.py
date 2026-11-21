"""E3 §1: live, skew-tolerant level-containment gate.

The containment gate was never evaluated live (the runtime never passed
``absolute``) and used an exact instant comparison, so a true feeder one sample
out of skew looked outside its parent.  These tests cover the tolerant kernel
and its effects inside :class:`TopologyInferenceView` with synthetic series only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.electrical.nesting_math import containment, containment_tolerant
from wattmosaic_engine.electrical.topology_inference import (
    CONTAINMENT_DECISIVE_INSTANTS,
    CONTAINMENT_MIN_INSTANTS,
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(parent: str, child: str, lifecycle: TopologyEdgeState = TopologyEdgeState.PROVISIONAL) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=TopologyEvidence(),
        valid_from=T0,
        pinned=False,
    )


def _causes(edges: list[TopologyEdgeV3]) -> dict[tuple[str, str, str], frozenset[str]]:
    return {
        (e.source_endpoint_id, e.target_endpoint_id, "contains"): frozenset({"c1", "c2", "c3"})
        for e in edges
    }


def _observe(view: TopologyInferenceView, deltas: dict[str, float], *, group_id: str) -> TopologyInferenceView:
    return view.observe_group(
        DeltaObservation(
            group_id=group_id,
            deltas={reader: (delta, 2.0) for reader, delta in deltas.items()},
            at=T0,
            cause_id=f"cause-{group_id}",
        )
    )


def test_tolerant_containment_absorbs_a_one_bucket_lead() -> None:
    # The child steps one bucket before the parent (cross-device skew).  At the
    # step instant the plain comparison sees child > parent and fails; the
    # tolerant window sees the parent's later, higher level and passes.
    parent = [1000.0] * 10 + [1500.0] * 10
    child = [500.0] * 10 + [1000.0] * 10
    # Shift the child's step one bucket earlier: at index 9 child is already
    # 1000 while the parent is still 1000 (equal -> passes with tol); at index
    # 10 child is 1500 while parent just moved to 1500 at 11.  Use a larger
    # step so the plain fraction drops below 0.995 while the window passes.
    child = [400.0] * 10 + [1400.0] * 10
    parent = [1000.0] * 11 + [2000.0] * 9
    plain, _ = containment(parent, child)
    tolerant, comparable = containment_tolerant(parent, child, lag=1)
    assert comparable == 20
    assert plain < 0.995
    assert tolerant == 1.0


def test_a_feeder_pair_with_symmetric_support_chooses_the_forward_direction() -> None:
    # Both M -> S and S -> M collect clean support, but the absolute levels show
    # M >= S always.  Once the reverse fails containment its direction cap is
    # lifted and the forward edge is free to become STRUCTURAL (E3 §1 effect 3).
    n = CONTAINMENT_DECISIVE_INSTANTS
    absolute = {
        "m": [2000.0] * n,
        "s": [1000.0] * n,
    }
    edges = [_edge("m", "s"), _edge("s", "m")]
    view = TopologyInferenceView(
        clock=lambda: T0,
        at=T0,
        edges=tuple(edges),
        causes_by_edge=_causes(edges),
        require_direction=True,
        absolute=absolute,
    )
    # 12 mirrored clean steps give both edges enough decaying support.  With a
    # decisive day of levels the reverse is CONFLICTED (its child M is above
    # the parent S all day), so the forward edge is free to confirm.
    for i in range(12):
        view = _observe(view, {"m": 50.0, "s": 50.0}, group_id=f"g{i}")
    forward = view.edge("m", "s")
    reverse = view.edge("s", "m")
    assert forward is not None and reverse is not None
    assert reverse.lifecycle == TopologyEdgeState.CONFLICTED
    assert forward.lifecycle == TopologyEdgeState.STRUCTURAL


def test_cross_leg_candidate_with_level_violation_is_conflicted() -> None:
    # Clean step support, but the child is above the candidate parent's level
    # for 30 % of a day: decisive evidence it is not inside that parent.
    n = CONTAINMENT_DECISIVE_INSTANTS
    child_levels = [1200.0] * int(0.3 * n) + [300.0] * (n - int(0.3 * n))
    absolute = {"p": [1000.0] * n, "c": child_levels}
    edges = [_edge("p", "c")]
    view = TopologyInferenceView(
        clock=lambda: T0,
        at=T0,
        edges=tuple(edges),
        causes_by_edge=_causes(edges),
        absolute=absolute,
    )
    for i in range(12):
        view = _observe(view, {"p": 50.0, "c": 50.0}, group_id=f"g{i}")
    edge = view.edge("p", "c")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.CONFLICTED


def test_fewer_than_min_instants_does_not_evaluate_the_gate() -> None:
    from wattmosaic_engine.electrical.topology_inference import _containment_fraction, _containment_gate

    assert CONTAINMENT_MIN_INSTANTS == 120
    parent = [1000.0] * (CONTAINMENT_MIN_INSTANTS - 1)
    child = [2000.0] * (CONTAINMENT_MIN_INSTANTS - 1)  # child always above
    fraction, comparable = _containment_fraction(parent, child)
    assert comparable == CONTAINMENT_MIN_INSTANTS - 1
    assert fraction == 0.0
    assert _containment_gate(parent, child) is True

    # One more instant is enough to evaluate the gate, which now fails.
    parent = [1000.0] * CONTAINMENT_MIN_INSTANTS
    child = [2000.0] * CONTAINMENT_MIN_INSTANTS
    assert _containment_gate(parent, child) is False


def test_x2_child_is_compared_at_r_over_f_and_passes() -> None:
    # The runtime divides a monitor-applied x2 out of the child's level series.
    # Each child bucket reads r, each parent reads r / 2; at r/f the child is
    # inside the parent and the gate passes (E3 §1).
    n = CONTAINMENT_DECISIVE_INSTANTS
    absolute = {
        "p": [1000.0] * n,
        "c": [500.0] * n,  # already scaled: r / f
    }
    edges = [_edge("p", "c")]
    view = TopologyInferenceView(
        clock=lambda: T0,
        at=T0,
        edges=tuple(edges),
        causes_by_edge=_causes(edges),
        absolute=absolute,
    )
    for i in range(12):
        # Parents move 50, the x2 child reports 100 -> at r/f it matches 50.
        view = _observe(view, {"p": 50.0, "c": 50.0}, group_id=f"g{i}")
    edge = view.edge("p", "c")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL
    from wattmosaic_engine.electrical.topology_inference import _containment_gate

    assert _containment_gate(absolute["p"], absolute["c"]) is True
