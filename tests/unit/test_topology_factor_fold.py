"""P3: monitor-applied factors are honoured in the T5 fold.

A x2 child reports ``r`` while each parent moves ``r / 2``.  Without the factor
the edge stays neutral forever (the parent's move is outside the child's match
tolerance, or the child's step is not resolvable at the parent).  The runtime
looks up the factor in force at the child's step and passes ``scale = 1 / m``
into the fold, which divides it out of the child's delta and sigma.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import MeasurementFactor
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _view() -> TopologyInferenceView:
    return TopologyInferenceView(
        at=T0,
        absolute={
            "parent_a": [1000.0] * 40,
            "parent_b": [1000.0] * 40,
            "child": [200.0] * 40,
        },
    )


def _observe(view: TopologyInferenceView, at: datetime, *, scale: float) -> TopologyInferenceView:
    return view.observe_group(
        DeltaObservation(
            group_id=f"g-{at.isoformat()}",
            deltas={"parent_a": (50.0, 1.0), "parent_b": (50.0, 1.0), "child": (100.0, 1.0)},
            at=at,
            cause_id=f"cause-{at.isoformat()}",
        ),
        scales={"child": scale},
        observed={
            "parent_a": (1.0, 50.0),
            "parent_b": (1.0, 50.0),
            "child": (1.0, 100.0),
        },
    )


def test_without_a_factor_the_x2_child_stays_neutral() -> None:
    view = _view()
    for i in range(10):
        view = _observe(view, T0 + timedelta(minutes=i), scale=1.0)
    edge = view.edge("parent_a", "child")
    assert edge is None or (edge.evidence.support == 0 and edge.evidence.conflict == 0)


def test_with_a_monitor_factor_the_child_gains_support_and_confirms() -> None:
    view = _view()
    registry = FactorRegistry(
        [MeasurementFactor(ct_id="child", multiplier=2.0, effective_from=T0, applied_by="monitor")]
    )
    step_times = [T0 + timedelta(minutes=i) for i in range(10)]
    for at in step_times:
        resolution = registry.resolve("child", at)
        assert resolution.reported_multiplier == 2.0
        view = _observe(view, at, scale=1.0 / resolution.factor.multiplier)  # type: ignore[union-attr]
    edge = view.edge("parent_a", "child")
    assert edge is not None
    assert edge.evidence.support >= 8
    assert edge.evidence.conflict == 0
    from wattmosaic_engine.contracts import TopologyEdgeState

    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL


def test_an_expired_factor_does_not_apply() -> None:
    registry = FactorRegistry(
        [
            MeasurementFactor(
                ct_id="child",
                multiplier=2.0,
                effective_from=T0,
                effective_to=T0 + timedelta(minutes=5),
                applied_by="monitor",
            )
        ]
    )
    # A step after the interval ended has no factor: scale is 1.0, so the x2
    # child stays neutral.
    at = T0 + timedelta(minutes=6)
    assert registry.factor_at("child", at) is None
    view = _view()
    view = _observe(view, at, scale=1.0)
    edge = view.edge("parent_a", "child")
    assert edge is None or edge.evidence.support == 0


def test_a_user_applied_factor_is_not_scaled_in_the_fold() -> None:
    # Engine-applied (user) multipliers were already removed in P1 normalize;
    # the fold must not scale them again.  A registry resolve reports no reported
    # multiplier for a user factor, so no scale is passed.
    registry = FactorRegistry(
        [MeasurementFactor(ct_id="child", multiplier=2.0, effective_from=T0, applied_by="user")]
    )
    resolution = registry.resolve("child", T0 + timedelta(minutes=1))
    assert resolution.reported_multiplier == 1.0
    assert resolution.applied_multiplier == 2.0
