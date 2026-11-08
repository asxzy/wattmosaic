"""Tests for evidence-based entity↔device mapping (orchestrator-owned).

The mapping tool in ``tools/audit/derive_state_entities.py`` decides which HA
state entity, if any, describes each CT channel.  Getting this wrong is the most
expensive mistake the project can make: a fabricated link attributes one
appliance's power to another and the error is invisible downstream.

Every test here pins a rule that was derived from measured recorder history in
this installation, so a future refactor cannot quietly reintroduce the failure.
All fixtures are synthetic; nothing touches Home Assistant.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "audit"))

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC).timestamp()


def _state_rows(episodes: list[tuple[float, str]]) -> list[dict[str, Any]]:
    """Recorder-shaped rows for ``[(t, label)]``, one row per episode."""
    return [{"s": label, "lu": t} for t, label in episodes]


def _power_rows(points: list[tuple[float, float]]) -> list[dict[str, Any]]:
    return [{"s": str(value), "lu": t} for t, value in points]


def _device(
    device_id: str,
    entity_ids: list[str],
    ct_ids: list[str] | None = None,
) -> dict[str, Any]:
    ct_ids = ct_ids or [f"ct_{device_id}"]
    return {
        "device_id": device_id,
        "name": device_id,
        "ct_ids": ct_ids,
        "ct_entities": entity_ids,
    }


def _topology(channels: dict[str, str], pairs: list[tuple[str, str]] | None = None) -> dict:
    partner: dict[str, str] = {}
    for a, b in pairs or []:
        partner[a] = b
        partner[b] = a
    return {
        "channels": [
            {"ct_id": ct_id, "entity_id": entity_id, "paired_ct_id": partner.get(ct_id)}
            for ct_id, entity_id in channels.items()
        ],
        "devices": [],
    }


# --------------------------------------------------------------------------- #
# Series handling: last_updated is not a new measurement
# --------------------------------------------------------------------------- #
def test_state_episodes_collapses_attribute_updates() -> None:
    """``lu`` moves on attribute updates; only real changes are episodes.

    Measured: the spa water heater emits 201 rows over 7 days but only 76 real
    state changes.  Counting rows would make it look like it switches every
    50 minutes and would wreck every lag estimate built on it.
    """
    from derive_state_entities import state_episodes

    rows = [
        {"s": "Heating", "lu": BASE + 0},
        {"s": "Heating", "lu": BASE + 60},
        {"s": "Heating", "lu": BASE + 120},
        {"s": "Idle", "lu": BASE + 180},
        {"s": "Idle", "lu": BASE + 240},
    ]
    episodes = state_episodes(rows)
    assert [label for _, label in episodes] == ["Heating", "Idle"]
    assert episodes[1][0] == BASE + 180


def test_state_episodes_tolerates_missing_last_changed() -> None:
    """``minimal_response=True`` omits ``lc``; ``lu`` must be enough."""
    from derive_state_entities import state_episodes

    assert state_episodes([{"s": "on", "lu": BASE}]) == [(BASE, "on")]
    assert state_episodes([{"s": "on"}]) == []


# --------------------------------------------------------------------------- #
# Labelling: a short episode is still evidence
# --------------------------------------------------------------------------- #
def test_label_table_keeps_short_episodes_in_the_label_total() -> None:
    """The sample minimum applies to the label, not to each episode.

    Measured defect: the HRV's upper socket turns off in 466 brief episodes out
    of 1 279.  Filtering episodes individually discarded 5 707 ``off`` samples
    and inflated the quiet median from 8 W to 141 W, which fabricated a coverage
    failure for a genuine 144 W load.
    """
    from derive_state_entities import MIN_LABEL_SAMPLES, label_table

    times = np.arange(BASE, BASE + 200 * 60, 60.0)
    # 100 samples at 8 W, then 100 at 152 W, alternating in 5-sample runs so
    # every episode is far below the per-label minimum.
    values = np.empty(times.size)
    for index in range(times.size):
        block = (index // 5) % 2
        values[index] = 8.0 if block == 0 else 152.0
    episodes: list[tuple[float, str]] = []
    for index in range(times.size):
        block = (index // 5) % 2
        label = "off" if block == 0 else "on"
        if not episodes or episodes[-1][1] != label:
            episodes.append((float(times[index]), label))
    assert len(episodes) > MIN_LABEL_SAMPLES

    table = label_table(times, values, episodes)
    assert table["off"][0] == pytest.approx(8.0)
    assert table["on"][0] == pytest.approx(152.0)
    assert table["off"][1] == 100
    assert table["on"][1] == 100


def test_split_labels_uses_measured_power_not_label_text() -> None:
    """The spa reports ``Heating`` (4.5 kW) and ``Cooling`` (22 W).

    The split must follow the watts, so ``Cooling`` is grouped with the quiet
    labels even though the engine's global table calls ``cooling`` ON.  That
    mismatch is then corrected by a per-device override.
    """
    from derive_state_entities import split_labels

    table = {"Heating": (4597.9, 500), "Cooling": (21.8, 500), "Idle": (18.0, 500)}
    split = split_labels(table)
    assert split is not None
    quiet, active, low, high = split
    assert active == ["Heating"]
    assert set(quiet) == {"Cooling", "Idle"}
    assert low < high


def test_state_overrides_correct_global_label_meaning() -> None:
    """A measured label meaning that contradicts the global table is declared."""
    from derive_state_entities import state_overrides

    overrides = state_overrides(active=["Heating"], quiet=["Cooling", "Idle"])
    # ``Heating`` already means ON globally, and ``Idle`` already means OFF, so
    # only the label whose measured meaning contradicts the global table is
    # overridden.  Declaring the others would add noise without changing
    # behaviour.
    assert overrides == {"Cooling": "off"}


# --------------------------------------------------------------------------- #
# The score: balanced accuracy, circuit recall, relocation null
# --------------------------------------------------------------------------- #
def test_balanced_accuracy_is_invariant_to_duty_cycle() -> None:
    """Raw agreement is dominated by base rate; balanced accuracy is not.

    A circuit that is high 97 % of the time agrees 94 % with an *independent*
    coin-flip prediction.  Only the balanced score reveals that as chance.
    """
    from derive_state_entities import balanced_accuracy

    rng = np.random.default_rng(4)
    n = 20_000
    circuit = rng.random(n) > 0.03
    independent = rng.random(n) > 0.03
    assert float(np.mean(circuit == independent)) > 0.90
    assert balanced_accuracy(circuit, independent) == pytest.approx(0.5, abs=0.03)

    perfect = circuit.copy()
    assert balanced_accuracy(circuit, perfect) == pytest.approx(1.0)

    always_on = np.ones(n, dtype=bool)
    assert balanced_accuracy(circuit, always_on) == 0.0


def test_relocation_null_preserves_duty_cycle_and_episodes() -> None:
    """The strict null keeps everything except *when* the entity was on."""
    from derive_state_entities import relocation_null

    n = 4000
    rng = np.random.default_rng(11)
    circuit = rng.random(n) < 0.3
    state_on = np.zeros(n, dtype=bool)
    # Six 40-sample episodes at positions unrelated to the circuit.
    for start in (100, 700, 1300, 1900, 2500, 3100):
        state_on[start : start + 40] = True
    null = relocation_null(circuit, state_on)
    assert 0.0 < null <= 1.0


def test_null_is_not_testable_with_a_single_episode() -> None:
    """One charging session must not be rejected by a circular-shift null."""
    from derive_state_entities import null_is_testable

    single = np.zeros(2000, dtype=bool)
    single[500:900] = True
    assert null_is_testable(single) is False

    repeated = np.zeros(2000, dtype=bool)
    for start in (100, 500, 900, 1300, 1700):
        repeated[start : start + 80] = True
    assert null_is_testable(repeated) is True


def test_inverts_on_off_rejects_a_remote_control_flag() -> None:
    """An entity that is ON while the circuit is low is not the circuit's load.

    Measured: a remote-control flag on an unrelated appliance.  The spa's
    ``Cooling`` label must *not* trigger this — it is not a canonical OFF word.
    """
    from derive_state_entities import inverts_on_off

    assert inverts_on_off(active=["off"], quiet=["on"]) is True
    assert inverts_on_off(active=["on"], quiet=["off"]) is False
    assert inverts_on_off(active=["Heating"], quiet=["Cooling"]) is False


# --------------------------------------------------------------------------- #
# Selection: coverage, recall, sole carrier
# --------------------------------------------------------------------------- #
def test_coverage_rejects_a_load_that_explains_a_slice_of_the_circuit() -> None:
    """A TV on a 260 W rack circuit covers 29 W: it is not that circuit's load."""
    from derive_state_entities import MIN_COVERAGE, classify

    row = {
        "entity_id": "media_player.tv",
        "balanced_accuracy": 0.995,
        "sensitivity": 0.99,
        "specificity": 0.99,
        "coverage": 0.11,
        "circuit_recall": 0.99,
        "separation_w": 29.0,
        "transitions": 40,
        "lag_s": 0,
        "circular": False,
        "null_testable": False,
        "margin_over_null": None,
    }
    assert row["coverage"] < MIN_COVERAGE
    status, reason = classify([row], None, None)
    assert status == "unsupported"
    assert reason.startswith("insufficient_coverage")


