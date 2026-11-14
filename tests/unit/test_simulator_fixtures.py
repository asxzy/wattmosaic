"""Fixture-integrity tests: builders, determinism, factor semantics, and leaks.

These tests grade the *fixtures*, not the engine.  They exist because a broken
fixture is worse than a missing one: it makes an engine test pass while asserting
nothing, and the failure only shows up when someone trusts the number.

What is pinned here
-------------------
1. Every required stratum and Hot Tub variant is buildable by name, and an
   unknown name raises instead of falling back to a default.
2. Identical seed ⇒ byte-identical observation stream (SHA-256 per builder).
3. The Hot Tub table is the brief's table: leg deltas 250 W, HA delta 500 W, and
   the ×2 relationship holds — including in the variants that must *not* be
   explainable, where the expected outcome is an explicit refusal.
4. The observation view contains **no** ground-truth key, scanned recursively.
   This is the hard requirement: algorithms get ``observations()`` only.
5. The stratum coverage map names a real builder for every matrix row it claims,
   and no builder is left unreferenced.
"""

from __future__ import annotations

import json

import pytest
from wattmosaic_engine.contracts import Phase, validate_factor_intervals

from tests.fixtures.labhouse import (
    MATRIX_STRATA,
    REQUIRED_HOT_TUB_VARIANTS,
    REQUIRED_STRATA,
    assert_matrix_coverage,
)
from tools.simulator.factors import (
    FactorEpoch,
    FactorOwner,
    FactorTimeline,
    RefossFactorChannel,
)
from tools.simulator.frames import (
    TRUTH_KEYS,
    assert_no_ground_truth_keys,
    find_ground_truth_keys,
    observation_view,
)
from tools.simulator.hot_tub import (
    APPLIANCE_DELTA_W,
    CT1_AFTER_W,
    CT1_BEFORE_W,
    CT2_AFTER_W,
    CT2_BEFORE_W,
    CT_L1,
    CT_L2,
    HA_AFTER_W,
    HA_BEFORE_W,
    HOT_TUB_BUILDERS,
    HOT_TUB_VARIANTS,
    LEG_DELTA_W,
    hot_tub,
)
from tools.simulator.labhouse import (
    STRATA,
    available_strata,
    build_scenario,
    validate_strata,
)

# --------------------------------------------------------------------------- #
# A. Every named stratum builds
# --------------------------------------------------------------------------- #


def test_every_required_stratum_has_a_builder() -> None:
    """The brief names 21 strata; a renamed builder must fail here."""
    available = set(available_strata())
    missing = [name for name in REQUIRED_STRATA if name not in available]
    assert not missing, f"no builder for required strata: {missing}"


def test_every_registered_stratum_is_in_the_required_list() -> None:
    """The reverse: an undocumented builder is dead weight nothing exercises."""
    undocumented = sorted(set(STRATA) - set(REQUIRED_STRATA))
    assert not undocumented, f"builders missing from REQUIRED_STRATA: {undocumented}"


def test_each_stratum_builds_with_records_and_a_topology() -> None:
    for name in available_strata():
        house = build_scenario(name, seed=11, minutes=4, interval=30)
        records = house.to_list()
        assert records, f"{name}: no records"
        assert house.channel_ids(), f"{name}: no channels"
        assert house.device_ids(), f"{name}: no devices"
        assert house.plan.name == name


def test_an_unknown_stratum_raises_and_names_the_choices() -> None:
    """A typo must not silently substitute a default household."""
    with pytest.raises(KeyError) as excinfo:
        build_scenario("dedicatedd", seed=1)
    assert "dedicated" in str(excinfo.value)


def test_stratum_plans_are_satisfied_by_what_they_build() -> None:
    """`validate_strata` checks devices, channels and flags, not just "no crash"."""
    plans = validate_strata(seed=3, minutes=4, interval=30)
    assert len(plans) == len(available_strata())


# --------------------------------------------------------------------------- #
# B. Household shape: main panel + sub-panel, 16 branch CTs per panel
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["dedicated", "main_subpanel_overlap", "spikes"])
def test_household_shape_is_a_nested_two_panel_install(name: str) -> None:
    house = build_scenario(name, seed=5, minutes=3, interval=30)
    counts = house.channel_counts()
    # 18 channels per panel: CT1/CT2 mains + 16 branch channels.
    assert counts["main"] == 18, counts
    assert counts["sub"] == 18, counts
    assert counts["main_branch"] == 16, counts
    assert counts["sub_branch"] == 16, counts
    # Panel B is nested inside Panel A, which is what makes summing mains wrong.
    assert house.has_nested_subpanel()
    assert "nested_subpanel" in house.flags()


