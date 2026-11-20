"""Regression for fix A (already applied in the tree): step-anchored verdicts win.

The multi-child explanation uses the parent's NET move over the whole group,
where unrelated opposite steps cancel.  Applied to a true feeder whose anchored
step matches the child, that net-zero move turned support into conflict (spec
README, fix A).  A member whose step sits inside the parent's noise must never be
credited support by a subset explanation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_a_true_feeder_with_net_zero_group_move_is_support() -> None:
    # The parent matched the child's anchored step (+300).  Later, an unrelated
    # -300 step on the same parent cancelled the group's net move to ~0.
    view = TopologyInferenceView(at=T0)
    view = view.observe_group(
        DeltaObservation(
            group_id="g1",
            deltas={"parent": (0.0, 2.0), "child": (300.0, 2.0), "other": (-300.0, 2.0)},
            at=T0,
            cause_id="c1",
        ),
        measured_by_child={"child": {"parent": 296.0}},
        group_moves={"parent": 2.0, "child": 300.0, "other": -300.0},
    )
    edge = view.edge("parent", "child")
    assert edge is not None
    assert (edge.evidence.support, edge.evidence.conflict) == (1, 0)


def test_a_two_watt_member_inside_parent_noise_is_never_credited_support() -> None:
    # A parent moves 300 W; a 2 W member sits deep inside its noise.  The
    # multi-child subset must never claim it explains any part of the move.
    view = TopologyInferenceView(at=T0)
    view = view.observe_group(
        DeltaObservation(
            group_id="g1",
            deltas={"parent": (300.0, 2.0), "big": (290.0, 2.0), "tiny": (2.0, 1.0)},
            at=T0,
            cause_id="c1",
        )
    )
    tiny = view.edge("parent", "tiny")
    assert tiny is None or tiny.evidence.support == 0
    big = view.edge("parent", "big")
    assert big is not None and big.evidence.support == 1