def test_circuit_recall_rejects_a_short_duty_flag() -> None:
    """A pot-light flag scores 0.98 balanced accuracy but covers 23 % of the circuit."""
    from derive_state_entities import MIN_CIRCUIT_RECALL, classify

    row = {
        "entity_id": "light.circuit_a_pot_lights",
        "balanced_accuracy": 0.9813,
        "sensitivity": 1.0,
        "specificity": 0.9625,
        "coverage": 0.86,
        "circuit_recall": 0.23,
        "separation_w": 106.4,
        "transitions": 10,
        "lag_s": 0,
        "circular": False,
        "null_testable": False,
        "margin_over_null": None,
    }
    assert row["circuit_recall"] < MIN_CIRCUIT_RECALL
    status, reason = classify([row], None, None)
    assert status == "unsupported"
    assert reason.startswith("insufficient_circuit_recall")


def test_select_best_ranks_by_circuit_recall_not_balanced_accuracy() -> None:
    """The real load must outrank a control flag that scores higher per entity."""
    from derive_state_entities import select_best

    real = {
        "entity_id": "binary_sensor.water_heater_zone_1_heating",
        "balanced_accuracy": 0.973,
        "circuit_recall": 0.927,
        "lag_s": 120,
    }
    flag = {
        "entity_id": "binary_sensor.oven_mobile_start",
        "balanced_accuracy": 0.9221,
        "circuit_recall": 0.0315,
        "lag_s": 600,
    }
    best = select_best([flag, real])
    assert best is not None
    assert best["entity_id"] == "binary_sensor.water_heater_zone_1_heating"


