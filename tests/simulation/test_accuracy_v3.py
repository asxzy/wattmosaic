"""Tests for the v3 simulator accuracy harness (V1 step 3).

Every threshold is the value measured on this deterministic tree (seed 17)
with documented headroom; they are regression gates, not aspirational targets.

Measured (``python -m tests.simulation.accuracy_v3``):

==================== ========= ======== ========== ========= ===========
stratum              attrib. % MEAS. n  MAE W      residual  forced
==================== ========= ======== ========== ========= ===========
hb17_co_switched          0.00        0       —       1.0000       0
hb17_solo_switches        0.00        0       —       1.0000       0
lab_dedicated            96.09       71    1.556      0.9583       0
lab_shared               22.65       23    0.444      1.0000       0
lab_ambiguous_pair        0.01      160    0.058      1.0000       0
lab_automation_burst     73.34      574    5.923      0.9583       0
==================== ========= ======== ========== ========= ===========

HB §17 houses (EngineCore runtime): 82 published estimates per house (both
lights, UNKNOWN with ``point_w=None`` from step 3 on), 0.00833 kWh of
published unknown-load energy (the fridge step on Circuit B, HB §17 step 7),
no device claim — one co-switched event is insufficient evidence to separate
or group the lights.  ``attributed %`` is therefore 0 by design, not by
vacuity.  ``solo_switches`` grades identically: in the simulator the solo
cycles fall while both lights are already on (or after the horizon), so they
add HA messages but no physics.

Group-identified energy is 0 in every stratum; ``lab_shared`` (two utility
loads on one CT) is where HB §21.7 requires a group estimate — pinned below as
a strict xfail naming the engine defect.
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
        # Both lights from the lights event on (41 records x 2), as UNKNOWN.
        assert stratum.published_points >= 80, (label, stratum.published_points)
        assert stratum.attributed_points == 0, label
        # The fridge's +150 W (no HA) is published once as unknown load:
        # 0.00833 kWh measured; floor 0.0075 (10 % headroom), ceiling 0.0125
        # (a triple count over Circuit B/Subpanel/Main would be ~0.025).
        assert 0.0075 <= stratum.unknown_energy_kwh <= 0.0125, (label, stratum.unknown_energy_kwh)
        assert stratum.residual_checked == stratum.n_records
        assert stratum.residual_preservation_ratio == 1.0, label


def test_measured_endpoints_match_truth_within_sigma(report: V3Report) -> None:
    """MEASURED single-subject endpoints track CT truth.

    Ceilings: measured value + headroom (dedicated 1.556 -> 2.0 W, RMSE 2.286
    -> 3.0 W, max 7.282 -> 10 W; shared 0.444 -> 1.0 W; ambiguous_pair 0.058
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
    assert len(shared.measured_pairs) >= 20
    assert shared.error.mae_w < 1.0, shared.error.mae_w
    pair = report.scenarios["lab_ambiguous_pair"]
    assert pair.error.mae_w < 0.2, pair.error.mae_w
    burst = report.scenarios["lab_automation_burst"]
    assert len(burst.measured_pairs) >= 500
    assert burst.error.mae_w < 7.5, burst.error.mae_w


def test_attributed_share_floors(report: V3Report) -> None:
    """Attributed share per stratum (measured with ~10 % headroom).

    dedicated 96.09 % -> 85; burst 73.34 % -> 65; shared 22.65 % -> 20 (only
    utility_3 on its own CT is attributable; the 2.4 kW pair is not split).
    """
    assert report.scenarios["lab_dedicated"].attributed_pct >= 85.0
    assert report.scenarios["lab_automation_burst"].attributed_pct >= 65.0
    shared = report.scenarios["lab_shared"].attributed_pct
    assert 20.0 <= shared <= 25.0, shared


def test_residual_preservation(report: V3Report) -> None:
    """Claims never eat the unmetered residual.

    Measured 1.0 everywhere except dedicated/burst at 0.9583 (one of 24
    records: a transition sample where a fresh branch reading meets a stale
    main reading).  Floor 0.9 (one more such record is tolerated).
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "engine defect: no group estimate is reachable for structurally inseparable subjects. "
        "inference/identifiability.py classifies two devices that only ever appear together on "
        "one CT as PARTIALLY_IDENTIFIABLE and publishes no group sum unless >= MIN_HISTORY_EVENTS "
        "(2) historical events touch them; infer_steady can only get that history through an "
        "EstablishedStructure built from CONFIRMED event inferences, and the live runtime never "
        "persists subject-level group history (see the strict xfail in "
        "tests/end_to_end/test_hb17_walkthrough.py).  HB §21.7 requires a group estimate here."
    ),
)
def test_shared_branch_publishes_group_identified_energy(report: V3Report) -> None:
    assert report.scenarios["lab_shared"].group_identified_energy_kwh > 0.0


def test_determinism_same_seed_identical_report(report: V3Report) -> None:
    assert run_all(seed=17).as_dict() == report.as_dict()
