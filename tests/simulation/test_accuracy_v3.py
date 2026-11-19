"""Tests for the v3 simulator accuracy harness (V1 step 3).

Every threshold is the value measured on this deterministic tree (seed 17)
with documented headroom; they are regression gates, not aspirational targets.

Measured (``python -m tests.simulation.accuracy_v3``, V1b):

==================== ======== ========= ========= ======== ========= ======
stratum              records  attrib. % group kWh MEAS. n  MAE W     forced
==================== ======== ========= ========= ======== ========= ======
hb17_co_switched          162     11.76  0.000000      156    0.228       0
hb17_solo_switches        162     11.76  0.000000      156    0.228       0
lab_dedicated              24     96.09  0.000000       71    1.556       0
lab_shared (runtime)      240     49.23  0.104445      236    0.313       0
lab_ambiguous_pair        160      0.01  0.000000      160    0.058       0
lab_automation_burst       24     73.34  0.000000      574    5.923       0
==================== ======== ========= ========= ======== ========= ======

HB §17 houses (EngineCore runtime): the TV is MEASURED from its plug every
record (156 points, 3 W standby, MAE 0.228 W); both lights are published as
UNKNOWN (``point_w=None``) from step 3 on — one co-switched event is not
enough history to separate or group them; the fridge's +150 W (no HA) is
published once as unknown load (0.00833 kWh).  ``solo_switches`` grades
identically: in the simulator its solo cycles fall while both lights are
already on (or after the horizon), so they add HA messages but no physics.

``lab_shared`` runs through the live runtime for one simulated hour: after two
CONFIRMED co-switches of utility_1 + utility_2 are in history, I4 publishes
their GROUP_IDENTIFIABLE sum and the steady pass keeps it current from the
shared CT (51 group points, 0.104445 kWh); utility_3, alone on its CT, is
MEASURED.
"""

from __future__ import annotations

import pytest

from tests.simulation.accuracy_v3 import (
    DEFAULT_SCENARIOS,
    HB17_LIGHTS,
    V3Report,
    grade_v3,
    run_all,
    run_scenario,
)


@pytest.fixture(scope="module")
def report() -> V3Report:
    return run_all(seed=17)


def test_all_configured_scenarios_are_graded(report: V3Report) -> None:
    assert set(report.scenarios) == {label for label, _spec in DEFAULT_SCENARIOS}
    for label, stratum in report.scenarios.items():
        assert stratum.n_records > 0, label
        assert stratum.truth_energy_kwh > 0.0, label
        assert stratum.published_points > 0, f"{label}: the engine published nothing"
        payload = stratum.as_dict()
        for key in (
            "attributed_pct",
            "group_identified_energy_kwh",
            "unknown_energy_kwh",
            "residual_preservation_ratio",
            "forced_attribution_count",
        ):
            assert key in payload, (label, key)


def test_forced_attribution_count_is_zero_everywhere(report: V3Report) -> None:
    """No subject the evidence cannot separate is given a number."""
    assert report.forced_attribution_count == 0
    for label, stratum in report.scenarios.items():
        assert stratum.forced_attribution_count == 0, label


def test_forced_attribution_detector_counts_a_fabricated_split() -> None:
    """The zero above is meaningful: a fabricated split of a truth group counts."""
    frames, predictions, honest = run_scenario("hb17_co_switched", "hb17:co_switched", seed=17)
    assert honest.forced_attribution_count == 0
    published = [i for i, p in enumerate(predictions) if HB17_LIGHTS[0] in p["devices"]]
    assert published, "the lights were never published"
    mutated = [dict(p, devices=dict(p["devices"])) for p in predictions]
    for index in published:
        for light in HB17_LIGHTS:
            mutated[index]["devices"][light] = {"point_w": 22.9, "status": "estimated"}
    graded = grade_v3(frames, mutated, stratum="mutated")
    assert graded.forced_attribution_count == 2 * len(published)

    frames, predictions, honest = run_scenario("lab_shared", "lab:shared", seed=17)
    assert honest.forced_attribution_count == 0
    mutated = [dict(p, devices=dict(p["devices"])) for p in predictions]
    for p in mutated:
        p["devices"]["utility_1"] = {"point_w": 1200.0, "status": "estimated"}
    assert grade_v3(frames, mutated).forced_attribution_count == len(mutated)


def test_hb17_runtime_publishes_real_estimates(report: V3Report) -> None:
    """The HB §17 houses grade what EngineCore actually published."""
    for label in ("hb17_co_switched", "hb17_solo_switches"):
        stratum = report.scenarios[label]
        # The TV every record (162) plus both lights from the lights event on
        # (41 records x 2): 238 measured; floor 230.
        assert stratum.published_points >= 230, (label, stratum.published_points)
        # Only the TV carries a number: MEASURED from its plug, 156 points
        # (from the first heartbeat on); floor 150.  The lights never do.
        assert stratum.attributed_points == len(stratum.measured_pairs), label
        assert len(stratum.measured_pairs) >= 150, (label, len(stratum.measured_pairs))
        assert stratum.error.mae_w < 0.5, (label, stratum.error.mae_w)  # 0.228 measured
        # 11.76 % = the TV's 3 W out of all device energy; floor 10, ceiling 13.
        assert 10.0 <= stratum.attributed_pct <= 13.0, (label, stratum.attributed_pct)
        # The fridge's +150 W (no HA) is published once as unknown load:
        # 0.00833 kWh measured; floor 0.0075 (10 % headroom), ceiling 0.0125
        # (a triple count over Circuit B/Subpanel/Main would be ~0.025).
        assert 0.0075 <= stratum.unknown_energy_kwh <= 0.0125, (label, stratum.unknown_energy_kwh)
        assert stratum.residual_checked == stratum.n_records
        assert stratum.residual_preservation_ratio == 1.0, label