def test_sole_carrier_settles_a_companion_flag() -> None:
    """``landscape_socket_1`` adds 169 W; the fixture flag adds -1 W.

    Both switch at the same instants, so both look like the load.  The cross-tab
    marginal shows which one carries the power, so this is not ambiguity and the
    mapping goes to the socket.
    """
    from derive_state_entities import detect_sole_carrier

    times = np.arange(BASE, BASE + 600 * 60, 60.0)
    # Quiet for the first third, then the socket alone, then both.
    socket_on = np.zeros(times.size, dtype=bool)
    socket_on[200:] = True
    fixture_on = np.zeros(times.size, dtype=bool)
    fixture_on[400:] = True
    values = np.where(socket_on, 175.0, 6.4)

    rows = [
        {
            "entity_id": "switch.landscape_socket_1",
            "active_labels": ["on"],
            "circuit_recall": 0.97,
            "balanced_accuracy": 0.99,
            "coverage": 0.95,
            "separation_w": 168.6,
            "transitions": 4,
            "circular": False,
            "null_testable": False,
        },
        {
            "entity_id": "light.exterior_front_fixture",
            "active_labels": ["on"],
            "circuit_recall": 0.96,
            "balanced_accuracy": 0.99,
            "coverage": 0.95,
            "separation_w": 168.0,
            "transitions": 4,
            "circular": False,
            "null_testable": False,
        },
    ]
    series = {
        "switch.landscape_socket_1": [
            (float(times[0]), "off"),
            (float(times[200]), "on"),
        ],
        "light.exterior_front_fixture": [
            (float(times[0]), "off"),
            (float(times[400]), "on"),
        ],
    }
    sole = detect_sole_carrier(rows, series, times, values)
    assert sole is not None
    assert sole["winner"] == "switch.landscape_socket_1"
    assert sole["rival"] == "light.exterior_front_fixture"


def test_sole_carrier_refuses_two_weak_candidates() -> None:
    """Two implausible candidates must not settle each other."""
    from derive_state_entities import detect_sole_carrier

    times = np.arange(BASE, BASE + 600 * 60, 60.0)
    a_on = np.zeros(times.size, dtype=bool)
    a_on[200:] = True
    b_on = np.zeros(times.size, dtype=bool)
    b_on[400:] = True
    values = np.where(a_on, 100.0, 31.0)

    weak = {
        "circuit_recall": 0.86,
        "balanced_accuracy": 0.86,
        "coverage": 0.60,
        "separation_w": 60.0,
        "transitions": 6,
        "circular": False,
        "null_testable": False,
    }
    rows = [
        {**weak, "entity_id": "light.a", "active_labels": ["on"]},
        {**weak, "entity_id": "light.b", "active_labels": ["on"]},
    ]
    series = {
        "light.a": [(float(times[0]), "off"), (float(times[200]), "on")],
        "light.b": [(float(times[0]), "off"), (float(times[400]), "on")],
    }
    assert detect_sole_carrier(rows, series, times, values) is None


