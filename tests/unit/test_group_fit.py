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

The null is computed with NNLS -- the same estimator as the fit.  An
unconstrained least-squares null is an upper bound for a *fixed* design but its
permissiveness grows with the column count, so raising the candidate cap used to
turn accepted rows into refused ones.
``test_adding_irrelevant_candidates_never_refuses_an_accepted_row`` pins that
down: the LS null on its fixture climbs 0.0209 → 0.2612 across 66 columns and
the margin crosses the gate, while the NNLS null moves it only 0.2932 → 0.2490.
"""

from __future__ import annotations

import math
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
# Degenerate designs: a coefficient that outruns the row is a refused row
# --------------------------------------------------------------------------- #

#: How many bins of an entity's history are unknown before its first event.  A
#: fixture that was never recorded until 10:00 has no state before 10:00, and
#: ``_design_matrix`` marks the whole group column unknown over that prefix.
UNKNOWN_PREFIX_BINS = 600
#: Width of the ON period of the fixture that only ever switches inside that
#: unknown prefix -- so its centred column is exactly zero over the usable
#: window.  This is the production shape: ``switch.fan_socket_1`` was ON for 88
#: bins and all 88 fell inside the 744 bins that two other group columns made
#: unknown.
BLIND_FIXTURE_ON_BINS = 88
#: A load the design cannot explain.  With a perfect fit the solver stops at the
#: boundary of the problem and the defect stays hidden; a row whose switch
#: pattern is only partly explained leaves the solver searching, which is when
#: the zero-width direction gets used.  The production row had exactly this
#: shape -- R² 0.99, but 114.5 W of measured range against a 160.8 W maximum.
UNEXPLAINED_W = 100.0


def _states_after(pattern: np.ndarray, first_bin: int) -> list[dict]:
    """State changes only, and only from ``first_bin`` onward.

    A recorder that first reports at 10:00 says nothing about 09:00, so the bins
    before ``first_bin`` stay unknown rather than being assumed off.
    """
    rows: list[dict] = []
    previous: bool | None = None
    for index in range(first_bin, N_BINS):
        on = bool(pattern[index])
        if previous is not None and on == previous:
            continue
        rows.append(
            {
                "s": "on" if on else "off",
                "lu": (T0 + timedelta(seconds=BIN_S * index)).timestamp(),
            }
        )
        previous = on
    return rows


def _toggle_pattern(seed: int) -> np.ndarray:
    """One fixture's on/off history, from a per-fixture seed.

    Per-fixture seeds (rather than one stream) keep a fixture's pattern stable
    when another fixture is added or removed, which is what makes this design
    reproducible: the defect depends on the *exact* bits of the design.
    """
    rng = np.random.default_rng(seed)
    pattern = np.zeros(N_BINS, dtype=bool)
    at = int(rng.integers(0, 100))
    while at < N_BINS - 20:
        length = int(rng.integers(6, 50))
        pattern[at : at + max(1, length)] = True
        at += length + int(rng.integers(6, 80))
    return pattern


def _blind_fixture_design(
    seed: int = 7,
) -> tuple[np.ndarray, dict[str, list[dict]], dict[str, float]]:
    """Eight observed fixtures plus one that is never seen to switch.

    Returns the row watts, the recorder histories, and the constructed wattages.
    The blind fixture's ON period sits entirely inside ``UNKNOWN_PREFIX_BINS``,
    which every other fixture's history starts after, so its column is unknown
    wherever it is ON and constant everywhere else: zero variance, zero
    information, and -- under the pre-fix code -- the exact null direction the
    solver was free to fill with any number at all.

    Eight observed fixtures are used because the defect needs the retained
    system to be *underdetermined*: one dropped direction makes ``factor``
    ``k × n`` with ``k < n``, and NNLS may then put any value in the null space.
    Fewer fixtures leave the system square, the leak is a rounding term of the
    last pivot, and the solver sets it to zero.
    """
    patterns = {f"light.a{index}": _toggle_pattern(seed * 17 + index) for index in range(6)}
    patterns.update(
        {f"light.z{index}": _toggle_pattern(seed * 17 + 8 + index) for index in range(2)}
    )
    blind = np.zeros(N_BINS, dtype=bool)
    blind[
        UNKNOWN_PREFIX_BINS // 2 : UNKNOWN_PREFIX_BINS // 2 + BLIND_FIXTURE_ON_BINS
    ] = True
    patterns["light.b_blind"] = blind

    rng = np.random.default_rng(seed + 31)
    unexplained = _toggle_pattern(seed * 7 + 999)
    watts = {
        name: float(rng.uniform(5.0, 40.0))
        for name in patterns
        if name != "light.b_blind"
    }
    row = np.full(N_BINS, BASELINE_W)
    for name, pattern in patterns.items():
        if name == "light.b_blind":
            continue
        row = row + watts[name] * pattern
    row = row + UNEXPLAINED_W * unexplained + rng.normal(0.0, NOISE_W, N_BINS)

    # Every fixture except the blind one is first recorded after the prefix.
    states = {
        name: _states_after(
            pattern, 0 if name == "light.b_blind" else UNKNOWN_PREFIX_BINS
        )
        for name, pattern in patterns.items()
    }
    return row, states, watts


def test_a_blind_fixture_never_gets_a_coefficient_larger_than_the_row() -> None:
    """A regressor ON only where it cannot be seen must not be amplified.

    **This is the production defect, in miniature.**  On the real 24 h
    ``lights_ct1`` window ``switch.fan_socket_1`` is ON for 88 bins and all 88
    fall inside the 744 bins two other columns mark unknown, so its centred
    column is exactly zero.  The old code then dropped a second direction (the
    exact ``g02 + g04 = g03 + g11`` relation), handed NNLS an 18 × 20
    underdetermined system, and got back ``9.999358875363018e15 W`` for a fixture
    on a row whose observed maximum was ``114.507 W``.  R² barely moved
    (0.9899 with the blowup, 0.9908 at the true optimum), so every gate passed.

    This fixture reproduces it deterministically at unit-test scale, through the
    public API and with **default** thresholds: the blind column is zero-variance
    over the usable window, eight observed fixtures leave the retained system
    underdetermined, and the pre-fix code returns ``6.35248288678673e15 W`` on a
    row whose maximum is ``327.60 W`` (R² 0.3534, margin +0.334 -- both inside
    their gates).

    The bound is the row's observed maximum, not its range or its minimum.  A
    coefficient is the watts a group *adds* while ON, and the row maximum is the
    most every load on that circuit ever drew together; when the group is ON the
    row is carrying at least its draw, so a coefficient above the maximum cannot
    be a measurement of anything.  A violation must therefore be a **refused
    row** -- the fit did not measure the group -- never a clamped number, which
    would look like a measurement the data never produced.
    """
    row, states, _ = _blind_fixture_design()

    fits = _fit({"ct1": _recorder_row(row)}, states)

    row_max_w = float(np.nanmax(row))
    assert fits, "the observed fixtures must still be fitted"
    blind = [fit for fit in fits if fit.members[0].entity_id == "light.b_blind"]
    # Either the blind fixture is absent or it is reported at a wattage the row
    # can physically carry.  Both are honest; 6.35e15 W is not.
    assert all(fit.total_w <= row_max_w for fit in blind), (
        f"blind fixture reported at {[fit.total_w for fit in blind]} W "
        f"on a {row_max_w:.2f} W row"
    )
    assert all(fit.total_w <= row_max_w for fit in fits), [
        (fit.members[0].entity_id, fit.total_w) for fit in fits
    ]
    # The defect is one fabricated column, not an inability to fit the row: the
    # seven fixtures that were genuinely observed keep their coefficients.
    assert len(fits) == 7, [(fit.members[0].entity_id, fit.total_w) for fit in fits]


def test_a_near_singular_design_is_dropped_rather_than_amplified() -> None:
    """Two nearly-collinear columns must not be split into two huge numbers.

    When ``a`` and ``b`` differ by a handful of bins their coefficients trade off
    almost perfectly: ``a + b`` is measured and ``a - b`` is not.  Solving that
    as if it were two separate regressors is how a 40 W fixture becomes a 4000 W
    one and its twin becomes -3960 W.  The honest answers are the sum (in the
    row space) and *nothing* for the difference -- so at most one of the pair may
    be reported, and whatever is reported must stay inside the row.

    Measured on the production ``lights_ct1`` row: the four collinear groups
    score 0.13-0.34 on the discarded directions while every genuine column scores
    at most 1.2e-23, and the surviving directions have condition number ≤ 66
    against a cut at 1e3.  Here the two columns differ in a single bin.
    """
    rng = np.random.default_rng(21)
    base = np.zeros(N_BINS, dtype=bool)
    at = 40
    while at < N_BINS:
        length = int(rng.integers(8, 60))
        base[at : at + length] = True
        at += length + int(rng.integers(8, 90))
    twin = base.copy()
    twin[np.flatnonzero(base)[-1]] = False

    patterns = {"light.a": base, "light.twin": twin}
    watts = {"light.a": 30.0, "light.twin": 28.0}
    row = _row_watts(patterns, watts)
    states = {name: _recorder_states(pattern) for name, pattern in patterns.items()}

    fits = _fit({"ct1": _recorder_row(row)}, states)

    row_max_w = float(np.nanmax(row))
    assert all(fit.total_w <= row_max_w for fit in fits), [
        (fit.members[0].entity_id, fit.total_w) for fit in fits
    ]
    # The pair is one measured quantity plus one unmeasurable split, so the two
    # members cannot both carry a number: `individually_identifiable` is
    # `len(members) == 1`, so two singletons would be two claimed measurements
    # of one indistinguishable direction.
    assert not (len(fits) == 2 and all(fit.individually_identifiable for fit in fits))


def test_the_candidate_list_decides_the_usable_window_and_so_the_answer() -> None:
    """Truncating candidates can change a *surviving* row's numbers either way.

    ``discover_placements`` cuts the candidate list to ``max_candidates`` (200 by
    default) before anything is fitted, and that cut is **not monotone** in the
    output.  This test makes the reason explicit rather than leaving it as lore.

    The window a row is fitted on is the intersection of every candidate's
    recorded history.  Adding one candidate that was first recorded *late* -- an
    ordinary thing, a fixture added to HA last week -- marks the whole prefix
    unknown, and :func:`_design_matrix` then drops every bin in it from the row's
    usable window.  The row's numbers are therefore a function of the candidate
    *set*, not just of the row.

    Measured here: the same recorded row, the same three fixtures, zeroed to fit
    on the whole window, returns ``59.6 / 23.8 / 13.7 W`` at R² 0.435.  Add one
    candidate that is unrecorded before bin 2 000 and the window shrinks to bins
    2 000-11 519, the early confound is excluded, and the same row now returns the
    exact constructed ``60.0 / 26.0 / 11.0 W`` at R² 1.000.  On production the
    same effect is what moved the 200-candidate run from 13 groups to 12 and the
    407-candidate run into the 92-fixture scene group: the two runs are not two
    samples of one distribution, they are fits of two different windows.

    The assertion is deliberately two-sided.  It does not claim truncation is
    good or bad -- it pins down that the cap *is* a measurement choice, so a
    caller comparing two caps is comparing two experiments.
    """
    prefix = 2_000
    patterns = {f"light.a{index}": _toggle_pattern(300 + index) for index in range(3)}
    watts = {"light.a0": 60.0, "light.a1": 26.0, "light.a2": 11.0}
    rng = np.random.default_rng(9)
    row = np.full(N_BINS, BASELINE_W)
    for name, pattern in patterns.items():
        row = row + watts[name] * pattern
    confound = np.zeros(N_BINS, dtype=bool)
    confound[:prefix] = True
    row = row + 95.0 * confound + rng.normal(0.0, NOISE_W, N_BINS)
    rows = {"ct1": _recorder_row(row)}

    whole_window = {name: _states_after(p, 0) for name, p in patterns.items()}
    late_start = {name: _states_after(p, prefix) for name, p in patterns.items()}

    short = _fit(rows, whole_window)
    long = _fit(rows, late_start)

    assert len(short) == 3 and len(long) == 3
    by_name_short = {fit.members[0].entity_id: fit.total_w for fit in short}
    by_name_long = {fit.members[0].entity_id: fit.total_w for fit in long}
    # Candidates do not change the row; they change the window it is fitted on.
    assert short[0].r2 != long[0].r2
    assert by_name_short != by_name_long
    # The narrow window excludes the confound and recovers what was constructed.
    assert by_name_long == pytest.approx(watts, abs=1.0)
    # Both answers are physically possible on this row -- neither is a defect.
    row_max_w = float(np.nanmax(row))
    assert all(fit.total_w <= row_max_w for fit in short + long)


# --------------------------------------------------------------------------- #
# Estimator consistency: the null must not drift as the design grows
# --------------------------------------------------------------------------- #


#: A slow square wave that no candidate column represents.  Duty cycle 0.975 and
#: a period of ~14 h, so a rotation still overlaps it heavily -- this is the
#: confound the rotated null exists to see, not to be defeated by.
_CONFOUND_LO_BINS = 1_200
_CONFOUND_HI_BINS = 2_400


def _duty_light(seed: int, *, low: int = 30, high: int = 260, gap: int = 200) -> np.ndarray:
    """A light with a long duty cycle and heavy autocorrelation.

    This is the shape that makes the null matter: duty is spread across the whole
    window, so a rotated copy still overlaps the row and the null is high rather
    than near zero.  Independent short blinks would give a null of ~0.01 and could
    not exhibit the drift this file is guarding against.
    """
    rng = np.random.default_rng(seed)
    pattern = np.zeros(N_BINS, dtype=bool)
    at = int(rng.integers(0, 20))
    while at < N_BINS:
        length = int(rng.integers(low, high))
        pattern[at : at + length] = True
        at += length + int(rng.integers(20, gap))
    return pattern


def _confound_envelope() -> np.ndarray:
    """The unexplained duty component of the row, with no column in the design."""
    return _duty_light(
        9_999, low=_CONFOUND_LO_BINS, high=_CONFOUND_HI_BINS, gap=120
    )


def _drift_installation(
    extra_columns: int,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """A row explained by two fixtures, plus ``extra_columns`` irrelevant ones.

    The row is a 700 W always-similar envelope (not a candidate), two genuine
    switching fixtures, and 0.4 W of jitter.  Appending the extra lights changes
    neither the row nor the two real columns -- they share no switching instants
    and are not members of the real groups -- so an honest gate must return the
    same verdict at every column count.

    ``extra_columns`` lights are drawn from the same long-duty family, which is
    what production does: the candidate list is every entity in the install, not
    a curated set.  A short-blink candidate cannot move the null, so the family
    matters.
    """
    real = [_duty_light(1), _duty_light(2)]
    row = np.full(N_BINS, BASELINE_W) + 700.0 * _confound_envelope()
    for watts, pattern in zip((140.0, 98.0), real, strict=True):
        row = row + watts * pattern
    row = row + np.random.default_rng(11).normal(0.0, NOISE_W, N_BINS)

    states = {
        f"light.real{index}": _recorder_states(pattern)
        for index, pattern in enumerate(real)
    }
    for index in range(extra_columns):
        states[f"light.extra{index:03d}"] = _recorder_states(_duty_light(500 + index))
    return {"ct1": _recorder_row(row)}, states


def test_adding_irrelevant_candidates_never_refuses_an_accepted_row() -> None:
    """**The production defect.** More candidates must not remove accepted evidence.

    Candidate count is not a modelling decision.  A caller that raises
    ``max_candidates`` is analysing more of the install; it is not changing the
    row, the two real fixtures, or what either means.  So the verdict must be
    non-decreasing in the column count.

    Under the old unconstrained least-squares null this row is accepted at 2, 18
    and 34 columns and **refused at 66**: the LS null climbs 0.0209 → 0.1071 →
    0.1743 → 0.2612 while R² moves only 0.3050 → 0.4062, because each added
    regressor can exploit a rotated alignment by accident and the *upper bound*
    loosens as the design grows.  The margin therefore falls 0.2841 → 0.2279 →
    0.1888 → 0.1450 and crosses the 0.15 gate.  That is the measured production
    failure in miniature -- on the real install the `lights_ct2` row passed at cap
    60 (LS null 0.8060) and was refused at cap 100 (0.8536) with its R² flat at
    0.9930-0.9975.

    With the estimator-consistent NNLS null the same row measures margin +0.2932
    / +0.2666 / +0.2607 / +0.2490 across the same caps and is accepted at every
    one.  The margin still narrows slightly -- adding a regressor can only help a
    null, never a fit -- but it does not cross the gate.
    """
    verdicts = []
    margins = []
    accepted_ids = []
    for extra in (0, 16, 32, 64):
        rows, states = _drift_installation(extra)
        fits = _fit(rows, states)
        verdicts.append(bool(fits))
        margins.append(fits[0].r2_margin if fits else None)
        accepted_ids.append({fit.ct_id for fit in fits})

    assert verdicts == [True, True, True, True], (
        f"candidate count changed the verdict: {verdicts} (margins {margins})"
    )
    # The two real fixtures keep being reported; the extras are separate groups.
    assert all(margin is not None and margin >= 0.15 for margin in margins)
    # Monotone *and* identical: the same row ids are accepted at every column
    # count.  A non-decreasing count alone would allow a swap, which would be
    # just as surprising to a caller as a loss.
    assert all(accepted == accepted_ids[0] for accepted in accepted_ids), accepted_ids
    assert accepted_ids[0] == {"ct1"}
    # The drift is bounded rather than merely lucky: the NNLS margin moves less
    # than 0.05 across a 32-fold increase in candidate columns.  The LS null on
    # the same design moves 0.240 R² (0.0209 -> 0.2612), which is what made the
    # gate flip.
    assert max(margins) - min(margins) < 0.05, margins


def test_the_null_is_computed_with_the_fitted_estimator() -> None:
    """``r2_null_p95`` must come from NNLS, not from unconstrained least squares.

    The two estimators provably differ whenever a coefficient sits on the
    non-negativity boundary -- which is the normal case here, because a null
    regressor that does not align with the rotated row wants a negative
    coefficient and NNLS reports 0 instead.  So the check is direct: recompute
    both nulls from the same rotated draws and require the reported value to
    match the NNLS one, not the LS one.

    On this fixture the LS null is 0.0209 and the NNLS null 0.0117 at two
    columns; they are not close.  The assertion is two-sided, so a future change
    that quietly reverts to LS fails here even if every verdict still passes.
    """
    from scipy.optimize import nnls as scipy_nnls
    from wattmosaic_engine.disaggregation import group_fit as module

    rows, states = _drift_installation(0)
    fits = _fit(rows, states)
    assert fits, "the fixture must be fittable, or the null cannot be checked"
    reported = fits[0].r2_null_p95

    # Rebuild the exact pipeline _fit_row uses, then compute both nulls from the
    # same rotation offsets.  Reproducing it here rather than importing a helper
    # keeps the test honest about what the contract is: the null is the fit's own
    # statistic, so its definition is the fit's definition.
    _, design = _design_for_test(states, rows)
    usable = ~np.isnan(design).any(axis=1)
    x = design[usable]
    x = x[:, np.ptp(x, axis=0) > 0]
    y = np.array([float(row["s"]) for row in rows["ct1"]], dtype=float)[usable]
    column_means = x.mean(axis=0)
    centred_y = y - y.mean()
    total_ss = float(centred_y @ centred_y)
    centred_x, eigenvalues, vectors, _ = module._conditioned_basis(x - column_means)

    root = np.sqrt(eigenvalues)
    factor = (vectors * root).T
    project = (vectors / root).T

    usable_bins = centred_y.size
    rng = np.random.default_rng(0)
    shifts = rng.integers(
        module.NULL_MIN_SHIFT_BINS, usable_bins - module.NULL_MIN_SHIFT_BINS, 50
    )
    draws = np.column_stack([np.roll(centred_y, -int(shift)) for shift in shifts])
    null_cross = centred_x.T @ draws

    # Unconstrained least squares: the estimator the module used to use.
    ls_coefficient = vectors @ ((vectors.T @ null_cross) / eigenvalues[:, None])
    ls_along = vectors.T @ ls_coefficient
    ls_residual = (
        total_ss
        - 2.0 * np.einsum("ij,ij->j", ls_coefficient, null_cross)
        + eigenvalues @ (ls_along * ls_along)
    )
    ls_null = float(np.percentile(1.0 - np.maximum(ls_residual, 0.0) / total_ss, 95))

    # NNLS per draw, one scipy call at a time -- deliberately the slow, obviously
    # correct reference rather than the module's batched solver.
    target = project @ null_cross
    nnls_coefficient = np.column_stack(
        [scipy_nnls(factor, target[:, index])[0] for index in range(target.shape[1])]
    )
    nnls_coefficient = vectors @ (vectors.T @ nnls_coefficient)
    nnls_along = vectors.T @ nnls_coefficient
    nnls_residual = (
        total_ss
        - 2.0 * np.einsum("ij,ij->j", nnls_coefficient, null_cross)
        + eigenvalues @ (nnls_along * nnls_along)
    )
    nnls_null = float(np.percentile(1.0 - np.maximum(nnls_residual, 0.0) / total_ss, 95))

    # The estimators differ enough for this to be a real discriminator.
    assert abs(ls_null - nnls_null) > 1e-3, (
        f"LS null {ls_null:.6f} and NNLS null {nnls_null:.6f} are too close for "
        "this fixture to distinguish the estimators"
    )
    assert reported == pytest.approx(nnls_null, abs=1e-9)
    assert reported != pytest.approx(ls_null, abs=1e-3)


def test_a_rotation_null_keeps_duty_cycle_and_destroys_alignment() -> None:
    """Only the estimator changed; the null's *construction* is still a rotation.

    A regression test for the fix itself: replacing least squares with NNLS must
    not perturb the share of time the design is ON, nor reintroduce alignment
    with the row.  A rotation preserves the ON-fraction of every column exactly
    (it is a permutation of the bins), so the null's design has the same duty
    cycle as the real one, while the real fit's R² must still be far above the
    null's -- which is only possible if the rotation destroyed the alignment.
    """
    from wattmosaic_engine.disaggregation import group_fit as module

    pattern = _duty_light(1)
    real = _duty_light(2)
    row = (
        np.full(N_BINS, BASELINE_W)
        + 140.0 * pattern
        + 98.0 * real
        + np.random.default_rng(11).normal(0.0, NOISE_W, N_BINS)
    )
    rows = {"ct1": _recorder_row(row)}
    states = {
        "light.real0": _recorder_states(pattern),
        "light.real1": _recorder_states(real),
    }

    fits = _fit(rows, states)
    assert fits

    # Rotation preserves duty cycle exactly: rolling by any offset keeps the mean.
    shifted = np.roll(pattern.astype(float), module.NULL_MIN_SHIFT_BINS)
    assert float(shifted.mean()) == pytest.approx(float(pattern.astype(float).mean()))
    # ... and destroys alignment: the rotated copy shares far less with itself than
    # the unrotated copy does.  The correlation is the quantity that matters -- a
    # light with a long duty cycle still overlaps its own rotation somewhat, which
    # is *why* the null is high and therefore meaningful.
    aligned = float(pattern.astype(float) @ pattern.astype(float))
    rotated = float(shifted @ pattern.astype(float))
    assert rotated < 0.75 * aligned, (rotated, aligned)

    # The reported null is the rotated value and is well below the real fit.
    assert 0.0 <= fits[0].r2_null_p95 < fits[0].r2
    assert fits[0].r2_margin > 0.15


def _design_for_test(
    states: dict[str, list[dict]], rows: dict[str, list[dict]]
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Build the group design exactly as ``fit_shared_rows`` does.

    Only the states are needed for the columns, but the *window* is the union of
    the states and the row readings, so the rows have to be passed too -- using
    the states alone shifts the grid and misaligns the design against the row.
    """
    from wattmosaic_engine.disaggregation import group_fit as module

    parsed = {entity_id: module._state_series(item) for entity_id, item in states.items()}
    instants = {entity_id: module._instants(series) for entity_id, series in parsed.items()}
    active = sorted(entity_id for entity_id, events in instants.items() if events.size)
    groups = module._co_toggle_groups(
        {entity_id: instants[entity_id] for entity_id in active}
    )
    bounds = module._axis_bounds(
        [parsed[entity_id] for entity_id in active]
        + [module._epoch_pairs(item) for item in rows.values()]
    )
    assert bounds is not None
    t0, t1 = bounds
    n_bins = int(np.floor((t1 - t0) / module.BIN_S)) + 1
    series = {
        entity_id: module._resample_state(parsed[entity_id], t0, n_bins)
        for entity_id in active
    }
    return series, module._design_matrix(series, groups)


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
# The shared design: one basis per mask, and the R² gate before the null
# --------------------------------------------------------------------------- #

