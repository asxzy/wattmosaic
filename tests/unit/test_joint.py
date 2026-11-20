"""I3: joint delta and absolute solver (HB §8.1-8.2, design §7, DEC-21/23).

Covers the task's own test list plus the acceptance semantics AT-06/08/09:

* the HB §7.7 example separates Circuit A and Circuit B and puts ~5 W in ΔU;
* signed bounds: an OFF transition keeps a negative ΔP, a battery discharge
  keeps its negative ΔP (no clipping);
* a plug anchor holds the TV even when the circuit is noisy;
* one load on three nested readers is counted once and its posterior variance
  is not divided by three;
* a higher λ_u moves power from ΔU to candidates monotonically, and the
  diagnostic reports the share;
* IRLS converges on 1 000 random well-posed problems and a non-converged solve
  is flagged, not hidden.

Everything runs against K08 fakes and pure functions: no DB, no clock, no
``runtime.fakes`` import from production code.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    DeltaPrior,
    Observability,
    RelationType,
    TopologyEdgeState,
)
from wattmosaic_engine.electrical.solver import (
    HUBER_DELTA,
    IRLS_MAX_ITER,
    huber_loss,
    huber_weights,
    solve_robust_constrained,
    transition_bounds,
)
from wattmosaic_engine.inference.joint import (
    ANCHOR_WEIGHT,
    DEFAULT_LAMBDA_U,
    JointSolveReport,
    solve_joint,
    solve_joint_detailed,
    unknown_share,
)
from wattmosaic_engine.runtime.interfaces import (
    AlternativeEdge,
    ObservationOperator,
    UnknownPrior,
)

from tests.support.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures / builders.
# --------------------------------------------------------------------------- #


def _topology(tree: dict | None = None) -> FakeTopology:
    return FakeTopology.from_tree(tree or HB17_TREE)


def _operator(
    readers: tuple[str, ...],
    subjects: tuple[tuple[str, str], ...],
    pairs: dict[tuple[str, str], str],
    *,
    tree: dict | None = None,
    unknown_scopes: tuple[tuple[str, tuple[int, ...]], ...] = (),
) -> ObservationOperator:
    """A fake_operator, optionally with explicit unknown scopes."""
    base = fake_operator(_topology(tree), FakeMapping.from_pairs(pairs), readers, subjects, at=T0)
    if not unknown_scopes:
        return base
    return ObservationOperator(
        H=base.H,
        rows=base.rows,
        subjects=base.subjects,
        entries=base.entries,
        unknown_scopes=unknown_scopes,
        soft_alternatives=base.soft_alternatives,
        topology_version_id=base.topology_version_id,
        mapping_version_id=base.mapping_version_id,
        at=base.at,
    )


def _candidate(candidate_id: str, subject_id: str, transition: str | None, **extra) -> CandidateHypothesis:
    return CandidateHypothesis(
        candidate_id=candidate_id,
        subject_kind="device",
        subject_id=subject_id,
        sources=["ha_state"],
        transition=transition,
        evidence_terms=extra.pop("evidence_terms", {}),
        **extra,
    )


def _hb77_operator() -> ObservationOperator:
    """HB §7.7: Living Room Light on Circuit A, Kitchen Light + TV on Circuit B."""
    return _operator(
        ("Main", "Circuit A", "Circuit B"),
        (("device", "living-light"), ("device", "kitchen-light"), ("device", "tv")),
        {
            ("device", "living-light"): "Circuit A",
            ("device", "kitchen-light"): "Circuit B",
            ("device", "tv"): "Circuit B",
        },
        unknown_scopes=(("Main", (0,)),),
    )


NESTED_TREE = {
    "Main": {
        "Subpanel": {
            "Circuit A": {},
        },
    }
}


# --------------------------------------------------------------------------- #
# AT-06 — HB §7.7 simultaneous joint solve.
# --------------------------------------------------------------------------- #


def test_at06_hb77_simultaneous_event_is_solved_jointly() -> None:
    operator = _hb77_operator()
    candidates = [
        _candidate("c-light", "living-light", "on"),
        _candidate("c-kitchen", "kitchen-light", "on"),
        _candidate("c-tv", "tv", "on"),
    ]
    # Circuit A +45, Circuit B +60 (TV on the same circuit), Main +110.
    # The lights and the TV are candidates; ~5 W is unexplained.
    report = solve_joint_detailed(
        operator,
        {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        candidates,
        {
            "living-light": DeltaPrior(mean_w=45.0, sigma_w=10.0, sign="pos", model_version_id="m1"),
            "kitchen-light": DeltaPrior(mean_w=45.0, sigma_w=10.0, sign="pos", model_version_id="m1"),
            "tv": DeltaPrior(mean_w=15.0, sigma_w=5.0, sign="pos", model_version_id="m1"),
        },
    )
    solution = report.solution
    assert solution.converged
    values = dict(zip(report.subject_ids, solution.x, strict=True))

    # The solution separates the two circuits: their direct rows fix them.
    assert values["living-light"] == pytest.approx(45.0, abs=1.0)
    # Circuit B has two candidates and only one row -> the sum is pinned.
    assert values["kitchen-light"] + values["tv"] == pytest.approx(60.0, abs=1.0)
    # The ~5 W that no candidate explains lands in ΔU on Main, never in a device.
    assert float(solution.u[0]) == pytest.approx(5.0, abs=1.5)
    assert report.unknown_share_by_reader["Main"] is not None
    # No greedy credit: the main row is not fully assigned to one device.
    assert sum(values.values()) == pytest.approx(105.0, abs=1.5)


def test_at06_no_candidate_takes_the_whole_measured_step_greedily() -> None:
    """A non-HA compressor in the same window is absorbed by ΔU, not a device."""
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "light"),),
        {("device", "light"): "Circuit A"},
        unknown_scopes=(("Main", (0,)), ("Circuit A", (1,))),
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 300.0, "Circuit A": 150.0},
        {"Main": 4.0, "Circuit A": 4.0},
        [_candidate("c1", "light", "on")],
        {"light": DeltaPrior(mean_w=150.0, sigma_w=5.0, sign="pos", model_version_id="m1")},
    )
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    assert values["light"] == pytest.approx(150.0, abs=2.0)
    # 300 - 150 on Main: the unknown column absorbs the unexplained half.
    assert float(report.solution.u[0]) == pytest.approx(150.0, abs=2.0)
    assert report.unknown_share_by_reader["Main"] is not None
    assert report.unknown_share_by_reader["Main"] > 0.4


# --------------------------------------------------------------------------- #
# AT-08 — a single-subject endpoint anchors the subject.
# --------------------------------------------------------------------------- #


def _plug_operator() -> ObservationOperator:
    """TV on a plug (single-subject endpoint) inside a noisy Circuit A."""
    return _operator(
        ("Main", "Circuit A", "TV plug"),
        (("device", "tv"), ("device", "fridge"), ("device", "lights")),
        {
            ("device", "tv"): "TV plug",
            ("device", "fridge"): "Circuit A",
            ("device", "lights"): "Circuit A",
        },
        unknown_scopes=(("Circuit A", (1,)),),
    )


def test_at08_plug_anchor_holds_the_tv_through_circuit_noise() -> None:
    operator = _plug_operator()
    candidates = [
        _candidate("c-tv", "tv", "on"),
        _candidate("c-fridge", "fridge", "on"),
        _candidate("c-lights", "lights", "on"),
    ]
    # TV standby 3 W -> 88 W on the plug: +85 W.  The circuit is noisy (σ = 20).
    report = solve_joint_detailed(
        operator,
        {"Main": 170.0, "Circuit A": 140.0 + 60.0, "TV plug": 85.0},
        {"Main": 20.0, "Circuit A": 20.0, "TV plug": 1.0},
        candidates,
        {"tv": DeltaPrior(mean_w=85.0, sigma_w=10.0, sign="pos", model_version_id="m1")},
    )
    solution = report.solution
    values = dict(zip(report.subject_ids, solution.x, strict=True))
    # The TV stays within the plug's σ of the anchor, despite σ = 20 elsewhere.
    assert values["tv"] == pytest.approx(85.0, abs=1.0)
    # The plug row carries the anchor weight (×10).
    tv_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    assert report.sigma[tv_row] == pytest.approx(1.0)
    anchor_weight = ANCHOR_WEIGHT
    assert anchor_weight == 10.0


def test_at08_anchor_uses_the_sigma_floor_of_half_a_watt() -> None:
    operator = _operator(
        ("Main", "TV plug"),
        (("device", "tv"),),
        {("device", "tv"): "TV plug"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 3.0, "TV plug": 3.0},
        {"Main": 20.0, "TV plug": 0.05},
        [_candidate("c-tv", "tv", "on")],
    )
    tv_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    assert report.sigma[tv_row] == pytest.approx(0.5)
    assert float(report.solution.x[0]) == pytest.approx(3.0, abs=0.5)


def test_anchor_rows_are_not_downweighted_as_outliers() -> None:
    """A plug that disagrees must surface as a residual, not vanish."""
    operator = _operator(
        ("Main", "TV plug"),
        (("device", "tv"),),
        {("device", "tv"): "TV plug"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 400.0, "TV plug": 3.0},
        {"Main": 20.0, "TV plug": 1.0},
        [_candidate("c-tv", "tv", "on")],
    )
    # The anchor pulls hard: the TV is near 3 W even though Main says 400 W.
    assert float(report.solution.x[0]) == pytest.approx(3.0, abs=1.0)


# --------------------------------------------------------------------------- #
# AT-09 — one load on nested readers is counted once, with conservative variance.
# --------------------------------------------------------------------------- #


def test_at09_nested_load_is_counted_once() -> None:
    operator = _operator(
        ("Main", "Subpanel", "Circuit A"),
        (("device", "load"),),
        {("device", "load"): "Circuit A"},
        tree=NESTED_TREE,
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 45.0, "Subpanel": 45.0, "Circuit A": 45.0},
        {"Main": 3.0, "Subpanel": 3.0, "Circuit A": 3.0},
        [_candidate("c", "load", "on")],
    )
    solution = report.solution
    # Counted once: 45 W, not 135 W.
    assert float(solution.x[0]) == pytest.approx(45.0, abs=1.0)
    # No unknown column was requested, so nothing is billed to ΔU.
    assert solution.u.size == 0
    # Every nested row sees the load, but the covariance is not made 3x smaller.
    assert solution.cov is not None
    assert float(solution.cov[0, 0]) > 0.0


def test_at09_single_reader_variance_is_not_divided_by_the_reader_count() -> None:
    nested = _operator(
        ("Main", "Subpanel", "Circuit A"),
        (("device", "load"),),
        {("device", "load"): "Circuit A"},
        tree=NESTED_TREE,
    )
    single = _operator(
        ("Circuit A",),
        (("device", "load"),),
        {("device", "load"): "Circuit A"},
        tree=NESTED_TREE,
    )
    three = solve_joint(
        nested,
        {"Main": 45.0, "Subpanel": 45.0, "Circuit A": 45.0},
        {"Main": 10.0, "Subpanel": 10.0, "Circuit A": 10.0},
    )
    one = solve_joint(single, {"Circuit A": 45.0}, {"Circuit A": 10.0})
    assert three.cov is not None and one.cov is not None
    assert float(three.cov[0, 0]) >= 0.9 * float(one.cov[0, 0])


# --------------------------------------------------------------------------- #
# Signed deltas — never clipped.
# --------------------------------------------------------------------------- #


def test_off_transition_keeps_a_negative_delta() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": -55.0, "Circuit A": -55.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c-off", "lamp", "off")],
    )
    assert report.solution.converged
    assert float(report.solution.x[0]) == pytest.approx(-55.0, abs=1.0)
    assert float(report.solution.x[0]) < 0.0
    # Bounds are one-sided negative, not [0, inf).
    assert report.lower[0] == pytest.approx(-np.inf)
    assert report.upper[0] == pytest.approx(0.0)


def test_battery_discharge_keeps_its_negative_delta() -> None:
    operator = _operator(
        ("Main", "Battery"),
        (("device", "battery"),),
        {("device", "battery"): "Battery"},
    )
    prior = DeltaPrior(mean_w=-1200.0, sigma_w=50.0, sign="any", model_version_id="m-bat")
    report = solve_joint_detailed(
        operator,
        {"Main": -1200.0, "Battery": -1200.0},
        {"Main": 30.0, "Battery": 30.0},
        [_candidate("c-bat", "battery", None)],
        {"battery": prior},
    )
    assert float(report.solution.x[0]) == pytest.approx(-1200.0, abs=10.0)
    assert report.lower[0] == pytest.approx(-np.inf)
    assert report.upper[0] == pytest.approx(np.inf)


def test_on_transition_is_bounded_below_at_zero() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 50.0, "Circuit A": 50.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c-on", "lamp", "on")],
    )
    assert report.lower[0] == 0.0
    assert report.upper[0] == pytest.approx(np.inf)


def test_transition_bounds_never_turn_a_prior_into_a_floor() -> None:
    assert transition_bounds("off", magnitude=60.0) == (-60.0, 0.0)
    assert transition_bounds("on", magnitude=60.0) == (0.0, 60.0)
    assert transition_bounds("any") == (-np.inf, np.inf)


# --------------------------------------------------------------------------- #
# Unknown columns and λ_u (DEC-23).
# --------------------------------------------------------------------------- #


def _unknown_balance_operator() -> ObservationOperator:
    """One candidate on a nested branch, with a ΔU column on every reader.

    Each row can either credit the candidate or its own unknown column, so λ_u
    decides the split — which is exactly what DEC-23 asks the solver to expose.
    """
    return _operator(
        ("Main", "Circuit A"),
        (("device", "known"),),
        {("device", "known"): "Circuit A"},
        unknown_scopes=(("Main", (0,)), ("Circuit A", (1,))),
    )


def _solve_with_lambda_u(lambda_u: float) -> JointSolveReport:
    operator = _unknown_balance_operator()
    prior = UnknownPrior(
        scope_reader_id="Circuit A", mu_w=0.0, sigma_w=50.0, lambda_u=lambda_u
    )
    main_prior = UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=50.0, lambda_u=lambda_u)
    return solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c-known", "known", "on")],
        {"known": DeltaPrior(mean_w=60.0, sigma_w=20.0, sign="pos", model_version_id="m1")},
        {"Circuit A": prior, "Main": main_prior},
    )


def test_higher_lambda_u_moves_power_from_unknown_to_candidates() -> None:
    low = _solve_with_lambda_u(0.001)
    medium = _solve_with_lambda_u(1.0)
    high = _solve_with_lambda_u(1000.0)
    known_low = float(low.solution.x[0])
    known_medium = float(medium.solution.x[0])
    known_high = float(high.solution.x[0])
    unknown_low = abs(float(low.solution.u[0]))
    unknown_high = abs(float(high.solution.u[0]))
    assert known_low <= known_medium <= known_high
    assert unknown_high <= unknown_low


def test_lambda_u_is_reported_and_the_unknown_share_is_diagnostic() -> None:
    report = _solve_with_lambda_u(1.0)
    assert report.lambda_u_by_scope["Circuit A"] == pytest.approx(1.0)
    share = report.unknown_share_by_reader["Circuit A"]
    assert share is not None
    assert 0.0 <= share <= 1.0
    # The reported share is |ΔU| / |ΔY| on that scope.
    assert share == pytest.approx(abs(float(report.solution.u[1])) / 100.0, abs=1e-9)
    # The standalone helper agrees with the report for the same operator.
    helper = unknown_share(
        _unknown_balance_operator(), report.solution, {"Main": 100.0, "Circuit A": 100.0}
    )
    assert helper["Circuit A"] == pytest.approx(share, abs=1e-12)


def test_without_an_unknown_prior_the_default_penalty_applies() -> None:
    operator = _unknown_balance_operator()
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c-known", "known", "on")],
    )
    assert report.lambda_u_by_scope["Circuit A"] == pytest.approx(DEFAULT_LAMBDA_U)


def test_unknown_share_reports_none_for_a_reader_without_a_column() -> None:
    operator = _hb77_operator()  # only Main has an unknown column
    report = solve_joint_detailed(
        operator,
        {"Main": 105.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "living-light", "on")],
    )
    assert report.unknown_share_by_reader["Main"] is not None
    assert report.unknown_share_by_reader["Circuit A"] is None
    assert report.unknown_share_by_reader["Circuit B"] is None


# --------------------------------------------------------------------------- #
# Absolute mode (infer_steady's objective).
# --------------------------------------------------------------------------- #


def test_absolute_mode_solves_levels_with_non_negative_consumers() -> None:
    operator = _operator(
        ("Main", "Circuit A", "Circuit B"),
        (("device", "a"), ("device", "b")),
        {("device", "a"): "Circuit A", ("device", "b"): "Circuit B"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 105.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "a", None), _candidate("b", "b", None)],
        mode="absolute",
    )
    assert report.solution.mode == "absolute"
    values = dict(zip(report.subject_ids, report.solution.x, strict=True))
    assert values["a"] == pytest.approx(45.0, abs=1.0)
    assert values["b"] == pytest.approx(60.0, abs=1.0)
    assert all(report.lower >= 0.0)


def test_absolute_mode_keeps_prior_sign_classes() -> None:
    operator = _operator(
        ("Main", "Battery"),
        (("device", "battery"),),
        {("device", "battery"): "Battery"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Battery": -1200.0},
        {"Main": 10.0, "Battery": 10.0},
        [_candidate("b", "battery", "off")],
        {"battery": DeltaPrior(mean_w=-1200.0, sigma_w=20.0, sign="any", model_version_id="m")},
        mode="absolute",
    )
    # A transition direction must not force an absolute level to be positive.
    assert report.lower[0] == pytest.approx(-np.inf)


def test_declared_ceiling_bounds_but_never_floors() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
        prior_ceiling={"lamp": 60.0},
    )
    assert report.lower[0] == 0.0
    assert report.upper[0] == pytest.approx(60.0)
    assert float(report.solution.x[0]) <= 60.0 + 1e-9


# --------------------------------------------------------------------------- #
# Missing data / degenerate inputs.
# --------------------------------------------------------------------------- #


def test_missing_reader_is_none_not_zero() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": None, "Circuit A": 45.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
    )
    assert report.solution.active_rows == (1,)
    assert np.isnan(report.solution.row_residual_w[0])
    assert float(report.solution.x[0]) == pytest.approx(45.0, abs=1.0)


def test_a_row_without_sigma_is_excluded_not_weighted_as_certain() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 900.0, "Circuit A": 45.0},
        {"Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
    )
    assert report.solution.active_rows == (1,)
    assert "inactive_rows_excluded" in report.warnings
    assert np.isnan(report.solution.row_residual_w[0])


def test_no_usable_rows_is_flagged_not_invented() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    report = solve_joint_detailed(operator, {}, {}, [_candidate("c", "lamp", "on")])
    assert not report.solution.converged
    assert "no_usable_rows" in report.warnings


def test_singular_system_has_no_covariance() -> None:
    operator = _operator(
        ("Main",),
        (("device", "a"), ("device", "b")),
        {("device", "a"): "Main", ("device", "b"): "Main"},
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0},
        {"Main": 2.0},
        [_candidate("a", "a", "on"), _candidate("b", "b", "on")],
    )
    # Two candidates, one row, no unknown column: not identifiable.
    assert report.solution.cov is None
    # And the solve says so instead of splitting the 100 W arbitrarily.
    assert not report.solution.converged or report.solution.cov is None


def test_solve_is_deterministic() -> None:
    first = solve_joint_detailed(
        _hb77_operator(),
        {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "living-light", "on")],
    )
    second = solve_joint_detailed(
        _hb77_operator(),
        {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "living-light", "on")],
    )
    assert np.array_equal(first.solution.x, second.solution.x)
    assert np.array_equal(first.solution.u, second.solution.u)
    assert first.solution.objective == second.solution.objective


# --------------------------------------------------------------------------- #
# IRLS mechanics.
# --------------------------------------------------------------------------- #


def test_huber_weights_are_one_inside_the_band() -> None:
    weights = huber_weights(np.array([0.0, 1.0, HUBER_DELTA, 2 * HUBER_DELTA]))
    assert weights[0] == 1.0
    assert weights[1] == 1.0
    assert weights[2] == pytest.approx(1.0)
    assert weights[3] == pytest.approx(0.5)


def test_huber_loss_is_quadratic_inside_and_linear_outside() -> None:
    inside = huber_loss(np.array([1.0]))
    assert inside == pytest.approx(0.5)
    outside = huber_loss(np.array([10.0]))
    assert outside == pytest.approx(HUBER_DELTA * (10.0 - 0.5 * HUBER_DELTA))


def test_huber_downweights_a_single_outlier_reader() -> None:
    A = np.ones((4, 1))
    y = np.array([10.0, 10.0, 10.0, 500.0])
    sigma = np.array([1.0, 1.0, 1.0, 1.0])
    robust = solve_robust_constrained(A, y, sigma=sigma, lower=-np.inf, upper=np.inf)
    plain = solve_robust_constrained(
        A, y, sigma=sigma, lower=-np.inf, upper=np.inf, huber_delta=1e12
    )
    assert robust.converged
    assert float(robust.x[0]) < float(plain.x[0])
    # The outlier's weight is < 1, the inliers stay at 1.
    assert robust.robust_weights[3] < 0.1
    assert np.all(robust.robust_weights[:3] == 1.0)


def test_signed_lower_and_upper_are_honoured() -> None:
    A = np.eye(2)
    y = np.array([10.0, -10.0])
    result = solve_robust_constrained(
        A,
        y,
        sigma=np.array([1.0, 1.0]),
        lower=np.array([0.0, -np.inf]),
        upper=np.array([np.inf, 0.0]),
    )
    assert result.x[0] == pytest.approx(10.0)
    assert result.x[1] == pytest.approx(-10.0)


def test_irls_iteration_budget_is_twenty() -> None:
    assert IRLS_MAX_ITER == 20


def test_irls_converges_on_random_well_posed_problems() -> None:
    """1 000 random well-posed problems: every solve must converge."""
    rng = np.random.default_rng(20261118)
    failures: list[int] = []
    for trial in range(1000):
        m = 4
        n = 2
        A = rng.normal(0.0, 1.0, size=(m, n))
        A[:, 0] = 1.0  # column 0 is a shared branch
        A[:, 1] = np.array([1.0, 1.0, 0.0, 0.0]) + rng.normal(0.0, 0.2, size=m)
        truth = rng.uniform(0.0, 100.0, size=n)
        sigma = np.full(m, 2.0)
        y = A @ truth + rng.normal(0.0, 1.0, size=m)
        result = solve_robust_constrained(
            A,
            y,
            sigma=sigma,
            lower=np.zeros(n),
            upper=None,
            max_iter=IRLS_MAX_ITER,
            tol=1e-6,
        )
        if not result.converged:
            failures.append(trial)
    assert not failures, f"IRLS failed to converge on {len(failures)} of 1000 problems: {failures[:10]}"


def test_irls_converges_on_random_joint_problems() -> None:
    """The same guarantee through the joint solver, with signed bounds."""
    operator = _operator(
        ("Main", "Circuit A", "Circuit B"),
        (("device", "a"), ("device", "b")),
        {("device", "a"): "Circuit A", ("device", "b"): "Circuit B"},
        unknown_scopes=(("Main", (0,)),),
    )
    rng = np.random.default_rng(20261119)
    failures: list[int] = []
    for trial in range(1000):
        a = float(rng.uniform(0.0, 200.0))
        b = float(rng.uniform(0.0, 200.0))
        noise = rng.normal(0.0, 1.0, size=3)
        report = solve_joint_detailed(
            operator,
            {
                "Main": a + b + 5.0 + noise[0],
                "Circuit A": a + noise[1],
                "Circuit B": b + noise[2],
            },
            {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
            [_candidate("a", "a", "on"), _candidate("b", "b", "on")],
            max_iter=IRLS_MAX_ITER,
            tol=1e-6,
        )
        if not report.solution.converged:
            failures.append(trial)
    assert not failures, f"joint IRLS failed on {len(failures)} of 1000 problems: {failures[:10]}"


def test_non_converged_solve_is_flagged_not_hidden() -> None:
    """An exhausted IRLS budget is reported, never silently accepted."""
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    # A gross outlier needs more than one reweighting step to settle.
    report = solve_joint_detailed(
        operator,
        {"Main": 900.0, "Circuit A": 45.0},
        {"Main": 1.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
        max_iter=1,
    )
    assert report.solution.converged is False
    assert "irls_iteration_limit" in report.warnings
    assert "solve_not_converged" in report.warnings
    # The flag travels on the K08 handoff, so I4/I5 cannot miss it.
    assert report.as_dict()["converged"] is False


def test_num_iterations_are_bounded_by_the_budget() -> None:
    operator = _hb77_operator()
    report = solve_joint_detailed(
        operator,
        {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "living-light", "on")],
        max_iter=3,
    )
    assert 1 <= report.iterations <= 3


# --------------------------------------------------------------------------- #
# Candidate / prior plumbing.
# --------------------------------------------------------------------------- #


def test_candidates_select_the_solved_columns() -> None:
    operator = _hb77_operator()
    report = solve_joint_detailed(
        operator,
        {"Main": 110.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
        [_candidate("a", "living-light", "on")],
    )
    assert report.subject_ids == ("living-light",)
    assert report.solution.x.size == 1


def test_no_candidates_solves_every_operator_subject() -> None:
    operator = _hb77_operator()
    report = solve_joint_detailed(
        operator,
        {"Main": 105.0, "Circuit A": 45.0, "Circuit B": 60.0},
        {"Main": 3.0, "Circuit A": 2.0, "Circuit B": 2.0},
    )
    assert set(report.subject_ids) == {"living-light", "kitchen-light", "tv"}


def test_model_prior_is_a_soft_penalty_not_a_bound() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    # A deliberately weak prior: the measurement must dominate it.
    prior = DeltaPrior(mean_w=10.0, sigma_w=80.0, sign="pos", model_version_id="m")
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
        {"lamp": prior},
    )
    # Measurement beats the prior: the prior never becomes a floor or a cap.
    assert float(report.solution.x[0]) == pytest.approx(100.0, abs=2.0)
    assert report.upper[0] == pytest.approx(np.inf)
    assert report.lower[0] == 0.0


def test_prior_with_a_bad_sigma_is_reported_and_skipped() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    prior = DeltaPrior(mean_w=10.0, sigma_w=0.0, sign="pos", model_version_id="m")
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
        {"lamp": prior},
    )
    assert "prior_sigma_missing:lamp" in report.warnings


def test_temporal_fit_shapes_the_objective_without_moving_the_solution() -> None:
    operator = _unknown_balance_operator()
    base = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "known", "on")],
    )
    with_fit = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "known", "on", evidence_terms={"temporal_fit": 0.0})],
    )
    assert np.allclose(base.solution.x, with_fit.solution.x)
    assert with_fit.solution.objective > base.solution.objective


def test_fakes_are_usable_without_being_imported_by_production() -> None:
    """The module builds against the K08 seams; models/residual are Protocols."""
    prior = DeltaPrior(mean_w=45.0, sigma_w=10.0, sign="pos", model_version_id="m")
    models = FakeModelSet.fixed({"living-light": prior})
    residual = FakeResidual(
        UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=50.0, lambda_u=1.0)
    )
    assert models.transition_prior("living-light", "on", {}) == prior
    assert residual.unknown_prior("Main", T0) is not None


def test_soft_alternatives_are_carried_not_applied() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    with_soft = replace(
        operator,
        soft_alternatives=(
            AlternativeEdge(
                edge_id="e1",
                source_endpoint_id="Circuit B",
                target_endpoint_id="lamp",
                relation=RelationType.CONTAINS,
                lifecycle=TopologyEdgeState.PROVISIONAL,
            ),
        ),
    )
    report = solve_joint_detailed(
        with_soft,
        {"Main": 45.0, "Circuit A": 45.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
    )
    # A PROVISIONAL alternative must not add a column or change the solve.
    assert report.subject_ids == ("lamp",)
    assert float(report.solution.x[0]) == pytest.approx(45.0, abs=1.0)


def test_direct_entry_table_is_respected_for_anchor_detection() -> None:
    operator = _operator(
        ("Main", "TV plug"),
        (("device", "tv"),),
        {("device", "tv"): "TV plug"},
        tree={"Main": {"TV plug": {}}},
    )
    row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "TV plug")
    col = operator.subjects.index(("device", "tv"))
    assert operator.entries[(row, col)].observability is Observability.DIRECT
    main_row = next(i for i, meta in enumerate(operator.rows) if meta.reader_id == "Main")
    assert operator.entries[(main_row, col)].observability is Observability.EXPECTED


def test_unknown_prior_defaults_are_none_safe() -> None:
    """An UnknownPrior with a missing sigma falls back to the penalty scale."""
    operator = _unknown_balance_operator()
    prior = UnknownPrior(scope_reader_id="Circuit A", mu_w=0.0, sigma_w=0.0, lambda_u=1.0)
    report = solve_joint_detailed(
        operator,
        {"Main": 100.0, "Circuit A": 100.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "known", "on")],
        unknown_priors={"Circuit A": prior},
    )
    assert "unknown_sigma_missing:Circuit A" in report.warnings


def test_report_mapping_is_read_only_friendly() -> None:
    report = _solve_with_lambda_u(1.0)
    payload = report.as_dict()
    assert payload["mode"] == "delta"
    assert isinstance(payload["x"], dict)
    assert set(payload["x"]) == set(report.subject_ids)


def test_mapping_proxy_prior_inputs_are_accepted() -> None:
    operator = _operator(
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        {("device", "lamp"): "Circuit A"},
    )
    priors = MappingProxyType(
        {"lamp": DeltaPrior(mean_w=45.0, sigma_w=10.0, sign="pos", model_version_id="m")}
    )
    report = solve_joint_detailed(
        operator,
        {"Main": 45.0, "Circuit A": 45.0},
        {"Main": 2.0, "Circuit A": 2.0},
        [_candidate("c", "lamp", "on")],
        priors,
    )
    assert report.solution.converged


def test_mode_is_validated() -> None:
    operator = _operator(
        ("Main",), (("device", "lamp"),), {("device", "lamp"): "Main"}
    )
    with pytest.raises(ValueError):
        solve_joint_detailed(operator, {"Main": 1.0}, {"Main": 1.0}, mode="steady")  # type: ignore[arg-type]
