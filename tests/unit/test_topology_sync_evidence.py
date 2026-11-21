"""E6 §6: co-sampling, synchronous verdicts, pulses and contrast ranking.

Synthetic only; no reader ids are special-cased in engine code.  Two co-sampled
readers whose parent window contains an unrelated opposite step still give
support (no spread veto).  A single-sample pulse gives support on the true
parent and conflict on a flat wrong parent.  Non-co-sampled pairs keep the
anchored-window verdict.  Contrast ranking picks the lower-conflict parent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.sync_evidence import (
    PulseRing,
    co_sampled_pair,
    sync_verdict,
)
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView
from wattmosaic_engine.electrical.topology_placement import compute_placements

T0 = datetime(2026, 11, 21, tzinfo=UTC)


def _contains(parent: str, child: str, *, support: int, conflict: int) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.PROVISIONAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, conflict=conflict, support_w=float(support), conflict_w=float(conflict)),
        valid_from=T0,
        pinned=False,
    )


def test_cosampled_pair_detected_from_stamps() -> None:
    stamps = [float(i * 10) for i in range(20)]
    assert co_sampled_pair(stamps, [t + 1 for t in stamps]) is True
    # Different cadence: almost no stamps coincide.
    assert co_sampled_pair(stamps, [t * 1.7 for t in stamps]) is False


def test_sync_verdict_no_window_no_spread_veto() -> None:
    # The child steps +300 W; the parent moves +300 W across the child's exact
    # sample pair, even though a concurrent opposite step elsewhere in its
    # anchored window would have made the legacy verdict neutral.
    assert sync_verdict(300.0, 300.0, sigma_p=2.0) == "support"
    # A flat wrong parent gives conflict, not neutral.
    assert sync_verdict(0.0, 300.0, sigma_p=2.0) == "conflict"
    # A different-size move is neutral.
    assert sync_verdict(150.0, 300.0, sigma_p=2.0) is None


def test_sync_verdict_preserves_sign_and_tolerance() -> None:
    assert sync_verdict(-300.0, -300.0, sigma_p=2.0) == "support"
    assert sync_verdict(300.0, -300.0, sigma_p=2.0) is None
    # 10 % + 15 W tolerance on a 300 W step = 45 W.
    assert sync_verdict(260.0, 300.0, sigma_p=2.0) == "support"


def test_pulse_ring_records_a_single_sample_pulse() -> None:
    ring = PulseRing(sigma_floor=1.0)
    assert ring.record(0.0, 10.0) is None
    assert ring.record(10.0, 310.0) is None
    pulse = ring.record(20.0, 11.0)
    assert pulse is not None
    assert abs(pulse.up_delta - 300.0) < 1e-6
    assert abs(pulse.down_delta + 299.0) < 1e-6
    assert len(ring.drain()) == 1
    assert len(ring.drain()) == 0


def test_non_cosampled_pair_keeps_anchored_window_verdict() -> None:
    # No cosampled set passed: the verdict comes from _classify_step through the
    # observed parent's measured level (legacy behaviour, +300 W matches).
    view = TopologyInferenceView(at=T0)
    updated = view.observe_group(
        DeltaObservation(group_id="g1", deltas={"parent": (300.0, 2.0), "child": (300.0, 2.0)}, at=T0),
        observed={"parent": (2.0, 300.0)},
    )
    edge = updated.edge("parent", "child")
    assert edge is not None and edge.evidence.support == 1


def test_cosampled_sync_verdict_folds_support_and_conflict() -> None:
    view = TopologyInferenceView(at=T0)
    # A co-sampled true parent moves +300 across the child's sample pair; a
    # co-sampled wrong parent is flat.  P3 steps are given for both parents so
    # the existing child-step pass runs, with the sync path overriding it.
    updated = view.observe_group(
        DeltaObservation(
            group_id="g2",
            deltas={"true_parent": (300.0, 2.0), "wrong_parent": (0.0, 2.0), "child": (300.0, 2.0)},
            at=T0,
        ),
        observed={"true_parent": (2.0, 300.0), "wrong_parent": (2.0, 0.0)},
        cosampled=frozenset({frozenset(("true_parent", "child")), frozenset(("wrong_parent", "child"))}),
        sync_changes={"child": {"true_parent": 300.0, "wrong_parent": 0.0}},
    )
    true_edge = updated.edge("true_parent", "child")
    wrong_edge = updated.edge("wrong_parent", "child")
    assert true_edge is not None and true_edge.evidence.support == 1
    # A flat wrong parent has an edge with a conflict verdict (the local sync
    # pass creates a conflict row so contrast ranking can demote it).
    assert wrong_edge is not None and wrong_edge.evidence.conflict >= 1


def test_pulse_gives_support_on_true_parent_and_conflict_on_flat_parent() -> None:
    stamps = [0.0, 10.0, 20.0]
    parent_values = [1000.0, 1300.0, 1000.0]
    from wattmosaic_engine.electrical.sync_evidence import PulseRecord, pulse_verdicts

    pulse = PulseRecord(pre_t=0.0, peak_t=10.0, post_t=20.0, pre=10.0, peak=310.0, post=11.0)
    verdicts = pulse_verdicts(pulse, stamps, parent_values, sigma_p=2.0)
    assert [v[0] for v in verdicts] == ["support", "support"]
    flat = pulse_verdicts(pulse, stamps, [1000.0] * 3, sigma_p=2.0)
    assert [v[0] for v in flat] == ["conflict", "conflict"]


def test_contrast_ranking_picks_the_lower_conflict_cosampled_parent() -> None:
    edges = [
        _contains("noisy", "child", support=10, conflict=8),
        _contains("quiet", "child", support=10, conflict=0),
    ]
    cosampled = frozenset({frozenset(("noisy", "child")), frozenset(("quiet", "child"))})
    report = compute_placements(
        edges,
        readers=["noisy", "quiet", "child"],
        wlift_by_pair={("noisy", "child"): 0.2, ("quiet", "child"): 0.2},
        n_by_child={"child": 20},
        cosampled=cosampled,
    )
    placement = report.by_child["child"]
    assert placement.parent == "quiet", placement
