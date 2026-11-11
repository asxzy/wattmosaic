"""The energy metric must integrate, not multiply a mean by a duration.

Why this file exists
--------------------
``ErrorStats.energy_error_kwh`` was originally ``mean(pred - truth) * duration``.
That is only correct when the error is constant in time. On the loads this
product actually cares about — a water heater or an AC compressor that cycles on
and off — it is not: a predictor can be *exactly* right on average and still move
the wrong number of kilowatt-hours, and one can be badly wrong on average while
moving the right amount.

So the coarse estimate cannot be used to catch an energy bug, and an
integral-based metric cannot be tested by asserting "the oracle is near zero":
a broken implementation that returns ``0.0`` for every device would pass that
too. These tests pin the integral against **hand-computed** trapezoid values on a
non-uniform time grid, and against the *opposite* failure — a predictor whose
mean error is exactly zero while its energy error is not.

Measured reference numbers (hand-computed, not observed):

* ``truth`` 0/0/1000/1000/0 W at 0/60/300/420/900 s integrates to
  ``0.1333333`` kWh.
* ``pred`` 0/500/0/2000/0 W at the same instants integrates to ``0.1875`` kWh.
* therefore the signed integral error is ``0.0541667`` kWh.
* the same samples scored by a left rule give ``+0.3541667`` and by a right rule
  ``+0.0208333`` kWh, so using either as the "integral" fails by >0.03 kWh.
"""

from __future__ import annotations

import pytest

from tests.simulation.metrics import (
    error_stats,
    evaluate,
    trapezoid_energy_kwh,
)
from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel

#: Hand-built, deliberately non-uniform: 1 min, then 4 min, then 2 min, then 8 min.
TIMES_S = [0.0, 60.0, 300.0, 420.0, 900.0]
TRUTH_W = [0.0, 0.0, 1000.0, 1000.0, 0.0]
PRED_W = [0.0, 500.0, 0.0, 2000.0, 0.0]

TRUTH_KWH = 0.13333333333333333
PRED_KWH = 0.1875
INTEGRAL_ERROR_KWH = PRED_KWH - TRUTH_KWH
#: Same samples, wrong quadrature rules.  Both are plausible implementations.
LEFT_RULE_ERROR_KWH = 0.3541666666666667
RIGHT_RULE_ERROR_KWH = 0.020833333333333332


def _timed() -> list[tuple[float, float, float]]:
    return list(zip(TIMES_S, TRUTH_W, PRED_W, strict=True))


# --------------------------------------------------------------------------- #
# The primitive
# --------------------------------------------------------------------------- #


def test_trapezoid_of_a_constant_is_power_times_time() -> None:
    """A 1 kW load held for an hour is 1 kWh, whatever the sample count."""
    fine = [(t, 1000.0) for t in range(0, 3601, 10)]
    coarse = [(0.0, 1000.0), (3600.0, 1000.0)]
    assert trapezoid_energy_kwh(fine) == pytest.approx(1.0, abs=1e-9)
    assert trapezoid_energy_kwh(coarse) == pytest.approx(1.0, abs=1e-12)


def test_trapezoid_of_a_linear_ramp_is_exact() -> None:
    """Trapezoid is exact on piecewise-linear signals, so an oracle cannot drift."""
    ramp = [(t, t / 3.6) for t in (0.0, 900.0, 3600.0)]  # 0 -> 1000 W over an hour
    assert trapezoid_energy_kwh(ramp) == pytest.approx(0.5, abs=1e-12)


def test_fewer_than_two_samples_integrates_to_zero() -> None:
    """No interval means no energy: ``0.0``, not an extrapolation."""
    assert trapezoid_energy_kwh([]) == 0.0
    assert trapezoid_energy_kwh([(0.0, 5000.0)]) == 0.0


def test_trapezoid_is_order_independent() -> None:
    """A capture can arrive out of order; the integral must not care."""
    forward = trapezoid_energy_kwh(list(zip(TIMES_S, PRED_W, strict=True)))
    backward = trapezoid_energy_kwh(
        list(zip(reversed(TIMES_S), reversed(PRED_W), strict=True))
    )
    assert forward == pytest.approx(backward, abs=1e-12)


# --------------------------------------------------------------------------- #
# The metric
# --------------------------------------------------------------------------- #


def test_integral_energy_error_is_the_trapezoid_difference() -> None:
    st = error_stats(
        list(zip(TRUTH_W, PRED_W, strict=True)),
        duration_s=TIMES_S[-1],
        timed=_timed(),
    )
    assert st.energy_error_kwh_integral == pytest.approx(INTEGRAL_ERROR_KWH, abs=1e-12)
    assert st.energy_error_ratio_integral == pytest.approx(
        INTEGRAL_ERROR_KWH / TRUTH_KWH, abs=1e-9
    )
    assert "energy_error_kwh_integral" in st.as_dict()


def test_integral_metric_rejects_the_wrong_quadrature_rules() -> None:
    """Left and right Riemann sums are the two obvious bugs. Neither may pass."""
    st = error_stats(
        list(zip(TRUTH_W, PRED_W, strict=True)),
        duration_s=TIMES_S[-1],
        timed=_timed(),
    )
    assert st.energy_error_kwh_integral != pytest.approx(LEFT_RULE_ERROR_KWH, abs=1e-4), (
        "the integral is a left Riemann sum, not the trapezoid rule"
    )
    assert st.energy_error_kwh_integral != pytest.approx(RIGHT_RULE_ERROR_KWH, abs=1e-4), (
        "the integral is a right Riemann sum, not the trapezoid rule"
    )