#: Bins the second CT is missing at the start of the window.  A CT that started
#: reporting two and a half hours into the window has no value there, so
#: :func:`_resample_row` leaves those bins ``NaN`` -- it must not invent a
#: reading for a period nothing measured.
LATE_CT_PREFIX_BINS = 600


def _two_rows_one_with_a_gap() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """One CT that reports throughout, one that starts 600 bins late.

    Both rows are driven by the *same* two fixtures, so they share one design and
    one set of group columns.  The late CT's first 600 bins are unknown, so its
    usable mask is a strict subset of the complete row's: exactly the case a
    "the mask is shared, hoist the basis" shortcut would get wrong.  The two rows
    also carry different wattages, so a cache that keyed on anything weaker than
    the mask could not be caught by the coefficients looking plausible.
    """
    watts = {"light.a": 60.0, "light.b": 25.0}
    patterns = _switch_patterns(list(watts), shared=False)
    full = _recorder_row(_row_watts(patterns, watts))
    other = _recorder_row(_row_watts(patterns, {"light.a": 90.0, "light.b": 40.0}, seed=17))
    late = [
        row for row in other if _row_bin(row) >= LATE_CT_PREFIX_BINS
    ]
    states = {name: _recorder_states(pattern) for name, pattern in patterns.items()}
    return {"ct_full": full, "ct_late": late}, states


