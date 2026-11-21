"""E3 §2: never pick one of two indistinguishable parent candidates.

A leg-unidentifiable child (two legs of a pair, or a one-leg x2 load scaled by
r/f) makes both candidate parents move by the same amount.  Steps alone cannot
say which parent the measured leg sits on, so neither crossing may become
STRUCTURAL; both stay PROVISIONAL and the child is recorded as leg-unidentified.
Asymmetric evidence breaks the tie in a later settle.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.electrical.topology_inference import (
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _edge(parent: str, child: str, support: int = 8, *, conflict: int = 0) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=TopologyEvidence(
            support=support, support_w=float(support), conflict=conflict,
            independent_causes=2,
        ),
        valid_from=T0,
        pinned=False,
    )


def _view(edges: list[TopologyEdgeV3]) -> TopologyInferenceView:
    causes = {
        (e.source_endpoint_id, e.target_endpoint_id, "contains"): frozenset({"c1", "c2"})
        for e in edges
    }
    return TopologyInferenceView(
        clock=lambda: T0, at=T0, edges=tuple(edges), causes_by_edge=causes,
    )


def _settle(view: TopologyInferenceView, deltas: dict[str, float], *, name: str = "g") -> TopologyInferenceView:
    return view.observe_group(
        DeltaObservation(
            group_id=name,
            deltas={r: (d, 2.0) for r, d in deltas.items()},
            at=T0,
            cause_id=name,
        )
    )


def test_two_unrelated_roots_both_move_for_every_child_step() -> None:
    edges = [_edge("root_a", "child"), _edge("root_b", "child")]
    view = _view(edges)
    # Both roots move by the child's step on every event: neither is
    # identifiable, so a plain re-settle must cap both at PROVISIONAL.
    view = _settle(view, {"root_a": 50.0, "root_b": 50.0, "child": 50.0})
    a = view.edge("root_a", "child")
    b = view.edge("root_b", "child")
    assert a is not None and b is not None
    assert a.lifecycle == TopologyEdgeState.PROVISIONAL
    assert b.lifecycle == TopologyEdgeState.PROVISIONAL
    assert "child" in view.leg_unidentified


def test_asymmetric_steps_break_the_tie_in_a_later_settle() -> None:
    edges = [_edge("root_a", "child"), _edge("root_b", "child")]
    view = _view(edges)
    view = _settle(view, {"root_a": 50.0, "root_b": 50.0, "child": 50.0}, name="g0")
    assert "child" in view.leg_unidentified
    # 20 steps move only root_a with the child: root_a's support climbs while
    # root_b collects conflict, so root_a breaks away and can be STRUCTURAL.
    for i in range(1, 21):
        view = _settle(view, {"root_a": 50.0, "child": 50.0}, name=f"g{i}")
    a = view.edge("root_a", "child")
    assert a is not None
    assert a.lifecycle == TopologyEdgeState.STRUCTURAL
    assert "child" not in view.leg_unidentified


def test_a_main_sub_child_chain_is_not_ambiguous() -> None:
    # main -> sub -> child: the two candidate parents are an ancestor chain, so
    # the existing one-direct-parent rule applies, not the ambiguity cap.
    edges = [
        _edge("main", "sub"),
        _edge("sub", "child"),
        _edge("main", "child", support=8),
    ]
    view = _view(edges)
    view = _settle(view, {"main": 50.0, "sub": 50.0, "child": 50.0})
    assert "child" not in view.leg_unidentified
    # The sub edge may be STRUCTURAL (the direct parent); the grandparent edge
    # is handled by the normal structure rule, not by ambiguity.
    assert view.edge("sub", "child").lifecycle in (
        TopologyEdgeState.STRUCTURAL,
        TopologyEdgeState.PROVISIONAL,
    )


def test_a_single_event_is_still_never_past_candidate() -> None:
    view = _view([])
    view = _settle(view, {"root_a": 50.0, "root_b": 50.0, "child": 50.0})
    for edge in view.edges:
        assert edge.lifecycle == TopologyEdgeState.CANDIDATE
    assert not view.leg_unidentified