# --------------------------------------------------------------------------- #
# Ambiguity: reported, never resolved
# --------------------------------------------------------------------------- #
def test_nested_dimmers_stay_ambiguous() -> None:
    """A main dimmer and its nested pot dimmer are never observed separately."""
    from derive_state_entities import classify, detect_ambiguity, detect_sole_carrier

    times = np.arange(BASE, BASE + 900 * 60, 60.0)
    main_on = np.zeros(times.size, dtype=bool)
    main_on[300:] = True
    # Nested: the pot dimmer is never on without the main dimmer.
    values = np.where(main_on, 819.3, 9.7)

    rows = [
        {
            "entity_id": "light.circuit_b_main",
            "active_labels": ["on"],
            "circuit_recall": 0.97,
            "balanced_accuracy": 0.974,
            "coverage": 0.99,
            "separation_w": 808.8,
            "transitions": 10,
            "circular": False,
            "null_testable": False,
        },
        {
            "entity_id": "light.circuit_b_pot",
            "active_labels": ["on"],
            "circuit_recall": 0.96,
            "balanced_accuracy": 0.901,
            "coverage": 0.60,
            "separation_w": 585.0,
            "transitions": 10,
            "circular": False,
            "null_testable": False,
        },
    ]
    series = {
        "light.circuit_b_main": [(float(times[0]), "off"), (float(times[300]), "on")],
        "light.circuit_b_pot": [(float(times[0]), "off"), (float(times[300]), "on")],
    }
    assert detect_sole_carrier(rows, series, times, values) is None
    ambiguity = detect_ambiguity(rows, series, times, values)
    assert ambiguity is not None
    assert set(ambiguity["members"]) == {
        "light.circuit_b_main",
        "light.circuit_b_pot",
    }
    status, _reason = classify(rows, None, ambiguity)
    assert status == "ambiguous"


def test_a_clear_winner_is_not_ambiguity() -> None:
    """A rival that covers far less of the circuit does not block the mapping."""
    from derive_state_entities import detect_ambiguity

    times = np.arange(BASE, BASE + 600 * 60, 60.0)
    series = {
        "light.real": [(float(times[0]), "off"), (float(times[100]), "on")],
        "light.flag": [(float(times[0]), "off"), (float(times[400]), "on")],
    }
    values = np.where(times >= times[100], 200.0, 8.0)
    rows = [
        {
            "entity_id": "light.real",
            "active_labels": ["on"],
            "circuit_recall": 0.999,
            "balanced_accuracy": 0.997,
            "coverage": 0.96,
            "separation_w": 192.0,
            "transitions": 4,
            "circular": False,
            "null_testable": False,
        },
        {
            "entity_id": "light.flag",
            "active_labels": ["on"],
            "circuit_recall": 0.23,
            "balanced_accuracy": 0.98,
            "coverage": 0.80,
            "separation_w": 150.0,
            "transitions": 4,
            "circular": False,
            "null_testable": False,
        },
    ]
    assert detect_ambiguity(rows, series, times, values) is None


# --------------------------------------------------------------------------- #
# Confound: two loads on one circuit
# --------------------------------------------------------------------------- #
def test_additive_pair_is_reported_as_confounded() -> None:
    """The HRV's two Tuya sockets each add ~143 W on different registry devices."""
    from derive_state_entities import detect_confound

    times = np.arange(BASE, BASE + 2000 * 60, 60.0)
    lower_on = np.zeros(times.size, dtype=bool)
    lower_on[500:1500] = True
    upper_on = np.zeros(times.size, dtype=bool)
    upper_on[1000:] = True
    values = (
        7.0
        + np.where(lower_on, 140.3, 0.0)
        + np.where(upper_on, 140.0, 0.0)
    )
    series = {
        "switch.hrv_lower_socket_1": [
            (float(times[0]), "off"),
            (float(times[500]), "on"),
            (float(times[1500]), "off"),
        ],
        "switch.hrv_upper_socket_1": [
            (float(times[0]), "off"),
            (float(times[1000]), "on"),
        ],
    }
    rows = [
        {
            "entity_id": "switch.hrv_lower_socket_1",
            "active_labels": ["on"],
            "circuit_recall": 0.66,
            "balanced_accuracy": 0.82,
            "coverage": 0.50,
            "separation_w": 141.3,
            "transitions": 2,
            "circular": False,
            "null_testable": False,
        },
        {
            "entity_id": "switch.hrv_upper_socket_1",
            "active_labels": ["on"],
            "circuit_recall": 0.75,
            "balanced_accuracy": 0.75,
            "coverage": 0.50,
            "separation_w": 143.9,
            "transitions": 1,
            "circular": False,
            "null_testable": False,
        },
    ]
    confound = detect_confound(rows, series, times, values)
    assert confound is not None
    assert confound["kind"] == "additive_multi_device"
    assert set(confound["members"]) == {
        "switch.hrv_lower_socket_1",
        "switch.hrv_upper_socket_1",
    }


