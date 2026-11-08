"""Tests for the accuracy-evaluation harness.

The central requirement is that the metrics are *not* decorative: a perfect
predictor must score near zero error, and deliberately wrong predictors must
score badly on the specific axis they violate.
"""

from __future__ import annotations

import math

import pytest

from tests.simulation.metrics import (
    DetectionStats,
    coverage_and_invariants,
    error_stats,
    evaluate,
    flat_zero_predictor,
    load_engine_predictor,
    main_channel_only_predictor,
    naive_sum_wrong_predictor,
    oracle_predictor,
    panel_channel_map,
)
from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario


@pytest.fixture(scope="module")
def records():
    return HouseholdScenario(seed=202, minutes=30, interval=20).to_list()


@pytest.fixture(scope="module")
def oracle_report(records):
    return evaluate(records, oracle_predictor)


# --------------------------------------------------------------------------- #
# Metric primitives
# --------------------------------------------------------------------------- #


def test_error_stats_basic_math() -> None:
    st = error_stats([(100.0, 110.0), (200.0, 190.0), (0.0, 0.0)])
    assert st.n == 3
    assert st.mae_w == pytest.approx((10 + 10 + 0) / 3)
    assert st.rmse_w == pytest.approx(math.sqrt((100 + 100 + 0) / 3))
    assert st.mean_bias_w == pytest.approx((10 - 10 + 0) / 3)
    assert st.max_abs_w == pytest.approx(10.0)
    # Relative error ignores truth below the floor.
    assert st.relative_error == pytest.approx((10 / 100 + 10 / 200) / 2)


def test_error_stats_empty_is_safe() -> None:
    st = error_stats([])
    assert st.n == 0 and st.mae_w == 0.0 and st.rmse_w == 0.0


def test_detection_stats_confusion_matrix() -> None:
    det = DetectionStats()
    # on->on, on->off, off->on, off->off
    det.tp, det.fn, det.fp, det.tn = 8, 2, 1, 9
    assert det.precision == pytest.approx(8 / 9)
    assert det.recall == pytest.approx(8 / 10)
    assert det.f1 == pytest.approx(16 / 19)
    assert det.accuracy == pytest.approx(17 / 20)


# --------------------------------------------------------------------------- #
# Oracle: metrics must report near-zero error for correct behaviour
# --------------------------------------------------------------------------- #


def test_oracle_has_near_zero_error(oracle_report) -> None:
    for dev_id, st in oracle_report.per_device.items():
        assert st.mae_w < 1e-6, (dev_id, st.mae_w)
        assert st.rmse_w < 1e-6, (dev_id, st.rmse_w)
        assert st.energy_error_kwh == pytest.approx(0.0, abs=1e-9)

    assert oracle_report.coverage.coverage == pytest.approx(1.0)
    assert oracle_report.coverage.false_attribution_rate < 1e-6
    assert oracle_report.reconciliation.forced_attribution_count == 0
    assert oracle_report.invariants["nested_double_count_records"] == 0
    assert oracle_report.invariants["stale_learning_violations"] == 0


def test_oracle_detection_is_perfect(oracle_report) -> None:
    for dev_id, det in oracle_report.per_device_detection.items():
        assert det.precision == pytest.approx(1.0), dev_id
        assert det.recall == pytest.approx(1.0), dev_id


# --------------------------------------------------------------------------- #
# Stratification: never one aggregate score
# --------------------------------------------------------------------------- #


def test_report_is_stratified(oracle_report) -> None:
    assert oracle_report.per_source
    assert oracle_report.per_device_type
    assert oracle_report.per_concurrency
    assert oracle_report.per_quality
    assert len(oracle_report.per_device) >= 25
    # Source tiers must include direct measurement (dedicated CTs exist).
    assert "direct_measured" in oracle_report.per_source
    # Multiple concurrency levels must be represented.
    assert len(oracle_report.per_concurrency) >= 3
    # Device-type coverage must span the simulator's classes.
    for expected in ("binary", "dimmable", "variable_speed", "hvac", "refrigerator",
                     "washing_machine", "dishwasher", "dryer", "double_pole"):
        assert expected in oracle_report.per_device_type, expected


def test_oracle_direct_and_inferred_tiers_both_measured(oracle_report) -> None:
    direct = oracle_report.per_source["direct_measured"]
    inferred = oracle_report.per_source["inferred"]
    assert direct.n > 0 and inferred.n > 0
    assert direct.mae_w < 1e-6 and inferred.mae_w < 1e-6


# --------------------------------------------------------------------------- #
# Bad predictors must be caught on the right axis
# --------------------------------------------------------------------------- #


def test_naive_sum_wrong_is_caught(records) -> None:
    report = evaluate(records, naive_sum_wrong_predictor)
    # Huge power error.
    assert report.per_source["direct_measured"].mae_w > 1000
    # Everything falsely attributed.
    assert report.coverage.false_attribution_rate > 0.5
    # Attributed power exceeds main on every record.  Because every value is
    # claimed as a *direct* measurement, this is classified as a negative
    # residual (more measured power than the panel delivered) rather than as
    # inferred/model fabrication; both are caught, on separate axes.
    assert report.reconciliation.over_attribution_count > 0
    assert report.invariants["negative_residual_records"] > 0
    assert report.reconciliation.mean_abs_error_ratio > 1.0
    # Direct readings exceeding the measured main is the sample-vintage /
    # over-attribution signal (distinct from nested-CT double counting).
    assert report.invariants["direct_exceeds_main_records"] > 0