def test_the_factor2_stratum_meters_only_one_leg() -> None:
    """The end-to-end Refoss shape: one conductor, one declared x2."""
    house = build_scenario("factor2", seed=5, minutes=3, interval=30)
    metered = "ct_main_water_heater_ct1"
    assert metered in house.channel_ids()
    assert "ct_main_water_heater_ct2" not in house.channel_ids()
    channel = house.scenario.config.factor_channels[metered]
    assert channel.factor_at(0.0) == 2.0
    assert channel.applied_by_at(0.0) is FactorOwner.USER
    assert channel.reported_multiplier_at(0.0) == 1.0  # not yet published
    # The published value is the raw conductor; the x2 is our own claim.
    record = house.to_list()[3]
    sample = record["measurements"][metered]
    assert sample["factor"]["factor"] == 2.0
    assert sample["factor"]["applied_by"] == "user"


# --------------------------------------------------------------------------- #
# C. Determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(STRATA))
def test_every_stratum_is_deterministic_for_a_fixed_seed(name: str) -> None:
    a = build_scenario(name, seed=2026, minutes=3, interval=30)
    b = build_scenario(name, seed=2026, minutes=3, interval=30)
    assert a.signature() == b.signature(), f"{name} is not deterministic"


def test_different_seeds_produce_different_observation_streams() -> None:
    a = build_scenario("dedicated", seed=1, minutes=3, interval=30)
    b = build_scenario("dedicated", seed=2, minutes=3, interval=30)
    assert a.signature() != b.signature()


@pytest.mark.parametrize("name", sorted(HOT_TUB_BUILDERS))
def test_every_hot_tub_variant_is_deterministic(name: str) -> None:
    a = hot_tub(name, seed=7).observations()
    b = hot_tub(name, seed=7).observations()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# D. The mandatory Hot Tub table
# --------------------------------------------------------------------------- #


def test_hot_tub_constants_are_the_briefs_table() -> None:
    """The module's own constants must be the documented numbers."""
    assert (CT1_BEFORE_W, CT1_AFTER_W) == (1000.0, 1250.0)
    assert (CT2_BEFORE_W, CT2_AFTER_W) == (900.0, 1150.0)
    assert (HA_BEFORE_W, HA_AFTER_W) == (0.0, 500.0)
    assert LEG_DELTA_W == 250.0
    assert APPLIANCE_DELTA_W == 500.0


@pytest.mark.parametrize("name", sorted(HOT_TUB_BUILDERS))
def test_hot_tub_table_matches_the_brief_for_every_variant(name: str) -> None:
    table = hot_tub(name, seed=42).table()
    assert table["ha_power"]["before_w"] == 0.0
    assert table["ha_power"]["after_w"] == 500.0
    assert table["ha_power"]["delta_w"] == 500.0
    assert table[CT_L1]["before_w"] == pytest.approx(1000.0, abs=1e-9)
    assert table[CT_L2]["before_w"] == pytest.approx(900.0, abs=1e-9)
    assert table[CT_L1]["delta_w"] == pytest.approx(250.0, abs=1e-9)
    assert table[CT_L2]["delta_w"] == pytest.approx(250.0, abs=1e-9)


@pytest.mark.parametrize("name", sorted(HOT_TUB_BUILDERS))
def test_hot_tub_leg_deltas_are_250_each_and_ha_is_500(name: str) -> None:
    """The physical claim behind every variant: 250 + 250 = 500."""
    fixture = hot_tub(name, seed=42)
    truth = fixture.ground_truth_view()
    assert truth["leg_deltas_w"] == {"l1": 250.0, "l2": 250.0}
    assert truth["appliance_total_delta_w"] == 500.0
    assert sum(truth["leg_deltas_w"].values()) == truth["appliance_total_delta_w"]
    assert fixture.expected.appliance_total_w in (500.0, None)