def test_incidental_cooccurrence_is_not_a_confound() -> None:
    """A pair with almost no co-occurrence must not fabricate a second load."""
    from derive_state_entities import detect_confound

    times = np.arange(BASE, BASE + 2000 * 60, 60.0)
    a_on = np.zeros(times.size, dtype=bool)
    a_on[100:900] = True
    b_on = np.zeros(times.size, dtype=bool)
    b_on[1500:1900] = True
    values = 10.0 + np.where(a_on, 200.0, 0.0) + np.where(b_on, 200.0, 0.0)
    series = {
        "light.a": [(float(times[0]), "off"), (float(times[100]), "on"), (float(times[900]), "off")],
        "light.b": [(float(times[0]), "off"), (float(times[1500]), "on"), (float(times[1900]), "off")],
    }
    rows = [
        {
            "entity_id": "light.a",
            "active_labels": ["on"],
            "circuit_recall": 0.40,
            "balanced_accuracy": 0.90,
            "coverage": 0.60,
            "separation_w": 200.0,
            "transitions": 2,
            "circular": False,
            "null_testable": False,
        },
        {
            "entity_id": "light.b",
            "active_labels": ["on"],
            "circuit_recall": 0.20,
            "balanced_accuracy": 0.85,
            "coverage": 0.60,
            "separation_w": 200.0,
            "transitions": 2,
            "circular": False,
            "null_testable": False,
        },
    ]
    # Neither covers enough of the circuit to be half of a two-load circuit.
    assert detect_confound(rows, series, times, values) is None


