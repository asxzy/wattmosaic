"""I2: temporal response kernels (HB §7.3).

Pure-function tests: no DB, no wall clock.  Model numbers come from K08 fakes;
declared timing comes from the shipped ``DevicePowerProfile``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import CandidateHypothesis, CandidateSource, DeltaPrior
from wattmosaic_engine.inference.temporal import (
    expected_response,
    onset_offsets,
    unit_step,
)
from wattmosaic_engine.models.device_profile import DevicePowerProfile
from wattmosaic_engine.runtime.fakes import FakeModelSet

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _candidate(subject_id: str = "dev") -> CandidateHypothesis:
    return CandidateHypothesis(
        candidate_id=f"cand::{subject_id}",
        subject_kind="device",
        subject_id=subject_id,
        sources=[CandidateSource.HA_STATE],
        transition="on",
        evidence_terms={},
    )


def _prior(mean: float, sigma: float = 5.0) -> DeltaPrior:
    return DeltaPrior(mean_w=mean, sigma_w=sigma, sign="pos", model_version_id="mdl-1")


# --------------------------------------------------------------------------- #
# Unit step / onset grid helpers.
# --------------------------------------------------------------------------- #


def test_unit_step_is_zero_before_and_one_at_delay() -> None:
    tau = np.array([-1.0, 0.0, 5.0, 6.0])
    np.testing.assert_array_equal(unit_step(tau, 5.0), np.array([0.0, 0.0, 1.0, 1.0]))


def test_onset_offsets_collapse_to_exact_zero_without_uncertainty() -> None:
    np.testing.assert_array_equal(onset_offsets(0.0), np.zeros(1))
    np.testing.assert_array_equal(onset_offsets(None), np.zeros(1))


def test_onset_offsets_are_symmetric_within_uncertainty() -> None:
    grid = onset_offsets(3.0, points=5)
    assert grid[0] == pytest.approx(-3.0)
    assert grid[-1] == pytest.approx(3.0)
    assert grid[len(grid) // 2] == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# expected_response priority: L3 kernel > declared delay > instantaneous.
# --------------------------------------------------------------------------- #


def test_kernel_response_is_used_when_available() -> None:
    grid = np.linspace(0.0, 120.0, 13)
    mean = np.full(grid.shape, 100.0)
    sigma = np.full(grid.shape, 10.0)
    models = FakeModelSet(
        kernels={"dev": (mean, sigma)},
        priors={"dev": _prior(50.0)},
    )
    response = expected_response(_candidate(), "on", {}, grid, models=models)
    assert response.source == "kernel"
    np.testing.assert_allclose(response.mean_w, mean)
    np.testing.assert_allclose(response.sigma_w, sigma)


def test_declared_delay_profile_produces_a_delayed_step() -> None:
    grid = np.linspace(0.0, 120.0, 25)
    models = FakeModelSet.fixed({"dev": _prior(150.0)})
    profile = DevicePowerProfile(device_id="dev", standby_w=0.0, active_w=150.0).with_response_delay(
        90.0
    )
    response = expected_response(
        _candidate(), "on", {}, grid, models=models, profile=profile
    )
    assert response.source == "declared_delay"
    assert response.delay_s == pytest.approx(90.0)
    assert response.amplitude_w == pytest.approx(150.0)
    before, before_sigma = response.evaluate(np.array([0.0, 30.0, 89.9]))
    after, after_sigma = response.evaluate(np.array([90.0, 120.0]))
    np.testing.assert_array_equal(before, np.zeros(3))
    np.testing.assert_array_equal(after, np.full(2, 150.0))
    assert before_sigma is not None and after_sigma is not None


def test_declared_delay_in_attrs_is_used_when_no_profile() -> None:
    grid = np.linspace(0.0, 60.0, 13)
    models = FakeModelSet.fixed({"dev": _prior(40.0)})
    response = expected_response(
        _candidate(), "on", {"response_delay_s": 20.0}, grid, models=models
    )
    assert response.source == "declared_delay"
    assert response.delay_s == pytest.approx(20.0)


def test_no_declaration_produces_an_instantaneous_step() -> None:
    grid = np.linspace(0.0, 60.0, 13)
    models = FakeModelSet.fixed({"dev": _prior(60.0)})
    response = expected_response(_candidate(), "on", {}, grid, models=models)
    assert response.source == "instantaneous"
    assert response.delay_s == pytest.approx(0.0)
    before, _ = response.evaluate(np.array([0.0]))
    np.testing.assert_array_equal(before, np.array([60.0]))


def test_missing_model_and_prior_yield_unknown_not_zero() -> None:
    grid = np.linspace(0.0, 60.0, 13)
    models = FakeModelSet()
    response = expected_response(_candidate(), "on", {}, grid, models=models)
    assert response.source == "unknown"
    assert response.mean_w is None
    mean, sigma = response.evaluate(np.array([0.0, 10.0]))
    assert mean is None and sigma is None


def test_response_handles_onset_offsets() -> None:
    grid = np.linspace(0.0, 120.0, 25)
    models = FakeModelSet.fixed({"dev": _prior(150.0)})
    profile = DevicePowerProfile(device_id="dev").with_response_delay(90.0)
    response = expected_response(
        _candidate(), "on", {}, grid, models=models, profile=profile
    )
    # A +5 s offset means the step is observed at t=95 s.
    before, _ = response.evaluate(np.array([94.0]), onset_offset_s=5.0)
    after, _ = response.evaluate(np.array([95.0]), onset_offset_s=5.0)
    np.testing.assert_array_equal(before, np.zeros(1))
    np.testing.assert_array_equal(after, np.full(1, 150.0))


def test_response_is_pure_and_deterministic() -> None:
    grid = np.linspace(0.0, 60.0, 13)
    models = FakeModelSet.fixed({"dev": _prior(60.0)})
    a = expected_response(_candidate(), "on", {}, grid, models=models)
    b = expected_response(_candidate(), "on", {}, grid, models=models)
    np.testing.assert_array_equal(a.mean_w, b.mean_w)
    assert a.delay_s == b.delay_s


def test_grid_times_are_monotonic_even_for_unsorted_input() -> None:
    grid = np.array([60.0, 0.0, 30.0])
    models = FakeModelSet.fixed({"dev": _prior(60.0)})
    response = expected_response(_candidate(), "on", {}, grid, models=models)
    np.testing.assert_array_equal(response.tau_s, np.array([0.0, 30.0, 60.0]))