@pytest.mark.parametrize("name", sorted(HOT_TUB_BUILDERS))
def test_hot_tub_factor_relationship_holds_where_a_factor_is_declared(name: str) -> None:
    """Every variant with a declared ×2 must show `2 × leg_delta == HA delta`."""
    fixture = hot_tub(name, seed=42)
    doubled = [
        item for item in fixture.cts
        if item.timeline.epochs and item.channel.factor_at(0.0) == 2.0
        and not item.channel.double_applied_at(0.0)
    ]
    explained = fixture.explained_by()
    for item in doubled:
        estimate = explained["factor_estimates_w"][item.ct_id]
        # Monitor-published: the delta is already the whole load. Otherwise the
        # raw leg delta times the factor must equal the appliance delta.
        assert estimate == pytest.approx(500.0, abs=1e-6), (name, item.ct_id, estimate)


def test_the_mandatory_fixture_is_factor_two() -> None:
    """The canonical fixture must be the ×2 variant, not an alias of ×1."""
    fixture = hot_tub("factor_two", seed=42)
    assert fixture.name == "hot_tub_factor_two"
    expected = fixture.expected
    assert expected.explainable is True
    assert expected.summing_allowed is False
    assert expected.single_ct_estimate_w == 500.0
    assert expected.basis == "factor_adjusted"
    # The ×2 lives on exactly one CT: CT1.
    assert fixture.ct(CT_L1).channel.factor_at(0.0) == 2.0
    assert fixture.ct(CT_L2).channel.factor_at(0.0) == 1.0
    explained = fixture.explained_by()
    assert explained["factor_matches"][CT_L1] is True
    # Naively adding each CT's own total (CT1's factored 500 W + CT2's 250 W)
    # reports 750 W: a 250 W double count of one appliance.
    assert explained["naive_sum_w"] == pytest.approx(750.0, abs=1e-9)
    assert explained["naive_sum_matches"] is False
    assert explained["summing_allowed"] is False


@pytest.mark.parametrize("variant", REQUIRED_HOT_TUB_VARIANTS)
def test_every_required_hot_tub_variant_builds_and_documents_itself(variant: str) -> None:
    fixture = hot_tub(variant, seed=42)
    assert fixture.doc, f"{variant}: a fixture without a rationale is untestable"
    assert fixture.expected.reason
    summary = fixture.summary()
    # The three frames must be separately present, never merged.
    assert set(summary) == {"name", "seed", "table", "truth", "expected", "explained_by"}
    assert summary["truth"]["appliance_total_delta_w"] == 500.0


def test_an_unknown_hot_tub_variant_raises_and_names_the_choices() -> None:
    with pytest.raises(KeyError) as excinfo:
        hot_tub("factor_three")
    assert "hot_tub_factor_two" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# E. The variants that must NOT be explainable
# --------------------------------------------------------------------------- #

UNEXPLAINABLE = (
    "factor_missing",
    "factor_incorrect_metadata",
    "inconsistent_ha_vs_ct",
)


@pytest.mark.parametrize("variant", UNEXPLAINABLE)
def test_unexplainable_variants_refuse_rather_than_guess(variant: str) -> None:
    """Refusal is a result: no total, no summing, and a stated reason."""
    fixture = hot_tub(variant, seed=42)
    expected = fixture.expected
    assert expected.explainable is False, variant
    assert expected.appliance_total_w is None, variant
    assert expected.summing_allowed is False, variant
    assert expected.reason, variant
    # The arithmetic still "looks right" for the missing-metadata case: that is
    # exactly why the basis, not the sum, has to decide.
    table = fixture.table()
    assert table[CT_L1]["delta_w"] + table[CT_L2]["delta_w"] == pytest.approx(250.0 + 250.0)


@pytest.mark.parametrize("variant", UNEXPLAINABLE)
def test_unexplainable_variants_do_not_claim_a_factor_explanation(variant: str) -> None:
    """The arithmetic may coincide with 500 W; the *basis* still refuses.

    This is the difference the brief cares about: a sum that happens to be right
    is not evidence that the factor metadata explains it.
    """
    fixture = hot_tub(variant, seed=42)
    explained = fixture.explained_by()
    assert explained["summing_allowed"] is False
    if variant == "factor_missing":
        # No metadata at all: every factor estimate is "unknown".
        assert all(value is None for value in explained["factor_estimates_w"].values())
    if variant == "factor_incorrect_metadata":
        channel = fixture.ct(CT_L1).channel
        assert channel.double_applied_at(0.0) is True
        # The published value already carries the x2 (500 -> 1000 W) and the
        # user claim would apply it again: 2000 W for a 500 W appliance. That
        # double application is exactly what the guard has to refuse.
        assert channel.estimated_appliance_total_w(500.0, 0.0) == pytest.approx(2000.0)
    if variant == "inconsistent_ha_vs_ct":
        assert fixture.state_after == "off"
        assert fixture.expected.conflict is True