def test_measured_endpoints_match_truth_within_sigma(report: V3Report) -> None:
    """MEASURED single-subject endpoints track CT truth.

    Ceilings: measured value + headroom (dedicated 1.556 -> 2.0 W, RMSE 2.286
    -> 3.0 W, max 7.282 -> 10 W; shared (live runtime, utility_3 on its own
    CT) 0.313 -> 0.6 W, RMSE 0.500 -> 1.0 W, max 2.273 -> 5 W; ambiguous_pair 0.058
    -> 0.2 W; automation_burst 5.923 -> 7.5 W).  Burst's RMSE (88 W) and max
    (1.49 kW) come from transition-instant samples whose held value is a
    level the CT's truth curve also took within a sample interval; the MAE
    gate is the regression signal there.
    """
    dedicated = report.scenarios["lab_dedicated"]
    assert len(dedicated.measured_pairs) >= 60
    assert dedicated.error.mae_w < 2.0, dedicated.error.mae_w
    assert dedicated.error.rmse_w < 3.0, dedicated.error.rmse_w
    assert dedicated.error.max_abs_w < 10.0, dedicated.error.max_abs_w
    shared = report.scenarios["lab_shared"]
    assert len(shared.measured_pairs) >= 220
    assert shared.error.mae_w < 0.6, shared.error.mae_w
    assert shared.error.rmse_w < 1.0, shared.error.rmse_w
    assert shared.error.max_abs_w < 5.0, shared.error.max_abs_w
    pair = report.scenarios["lab_ambiguous_pair"]
    assert pair.error.mae_w < 0.2, pair.error.mae_w
    burst = report.scenarios["lab_automation_burst"]
    assert len(burst.measured_pairs) >= 500
    assert burst.error.mae_w < 7.5, burst.error.mae_w


def test_attributed_share_floors(report: V3Report) -> None:
    """Attributed share per stratum (measured with ~10 % headroom).

    dedicated 96.09 % -> 85; burst 73.34 % -> 65; shared 49.23 % -> 44..55
    (utility_3 MEASURED plus the utility_1+2 group sum once it is history-backed;
    the 2.4 kW pair is never split).
    """
    assert report.scenarios["lab_dedicated"].attributed_pct >= 85.0
    assert report.scenarios["lab_automation_burst"].attributed_pct >= 65.0
    shared = report.scenarios["lab_shared"].attributed_pct
    assert 44.0 <= shared <= 55.0, shared


def test_residual_preservation(report: V3Report) -> None:
    """Claims never eat the unmetered residual.

    Measured 1.0 everywhere except dedicated/burst at 0.9583 (one of 24
    records: a transition sample where a fresh branch reading meets a stale
    main reading) and lab_shared at 0.9708 (7 of 240, the same transition
    effect).  Floor 0.9.
    """
    for label, stratum in report.scenarios.items():
        assert stratum.residual_checked == stratum.n_records, label
        assert stratum.residual_preservation_ratio is not None, label
        assert stratum.residual_preservation_ratio >= 0.9, label


def test_unknown_is_no_claim_not_zero(report: V3Report) -> None:
    """The inseparable pair stays UNKNOWN (no claim), never 0 W."""
    _frames, predictions, graded = run_scenario("lab_shared", "lab:shared", seed=17)
    for prediction in predictions:
        for device in ("utility_1", "utility_2"):
            claim = prediction["devices"][device]
            assert claim["point_w"] is None, claim
            assert claim["status"] == "unknown"
        # utility_3 is alone on its CT: a real number every record.
        assert prediction["devices"]["utility_3"]["point_w"] is not None
        assert prediction["devices"]["utility_3"]["status"] in {"measured", "estimated"}
    # MEASURED on 23 of 24 records (one transition record is ESTIMATED).
    measured = sum(p["devices"]["utility_3"]["status"] == "measured" for p in predictions)
    assert measured >= len(predictions) - 1, measured
    assert graded.unknown_points >= 2 * graded.n_records


def test_shared_branch_publishes_group_identified_energy(report: V3Report) -> None:
    """HB §21.7: two loads only ever seen together on one CT -> a group sum.

    Measured 51 group points, 0.104445 kWh; floors 45 points and 0.09 kWh.
    The members are never given a number (forced attributions stay 0).
    """
    shared = report.scenarios["lab_shared"]
    assert shared.group_points >= 45, shared.group_points
    assert shared.group_identified_energy_kwh >= 0.09, shared.group_identified_energy_kwh
    assert shared.forced_attribution_count == 0
    _frames, predictions, _graded = run_scenario("lab_shared", "lab-runtime:shared", seed=17)
    grouped = [p for p in predictions if p["groups"]]
    assert grouped
    for prediction in grouped:
        (group,) = prediction["groups"]
        assert sorted(group["member_ids"]) == ["utility_1", "utility_2"]
        for member in ("utility_1", "utility_2"):
            assert prediction["devices"][member]["point_w"] is None
    # The group tracks the shared CT: ~2.4 kW while both run, ~0 W when off.
    points = [p["groups"][0]["point_w"] for p in grouped]
    assert max(points) == pytest.approx(2400.0, abs=10.0)
    assert min(points) == pytest.approx(0.0, abs=10.0)


def test_determinism_same_seed_identical_report(report: V3Report) -> None:
    assert run_all(seed=17).as_dict() == report.as_dict()
