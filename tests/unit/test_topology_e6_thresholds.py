"""E6 §2 / ADR-0045: calibrated lifecycle thresholds.

The one-event invariant still holds: one event never passes CANDIDATE.  These
tests drive a pure TopologyInferenceView with synthetic evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import TopologyEdgeState
from wattmosaic_engine.electrical.topology_inference import (
    CONFLICT_RATIO_PROVISIONAL,
    CONFLICT_RATIO_STRUCTURAL,
    KEEP_CONFLICT_RATIO_STRUCTURAL,
    KEEP_SUPPORT_STRUCTURAL,
    LIFT_SUBSTITUTION_MIN_STEPS,
    MIN_SUPPORT_PROVISIONAL,
    MIN_SUPPORT_STRUCTURAL,
    TopologyInferenceView,
    _e4_containment,
)
from wattmosaic_engine.electrical.topology_pairs import PairStats

T0 = datetime(2026, 11, 21, tzinfo=UTC)


def test_calibrated_constants() -> None:
    assert MIN_SUPPORT_PROVISIONAL == 2
    assert MIN_SUPPORT_STRUCTURAL == 3
    assert CONFLICT_RATIO_PROVISIONAL == 0.34
    assert CONFLICT_RATIO_STRUCTURAL == 0.10
    assert KEEP_SUPPORT_STRUCTURAL == 2
    assert KEEP_CONFLICT_RATIO_STRUCTURAL == 0.15
    assert LIFT_SUBSTITUTION_MIN_STEPS == 8


def test_one_event_is_still_candidate() -> None:
    view = TopologyInferenceView(clock=lambda: T0, at=T0)
    view = view.observe_group(
        _group("g0", {"feed": 30.0, "lamp": 30.0}, cause="c0")
    )
    edge = view.edge("feed", "lamp")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.CANDIDATE


def _group(group_id: str, deltas: dict[str, float], cause: str):
    from wattmosaic_engine.electrical.topology_inference import DeltaObservation

    return DeltaObservation(
        group_id=group_id,
        deltas={reader: (value, 2.0) for reader, value in deltas.items()},
        at=T0,
        cause_id=cause,
    )


def test_two_clean_events_reach_provisional_three_structural() -> None:
    view = TopologyInferenceView(clock=lambda: T0, at=T0)
    for i, cause in enumerate(("a", "b", "c", "d")):
        view = view.observe_group(
            _group(f"g{i}", {"feed": 30.0, "lamp": 30.0}, cause)
        )
        lifecycle = view.edge("feed", "lamp").lifecycle
        if i < 2:
            assert lifecycle == TopologyEdgeState.CANDIDATE  # 1-2 events
        elif i == 2:
            assert lifecycle == TopologyEdgeState.PROVISIONAL  # sw ~= 2.85
        else:
            assert lifecycle == TopologyEdgeState.STRUCTURAL  # sw ~= 3.7


def test_informative_containment_veto_needs_ten_per_1000() -> None:
    """E6 §2 drops the '>0 caps' rule: 1 per 1000 no longer caps."""

    class _Pairs:
        def __init__(self, violations: int, n: int) -> None:
            self._stats = PairStats(violations=violations, informative_n=n)

        def stats_for(self, parent: str, child: str):
            return self._stats

    # 1 violation in 200 informative buckets (5 per 1000) does NOT cap now.
    assert _e4_containment("feed", "lamp", _Pairs(1, 200)) is None
    # 2 in 200 (10 per 1000) caps at CANDIDATE.
    assert _e4_containment("feed", "lamp", _Pairs(2, 200)) == TopologyEdgeState.CANDIDATE
    # Need at least 120 informative buckets to evaluate.
    assert _e4_containment("feed", "lamp", _Pairs(3, 100)) is None