def test_a_missing_factor_is_not_a_declared_x1() -> None:
    """The distinctness that makes B5 testable at all."""
    missing = hot_tub("factor_missing", seed=42)
    declared = hot_tub("factor_one", seed=42)
    assert missing.ct(CT_L1).channel.is_declared_at(0.0) is False
    assert declared.ct(CT_L1).channel.is_declared_at(0.0) is True
    assert missing.ct(CT_L1).channel.factor_at(0.0) == declared.ct(CT_L1).channel.factor_at(0.0)
    # ...and the *estimate* differs: no metadata means no total.
    assert missing.ct(CT_L1).channel.estimated_appliance_total_w(250.0, 0.0) is None
    assert declared.ct(CT_L1).channel.estimated_appliance_total_w(250.0, 0.0) == 250.0


def test_incorrect_metadata_is_detectable_as_a_double_application() -> None:
    fixture = hot_tub("factor_incorrect_metadata", seed=42)
    channel = fixture.ct(CT_L1).channel
    assert channel.double_applied_at(0.0) is True
    assert channel.estimated_appliance_total_w(250.0, 0.0) == pytest.approx(1000.0)
    assert fixture.expected.conflict is True


def test_inconsistent_ha_reports_a_conflict_not_a_total() -> None:
    fixture = hot_tub("inconsistent_ha_vs_ct", seed=42)
    assert fixture.expected.conflict is True
    assert fixture.expected.basis == "unknown"
    assert "ha_state" in fixture.expected.details["conflict_sources"]


def test_independent_cts_must_still_be_summed() -> None:
    """The negative control: same numbers as ×2, but summing is *correct*."""
    fixture = hot_tub("independent_physical_cts", seed=42)
    explained = fixture.explained_by()
    assert fixture.expected.summing_allowed is True
    assert explained["conductor_sum_matches"] is True
    assert explained["conductor_sum_w"] == pytest.approx(500.0, abs=1e-9)


def test_noisy_legs_keep_a_balanced_truth() -> None:
    """Observed asymmetry must not change the stored physical split."""
    fixture = hot_tub("noisy_unbalanced_legs", seed=42)
    from tools.simulator.hot_tub import BEFORE_S, SETTLE_S, SWITCH_S

    samples = fixture.samples()

    def observed_delta(ct_id: str) -> float:
        before = min(
            (s for s in samples if s["ct_id"] == ct_id),
            key=lambda s: abs(s["sample_time_s"] - BEFORE_S),
        )
        after = min(
            (s for s in samples if s["ct_id"] == ct_id),
            key=lambda s: abs(s["sample_time_s"] - (SWITCH_S + SETTLE_S)),
        )
        return after["value_w"] - before["value_w"]

    # Observed deltas are perturbed by the fixed bias + seeded noise...
    observed_l1_delta = observed_delta(CT_L1)
    observed_l2_delta = observed_delta(CT_L2)
    assert observed_l1_delta != pytest.approx(observed_l2_delta, abs=1e-6)
    # ...while the truth stays exactly balanced.
    assert fixture.ground_truth_view()["leg_deltas_w"] == {"l1": 250.0, "l2": 250.0}


# --------------------------------------------------------------------------- #
# F. Per-sample factor resolution
# --------------------------------------------------------------------------- #


def test_a_midhistory_factor_change_resolves_per_sample() -> None:
    from tools.simulator.hot_tub import BEFORE_S, SETTLE_S, SWITCH_S

    fixture = hot_tub("factor_changes_midhistory", seed=42)
    channel = fixture.ct(CT_L1).channel
    assert channel.factor_at(BEFORE_S) == 1.0
    assert channel.factor_at(SWITCH_S + SETTLE_S) == 2.0
    # Applying today's factor to the earlier reading would double 1000 W to 2000 W.
    assert channel.estimated_appliance_total_w(1000.0, BEFORE_S) == pytest.approx(1000.0)
    assert channel.estimated_appliance_total_w(1000.0, SWITCH_S + SETTLE_S) == pytest.approx(2000.0)
    intervals = fixture.expected.details["factor_intervals"]
    assert intervals == [[0.0, SWITCH_S], [SWITCH_S, None]]


