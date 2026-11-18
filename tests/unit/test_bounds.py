"""I4 justified bounds: a small LP per member, never a model prior (DEC-19).

The canonical case is HB §8.4's: a group sum of 250 W with non-negative members
gives every member ``[0, 250]``.  Model priors are *not* inputs to this module,
so a 125 W model cannot tighten the interval — that is the defect DEC-19 closes.
"""

from __future__ import annotations

import pytest
from wattmosaic_engine.contracts import Bounds, IdentifiabilityStatus
from wattmosaic_engine.inference.bounds import (
    DEFAULT_N_SIGMA,
    SumConstraint,
    ZeroWidthBoundError,
    compute_bounds,
    group_sum_constraint,
)

MEMBERS = ("L1", "L2")


# --------------------------------------------------------------------------- #
# The canonical [0, G].
# --------------------------------------------------------------------------- #
def test_non_negativity_gives_zero_to_the_identified_sum():
    bounds = compute_bounds(
        MEMBERS, sums=[group_sum_constraint(MEMBERS, 250.0, None)]
    )
    for member in MEMBERS:
        assert bounds[member].lower_w == pytest.approx(0.0)
        assert bounds[member].upper_w == pytest.approx(250.0)
        assert bounds[member].basis == "non_negativity"


def test_the_range_always_contains_the_measured_sum():
    """The regression the legacy probe failed: ``max_w=337.5 < 500``."""
    bounds = compute_bounds(MEMBERS, sums=[group_sum_constraint(MEMBERS, 500.0, None)])
    for member in MEMBERS:
        assert bounds[member].lower_w <= 500.0 <= bounds[member].upper_w


def test_a_posterior_widens_the_sum_by_three_sigma():
    constraint = group_sum_constraint(MEMBERS, 250.0, 2.0)
    assert constraint.lower_w == pytest.approx(250.0 - DEFAULT_N_SIGMA * 2.0)
    assert constraint.upper_w == pytest.approx(250.0 + DEFAULT_N_SIGMA * 2.0)
    bounds = compute_bounds(MEMBERS, sums=[constraint])
    for member in MEMBERS:
        assert bounds[member].upper_w == pytest.approx(256.0)


def test_a_missing_sigma_keeps_the_sum_exact_and_never_defaults_to_zero():
    exact = group_sum_constraint(MEMBERS, 250.0, None)
    assert exact.lower_w == exact.upper_w == 250.0
    with_sigma = group_sum_constraint(MEMBERS, 250.0, 0.0)
    assert with_sigma.lower_w == with_sigma.upper_w == 250.0


# --------------------------------------------------------------------------- #
# Zero width is only for an identified subject.
# --------------------------------------------------------------------------- #
def test_an_identified_subject_gets_a_point_interval():
    bounds = compute_bounds(
        ["L1"],
        identified={"L1": 100.0},
        identifiability={"L1": IdentifiabilityStatus.IDENTIFIABLE},
    )
    assert bounds["L1"].lower_w == bounds["L1"].upper_w == 100.0


def test_a_point_for_a_non_identified_subject_is_refused():
    with pytest.raises(ZeroWidthBoundError):
        compute_bounds(
            ["L1"],
            identified={"L1": 125.0},
            identifiability={"L1": IdentifiabilityStatus.NON_IDENTIFIABLE},
        )
    with pytest.raises(ZeroWidthBoundError):
        compute_bounds(
            ["L1"],
            identified={"L1": 125.0},
            identifiability={"L1": IdentifiabilityStatus.GROUP_IDENTIFIABLE},
        )


def test_a_system_that_forces_a_point_on_a_member_raises():
    """Two contradictory-but-exact sums can pin a member; that must not silently
    publish ``[250, 250]`` for a non-identified member."""
    sums = [
        group_sum_constraint(MEMBERS, 250.0, None),
        group_sum_constraint(("L1",), 125.0, None),
    ]
    with pytest.raises(ZeroWidthBoundError):
        compute_bounds(MEMBERS, sums=sums)


# --------------------------------------------------------------------------- #
# Sign classes and ceilings.
# --------------------------------------------------------------------------- #
def test_a_non_positive_subject_is_bounded_above_by_zero():
    bounds = compute_bounds(
        ["gen"],
        sums=[SumConstraint(("gen",), -200.0, 0.0)],
        signs={"gen": "non_positive"},
    )
    assert bounds["gen"].lower_w == pytest.approx(-200.0)
    assert bounds["gen"].upper_w == pytest.approx(0.0)
    assert bounds["gen"].basis == "group_sum"


def test_an_unconstrained_subject_has_absent_bounds_not_zero():
    bounds = compute_bounds(["pv"], signs={"pv": "any"}, sums=[SumConstraint(("pv",), 0.0, None)])
    assert bounds["pv"].lower_w == pytest.approx(0.0)
    # ``any`` with no ceiling: the LP is unbounded above, so the upper bound is
    # absent -- never a fabricated ``0.0``.
    assert bounds["pv"].upper_w is None


def test_a_declared_ceiling_caps_the_member_and_is_reported_as_its_basis():
    bounds = compute_bounds(
        MEMBERS,
        sums=[group_sum_constraint(MEMBERS, 250.0, None)],
        ceilings={"L1": 100.0},
    )
    assert bounds["L1"].upper_w == pytest.approx(100.0)
    assert bounds["L1"].basis == "declared_ceiling"
    # The sibling is unaffected: a declared ceiling is per subject.
    assert bounds["L2"].upper_w == pytest.approx(250.0)


def test_a_model_prior_cannot_appear_as_a_constraint():
    """There is no model parameter, so ``[0, G]`` stays ``[0, G]`` for a 125 W
    model.  The signature is the guard: adding one requires a new API."""
    import inspect

    parameters = inspect.signature(compute_bounds).parameters
    assert "models" not in parameters
    assert "priors" not in parameters
    assert "model_w" not in parameters
    bounds = compute_bounds(MEMBERS, sums=[group_sum_constraint(MEMBERS, 250.0, None)])
    assert bounds["L1"].upper_w == pytest.approx(250.0)
    assert bounds["L1"].upper_w != pytest.approx(125.0)


# --------------------------------------------------------------------------- #
# Input validation and edge cases.
# --------------------------------------------------------------------------- #
def test_a_sum_constraint_validates_its_inputs():
    with pytest.raises(ValueError):
        SumConstraint(())
    with pytest.raises(ValueError):
        SumConstraint(("a", "a"), 1.0, 2.0)
    with pytest.raises(ValueError):
        SumConstraint(("a",), 5.0, 1.0)


def test_duplicate_subjects_are_rejected():
    with pytest.raises(ValueError):
        compute_bounds(["a", "a"])


def test_an_unknown_subject_is_simply_not_bounded():
    bounds = compute_bounds(["a"], sums=[group_sum_constraint(("b",), 250.0, None)])
    assert bounds["a"].lower_w == pytest.approx(0.0)
    assert bounds["a"].upper_w is None


def test_bounds_are_plain_contract_objects_with_a_justified_basis():
    bounds = compute_bounds(MEMBERS, sums=[group_sum_constraint(MEMBERS, 250.0, None)])
    assert all(isinstance(value, Bounds) for value in bounds.values())
    assert {value.basis for value in bounds.values()} == {"non_negativity"}


def test_an_unconstrained_system_produces_absent_bounds():
    bounds = compute_bounds(["a"], signs={"a": "any"})
    assert bounds["a"].lower_w is None
    assert bounds["a"].upper_w is None
    assert bounds["a"].basis == "non_negativity"