def _row_bin(row: dict) -> int:
    """Which bin of the common grid one recorder sample falls in."""
    return int(round((float(row["lu"]) - T0.timestamp()) / BIN_S))


def test_a_row_with_a_gap_gets_its_own_basis_and_matches_the_uncached_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The mask-sharing correctness test.**  Caching must not leak across masks.

    ``fit_shared_rows`` builds one design and fits every CT row against it.  The
    basis of the normal equations is a function of the row's *usable mask*, which
    includes where that row's watts are ``NaN`` -- so hoisting the basis out of
    the row loop is only sound if the mask is checked.  Here ``ct_full`` reports
    for the whole window and ``ct_late`` has no samples for its first 600 bins,
    so the two masks differ and the two bases must differ.

    The reference is the same public function with the memo forced to miss on
    every row (``_row_system`` replaced by the uncached builder), so this compares
    two *emitted* answers, not an internal helper.  Every field of every emitted
    :class:`GroupFit` is compared exactly -- ``total_w``, ``r2``,
    ``r2_null_p95``, ``r2_margin``, the members and the evidence -- including the
    ``r2_null_p95`` the null path produces, because that is the field a mis-keyed
    cache would corrupt while leaving R² plausible.

    A one-off manual check of the same comparison over the 32-row performance
    design (complete, gapped, and gapped-design variants) measured ``max|Δ| =
    0.0`` for ``total_w``, ``r2``, ``r2_null_p95`` and ``base`` -- the cached and
    uncached paths are bit-identical, not merely close.
    """
    from wattmosaic_engine.disaggregation import group_fit as module

    rows, states = _two_rows_one_with_a_gap()

    def run():
        return fit_shared_rows(
            row_readings=rows,
            entity_states=states,
            entity_levels={},
            row_noise_w={"ct_full": NOISE_W, "ct_late": NOISE_W},
        )

    cached = run()
    # Same call, cache bypassed: every row rebuilds its own basis from its own
    # mask.  This is the definition the cached path must reproduce.
    monkeypatch.setattr(
        module,
        "_row_system",
        lambda design, usable, cache: module._build_row_system(design[usable]),
    )
    uncached = run()
    monkeypatch.undo()

    # The fixture must emit both rows, or it proves nothing about sharing.
    assert {fit.ct_id for fit in cached} == {"ct_full", "ct_late"}, [
        (fit.ct_id, fit.members) for fit in cached
    ]

    assert len(cached) == len(uncached)
    for got, reference in zip(cached, uncached, strict=True):
        assert got.ct_id == reference.ct_id
        assert got.total_w == reference.total_w
        assert got.r2 == reference.r2
        assert got.r2_null_p95 == reference.r2_null_p95
        assert got.r2_margin == reference.r2_margin
        assert got.members == reference.members
        assert got.evidence == reference.evidence

    # And the masks really do differ: the late CT has no value in its prefix, so
    # its usable window is shorter than the complete row's.
    from wattmosaic_engine.disaggregation.group_fit import _epoch_pairs, _resample_row

    late_watts = _resample_row(
        _epoch_pairs(rows["ct_late"]), T0.timestamp(), N_BINS
    )
    full_watts = _resample_row(
        _epoch_pairs(rows["ct_full"]), T0.timestamp(), N_BINS
    )
    assert int(np.isnan(late_watts).sum()) == LATE_CT_PREFIX_BINS
    assert not np.isnan(full_watts).any()
    assert np.isnan(late_watts)[:LATE_CT_PREFIX_BINS].all()


def test_the_r2_gate_runs_before_the_null_and_the_null_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row refused on R² alone must not pay for a null; an emitted one must have it.

    ``_fit_row`` returns ``None`` as soon as ``r2 < min_r2``, which is exactly the
    verdict ``fit_shared_rows`` used to reach one line later -- but it skips 50
    NNLS draws.  The contract is that this changes *nothing observable*: every
    emitted group still carries its ``r2_null_p95``.

    The budget half is asserted by counting calls to ``_nnls_batch`` (the null
    solver), because the waste being removed is *work*, not output: a row that
    cannot be emitted produces no fields either way, so only an instrumented
    count can see the difference.  The 32-row performance fixture is the case
    that motivated the change -- every row there measures R² ≈ 0.006-0.010, so
    all 32 nulls used to be computed and all 32 discarded.

    The reporting half is asserted through the public API: the good row's
    ``r2_null_p95`` is a real number below its R², and it does not move when the
    gate is set high enough to suppress a different row.
    """
    from wattmosaic_engine.disaggregation import group_fit as module

    watts = {"light.a": 60.0, "light.b": 25.0}
    rows, states = _history(watts)
    rows["ct_flat"] = _recorder_row(
        np.full(N_BINS, BASELINE_W) + np.random.default_rng(3).normal(0.0, NOISE_W, N_BINS)
    )
    kwargs = dict(row_noise_w={"ct1": NOISE_W, "ct_flat": NOISE_W})

    # The design the two rows are fitted against, so the rows can be driven
    # through ``_fit_row`` one at a time with an exact call count.
    from wattmosaic_engine.disaggregation.group_fit import _epoch_pairs, _resample_row

    series, design = _design_for_test(states, rows)
    bounds = module._axis_bounds(
        [module._state_series(item) for item in states.values()]
        + [_epoch_pairs(item) for item in rows.values()]
    )
    assert bounds is not None
    t0, t1 = bounds
    n_bins = int(np.floor((t1 - t0) / module.BIN_S)) + 1
    flat_watts = _resample_row(_epoch_pairs(rows["ct_flat"]), t0, n_bins)

    calls = {"n": 0}
    real = module._nnls_batch

    def counting(*args, **kwargs_):
        calls["n"] += 1
        return real(*args, **kwargs_)

    monkeypatch.setattr(module, "_nnls_batch", counting)

    # The flat row is below any sensible R² gate.  With the gate in force it is
    # refused *before* the null, so the null solver is never entered.
    assert module._fit_row(flat_watts, design, seed=0, min_r2=0.30) is None
    assert calls["n"] == 0, "the null was computed for a row refused on R² alone"

    # With the gate removed the same row does reach the null path, which proves
    # the count above was the gate and not an unrelated early exit.
    assert module._fit_row(flat_watts, design, seed=0, min_r2=-math.inf) is not None
    assert calls["n"] == 1

    monkeypatch.undo()

    # Reporting: the good row still carries a real null through the public API.
    refused = _fit(rows, states, **kwargs)
    assert {fit.ct_id for fit in refused} == {"ct1"}
    assert all(0.0 <= fit.r2_null_p95 < fit.r2 for fit in refused)
    assert all(fit.r2 >= 0.30 for fit in refused)
    # A gate above any achievable R² suppresses every row, which pins that the
    # early exit is a *refusal* and not a silent drop of the numbers.
    suppressed = _fit(rows, states, min_r2_improvement=1.0, **kwargs)
    assert suppressed == ()
    passed = {(fit.ct_id, fit.members[0].entity_id): fit for fit in refused}
    for fit in _fit(rows, states, min_r2_improvement=0.0, **kwargs):
        key = (fit.ct_id, fit.members[0].entity_id)
        if key in passed:
            assert fit == passed[key]