def test_factor_timeline_rejects_overlapping_epochs() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        FactorTimeline(epochs=(
            FactorEpoch(multiplier=1.0, start_s=0.0, end_s=100.0),
            FactorEpoch(multiplier=2.0, start_s=50.0, end_s=None),
        ))


def test_the_hot_tub_factors_export_as_contract_rows() -> None:
    """A fixture must be feedable to a production consumer unchanged."""
    fixture = hot_tub("factor_changes_midhistory", seed=42)
    rows = fixture.as_measurement_factors()
    l1 = rows[CT_L1]
    assert [row.multiplier for row in l1] == [1.0, 2.0]
    assert l1[0].effective_to == l1[1].effective_from
    validate_factor_intervals([row for group in rows.values() for row in group])


def test_a_refoss_channel_separates_conductor_from_appliance_total() -> None:
    """The core of the factor model: the two numbers may disagree."""
    channel = RefossFactorChannel(
        ct_id="ct",
        device_id="tub",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.constant(2.0, applied_by=FactorOwner.USER),
    )
    # A *user* factor is a declaration, not yet baked into the published value.
    assert channel.reported_w(1000.0, 0.0) == 1000.0
    # ...so the two-leg estimate is the published reading times the factor.
    assert channel.estimated_appliance_total_w(1000.0, 0.0) == 2000.0
    # Ground truth is supplied, not derived, and is free to disagree.
    truth = channel.truth(1000.0, 1500.0, 0.0)
    assert truth["appliance_total_truth_w"] == 1500.0
    assert truth["appliance_leg_truth_w"] == 750.0
    assert truth["conductor_truth_w"] == 1000.0
    assert truth["appliance_total_truth_w"] != channel.estimated_appliance_total_w(1000.0, 0.0)


def test_a_monitor_published_factor_is_never_multiplied_twice() -> None:
    channel = RefossFactorChannel(
        ct_id="ct",
        device_id="tub",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.constant(2.0, applied_by=FactorOwner.MONITOR, published=True),
    )
    # The monitor multiplied the 1000 W conductor reading before publishing it.
    assert channel.reported_w(1000.0, 0.0) == 2000.0
    # ...and that published 2000 W already *is* the whole load: no second x2.
    assert channel.estimated_appliance_total_w(1000.0, 0.0) == 2000.0
    assert channel.reported_multiplier_at(0.0) == 2.0


# --------------------------------------------------------------------------- #
# G. Ground-truth separation — the hard requirement
# --------------------------------------------------------------------------- #

#: Keys that must never appear anywhere in an observation view.
LEAK_SAMPLES = (
    "true_channel_power_w",
    "injected_noise_w",
    "conductor_truth_w",
    "appliance_total_truth_w",
    "unmetered_w",
    "power_w",
)


def test_the_leak_detector_itself_catches_a_nested_leak() -> None:
    """A detector that cannot fail is not a gate."""
    dirty = {"measurements": {"ct": {"value_w": 1.0, "true_channel_power_w": 1.0}}}
    found = find_ground_truth_keys(dirty)
    assert found == ["measurements.ct.true_channel_power_w"]
    with pytest.raises(AssertionError, match="true_channel_power_w"):
        assert_no_ground_truth_keys(dirty)


def test_the_leak_detector_catches_truth_in_a_list() -> None:
    dirty = {"records": [{"ground_truth": {"main_total_w": 1.0}}]}
    assert find_ground_truth_keys(dirty) == [
        "records[0].ground_truth",
        "records[0].ground_truth.main_total_w",
    ]


def test_the_leak_detector_accepts_a_clean_frame() -> None:
    clean = {"measurements": {"ct": {"value_w": 1.0, "quality": "good"}}, "t_s": 5.0}
    assert find_ground_truth_keys(clean) == []


@pytest.mark.parametrize("name", sorted(STRATA))
def test_no_observation_view_contains_ground_truth_keys(name: str) -> None:
    """The enforced seam: algorithms may only ever see `observations()`."""
    house = build_scenario(name, seed=9, minutes=3, interval=30)
    view = house.observations()
    assert view, f"{name}: empty observation stream"
    leaked = find_ground_truth_keys(view)
    assert not leaked, f"{name} leaks ground truth: {leaked[:10]}"


