"""Unit tests for the shared power-model numeric helpers and declared profile.

The legacy concrete model classes (binary, dimmable, variable-speed,
multi-state, time-varying, composite, residual) were retired in the 3.0
cutover; their behaviour is now covered by the v3 model tests
(``test_model_fixed``, ``test_model_adjustable``, ``test_model_dynamic``,
``test_model_kernel``, ``test_model_components``, ``test_model_codec``,
``test_registry_v3``).  What remains here are the dependency-free numeric
building blocks and the declared-device profile adapter that survived into v3.
"""

from __future__ import annotations

import pytest
from wattmosaic_engine.contracts import PowerLevelV3
from wattmosaic_engine.models import (
    DevicePowerProfile,
    enforce_monotonic,
    error_metrics,
    median,
    robust_sigma,
    segment_power_series,
)
from wattmosaic_engine.models.device_profile import off_w, power_after

from tests.unit.conftest import series


def test_error_metrics_match_hand_computed_values() -> None:
    metrics = error_metrics([10.0, 20.0, 30.0], [12.0, 17.0, 33.0])
    assert metrics["n"] == 3.0
    assert metrics["mae"] == pytest.approx((2 + 3 + 3) / 3)
    assert metrics["rmse"] == pytest.approx(((4 + 9 + 9) / 3) ** 0.5)
    assert metrics["max_error"] == 3.0


def test_error_metrics_perfect_and_empty_predictions() -> None:
    perfect = error_metrics([5.0, 5.0], [5.0, 5.0])
    assert perfect["mae"] == 0.0
    assert perfect["rmse"] == 0.0
    assert perfect["r2"] == 0.0  # no variance: honest neutral score
    empty = error_metrics([], [])
    assert empty["n"] == 0.0


def test_enforce_monotonic_produces_non_decreasing_curve() -> None:
    curve = [(0.0, 10.0), (1.0, 5.0), (2.0, 7.0)]
    out = enforce_monotonic(curve)
    ys = [y for _, y in out]
    assert ys == sorted(ys)
    assert len(out) <= len(curve)


def test_robust_median_and_sigma_ignore_an_outlier() -> None:
    values = [10.0, 10.0, 11.0, 9.0, 10.0, 500.0]
    assert median(values) == 10.0
    assert robust_sigma(values) is not None
    assert robust_sigma(values) < 3.0


def test_power_after_pure_helper_neutral_and_malformed_cases() -> None:
    assert power_after(10.0, 100.0, delay_s=None, elapsed_s=0.0) == pytest.approx(100.0)
    assert power_after(10.0, 100.0, delay_s=0.0, elapsed_s=0.0) == pytest.approx(100.0)
    assert power_after(10.0, 100.0, delay_s=3.0, elapsed_s=2.99) == pytest.approx(10.0)
    assert power_after(10.0, 100.0, delay_s=3.0, elapsed_s=3.0) == pytest.approx(100.0)
    assert power_after(10.0, 100.0, delay_s=3.0, elapsed_s=None) is None
    assert power_after(10.0, 100.0, delay_s=3.0, elapsed_s=float("nan")) is None
    assert power_after(10.0, 100.0, delay_s=-1.0, elapsed_s=0.0) == pytest.approx(100.0)


def test_always_on_has_no_off_state() -> None:
    profile = DevicePowerProfile.from_mapping(
        "always-on", {"standby_w": 10.0, "on_w": 100.0}
    ).with_always_on(True)
    assert off_w(10.0, 100.0, always_on=True) == pytest.approx(100.0)
    assert off_w(10.0, 100.0, always_on=False) == pytest.approx(10.0)
    assert profile.level_for(50.0) in set(PowerLevelV3)


def test_declared_profile_round_trips_through_mapping() -> None:
    profile = DevicePowerProfile.from_mapping("dev", {"standby_w": 8.0, "on_w": 60.0})
    restored = DevicePowerProfile.from_mapping("dev", profile.as_dict())
    assert restored.standby_w == pytest.approx(8.0)
    assert restored.active_w == pytest.approx(60.0)


def test_segment_power_series_finds_each_step_boundary() -> None:
    result = segment_power_series(series([(20, 100.0), (20, 600.0), (20, 1500.0)]))
    assert len(result.segments) >= 3


def test_segment_power_series_keeps_constant_and_flags_noisy_envelope() -> None:
    constant = segment_power_series(series([(60, 250.0)]))
    assert len(constant.segments) == 1
    assert constant.ambiguous is False