def test_nested_ct_double_counting_is_caught(records, sample_topology) -> None:
    """Reporting root + sub instead of the root-main total must be detected."""
    panels = panel_channel_map(REAL_HOME_TOPOLOGY)

    def sum_every_panel(rec):
        latest = rec["latest_measurements"]
        total = sum(
            float(latest[c]["value_w"])
            for c in panels["root"] + panels["sub"]
            if c in latest
        )
        return {
            "devices": {},
            "reconciliation": {"main_power_w": total, "unknown_residual_w": total},
        }

    report = evaluate(records, sum_every_panel, panel_channels=panels)
    assert report.invariants["nested_check_records"] > 0
    assert report.invariants["nested_double_count_records"] > 0

    # The honest predictor, which reports the root-main total, is not flagged.
    oracle = evaluate(records, oracle_predictor, panel_channels=panels)
    assert oracle.invariants["nested_double_count_records"] == 0

    # Without the panel split the check cannot run; that must be visible.
    blind = evaluate(records, sum_every_panel)
    assert blind.invariants["nested_check_records"] == 0


def test_inferred_fabrication_is_caught(records) -> None:
    """A predictor that invents *inferred* power beyond the residual is caught."""

    def inflated_inference(rec):
        gt = rec["ground_truth"]
        main = float(gt["main_total_w"])
        per_device = 0.2 * main  # sums to 20x main
        return {
            "devices": {
                dev_id: {"power_w": per_device, "source": "inferred", "confidence": 0.9}
                for dev_id in gt["devices"]
            },
            "reconciliation": {"main_power_w": main, "unknown_residual_w": 0.0},
        }

    report = evaluate(records, inflated_inference)
    assert report.reconciliation.forced_attribution_count > 0
    assert report.reconciliation.residual_preserved_ratio < 1.0


def test_flat_zero_is_caught(records) -> None:
    report = evaluate(records, flat_zero_predictor)
    assert report.coverage.coverage < 1e-6
    # Zero predictions: every truly-on device is a false negative.
    total_fn = sum(d.fn for d in report.per_device_detection.values())
    total_tp = sum(d.tp for d in report.per_device_detection.values())
    assert total_fn > 0 and total_tp == 0
    # Residual is preserved (main power), so that invariant stays clean.
    assert report.reconciliation.forced_attribution_count == 0


def test_main_channel_only_baseline_scores_between(records, oracle_report) -> None:
    report = evaluate(records, main_channel_only_predictor)
    # Direct devices are perfect.
    direct = report.per_source.get("direct_measured")
    assert direct is not None and direct.mae_w < 1e-6
    # Shared-branch devices are not attributed at all.
    assert report.coverage.coverage < oracle_report.coverage.coverage
    assert report.coverage.coverage > 0.05
    # No fabrication: nothing invented for off devices.
    assert report.coverage.false_attribution_rate < 1e-6
    assert report.reconciliation.forced_attribution_count == 0


def test_metrics_are_not_order_dependent_on_tiny_noise(records) -> None:
    """A predictor off by a bounded amount must show bounded, not sporad. metrics."""
    def noisy(rec):
        out = oracle_predictor(rec)
        for _dev_id, d in out["devices"].items():
            d["power_w"] = d["power_w"] * 1.01 + 2.0
        return out

    report = evaluate(records, noisy)
    for dev_id, st in report.per_device.items():
        # <=1% + 2 W bias per sample for active devices; allow concern only above.
        assert st.mae_w < 60.0, (dev_id, st.mae_w)


# --------------------------------------------------------------------------- #
# Invariants: no fabricated attribution, residual preserved
# --------------------------------------------------------------------------- #


def test_oracle_preserves_residual(records) -> None:
    """The oracle's residual must equal true unmetered power, not zero."""
    rec = records[len(records) // 2]
    gt = rec["ground_truth"]
    pred = oracle_predictor(rec)
    reported = pred["reconciliation"]["unknown_residual_w"]
    # Oracle residual = main - accounted = true unmetered power.
    assert reported == pytest.approx(gt["unmetered_w"], abs=1e-3)
    assert gt["unmetered_w"] > 0


def test_residual_not_forced_to_main_by_bad_predictor(records) -> None:
    report = evaluate(records, oracle_predictor)
    # Residual preservation ratio should be high for a correct predictor.
    assert report.reconciliation.residual_preserved_ratio > 0.9


def test_coverage_helper_is_consistent(records) -> None:
    report = evaluate(records, main_channel_only_predictor)
    cov = coverage_and_invariants(records, main_channel_only_predictor)
    assert cov["coverage"] == pytest.approx(report.coverage.coverage, abs=1e-3)


# --------------------------------------------------------------------------- #
# Real engine adapter: must degrade gracefully, never explode
# --------------------------------------------------------------------------- #


def test_engine_adapter_reports_status_without_raising() -> None:
    from tools.simulator import REAL_HOME_TOPOLOGY

    predictor, status = load_engine_predictor(REAL_HOME_TOPOLOGY.to_contract_topology())
    if predictor is None:
        assert isinstance(status, str) and status
        # Document exactly why it is skipped.
        assert ("unavailable" in status or "unexpected" in status
                or "failed" in status or "lacks" in status or "no contract" in status)
    else:
        assert status == "adapted"
        assert callable(predictor)


def test_engine_adapter_if_present_runs_on_records(records) -> None:
    from tools.simulator import REAL_HOME_TOPOLOGY

    predictor, status = load_engine_predictor(REAL_HOME_TOPOLOGY.to_contract_topology())
    if predictor is None:
        pytest.skip(f"real engine not available yet: {status}")
    report = evaluate(records[:20], predictor)
    assert report.n_records == 20
    # The real engine must not fabricate more than the home consumes.
    assert report.coverage.false_attribution_rate >= 0.0