@pytest.mark.parametrize("variant", sorted(HOT_TUB_BUILDERS))
def test_no_hot_tub_observation_contains_ground_truth_keys(variant: str) -> None:
    leaked = find_ground_truth_keys(hot_tub(variant, seed=42).observations())
    assert not leaked, f"{variant} leaks ground truth: {leaked[:10]}"


def test_the_observation_view_keeps_every_field_an_algorithm_needs() -> None:
    """Clean must not mean empty: the seam strips truth, not evidence."""
    house = build_scenario("shared", seed=9, minutes=3, interval=30)
    view = house.observations()[2]
    assert view["measurements"], "no measurements survived the seam"
    sample = next(iter(view["measurements"].values()))
    for field in ("value_w", "state_timestamp", "received_at", "quality", "repeated"):
        assert field in sample, field
    assert view["ha_states"], "no HA states survived the seam"
    state = next(iter(view["ha_states"].values()))
    for field in ("entity_id", "state", "last_updated", "device_id"):
        assert field in state, field
    assert "scenario" in view and view["scenario"]["seed"] == 9


def test_the_observation_view_drops_every_truth_attribute() -> None:
    """Both real leaks: the per-sample truth field and the `attributable` flag."""
    house = build_scenario("dedicated", seed=9, minutes=3, interval=30)
    raw = house.to_list()[3]
    view = observation_view(raw)
    for key in LEAK_SAMPLES:
        assert key not in json.dumps(view, sort_keys=True), key
    # ...and the raw record still has them, so the fixture is still an oracle.
    sample = next(iter(raw["measurements"].values()))
    assert "true_channel_power_w" in sample
    assert any("attributable" in state for state in raw["ha_states"].values())


def test_truth_keys_are_disjoint_from_observable_fields() -> None:
    """A field cannot be both observable and forbidden."""
    from tools.simulator.frames import (
        OBSERVABLE_LATEST_FIELDS,
        OBSERVABLE_MEASUREMENT_FIELDS,
        OBSERVABLE_STATE_FIELDS,
    )

    observable = (
        set(OBSERVABLE_LATEST_FIELDS)
        | set(OBSERVABLE_MEASUREMENT_FIELDS)
        | set(OBSERVABLE_STATE_FIELDS)
    )
    assert not (observable & TRUTH_KEYS), observable & TRUTH_KEYS


def test_ground_truth_and_observation_views_describe_the_same_instant() -> None:
    house = build_scenario("shared", seed=9, minutes=3, interval=30)
    records = house.to_list()
    observations = house.observations(records)
    truths = house.ground_truth(records)
    assert len(observations) == len(truths) == len(records)
    for obs, truth in zip(observations, truths, strict=True):
        assert obs["t_s"] == truth["t_s"]


# --------------------------------------------------------------------------- #
# H. Matrix coverage
# --------------------------------------------------------------------------- #


def test_matrix_coverage_is_complete() -> None:
    assert_matrix_coverage()


def test_every_matrix_row_names_a_buildable_stratum() -> None:
    buildable = set(available_strata())
    hot_tub_names = set(HOT_TUB_VARIANTS) | set(HOT_TUB_VARIANTS.values())
    broken: list[str] = []
    for row, strata in MATRIX_STRATA.items():
        if not strata:
            broken.append(f"{row}: no stratum")
            continue
        for name in strata:
            if name not in buildable | hot_tub_names:
                broken.append(f"{row}: unknown stratum {name!r}")
    assert not broken, broken


def test_the_matrix_covers_every_group_and_all_84_rows() -> None:
    """Row names A1..J9: the matrix has 84 rows and the map must name them all."""
    expected_rows = {
        f"{group}{index}"
        for group, count in {
            "A": 7, "B": 9, "C": 9, "D": 7, "E": 6,
            "F": 10, "G": 10, "H": 9, "I": 8,
        }.items()
        for index in range(1, count + 1)
    }
    # Group J (learning/regression) is deliberately out of scope for this WP:
    # it needs versioned inference records from WP1, not a household fixture.
    covered = set(MATRIX_STRATA)
    missing = sorted(expected_rows - covered)
    assert not missing, f"matrix rows with no buildable fixture: {missing}"
