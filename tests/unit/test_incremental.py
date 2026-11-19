"""L5 — per-class incremental learners (design §9, analysis F5).

The incremental updaters must agree with an equivalent batch fit within 2 %,
must never fabricate a zero for "no data" (HB §18), and must be pure: no DB and
no wall clock.  The P² quantile estimator is checked against the exact median.
"""

from __future__ import annotations

import math
import random
import statistics
from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    DynamicSpec,
    EvidenceRef,
    FixedSpec,
    IdentifiabilityStatus,
    LearningObservation,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    ResponseKernelSpec,
)
from wattmosaic_engine.learning.incremental import (
    P2Quantile,
    RobustStats,
    fit_batch,
    fit_incremental,
    predict_spec,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _observation(
    *,
    index: int,
    delta_w: float,
    group: str | None = None,
    transition: str = "active",
    control_value: float | None = None,
    state: str | None = None,
    duration_s: float | None = None,
    delay_s: float | None = None,
    amplitude_w: float | None = None,
) -> LearningObservation:
    attributes: dict[str, object] = {}
    if control_value is not None:
        attributes["control_value"] = control_value
    if state is not None:
        attributes["state"] = state
    if duration_s is not None:
        attributes["duration_s"] = duration_s
    if delay_s is not None:
        attributes["delay_s"] = delay_s
    if amplitude_w is not None:
        attributes["amplitude_w"] = amplitude_w
    return LearningObservation(
        observation_id=f"obs-{index:04d}",
        subject_kind="device",
        subject_id="dev-a",
        endpoint_id="ct-row",
        event_group_ref=EvidenceRef(kind="event_group", id=group or f"grp-{index:04d}"),
        transition=transition,
        attributes=attributes,
        delta_w=delta_w,
        level_w=None,
        status="confirmed",
        rejection_reason=None,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        basis=ModelBasis.DELTA,
        created_at=T0,
    )


def test_p2_quantile_tracks_the_exact_median() -> None:
    rng = random.Random(7)
    values = [rng.gauss(60.0, 0.5) for _ in range(2000)]
    estimator = P2Quantile(0.5)
    for value in values:
        estimator.add(value)
    assert estimator.count == len(values)
    assert estimator.median() == pytest.approx(statistics.median(values), rel=0.02)


def test_robust_stats_windowed_median_and_mad_match_batch() -> None:
    values = [59.7 + (i % 7) * 0.1 for i in range(60)]
    stats = RobustStats()
    for value in values:
        stats.add(value)
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])

    assert stats.median() == pytest.approx(med, rel=1e-9)
    assert stats.mad() == pytest.approx(mad, rel=1e-9)
    assert stats.count == len(values)


def test_robust_stats_absence_is_none_not_zero() -> None:
    stats = RobustStats()
    assert stats.median() is None
    assert stats.mad() is None
    assert stats.count == 0


def test_incremental_fixed_matches_batch_within_two_percent() -> None:
    values = [60.0 + ((i % 9) - 4) * 0.1 for i in range(90)]
    observations = [_observation(index=i, delta_w=v) for i, v in enumerate(values)]

    incremental = fit_incremental(observations, ModelClass.FIXED)
    batch = fit_batch(observations, ModelClass.FIXED)
    assert isinstance(incremental, FixedSpec)
    assert isinstance(batch, FixedSpec)

    inc = incremental.levels[PowerLevelV3.ACTIVE].median_w
    bat = batch.levels[PowerLevelV3.ACTIVE].median_w
    assert inc is not None and bat is not None
    assert inc == pytest.approx(bat, rel=0.02)
    assert incremental.levels[PowerLevelV3.ACTIVE].mad_w == pytest.approx(
        batch.levels[PowerLevelV3.ACTIVE].mad_w, rel=0.02
    )


def test_incremental_adjustable_matches_batch_within_two_percent() -> None:
    observations = [
        _observation(
            index=i,
            delta_w=20.0 + 8.0 * (i / 49.0),
            control_value=round(i / 49.0, 4),
        )
        for i in range(50)
    ]

    incremental = fit_incremental(observations, ModelClass.ADJUSTABLE)
    batch = fit_batch(observations, ModelClass.ADJUSTABLE)
    assert isinstance(incremental, AdjustableSpec)
    assert isinstance(batch, AdjustableSpec)

    assert len(incremental.knots_x) == len(batch.knots_x)
    for ix, bx, iw, bw in zip(
        incremental.knots_x, batch.knots_x, incremental.knots_w, batch.knots_w, strict=True
    ):
        assert ix == pytest.approx(bx, rel=1e-9)
        assert iw == pytest.approx(bw, rel=0.02)