# --------------------------------------------------------------------------- #
# Duplicate channels: one conductor, two clamps
# --------------------------------------------------------------------------- #
def _series(values: np.ndarray, times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return times, values


def test_duplicate_channels_detects_a_shared_conductor() -> None:
    """Two channels with a fixed 0.539 ratio are one conductor, not two loads.

    Measured: relative IQR 0.005 and a 0.15 W median residual.  Mapping a state
    entity to both would attribute one appliance twice.
    """
    from derive_state_entities import detect_duplicate_channels

    times = np.arange(BASE, BASE + 3000 * 60, 60.0)
    rng = np.random.default_rng(7)
    # A real load that switches on and off, with a small measurement floor.
    primary = 58.2 + np.where(rng.random(times.size) < 0.15, 120.0, 0.0)
    echo = primary * 0.539 + rng.normal(0, 0.1, times.size)

    findings = detect_duplicate_channels(
        {"ct_a": _series(primary, times), "ct_b": _series(echo, times)}
    )
    assert findings
    finding = findings[0]
    assert finding["kind"] == "same_current"
    assert finding["ratio_relative_iqr"] < 0.05
    assert finding["median_abs_residual_w"] < 2.0


def test_correlated_activity_is_not_claimed_as_duplication() -> None:
    """A TV and a server rack correlate at 0.84 but are different currents.

    Measured: relative IQR 0.72.  Reporting this as duplication would disable a
    real channel; it must stay a weaker finding.
    """
    from derive_state_entities import detect_duplicate_channels

    times = np.arange(BASE, BASE + 3000 * 60, 60.0)
    rng = np.random.default_rng(3)
    busy = rng.random(times.size) < 0.4
    rack = 232.0 + np.where(busy, 60.0, 0.0) + rng.normal(0, 3.0, times.size)
    # The TV is on during the same busy periods, but draws a variable amount.
    tv = 55.0 + np.where(busy, rng.normal(30.0, 60.0, times.size), 0.0)

    findings = detect_duplicate_channels(
        {"ct_rack": _series(rack, times), "ct_tv": _series(tv, times)}
    )
    kinds = {finding["kind"] for finding in findings}
    assert "same_current" not in kinds


def test_declared_double_pole_legs_are_not_a_defect() -> None:
    """Two legs of one 240 V circuit correlate by design."""
    from derive_state_entities import detect_duplicate_channels

    times = np.arange(BASE, BASE + 2000 * 60, 60.0)
    leg1 = np.where(times >= times[500], 1220.0, 0.0)
    leg2 = leg1 * 1.01

    findings = detect_duplicate_channels(
        {"ct1": _series(leg1, times), "ct2": _series(leg2, times)},
        paired={"ct1": "ct2", "ct2": "ct1"},
    )
    assert findings
    assert findings[0]["kind"] == "double_pole_legs"


def test_declared_pair_that_does_not_track_is_flagged() -> None:
    """A declared leg pair with r = 0.28 is not a leg pair: the declaration is wrong."""
    from derive_state_entities import detect_duplicate_channels

    times = np.arange(BASE, BASE + 2000 * 60, 60.0)
    rng = np.random.default_rng(5)
    leg1 = rng.random(times.size) * 300.0
    leg2 = rng.random(times.size) * 60.0

    findings = detect_duplicate_channels(
        {"ct1": _series(leg1, times), "ct2": _series(leg2, times)},
        paired={"ct1": "ct2", "ct2": "ct1"},
    )
    assert findings
    assert findings[0]["kind"] == "double_pole_legs_uncorrelated"


# --------------------------------------------------------------------------- #
# End-to-end: the report refuses to map what it cannot justify
# --------------------------------------------------------------------------- #
def _build_report(
    *,
    power: dict[str, list[dict[str, Any]]],
    states: dict[str, list[dict[str, Any]]],
    devices: list[dict[str, Any]],
    topology: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from derive_state_entities import build_report

    return build_report(
        power=power,
        states=states,
        devices=devices,
        topology=topology or {"channels": []},
        top_k=8,
    )


def test_report_withdraws_a_mapping_on_a_duplicated_conductor() -> None:
    """Two devices on one conductor must not both receive a state entity."""
    times = np.arange(BASE, BASE + 2400 * 60, 60.0)
    rng = np.random.default_rng(13)
    load = 58.2 + np.where(rng.random(times.size) < 0.2, 120.0, 0.0)
    primary = load
    echo = load * 0.539
    on = load > 100.0
    episodes = [{"s": "off", "lu": float(times[0])}]
    for index in range(1, times.size):
        if on[index] and not on[index - 1]:
            episodes.append({"s": "on", "lu": float(times[index])})
        elif not on[index] and on[index - 1]:
            episodes.append({"s": "off", "lu": float(times[index])})

    report = _build_report(
        power={
            "sensor.channel_a": _power_rows([(float(t), float(v)) for t, v in zip(times, primary, strict=True)]),
            "sensor.channel_b": _power_rows([(float(t), float(v)) for t, v in zip(times, echo, strict=True)]),
        },
        states={"light.circuit_c": episodes},
        devices=[
            _device("dev_channel_a", ["sensor.channel_a"], ["ct_a"]),
            _device("dev_channel_b", ["sensor.channel_b"], ["ct_b"]),
        ],
        topology=_topology({"ct_a": "sensor.channel_a", "ct_b": "sensor.channel_b"}),
    )
    findings = report["duplicate_channel_candidates"]
    assert any(finding["kind"] == "same_current" for finding in findings)
    for device_id in ("dev_channel_a", "dev_channel_b"):
        entry = report["devices"][device_id]
        assert entry["state_entity_ids"] == []
        assert entry["duplicate_of"]


def test_report_never_emits_a_mapping_for_a_confounded_device() -> None:
    """A confounded verdict must carry no entity, even if one scored well."""
    times = np.arange(BASE, BASE + 3000 * 60, 60.0)
    # Two independently switchable loads on one CT: lower alone 147 W, upper
    # alone 147 W, both 287 W, quiet 7 W — the measured HRV cross-tab.  The
    # cycle must include genuine quiet time, otherwise the circuit never returns
    # to its floor and neither label separates.
    lower_episodes: list[tuple[float, str]] = [(float(times[0]), "off")]
    upper_episodes: list[tuple[float, str]] = [(float(times[0]), "off")]
    a_on = np.zeros(times.size, dtype=bool)
    b_on = np.zeros(times.size, dtype=bool)
    for cycle in range(7):
        start = cycle * 400
        a_on[start : start + 150] = True
        lower_episodes.append((float(times[start]), "on"))
        lower_episodes.append((float(times[start + 150]), "off"))
        b_on[start + 100 : start + 250] = True
        upper_episodes.append((float(times[start + 100]), "on"))
        upper_episodes.append((float(times[start + 250]), "off"))
    values = 7.0 + np.where(a_on, 140.3, 0.0) + np.where(b_on, 140.0, 0.0)

    report = _build_report(
        power={
            "sensor.hrv": _power_rows(
                [(float(t), float(v)) for t, v in zip(times, values, strict=True)]
            )
        },
        states={
            "switch.lower": _state_rows(lower_episodes),
            "switch.upper": _state_rows(upper_episodes),
        },
        devices=[_device("dev_hrv", ["sensor.hrv"], ["ct_hrv"])],
        topology=_topology({"ct_hrv": "sensor.hrv"}),
    )
    entry = report["devices"]["dev_hrv"]
    assert entry["status"] == "confounded"
    assert entry["state_entity_ids"] == []
    assert entry["state_overrides"] == {}


def test_mapping_never_uses_name_similarity() -> None:
    """An entity named like the device but unrelated in power is not linked.

    ``light.circuit_d`` would be the obvious name guess for a channel called
    ``circuit_d``.  If its state does not gate the measured power, the tool must
    not map it — a name is not evidence.
    """
    times = np.arange(BASE, BASE + 2400 * 60, 60.0)
    rng = np.random.default_rng(23)
    values = 58.2 + np.where(rng.random(times.size) < 0.2, 120.0, 0.0)
    # A plausible-looking name whose state is unrelated (random).
    unrelated = [{"s": "off", "lu": float(times[0])}]
    flips = rng.random(times.size) < 0.5
    for index in range(1, times.size):
        if flips[index] != flips[index - 1]:
            unrelated.append(
                {"s": "on" if flips[index] else "off", "lu": float(times[index])}
            )

    report = _build_report(
        power={
            "sensor.circuit_d": _power_rows(
                [(float(t), float(v)) for t, v in zip(times, values, strict=True)]
            )
        },
        states={"light.circuit_d": unrelated},
        devices=[_device("dev_circuit_d", ["sensor.circuit_d"], ["ct_circuit_d"])],
        topology=_topology({"ct_circuit_d": "sensor.circuit_d"}),
    )
    entry = report["devices"]["dev_circuit_d"]
    assert entry["state_entity_ids"] == []
    assert entry["status"] != "supported"


def test_circular_template_entity_is_rejected() -> None:
    """A template of the CT itself cannot confirm the CT."""
    times = np.arange(BASE, BASE + 2400 * 60, 60.0)
    rng = np.random.default_rng(31)
    values = 30.0 + np.where(rng.random(times.size) < 0.3, 170.0, 0.0)
    # The "entity" is a pure threshold of the CT: perfectly circular.
    threshold = 100.0
    rows: list[dict[str, Any]] = []
    previous = None
    for t, value in zip(times, values, strict=True):
        label = "on" if value > threshold else "off"
        if label != previous:
            rows.append({"s": label, "lu": float(t)})
            previous = label

    report = _build_report(
        power={
            "sensor.ct": _power_rows(
                [(float(t), float(v)) for t, v in zip(times, values, strict=True)]
            )
        },
        states={"sensor.ct_template": rows},
        devices=[_device("dev_ct", ["sensor.ct"], ["ct_main"])],
        topology=_topology({"ct_main": "sensor.ct"}),
    )
    entry = report["devices"]["dev_ct"]
    assert entry["state_entity_ids"] == []
    assert entry["status"] != "supported"


# --------------------------------------------------------------------------- #
# Topology consumption
# --------------------------------------------------------------------------- #
def test_topology_attaches_only_positive_single_entity_verdicts() -> None:
    """``build_topology`` must ignore confounded/ambiguous/duplicate entries."""
    from build_topology import _mapping_for

    mapping = {
        "dev_supported": {
            "status": "supported",
            "reason": "state_gated_level_evidence",
            "state_entity_ids": ["binary_sensor.water_heater_zone_1_heating"],
            "state_overrides": {},
            "evidence": {"balanced_accuracy": 0.97},
        },
        "dev_confounded": {
            "status": "confounded",
            "reason": "additive_multi_device",
            "state_entity_ids": ["switch.a", "switch.b"],
        },
        "dev_ambiguous": {
            "status": "ambiguous",
            "reason": "ambiguous_multiple_entities",
            "state_entity_ids": ["light.a"],
        },
        "dev_duplicate": {
            "status": "supported",
            "state_entity_ids": ["light.unknown_model_40a6a7"],
            "duplicate_of": "dev_circuit_d",
        },
        "dev_multi": {
            "status": "supported",
            "state_entity_ids": ["light.a", "light.b"],
        },
    }
    attached = _mapping_for("dev_supported", mapping)
    assert attached["state_entity_ids"] == ["binary_sensor.water_heater_zone_1_heating"]

    for device_id in ("dev_confounded", "dev_ambiguous", "dev_duplicate", "dev_multi"):
        assert _mapping_for(device_id, mapping) == {}, device_id
    assert _mapping_for("dev_absent", mapping) == {}


def test_topology_carries_state_overrides_for_the_spa() -> None:
    """The spa's ``Cooling`` must be declared OFF wherever it is mapped."""
    from build_topology import _mapping_for

    mapping = {
        "dev_spa": {
            "status": "supported",
            "state_entity_ids": ["binary_sensor.spa_heating"],
            "state_overrides": {"Heating": "on", "Cooling": "off"},
            "evidence": {"balanced_accuracy": 0.99},
        }
    }
    attached = _mapping_for("dev_spa", mapping)
    assert attached["state_overrides"] == {"Heating": "on", "Cooling": "off"}


def test_topology_polarity_follows_measured_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A channel negative for the whole window with no generation is inverted."""
    import build_topology

    payload = {
        "channels": {
            "sensor.ct_inverted": {
                "polarity_evidence": {
                    "candidate_polarity": "inverted",
                    "negative_fraction": 0.988,
                }
            },
            "sensor.ct_normal": {"polarity_evidence": None},
        }
    }
    path = tmp_path / "state_entity_candidates.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(build_topology, "DATA", tmp_path)
    inverted = build_topology._load_polarity()
    assert inverted == {"sensor.ct_inverted": "inverted"}


# --------------------------------------------------------------------------- #
# Engine: per-device state overrides
# --------------------------------------------------------------------------- #
def test_engine_state_meaning_honours_overrides() -> None:
    """``Cooling`` is OFF for the spa and ON by default everywhere else."""
    from wattmosaic_engine.disaggregation.engine import _is_off, _is_on

    spa = {"heating": "on", "cooling": "off"}
    assert _is_on("Heating", spa) is True
    assert _is_off("Cooling", spa) is True
    assert _is_on("Cooling") is True  # global default unchanged
    assert _is_off("Cooling") is False
    # An override may not silently invent a third meaning.
    assert _is_on("Idle", spa) is False


def test_engine_transition_direction_uses_overrides() -> None:
    """Heating -> Cooling is a deactivation for the spa, not a no-op."""
    from wattmosaic_engine.disaggregation.engine import _transition_direction

    spa = {"heating": "on", "cooling": "off"}
    assert _transition_direction("Heating", "Cooling", spa) == "deactivation"
    assert _transition_direction("Cooling", "Heating", spa) == "activation"
    assert _transition_direction("Idle", "Cooling", spa) is None


def test_engine_reads_overrides_from_topology_metadata() -> None:
    from wattmosaic_engine.contracts import DeviceTopology, Topology
    from wattmosaic_engine.disaggregation.engine import _device_state_overrides

    topology = Topology(
        devices=[
            DeviceTopology(
                device_id="dev_spa",
                metadata={"state_overrides": {"Heating": "on", "Cooling": "off"}},
            ),
            DeviceTopology(device_id="dev_plain", metadata={}),
            DeviceTopology(
                device_id="dev_bad",
                metadata={"state_overrides": {"Weird": "maybe"}},
            ),
        ]
    )
    overrides = _device_state_overrides(topology)
    assert overrides["dev_spa"] == {"heating": "on", "cooling": "off"}
    assert "dev_plain" not in overrides
    assert "dev_bad" not in overrides


def test_sample_filter_honours_device_state_overrides() -> None:
    """The learning filter must not read ``Cooling`` as a 4.5 kW event."""
    from wattmosaic_engine.learning.sample_filter import _is_on_state

    assert _is_on_state("Cooling") is True
    assert _is_on_state("Cooling", {"cooling": "off"}) is False
    assert _is_on_state("Heating", {"heating": "on"}) is True
    assert _is_on_state("Heating", {"heating": "off"}) is False
    assert _is_on_state("Unavailable", {"cooling": "off"}) is None
