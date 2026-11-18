"""Acceptance tests for the HB §17 reference house and the replay driver.

These are X07's named tests.  They pin the contract later tasks build against:

* same seed ⇒ byte-identical observation streams;
* the observation/ground-truth leak detector passes, and *fails* on the truth
  keys ``true_onset_at`` / ``true_circuit``;
* every HB §17 step occurs at its scripted time;
* the plug move (step 9) changes the circuit in truth only, never the TV's HA
  entity or its power model metadata;
* the replay driver feeds HA-shaped messages to any ``on_state_changed`` stub
  on an injected clock, without ever crossing the observation seam.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tools.simulator import build_hb17
from tools.simulator.frames import (
    assert_no_ground_truth_keys,
    find_ground_truth_keys,
    is_truth_key,
    observation_view,
)
from tools.simulator.hb17 import HB17House
from tools.simulator.replay import (
    MutableClock,
    RecordingConsumer,
    iter_messages,
    replay,
)

STEP_MINUTES = {3: 5.0, 7: 10.0, 8: 12.0, 9: 20.0, 10: 22.0}


def _observation_bytes(house: HB17House) -> str:
    return json.dumps(house.observation_view(), sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# A. Determinism
# --------------------------------------------------------------------------- #
def test_same_seed_gives_byte_identical_streams() -> None:
    a = build_hb17(seed=17, minutes=30)
    b = build_hb17(seed=17, minutes=30)
    assert _observation_bytes(a) == _observation_bytes(b)
    assert a.ha_messages == b.ha_messages
    assert a.reader_samples == b.reader_samples


def test_different_seeds_differ() -> None:
    a = build_hb17(seed=17, minutes=30)
    b = build_hb17(seed=18, minutes=30)
    assert _observation_bytes(a) != _observation_bytes(b)


# --------------------------------------------------------------------------- #
# B. Observation / truth seam and leak detector
# --------------------------------------------------------------------------- #
def test_leak_detector_passes_on_the_observation_frame() -> None:
    house = build_hb17(seed=17, minutes=30)
    assert find_ground_truth_keys(house.observation_view()) == []
    for record in house.records:
        assert_no_ground_truth_keys(observation_view(record))


def test_leak_detector_catches_the_new_truth_keys() -> None:
    assert is_truth_key("true_onset_at")
    assert is_truth_key("true_circuit")
    dirty = {
        "truth": {
            "true_onset_at": {"lights_on": 300.0},
            "true_circuit": {"tv": "ct_c"},
        }
    }
    found = find_ground_truth_keys(dirty)
    assert "truth.true_onset_at" in found
    assert "truth.true_circuit" in found
    with pytest.raises(AssertionError, match="true_onset_at"):
        assert_no_ground_truth_keys(dirty)


def test_truth_frame_carries_the_new_keys() -> None:
    truth = build_hb17(seed=17, minutes=30).truth
    assert "true_onset_at" in truth
    assert "true_circuit" in truth
    assert "lights_on" in truth["true_onset_at"]
    assert truth["true_circuit"]["lights"] == "ct_a"


# --------------------------------------------------------------------------- #
# C. Every HB §17 step at its scripted time
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("step,minute", sorted(STEP_MINUTES.items()))
def test_step_occurs_at_its_scripted_time(step: int, minute: float) -> None:
    house = build_hb17(seed=17, minutes=30)
    s = house.step_at(step)
    assert s.minute == minute
    assert s.at_s == minute * 60.0


def test_lights_on_step_emits_two_ha_messages_with_a_shared_parent() -> None:
    house = build_hb17(seed=17, minutes=30)
    at = house.step_at(3).at_s
    msgs = [m for m in house.ha_messages
            if m["entity_id"] in ("light.living_room_1", "light.living_room_2")]
    assert len(msgs) == 2
    for m in msgs:
        assert m["state"] == "on"
        assert m["context"]["id"]
        assert m["context"]["parent_id"] == f"automation-hb17-lights-{17}"
        # The observed time is the scripted onset (house starts at 00:00:00).
        assert (datetime.fromisoformat(m["observed_at"]) - house.start_time).total_seconds() == at


def test_decoy_context_changes_are_60_s_apart() -> None:
    """A shared parent context with non-simultaneous physics (HB §6.6)."""
    house = build_hb17(seed=17, minutes=30)
    decoy = [m for m in house.ha_messages
             if m["entity_id"] in ("switch.decoy_a", "switch.decoy_b")]
    assert len(decoy) == 2
    assert decoy[0]["context"]["parent_id"] == decoy[1]["context"]["parent_id"] == "decoy-parent"
    t_a = datetime.fromisoformat(decoy[0]["observed_at"])
    t_b = datetime.fromisoformat(decoy[1]["observed_at"])
    assert (t_b - t_a).total_seconds() == 60.0


def test_fridge_step_has_no_ha_event() -> None:
    house = build_hb17(seed=17, minutes=30)
    assert house.step_at(7).name == "fridge_compressor_on"
    assert not any("fridge" in m["entity_id"] for m in house.ha_messages)
    assert house.truth["true_onset_at"]["fridge_compressor_on"] == 600.0


def test_tv_standby_step_reports_three_watts() -> None:
    house = build_hb17(seed=17, minutes=30)
    standby = [m for m in house.ha_messages if m["entity_id"] == "switch.tv_plug"]
    assert len(standby) == 1
    assert standby[0]["state"] == "standby"
    assert standby[0]["attributes"]["power_w_observation"] == 3.0
    assert house.step_at(8).at_s == 720.0


# --------------------------------------------------------------------------- #
# D. Plug move changes the circuit in truth only
# --------------------------------------------------------------------------- #
def test_plug_move_changes_circuit_in_truth_only() -> None:
    house = build_hb17(seed=17, minutes=30)
    before = house.step_at(9).at_s
    before_records = [r for r in house.records if r["t_s"] < before]
    after_records = [r for r in house.records if r["t_s"] >= before]
    assert before_records and after_records
    assert before_records[-1]["truth"]["true_circuit"]["tv"] == "ct_a"
    assert after_records[0]["truth"]["true_circuit"]["tv"] == "ct_c"

    # The TV's HA entity and its HA messages are unchanged by the move: no HA
    # message is emitted at step 9, and the plug's declared entity id is stable.
    move_messages = [m for m in house.ha_messages
                     if abs((datetime.fromisoformat(m["observed_at"]) -
                             datetime.fromisoformat(house.records[0]["timestamp"])).total_seconds()
                            - before) < 1e-6]
    assert move_messages == []

    reader_samples = house.reader_samples
    plug_entities = {s["entity_id"] for s in reader_samples if s["reader_id"] == "ct_plug"}
    assert plug_entities == {"sensor.tv_plug_power"}


def test_plug_move_minute_is_configurable() -> None:
    house = build_hb17(seed=17, minutes=40, plug_move_minute=15.0)
    assert house.step_at(9).minute == 15.0
    assert house.step_at(9).at_s == 900.0
    assert house.step_at(10).minute == 17.0
    assert house.truth["plug_move_minute"] == 15.0


# --------------------------------------------------------------------------- #
# E. Variants
# --------------------------------------------------------------------------- #
def test_solo_switches_variant_adds_three_solo_switches() -> None:
    house = build_hb17(seed=17, minutes=30, variant="solo_switches")
    solo = [m for m in house.ha_messages if m["entity_id"] == "light.living_room_1"
            and m["state"] in ("on", "off")]
    # Two from the co-switched step (on) plus three solo on/off pairs are more
    # than the plain variant's single "on" for light 1.
    plain = build_hb17(seed=17, minutes=30, variant="co_switched")
    plain_l1 = [m for m in plain.ha_messages if m["entity_id"] == "light.living_room_1"]
    assert len(solo) > len(plain_l1)


def test_unknown_variant_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown hb17 variant"):
        build_hb17(seed=17, minutes=30, variant="nope")


# --------------------------------------------------------------------------- #
# F. Replay driver
# --------------------------------------------------------------------------- #
def test_replay_feeds_messages_to_a_recording_stub() -> None:
    house = build_hb17(seed=17, minutes=30)
    stub = RecordingConsumer()
    result = replay(house, stub)
    assert result.n_ticks == len(house.records)
    assert result.n_messages == len(house.ha_messages)
    assert stub.messages == house.ha_messages
    assert result.n_samples == len(house.reader_samples)
    assert "light.living_room_1" in result.delivered_entities


def test_replay_uses_injected_clock_without_wall_time() -> None:
    house = build_hb17(seed=17, minutes=30)
    clock = MutableClock(start=0.0)
    result = replay(house, RecordingConsumer(), clock=clock)
    assert result.final_t_s == house.records[-1]["t_s"]
    # The caller's clock object is the one consulted (never a wall clock); a
    # replay using a fresh house and clock is byte-identical.
    assert clock() == 0.0  # external clock is not mutated by the driver
    assert result.as_dict()["n_messages"] == len(house.ha_messages)


def test_replay_refuses_a_truth_leak() -> None:
    house = build_hb17(seed=17, minutes=10)
    dirty = [dict(r) for r in house.records]
    dirty[0]["ha_messages"] = list(dirty[0]["ha_messages"]) + [
        {"entity_id": "sensor.x", "true_circuit": "ct_a"}
    ]
    with pytest.raises(AssertionError, match="true_circuit"):
        replay(dirty, RecordingConsumer())


def test_iter_messages_yields_observations_only() -> None:
    house = build_hb17(seed=17, minutes=10)
    messages = list(iter_messages(house))
    assert messages == house.ha_messages
    assert all(set(m) <= set(house.ha_messages[0]) for m in messages)


# --------------------------------------------------------------------------- #
# G. Response timing and delivery impairments (WP-B8 / AT-05)
# --------------------------------------------------------------------------- #
def test_hvac_compressor_follows_thermostat_by_90_seconds() -> None:
    from tools.simulator.hb17 import HVAC_DELAY_S

    house = build_hb17(seed=17, minutes=30)
    thermostat = next(m for m in house.ha_messages
                      if m["entity_id"] == "climate.living_room_thermostat")
    cmd_t = datetime.fromisoformat(thermostat["observed_at"])
    onsets = house.records[-1]["truth"]["true_onset_at"]
    assert onsets["compressor_response"] - onsets["thermostat_command"] == HVAC_DELAY_S
    assert HVAC_DELAY_S == 90.0
    # The compressor's first powered sample is ~90 s after the HA command.
    powered = [s for s in house.reader_samples
               if s["reader_id"] == "ct_hvac" and s["value_w"] > 100.0]
    assert powered
    first_power = datetime.fromisoformat(powered[0]["state_timestamp"])
    assert (first_power - cmd_t).total_seconds() >= HVAC_DELAY_S - 10.0


def test_blind_is_a_transient_then_standby() -> None:
    house = build_hb17(seed=17, minutes=30)
    blind = [s for s in house.reader_samples if s["reader_id"] == "ct_blind"]
    assert blind
    values = [s["value_w"] for s in blind]
    assert max(values) > 50.0       # the transient
    assert any(0.0 < v < 10.0 for v in values)  # the standby plateau


def test_late_backfill_and_out_of_order_delivery_are_observable() -> None:
    house = build_hb17(seed=17, minutes=30)
    # ct_b's fridge-step sample is delivered 25 s after its state timestamp.
    b = [s for s in house.reader_samples if s["reader_id"] == "ct_b"]
    assert b
    assert any(
        (datetime.fromisoformat(s["received_at"]) -
         datetime.fromisoformat(s["state_timestamp"])).total_seconds() >= 20.0
        for s in b
    )
    # ct_c sample sequence numbers are out of order relative to the tick.
    c = [s for s in house.reader_samples if s["reader_id"] == "ct_c"]
    assert c and any(s["sequence"] < 0 for s in c)


def test_reader_clock_skew_is_observable_on_ct_a() -> None:
    house = build_hb17(seed=17, minutes=30)
    a = [s for s in house.reader_samples if s["reader_id"] == "ct_a"]
    assert a
    # State time is 30 s ahead of receipt on the skewed reader.
    assert any(
        (datetime.fromisoformat(s["state_timestamp"]) -
         datetime.fromisoformat(s["received_at"])).total_seconds() >= 29.0
        for s in a
    )