def test_incremental_dynamic_running_state_distribution() -> None:
    observations = []
    for i in range(60):
        state = "compressor" if i % 2 == 0 else "fan"
        power = 120.0 if state == "compressor" else 40.0
        observations.append(
            _observation(
                index=i,
                delta_w=power + ((i % 3) - 1) * 0.5,
                state=state,
                transition=state,
                duration_s=300.0 + i,
            )
        )

    incremental = fit_incremental(observations, ModelClass.DYNAMIC)
    batch = fit_batch(observations, ModelClass.DYNAMIC)
    assert isinstance(incremental, DynamicSpec)
    assert isinstance(batch, DynamicSpec)

    for state in ("compressor", "fan"):
        inc = incremental.states[state].median_w
        bat = batch.states[state].median_w
        assert inc is not None and bat is not None
        assert inc == pytest.approx(bat, rel=0.02)
    assert incremental.transition_counts["compressor"]["fan"] >= 1


def test_incremental_response_kernel_robust_phase_stats() -> None:
    observations = [
        _observation(
            index=i,
            delta_w=150.0 + ((i % 5) - 2) * 0.5,
            transition="on",
            delay_s=2.0 + ((i % 3) - 1) * 0.1,
            amplitude_w=150.0 + ((i % 5) - 2) * 0.5,
        )
        for i in range(40)
    ]

    incremental = fit_incremental(observations, ModelClass.RESPONSE_KERNEL)
    batch = fit_batch(observations, ModelClass.RESPONSE_KERNEL)
    assert isinstance(incremental, ResponseKernelSpec)
    assert isinstance(batch, ResponseKernelSpec)

    inc_phases = {p.phase: p for p in incremental.by_transition["on"]}
    bat_phases = {p.phase: p for p in batch.by_transition["on"]}
    assert set(inc_phases) == {"startup", "active", "steady", "shutdown", "standby"}
    assert inc_phases["startup"].delay_s.median_w == pytest.approx(
        bat_phases["startup"].delay_s.median_w, rel=0.02
    )
    assert inc_phases["active"].amplitude_w.median_w == pytest.approx(
        bat_phases["active"].amplitude_w.median_w, rel=0.02
    )


def test_predict_spec_fixed_returns_level_or_none() -> None:
    spec = FixedSpec(
        levels={PowerLevelV3.ACTIVE: wattmosaic_level_stat(60.0, 0.2, 10)},
    )
    obs = _observation(index=0, delta_w=60.0, transition="on")
    assert predict_spec(spec, obs) == pytest.approx(60.0)

    many = FixedSpec(
        levels={
            PowerLevelV3.ACTIVE: wattmosaic_level_stat(60.0, 0.2, 10),
            PowerLevelV3.STANDBY: wattmosaic_level_stat(2.0, 0.1, 10),
        }
    )
    assert predict_spec(many, _observation(index=1, delta_w=0.0, transition="unknown")) is None


def wattmosaic_level_stat(median_w: float, mad_w: float, n: int):  # type: ignore[no-untyped-def]
    from wattmosaic_engine.contracts import LevelStat

    return LevelStat(median_w=median_w, mad_w=mad_w, n=n)


def test_fit_incremental_is_order_independent_and_deterministic() -> None:
    observations = [
        _observation(index=i, delta_w=60.0 + ((i % 5) - 2) * 0.2) for i in range(30)
    ]
    first = fit_incremental(observations, ModelClass.FIXED)
    shuffled = list(reversed(observations))
    second = fit_incremental(shuffled, ModelClass.FIXED)
    assert first.model_dump() == second.model_dump()


def test_merge_equals_single_pass() -> None:
    from wattmosaic_engine.learning.incremental import IncrementalAccumulator

    observations = [_observation(index=i, delta_w=60.0 + (i % 4) * 0.1) for i in range(40)]
    one_pass = IncrementalAccumulator(ModelClass.FIXED)
    for obs in observations:
        one_pass.update(obs)

    left = IncrementalAccumulator(ModelClass.FIXED)
    right = IncrementalAccumulator(ModelClass.FIXED)
    for obs in observations[:20]:
        left.update(obs)
    for obs in observations[20:]:
        right.update(obs)
    merged = left.merge(right)

    assert math.isclose(
        one_pass.spec().levels[PowerLevelV3.ACTIVE].median_w or 0.0,
        merged.spec().levels[PowerLevelV3.ACTIVE].median_w or 0.0,
    )