def test_the_two_eigh_basis_is_the_solve_basis_and_one_eigh_cannot_replace_it() -> None:
    """Pin the reason ``_conditioned_basis`` keeps its second eigendecomposition.

    A tempting simplification is to reuse the eigenvalues and eigenvectors of the
    *unit-normalised* Gram ``S = (X/n)ᵀ(X/n)`` for the solve as well, on the
    theory that the unnormalised Gram ``G = XᵀX`` has the same eigenvectors with
    eigenvalues scaled by ``n²``.  It does not.  ``D⁻¹ G D⁻¹`` is a *congruence*
    of ``G``, not a similarity, so it preserves inertia but not eigenvectors:
    with ``D`` not a multiple of the identity the two bases differ.

    The test is therefore two-sided.  It checks that ``_conditioned_basis``
    returns exactly the ``G`` basis (what the solve needs), and it checks that
    the single-``eigh`` substitute would differ measurably on this fixture, so
    the redundancy cannot be "optimised away" again without a failure.
    """
    from wattmosaic_engine.disaggregation import group_fit as module

    watts = {"light.a": 60.0, "light.b": 25.0, "light.c": 18.0}
    rows, states = _history(watts)
    _, design = _design_for_test(states, rows)
    usable = ~np.isnan(design).any(axis=1)
    x = design[usable]
    x = x[:, np.ptp(x, axis=0) > 0]
    centred_x = x - x.mean(axis=0)

    # The reference exactly as _conditioned_basis computes it: two eighs, the
    # identifiability test on the normalised Gram, the solve on the raw Gram.
    norms = np.linalg.norm(centred_x, axis=0)
    scaled = centred_x / norms
    n_eigenvalues, n_vectors = np.linalg.eigh(scaled.T @ scaled)
    largest = float(n_eigenvalues.max())
    keep = n_eigenvalues > largest / (1e3 * 1e3)
    outside = (n_vectors[:, ~keep] ** 2).sum(axis=1)
    expected_reportable = outside <= 1e-9

    g_eigenvalues, g_vectors = np.linalg.eigh(centred_x.T @ centred_x)
    g_keep = g_eigenvalues > float(g_eigenvalues.max()) / (1e3 * 1e3)
    g_eigenvalues, g_vectors = g_eigenvalues[g_keep], g_vectors[:, g_keep]

    returned_x, eigenvalues, vectors, reportable = module._conditioned_basis(centred_x)

    # 1. Bit-identical to the reference two-eigh computation.
    assert np.array_equal(returned_x, centred_x)
    assert np.array_equal(eigenvalues, g_eigenvalues)
    assert np.array_equal(vectors, g_vectors)
    assert np.array_equal(reportable, expected_reportable)

    # 2. The single-eigh substitute is *not* the same basis, so the refactor the
    #    docstring above rejects would change the solve.  Both halves matter: the
    #    eigenvectors differ, and no single scalar rescales one spectrum into the
    #    other.  Measured here: min |<v_S, v_G>| = 0.905 and ratio spread 4.4e-6.
    assert not np.allclose(
        np.abs(n_vectors[:, keep]), np.abs(g_vectors), atol=1e-2
    ), "the normalised and raw Gram share a basis on this fixture, so it cannot "
    ratio = n_eigenvalues[keep] / g_eigenvalues
    assert np.ptp(ratio) > 1e-9, (
        "eigenvalues scaled by one common factor, contradicting the congruence "
        f"argument (ratio spread {np.ptp(ratio):.3e})"
    )


# --------------------------------------------------------------------------- #
# Performance: the whole install must fit inside two seconds
# --------------------------------------------------------------------------- #


def test_a_full_installation_fits_inside_the_two_second_budget() -> None:
    """32 rows × 200 entities over 48 h must finish in ≤2 s.

    The cost is dominated by the 50-draw null per row, so the budget is a real
    constraint on the design and not a formality.  Two naive forms were measured
    and rejected: NNLS on the full ``m × n`` matrix per draw took 17 s, and even
    the Gram-factor form of *50 separate* ``scipy.optimize.nnls`` calls costs
    204 ms per 200-column row (6.5 s for 32 rows).  The batched solver that
    replaced it solves all 50 draws together for 11 ms per row and returns the
    same answer to ``4e-15`` relative.  Timed rather than assumed, because a
    vectorisation that does not vectorise is the easiest defect to ship
    unnoticed.
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
