"""T5: a step must be resolvable at the parent to be support (spec P7).

A small child step on a noisy feeder is indistinguishable from the feeder's own
noise: treating a "match" as support promotes wrong STRUCTURAL edges.  A step
that clears the combined noise of parent and child is a real, honest match.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.electrical.topology_inference import (
    DeltaObservation,
    TopologyInferenceView,
    _classify_step,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_a_step_inside_combined_noise_is_neutral_not_support() -> None:
    # 20 W step, parent sigma 11 W: 3*sqrt(11^2 + 2^2) ~= 33.5 W > 20 W.
    assert _classify_step(20.0, 20.0, sigma_p=11.0, sigma_c=2.0) is None
    view = TopologyInferenceView(at=T0)
    for i in range(8):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"p": (20.0, 11.0), "c": (20.0, 2.0)},
                at=T0,
                cause_id=f"cause-{i % 2}",
            )
        )
    edge = view.edge("p", "c")
    assert edge is None or (edge.evidence.support == 0 and edge.evidence.conflict == 0)


def test_a_large_matched_step_is_support() -> None:
    # 200 W step matched within tolerance: well above 3*sqrt(11^2 + 2^2).
    assert _classify_step(200.0, 200.0, sigma_p=11.0, sigma_c=2.0) == "support"
    view = TopologyInferenceView(at=T0)
    for i in range(3):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"p": (200.0, 11.0), "c": (200.0, 2.0)},
                at=T0,
                cause_id=f"cause-{i % 2}",
            )
        )
    edge = view.edge("p", "c")
    assert edge is not None
    assert (edge.evidence.support, edge.evidence.conflict) == (3, 0)


def test_a_balanced_parent_window_is_neutral_not_conflict() -> None:
    # P6: a net-zero parent move whose pre window is not flat is a concurrent
    # opposite step inside the window -- neutral, never conflict.
    assert (
        _classify_step(
            0.0, 150.0, sigma_p=2.0, sigma_c=2.0, parent_spread=40.0
        )
        is None
    )
    # A parent that really stayed flat still contradicts an unmatched child.
    assert (
        _classify_step(
            0.0, 150.0, sigma_p=2.0, sigma_c=2.0, parent_spread=0.5
        )
        == "conflict"
    )


def test_a_small_step_inside_parent_noise_is_never_support_even_with_beta() -> None:
    # With a factor beta, the resolvability test uses the scaled child step.
    assert _classify_step(10.0, 20.0, sigma_p=11.0, sigma_c=2.0, beta=0.5) is None
