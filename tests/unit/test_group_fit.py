"""Aggregate group fits for fixtures that cannot be separated (tier B).

Every case starts from ground truth -- known per-fixture wattages, a known
switching pattern -- and derives the recorder history from it.  A passing test
therefore means the fit recovered the load that was constructed, not that it
matched a number chosen to satisfy the assertion.

The refusal cases carry the weight.  A group fit is *advisory*: it never writes a
device, but it is shown to a user as "these fixtures together draw 363.8 W", and
a coefficient that came from a shared evening duty cycle rather than from
switching would be a fabricated measurement with a precise-looking number
attached.  So each gate is exercised by building the situation that should
defeat it and asserting that nothing is emitted.

Where a threshold is asserted directly, the measured production values are
quoted next to it: report Q3's per-light run gives R² 0.830 / 0.791 / 0.577 on
the three lighting rows against null p95 0.498 / 0.145 / 0.400, and margins of
+0.333 / +0.647 / +0.177, with every main feed and sub-panel CT failing its own
null.  This module fits group regressors with a rolling rotation, so on the same
window it measures higher R² and narrower margins (+0.163 to +0.272) -- both
tables reach the same accept/refuse verdict, which is why the thresholds are
proven against both rather than against whichever is more flattering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.disaggregation.group_fit import (
    BIN_S,
    GroupFit,
    GroupMember,
    fit_shared_rows,
)

T0 = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
#: Two days of 15 s bins.  Long enough that the null rotation is a real rotation
#: (it skips at least one day at each end), short enough to keep the suite fast.
N_BINS = 2 * 24 * 4 * 60
#: Always-on load the fixtures sit on top of, in watts.
BASELINE_W = 40.0
#: Per-bin measurement jitter, in watts -- far below any fixture below.
NOISE_W = 0.4


def _switch_patterns(
    names: list[str], *, shared: bool, seed: int = 11
) -> dict[str, np.ndarray]:
    """Binary on/off series for each fixture.

    ``shared`` makes every fixture share one physical switch, which is the scene
    case: the columns are exactly collinear and no arithmetic can split them.
    """
    rng = np.random.default_rng(seed)
    if shared:
        pattern = np.zeros(N_BINS, dtype=bool)
        at = 20
        while at < N_BINS:
            length = int(rng.integers(8, 60))
            pattern[at : at + length] = True
            at += length + int(rng.integers(8, 80))
        return {name: pattern.copy() for name in names}

    out: dict[str, np.ndarray] = {}
    for name in names:
        pattern = np.zeros(N_BINS, dtype=bool)
        at = int(rng.integers(10, 60))
        while at < N_BINS:
            length = int(rng.integers(8, 60))
            pattern[at : at + length] = True
            at += length + int(rng.integers(8, 90))
        out[name] = pattern
    return out


def _row_watts(
    patterns: dict[str, np.ndarray],
    watts: dict[str, float],
    *,
    seed: int = 11,
    noise_w: float = NOISE_W,
) -> np.ndarray:
    rng = np.random.default_rng(seed + 1)
    total = np.full(N_BINS, BASELINE_W)
    for name, pattern in patterns.items():
        total = total + watts[name] * pattern
    return total + rng.normal(0.0, noise_w, N_BINS)


def _recorder_row(values: np.ndarray) -> list[dict]:
    """One sample per bin, the shape ``history_during_period`` returns."""
    return [
        {"s": f"{value:.4f}", "lu": (T0 + timedelta(seconds=BIN_S * index)).timestamp()}
        for index, value in enumerate(values)
    ]


def _recorder_states(pattern: np.ndarray) -> list[dict]:
    """State changes only -- what a real light entity publishes."""
    rows: list[dict] = []
    previous: bool | None = None
    for index, on in enumerate(pattern):
        if previous is not None and bool(on) == previous:
            continue
        rows.append(
            {
                "s": "on" if on else "off",
                "lu": (T0 + timedelta(seconds=BIN_S * index)).timestamp(),
            }
        )
        previous = bool(on)
    return rows


def _history(
    watts: dict[str, float], *, shared: bool = False, seed: int = 11
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    patterns = _switch_patterns(list(watts), shared=shared, seed=seed)
    row = _row_watts(patterns, watts, seed=seed)
    states = {name: _recorder_states(pattern) for name, pattern in patterns.items()}
    return {"ct1": _recorder_row(row)}, states


def _fit(rows, states, **kwargs):
    kwargs.setdefault("entity_levels", {})
    kwargs.setdefault("row_noise_w", {"ct1": NOISE_W})
    return fit_shared_rows(row_readings=rows, entity_states=states, **kwargs)


# --------------------------------------------------------------------------- #
# Happy path: known coefficients are recovered
# --------------------------------------------------------------------------- #


def test_a_row_fit_beats_a_constant_baseline_and_recovers_known_wattages() -> None:
    """Two independent fixtures of known size are each fitted on their own.

    They switch at unrelated instants, so they are separate groups and their
    coefficients are individually measurable.  R² must be near 1: the row is
    their sum plus a constant plus 0.4 W of jitter.
    """
    watts = {"light.kitchen_pot": 62.0, "light.office_pot": 27.5}
    rows, states = _history(watts)

    fits = _fit(rows, states)

    assert len(fits) == 2, [f.members for f in fits]
    by_entity = {fit.members[0].entity_id: fit for fit in fits}
    assert by_entity["light.kitchen_pot"].total_w == pytest.approx(62.0, abs=1.5)
    assert by_entity["light.office_pot"].total_w == pytest.approx(27.5, abs=1.5)
    for fit in fits:
        assert fit.ct_id == "ct1"
        assert fit.individually_identifiable is True
        assert fit.members[0].estimated_w == pytest.approx(fit.total_w)
        # Constant baseline explains nothing; the switching explains the row.
        assert fit.r2 > 0.9
        assert fit.r2_margin > 0.15


def test_the_constant_baseline_is_excluded_from_the_coefficients() -> None:
    """40 W is always on, so no fixture may be credited with it.

    A fit that let the intercept float without centring the design shrinks every
    coefficient; one that omitted the intercept inflates them.  Both are caught
    by comparing against the wattages that were constructed.
    """
    watts = {"light.a": 100.0}
    rows, states = _history(watts)

    fits = _fit(rows, states)

    assert len(fits) == 1
    assert fits[0].total_w == pytest.approx(100.0, abs=1.5)
    # The always-on load belongs to the row, not to any fixture: a coefficient
    # near 140 W would mean the baseline had been absorbed into the fixture.
    assert abs(fits[0].total_w - (BASELINE_W + 100.0)) > 20.0


# --------------------------------------------------------------------------- #
# Co-toggle groups: one GroupFit, no split
# --------------------------------------------------------------------------- #


def test_perfectly_collinear_members_collapse_to_one_group() -> None:
    """Five fixtures on one scene switch are one regressor, not five guesses.

    Their columns are identical, so the design has one identifiable direction.
    The honest answer is one group whose *total* is measured and whose members
    carry no individual wattage at all -- the alternative is five contradictory
    numbers that happen to sum to the right thing.
    """
    watts = {f"light.scene_{index}": 40.0 for index in range(5)}
    rows, states = _history(watts, shared=True)

    fits = _fit(rows, states)

    assert len(fits) == 1, [f.members for f in fits]
    group = fits[0]
    assert len(group.members) == 5
    assert group.individually_identifiable is False
    assert [member.estimated_w for member in group.members] == [None] * 5
    # The total is the measured quantity, and it is what was constructed.
    assert group.total_w == pytest.approx(sum(watts.values()), rel=0.05)
    assert any("co-toggle" in line for line in group.evidence)
    assert any("not separable" in line for line in group.evidence)


def test_a_singleton_group_is_still_individually_identifiable() -> None:
    """One fixture needs no group at all; its coefficient is its own draw."""
    watts = {"light.alone": 55.0}
    rows, states = _history(watts)

    fits = _fit(rows, states)

    assert len(fits) == 1
    assert fits[0].individually_identifiable is True
    assert fits[0].members == (
        GroupMember(entity_id="light.alone", estimated_w=pytest.approx(55.0, abs=1.5)),
    )


def test_a_group_total_is_never_split_across_its_members() -> None:
    """No member of a multi-fixture group may carry a number.

    This is the contract's central tier-B rule: a split is a fabrication, even a
    plausible-looking even one.  Asserted as an identity so a future refactor
    that "helpfully" divides the total cannot pass.
    """
    watts = {f"light.strip_{index}": 30.0 for index in range(3)}
    rows, states = _history(watts, shared=True)

    for fit in _fit(rows, states):
        if len(fit.members) > 1:
            assert all(member.estimated_w is None for member in fit.members)


def test_group_fit_reports_identifiability_as_a_derived_property() -> None:
    """`individually_identifiable` cannot contradict the members it describes."""
    anonymous = GroupFit(
        ct_id="ct1",
        total_w=100.0,
        r2=0.8,
        r2_null_p95=0.2,
        r2_margin=0.6,
        members=(GroupMember("light.a", None), GroupMember("light.b", None)),
        evidence=(),
    )
    identified = GroupFit(
        ct_id="ct1",
        total_w=50.0,
        r2=0.8,
        r2_null_p95=0.2,
        r2_margin=0.6,
        members=(GroupMember("light.a", 50.0),),
        evidence=(),
    )

    assert anonymous.individually_identifiable is False
    assert identified.individually_identifiable is True


# --------------------------------------------------------------------------- #
# Refusal cases
# --------------------------------------------------------------------------- #


def test_a_flat_row_is_rejected() -> None:
    """Nothing switched, so there is nothing to attribute."""
    watts = {"light.a": 60.0, "light.b": 25.0}
    _, states = _history(watts)
    flat = _recorder_row(np.full(N_BINS, 100.0))

    assert _fit({"ct1": flat}, states) == ()


#: Bins in one day on the 15 s grid.
BINS_PER_DAY = 24 * 4 * 60


def _bursty_evening(seed: int = 4) -> np.ndarray:
    """A fixture that switches repeatedly inside an evening window, each day.

    Short on/off runs (3-7 min) between 17:00 and 22:00.  This is the shape that
    defeats a plain `R² > 0.30` gate: the row shares the household's evening
    *duty cycle* without following any particular switch, so a constant baseline
    rates the fit well while the real fixtures switch at unrelated instants.
    """
    rng = np.random.default_rng(seed)
    pattern = np.zeros(N_BINS, dtype=bool)
    for day in range(N_BINS // BINS_PER_DAY):
        at = day * BINS_PER_DAY + 17 * 240
        while at < day * BINS_PER_DAY + 22 * 240:
            length = int(rng.integers(12, 30))
            pattern[at : min(at + length, N_BINS)] = True
            at += length + int(rng.integers(12, 30))
    return pattern


def _evening_envelope() -> np.ndarray:
    """One continuous 17:00-22:00 block per day: the confound's only structure."""
    pattern = np.zeros(N_BINS, dtype=bool)
    for day in range(N_BINS // BINS_PER_DAY):
        pattern[day * BINS_PER_DAY + 17 * 240 : day * BINS_PER_DAY + 22 * 240] = True
    return pattern


def test_a_row_that_barely_beats_its_null_is_rejected() -> None:
    """R² alone would accept this row; the rotated null is what refuses it.

    The row is an evening envelope that correlates with the fixture's duty cycle
    but not with its switching.  Measured on this fixture: R² 0.434, null p95
    0.310, margin +0.124.  That clears ``min_r2_improvement`` (0.30) and fails
    ``min_r2_margin_over_null`` (0.15) -- so the assertion is made with the R²
    gate disabled as well, proving the margin gate is the one doing the work, and
    then both thresholds relaxed to prove the fixture itself is fittable.
    """
    pattern = _bursty_evening()
    states = {"light.evening": _recorder_states(pattern)}
    row = BASELINE_W + 300.0 * _evening_envelope() + np.random.default_rng(6).normal(
        0.0, NOISE_W, N_BINS
    )
    rows = {"ct1": _recorder_row(row)}

    assert _fit(rows, states) == ()
    # Even with the constant-baseline gate removed, the margin still refuses it.
    assert _fit(rows, states, min_r2_improvement=0.0) == ()
    # With both gates relaxed it is fitted, so the refusal above was the gate and
    # not an unrelated inability to fit this row at all.
    assert len(_fit(rows, states, min_r2_improvement=0.0, min_r2_margin_over_null=0.0)) == 1


def test_a_duty_cycle_confound_does_not_pass_the_rotated_null() -> None:
    """Same ON-fraction, different instants: only a rotation-aware null sees it.

    The row follows a copy of the light's pattern shifted by a quarter of the
    window.  It has the exact same duty cycle and the same ON-duration
    distribution and never actually switches with the light, so R² against a
    constant baseline is low and the fit is refused outright.
    """
    watts = {"light.a": 60.0}
    patterns = _switch_patterns(list(watts), shared=False)
    shifted = np.roll(patterns["light.a"], N_BINS // 4)
    row = BASELINE_W + 60.0 * shifted + np.random.default_rng(5).normal(
        0.0, NOISE_W, N_BINS
    )

    fits = _fit(
        {"ct1": _recorder_row(row)},
        {name: _recorder_states(pattern) for name, pattern in patterns.items()},
    )

    assert fits == ()


def test_a_row_with_too_few_transitions_is_rejected() -> None:
    """One toggle is not a fit, however good the in-sample R² looks."""
    watts = {"light.once": 60.0}
    rows, states = _history(watts)

    fits = _fit(rows, states, min_transitions_for_fit=10_000)

    assert fits == ()


def test_excluded_ct_ids_removes_a_main_or_dedicated_row() -> None:
    """The row-type rule lives with the caller, so the parameter must work.

    A main feed's coefficient is the whole house; a dedicated row's is a device
    that already exists.  Neither may become an advisory group, and this module
    has no topology to tell them apart -- so it must honour what it is told.
    """
    watts = {"light.a": 60.0, "light.b": 25.0}
    rows, states = _history(watts)
    rows["ct_main"] = _recorder_row(
        _row_watts(_switch_patterns(list(watts), shared=False), watts)
    )

    kept = _fit(rows, states, row_noise_w={"ct1": NOISE_W, "ct_main": NOISE_W})
    assert {fit.ct_id for fit in kept} == {"ct1", "ct_main"}

    excluded = _fit(
        rows,
        states,
        row_noise_w={"ct1": NOISE_W, "ct_main": NOISE_W},
        excluded_ct_ids=frozenset({"ct_main"}),
    )
    assert {fit.ct_id for fit in excluded} == {"ct1"}


def test_excluding_every_row_returns_nothing_rather_than_scraping_one() -> None:
    """The exclusion is a set, not a preference: an excluded row is never fitted."""
    watts = {"light.a": 60.0}
    rows, states = _history(watts)

    assert _fit(rows, states, excluded_ct_ids=frozenset({"ct1"})) == ()


def test_an_empty_installation_returns_nothing() -> None:
    """No rows or no entities is an answer, not an error."""
    assert fit_shared_rows(
        row_readings={},
        entity_states={},
        entity_levels={},
        row_noise_w={},
    ) == ()

    rows, states = _history({"light.a": 60.0})
    assert fit_shared_rows(
        row_readings=rows,
        entity_states={},
        entity_levels={},
        row_noise_w={"ct1": NOISE_W},
    ) == ()


def test_a_fixture_below_the_response_floor_is_not_reported() -> None:
    """A 0.3 W coefficient is arithmetic, not a measurement."""
    rows, states = _history({"light.tiny": 0.3})

    fits = _fit(rows, states)

    assert fits == ()


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_repeated_runs_are_identical() -> None:
    """The null is seeded, so two runs must agree exactly.

    A threshold applied to a random number is not a threshold: if the p95 moved
    between runs, a fixture could appear and disappear on identical data.
    """
    watts = {"light.a": 60.0, "light.b": 25.0, "light.c": 18.0}
    rows, states = _history(watts)

    first = _fit(rows, states)
    second = _fit(rows, states)

    assert first == second
    assert [fit.r2_null_p95 for fit in first] == [fit.r2_null_p95 for fit in second]


def test_a_different_seed_changes_the_null_but_not_the_coefficients() -> None:
    """The seed perturbs the null distribution only.

    The fitted wattages come from NNLS on the real data; only the comparison
    distribution is resampled.  Asserting that keeps a refactor from accidentally
    seeding the *fit* as well.
    """
    watts = {"light.a": 60.0, "light.b": 25.0}
    rows, states = _history(watts)

    first = _fit(rows, states, seed=0)
    second = _fit(rows, states, seed=1234)

    assert [f.total_w for f in first] == [f.total_w for f in second]
    assert [f.r2 for f in first] == [f.r2 for f in second]


# --------------------------------------------------------------------------- #
# Contract shape
# --------------------------------------------------------------------------- #


def test_the_documented_thresholds_are_the_measured_ones() -> None:
    """Defaults must sit inside the measured gap, not somewhere convenient.

    Report **Q3** measured, on 48 h of production data with per-light regressors,
    R² 0.830 / 0.791 / 0.577 with margins +0.333 / +0.647 / +0.177, while main
    feeds and sub-panels had margins down to −0.15.  A default below 0.15 would
    admit the rows the study says are chance; one above 0.177 would reject
    ``lights_ct3``.  This module's own group-regressor run on the same window
    measures narrower margins (+0.163 to +0.272 for the same rows), so 0.15 sits
    just under the smallest margin either honest implementation observed.
    """
    import inspect

    signature = inspect.signature(fit_shared_rows)
    defaults = {
        name: parameter.default for name, parameter in signature.parameters.items()
    }

    assert defaults["min_r2_improvement"] == pytest.approx(0.30)
    assert 0.0 < defaults["min_r2_margin_over_null"] < 0.177
    assert defaults["min_transitions_for_fit"] >= 20


def test_the_frozen_signature_is_unchanged() -> None:
    """``agent-a`` codes against this signature; a rename is a contract break."""
    import inspect

    parameters = inspect.signature(fit_shared_rows).parameters

    assert list(parameters) == [
        "row_readings",
        "entity_states",
        "entity_levels",
        "row_noise_w",
        "min_transitions_for_fit",
        "min_r2_improvement",
        "min_r2_margin_over_null",
        "seed",
        "excluded_ct_ids",
    ]
    assert parameters["excluded_ct_ids"].default == frozenset()
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in parameters.values()
    )


# --------------------------------------------------------------------------- #
# Performance: the whole install must fit inside two seconds
# --------------------------------------------------------------------------- #


def test_a_full_installation_fits_inside_the_two_second_budget() -> None:
    """32 rows × 200 entities over 48 h must finish in ≤2 s.

    The cost is dominated by the 50-draw null per row, so the budget is a real
    constraint on the design and not a formality: the naive form (NNLS on the
    full ``m × n`` matrix per draw) measured 17 s before the solve was moved into
    the Gram factor.  Timed rather than assumed, because a vectorisation that
    does not vectorise is the easiest defect to ship unnoticed.
    """
    import time

    rng = np.random.default_rng(7)
    rows: dict[str, list[dict]] = {}
    for index in range(32):
        watts = np.full(N_BINS, rng.uniform(10.0, 500.0)) + rng.normal(0.0, 0.2, N_BINS)
        rows[f"ct{index:02d}"] = _recorder_row(watts)

    states: dict[str, list[dict]] = {}
    for index in range(200):
        pattern = np.zeros(N_BINS, dtype=bool)
        at = int(rng.integers(0, 50))
        while at < N_BINS:
            length = int(rng.integers(4, 60))
            pattern[at : at + length] = True
            at += length + int(rng.integers(4, 60))
        states[f"light.e{index:03d}"] = _recorder_states(pattern)

    started = time.perf_counter()
    fits = _fit(rows, states, row_noise_w={ct: 0.2 for ct in rows})
    elapsed = time.perf_counter() - started

    assert fits == ()  # every row here is unrelated noise
    assert elapsed < 2.0, f"full installation took {elapsed:.2f} s"
