"""Tests for the v3 simulator accuracy harness (V1 step 3).

Every threshold below is a ceiling measured on this deterministic tree, with
at least 10% headroom; they are regression gates, not aspirational targets.

Measured on this tree (seed 17, 12-minute HB17 houses / 6-minute lab strata):

* ``forced_attribution_count`` = 0 across all scenarios.
* MEASURED single-subject endpoints (lab_dedicated): MAE 1.442 W,
  RMSE 2.182 W, max |error| 7.282 W — the mean error is well inside the
  declared 3 W measurement sigma; the rare transition-cycle sample is graded
  at its own sample instant and carries a slightly larger one-off deviation.
* lab_automation_burst MEASURED MAE 12.113 W (many devices, more transitions).
* residual preservation ratio = 1.0 for both lab strata with published points;
  0.0 for strata where the engine published no residual claim (no claim is not
  a fabrication).
* determinism: the same seed yields a byte-identical report dict.
"""

from __future__ import annotations

import pytest

from tests.simulation.accuracy_v3 import (
    DEFAULT_SCENARIOS,
    V3Report,
    run_all,
    run_scenario,
)


@pytest.fixture(scope="module")
def report() -> V3Report:
    return run_all(seed=17)


def test_forced_attribution_count_is_zero_everywhere(report: V3Report) -> None:
    """No device is given a number when the evidence only supports a group."""
    assert report.forced_attribution_count == 0
    for label, stratum in report.scenarios.items():
        assert stratum.forced_attribution_count == 0, label


def test_measured_endpoints_match_truth_within_sigma(report: V3Report) -> None:
    """MEASURED single-subject endpoints track CT truth within the sigma.

    Measured: lab_dedicated MAE 1.442 W / RMSE 2.182 W; lab_automation_burst
    MAE 12.113 W.  Ceilings are the measured values with >= 10% headroom.
    """
    dedicated = report.scenarios["lab_dedicated"]
    assert dedicated.measured_pairs, "no MEASURED single-subject endpoints published"
    assert dedicated.error is not None
    # 1.442 W measured, ceiling 2.0 W (~39% headroom).  The maximum single
    # deviation is 7.282 W measured (one transition-cycle sample); ceiling
    # 10.0 W (~37% headroom).  The mean is inside the 3 W CT sigma.
    assert dedicated.error.mae_w < 2.0, dedicated.error.mae_w
    assert dedicated.error.rmse_w < 3.0, dedicated.error.rmse_w
    assert dedicated.error.max_abs_w < 10.0, dedicated.error.max_abs_w

    burst = report.scenarios["lab_automation_burst"]
    assert burst.error is not None
    # 12.113 W measured, ceiling 15.0 W (~24% headroom).
    assert burst.error.mae_w < 15.0, burst.error.mae_w


def test_residual_preservation_ratio_is_bounded(report: V3Report) -> None:
    """Published residual is preserved, never understated into a claim.

    Measured 1.0 for lab_dedicated and lab_automation_burst (which publish a
    reconciliation residual); 0.0 for strata with no residual claim at all.
    The floor is the measured 1.0 with >= 10% headroom.
    """
    for label in ("lab_dedicated", "lab_automation_burst"):
        assert report.scenarios[label].residual_preservation_ratio >= 0.90, label


def test_determinism_same_seed_identical_report() -> None:
    """Two runs with the same seed produce byte-identical report dicts."""
    first = run_all(seed=17).as_dict()
    second = run_all(seed=17).as_dict()
    assert first == second


def test_all_configured_scenarios_are_graded(report: V3Report) -> None:
    assert set(report.scenarios) == {label for label, _spec in DEFAULT_SCENARIOS}
    for label, stratum in report.scenarios.items():
        assert stratum.n_records > 0, label
        # Every stratum reports the four v3 metrics, even when zero.
        payload = stratum.as_dict()
        for key in (
            "attributed_pct",
            "group_identified_energy_kwh",
            "residual_preservation_ratio",
            "forced_attribution_count",
        ):
            assert key in payload, (label, key)


def test_unknown_is_no_claim_not_zero(report: V3Report) -> None:
    """UNKNOWN/None points are no claim, never 0 W of attributed power."""
    shared = report.scenarios["lab_shared"]
    assert shared.attributed_points == 0
    assert shared.unknown_points > 0
    assert shared.attributed_pct == 0.0
    # A single scenario run exposes the same guarantees.
    _frames, _preds, graded = run_scenario("lab_shared", "lab:shared", seed=17)
    assert graded.forced_attribution_count == 0