def test_integral_and_coarse_estimate_are_different_questions() -> None:
    """The two numbers must disagree here, and each must be individually right.

    Mean error of these samples is ``+100 W``; over 900 s that is ``0.025`` kWh.
    The true energy error is more than twice that. Replacing one with the other
    is exactly the bug this file guards.
    """
    st = error_stats(
        list(zip(TRUTH_W, PRED_W, strict=True)),
        duration_s=TIMES_S[-1],
        timed=_timed(),
    )
    assert st.mean_bias_w == pytest.approx(100.0, abs=1e-12)
    assert st.energy_error_kwh == pytest.approx(0.025, abs=1e-12)
    assert st.energy_error_kwh != pytest.approx(st.energy_error_kwh_integral, abs=1e-3)


def test_truth_energy_kwh_overrides_the_recomputed_curve() -> None:
    """The simulator integrates its own truth; use that, don't re-derive it.

    ``device_energy_kwh`` is accumulated on the simulation clock, which has finer
    resolution than the record cadence. Passing it in must change the result, and
    omitting it must fall back to the samples supplied in ``timed``.
    """
    from_grid = error_stats(
        list(zip(TRUTH_W, PRED_W, strict=True)), duration_s=900.0, timed=_timed()
    )
    from_simulator = error_stats(
        list(zip(TRUTH_W, PRED_W, strict=True)),
        duration_s=900.0,
        timed=_timed(),
        truth_energy_kwh=2.0,
    )
    assert from_grid.energy_error_kwh_integral == pytest.approx(
        INTEGRAL_ERROR_KWH, abs=1e-12
    )
    assert from_simulator.energy_error_kwh_integral == pytest.approx(
        PRED_KWH - 2.0, abs=1e-12
    )


# --------------------------------------------------------------------------- #
# End-to-end: the failure mode the coarse metric cannot see
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def spiky_records():
    """Two hours of a 6 kW heater cycling on and off, sampled every two minutes."""
    cfg = SimulatorConfig(
        seed=202, minutes=120, interval=120, quality=QualityLevel.GOOD
    )
    return list(HouseholdScenario(cfg, topology=REAL_HOME_TOPOLOGY).records())


def _window_mean_predictor(records):
    """Return the device's *window mean*, constant across every record.

    Its mean signed error is zero by construction, so ``energy_error_kwh`` is
    exactly zero. The energy it claims is not right, because a mean over a
    cycled load says nothing about the duty cycle at either end of the window.
    """
    truth = [r["ground_truth"]["devices"]["water_heater"]["power_w"] for r in records]
    mean = sum(truth) / len(truth)

    def predict(record: dict) -> dict:
        main = float(record["ground_truth"]["main_total_w"])
        return {
            "devices": {
                "water_heater": {
                    "power_w": mean,
                    "source": "inferred",
                    "confidence": 0.5,
                }
            },
            "reconciliation": {"main_power_w": main, "unknown_residual_w": main},
        }

    return predict


def test_oracle_integrates_to_the_simulators_own_curve(spiky_records) -> None:
    """Guard rail: the integral path is consistent with ``device_energy_kwh``.

    The oracle reproduces every sample, so it must integrate to the simulator's
    own accumulated energy. Small non-zero is expected (the simulator integrates
    on a 5 s grid, the records land on the cadence), but a misplaced time base
    would be orders of magnitude larger.
    """
    from tests.simulation.metrics import oracle_predictor

    report = evaluate(spiky_records, oracle_predictor)
    for dev_id in ("water_heater", "air_conditioner", "vehicle_charger"):
        st = report.per_device[dev_id]
        assert st.energy_error_kwh_integral == pytest.approx(0.0, abs=1e-5), (
            dev_id,
            st.energy_error_kwh_integral,
        )


def test_zero_mean_error_can_still_be_wrong_energy(spiky_records) -> None:
    """The test the coarse metric cannot pass, and the integral must fail.

    Measured: ``energy_error_kwh`` is 0.0 to 1e-12 while
    ``energy_error_kwh_integral`` is ~0.010 kWh on a 6 kWh/day-class load. The
    assertion is on the *sign of the disagreement*, not on the exact magnitude,
    so it does not move when the scenario seed or cadence changes.
    """
    report = evaluate(spiky_records, _window_mean_predictor(spiky_records))
    st = report.per_device["water_heater"]
    assert st.n == 60
    assert st.energy_error_kwh == pytest.approx(0.0, abs=1e-9), (
        "the coarse metric is expected to be blind to this predictor; if it is "
        "not, the fixture no longer demonstrates the defect"
    )
    assert abs(st.energy_error_kwh_integral) > 5e-3, (
        "a predictor with zero mean error was scored as zero energy error: the "
        "integral metric is probably mean*duration, not an integral "
        f"({st.energy_error_kwh_integral})"
    )
    assert st.energy_error_ratio_integral > 1e-3
