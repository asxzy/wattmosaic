"""Simulator tests for the WP2 additions: factor channels, gaps, spikes, flips.

These live in ``tests/simulation`` because they test the *simulator* itself, not
the fixtures built on it.  Every assertion here is about a knob behaving
surgically: the impairment must move the measurement and leave ground truth
alone, or the fixture it enables would prove nothing.

Nothing in this file modifies or replaces an existing simulation test.
"""

from __future__ import annotations

import json

import pytest
from wattmosaic_engine.contracts import Phase

from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.behavior import (
    HotTubProfile,
    MotorProfile,
    StepProfile,
    build_profiles,
)
from tools.simulator.cli import _clean
from tools.simulator.factors import FactorEpoch, FactorOwner, FactorTimeline, RefossFactorChannel
from tools.simulator.frames import find_ground_truth_keys
from tools.simulator.sensors import QualityLevel
from tools.simulator.topology import (
    SimChannel,
    SimDevice,
    SimDeviceType,
    build_real_home_topology,
)


def _single_device_topology(device: SimDevice, channels: list[SimChannel]):
    topo = build_real_home_topology()
    topo.channels.extend(channels)
    topo.devices = [
        d for d in topo.devices if d.device_id in {"unknown_load_l1", "lighting_1"}
    ]
    topo.devices.append(device)
    # Depth is only needed for parent propagation; these are leaves under L1.
    for channel in channels:
        channel._depth = 1
    return topo


# --------------------------------------------------------------------------- #
# Factor channels
# --------------------------------------------------------------------------- #


def test_a_user_factor_multiplies_the_published_value_only() -> None:
    """The conductor reading is truth; the ×2 applies to the report."""
    topo = build_real_home_topology()
    device = next(d for d in topo.devices if d.device_id == "water_heater")
    device.circuits = {"ct_main_water_heater_ct1": 1.0}
    topo.channels = [c for c in topo.channels if c.ct_id != "ct_main_water_heater_ct2"]
    channel = RefossFactorChannel(
        ct_id="ct_main_water_heater_ct1",
        device_id="water_heater",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.constant(2.0, applied_by=FactorOwner.USER),
    )
    scenario = HouseholdScenario(
        SimulatorConfig(
            seed=202, minutes=12, interval=15,
            factor_channels={channel.ct_id: channel},
            simultaneous_event_batches=0,
        ),
        topology=topo,
    )
    records = scenario.to_list()
    assert records
    scaled = [
        rec["measurements"]["ct_main_water_heater_ct1"]
        for rec in records
        if "ct_main_water_heater_ct1" in rec["measurements"]
        and not rec["measurements"]["ct_main_water_heater_ct1"]["repeated"]
    ]
    assert scaled, "no delivered sample for the factor channel"
    for sample in scaled[:10]:
        # Ground truth is the *device* contribution to that conductor.
        truth = sample["true_channel_power_w"]
        assert sample["value_w"] == pytest.approx(truth * 2.0, rel=0.05, abs=20.0)
        assert sample["factor"]["factor"] == 2.0
        assert sample["factor"]["applied_by"] == "user"


def test_a_monitor_published_factor_is_reported_unscaled() -> None:
    """The monitor already applied it, so the simulator must not apply it again."""
    topo = build_real_home_topology()
    device = next(d for d in topo.devices if d.device_id == "water_heater")
    device.circuits = {"ct_main_water_heater_ct1": 1.0}
    topo.channels = [c for c in topo.channels if c.ct_id != "ct_main_water_heater_ct2"]
    channel = RefossFactorChannel(
        ct_id="ct_main_water_heater_ct1",
        device_id="water_heater",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.constant(
            2.0, applied_by=FactorOwner.MONITOR, published=True
        ),
    )
    scenario = HouseholdScenario(
        SimulatorConfig(seed=7, minutes=10, interval=15,
                        factor_channels={channel.ct_id: channel},
                        simultaneous_event_batches=0),
        topology=topo,
    )
    sample = next(
        rec["measurements"]["ct_main_water_heater_ct1"]
        for rec in scenario.to_list()
        if "ct_main_water_heater_ct1" in rec["measurements"]
        and not rec["measurements"]["ct_main_water_heater_ct1"]["repeated"]
    )
    assert sample["value_w"] == pytest.approx(sample["true_channel_power_w"], rel=0.05, abs=20.0)
    assert sample["factor"]["published"] is True


def test_factor_metadata_is_observable_but_never_ground_truth() -> None:
    """The factor frame is configuration, so it survives the seam — cleanly."""
    topo = build_real_home_topology()
    device = next(d for d in topo.devices if d.device_id == "water_heater")
    device.circuits = {"ct_main_water_heater_ct1": 1.0}
    topo.channels = [c for c in topo.channels if c.ct_id != "ct_main_water_heater_ct2"]
    channel = RefossFactorChannel(
        ct_id="ct_main_water_heater_ct1",
        device_id="water_heater",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.constant(2.0, applied_by=FactorOwner.USER),
    )
    scenario = HouseholdScenario(
        SimulatorConfig(seed=3, minutes=4, interval=30,
                        factor_channels={channel.ct_id: channel},
                        simultaneous_event_batches=0),
        topology=topo,
    )
    meta = scenario.config.as_metadata()["factor_channels"]
    assert meta["ct_main_water_heater_ct1"]["factor_intervals"][0]["multiplier"] == 2.0
    assert not find_ground_truth_keys(meta)


def test_the_simulator_version_is_bumped_for_the_new_controls() -> None:
    meta = SimulatorConfig().as_metadata()
    assert meta["simulator_version"] == "1.1.0"
    for key in (
        "factor_channels",
        "channel_delay_s",
        "missing_windows",
        "spike_windows",
        "polarity_flips",
    ):
        assert key in meta, key


# --------------------------------------------------------------------------- #
# Missing intervals, spikes, polarity flips
# --------------------------------------------------------------------------- #


def test_a_missing_window_produces_no_sample_at_all() -> None:
    """A gap is silence; an outage is a stale repeat. The two must differ."""
    cfg = SimulatorConfig(
        seed=5, minutes=20, interval=15,
        missing_windows={"ct_main_lighting_ct1": [(300.0, 900.0)]},
        simultaneous_event_batches=0,
    )
    records = HouseholdScenario(cfg).to_list()
    in_gap = [
        rec for rec in records
        if 320.0 <= rec["t_s"] <= 880.0
    ]
    assert in_gap
    assert not any("ct_main_lighting_ct1" in rec["measurements"] for rec in in_gap)
    # Neighbouring channels are unaffected, so the gap is per-channel.
    assert any(rec["measurements"] for rec in in_gap)


def test_a_spike_changes_the_measurement_but_not_the_truth() -> None:
    cfg = SimulatorConfig(
        seed=11, minutes=20, interval=15,
        spike_windows={"ct_main_lighting_ct1": [(600.0, 615.0, 9000.0)]},
        simultaneous_event_batches=0,
    )
    scenario = HouseholdScenario(cfg)
    records = scenario.to_list()
    spiked = [
        rec["measurements"]["ct_main_lighting_ct1"]
        for rec in records
        if "ct_main_lighting_ct1" in rec["measurements"]
        and 590.0 <= rec["t_s"] <= 640.0
        and not rec["measurements"]["ct_main_lighting_ct1"]["repeated"]
    ]
    assert spiked
    assert any(sample["value_w"] > sample["true_channel_power_w"] + 8000.0 for sample in spiked)
    # Ground truth for the channel never sees the impulse.
    truths = [rec["ground_truth"]["ct_powers_w"]["ct_main_lighting_ct1"] for rec in records]
    assert max(truths) < 1000.0


def test_a_polarity_flip_changes_the_sign_without_changing_the_declaration() -> None:
    cfg = SimulatorConfig(
        seed=17, minutes=20, interval=15,
        polarity_flips={"ct_main_lighting_ct2": 600.0},
        simultaneous_event_batches=0,
    )
    records = HouseholdScenario(cfg).to_list()
    early = [
        rec["measurements"]["ct_main_lighting_ct2"]
        for rec in records
        if "ct_main_lighting_ct2" in rec["measurements"]
        and rec["t_s"] < 555.0
        and not rec["measurements"]["ct_main_lighting_ct2"]["repeated"]
    ]
    late = [
        rec["measurements"]["ct_main_lighting_ct2"]
        for rec in records
        if "ct_main_lighting_ct2" in rec["measurements"]
        and rec["t_s"] > 900.0
        and not rec["measurements"]["ct_main_lighting_ct2"]["repeated"]
    ]
    assert early and late
    assert all(sample["polarization"] == "normal" for sample in early + late)
    assert any(sample["value_w"] < 0 for sample in late)
    # Truth stays positive: only the reading flipped.
    assert all(
        rec["ground_truth"]["ct_powers_w"]["ct_main_lighting_ct2"] >= 0 for rec in records
    )


def test_per_channel_delay_is_measurable_in_received_at() -> None:
    cfg = SimulatorConfig(
        seed=23, minutes=10, interval=15,
        channel_delay_s={"ct_main_lighting_ct1": 30.0},
        quality=QualityLevel.EXCELLENT,
        simultaneous_event_batches=0,
    )
    records = HouseholdScenario(cfg).to_list()
    from datetime import datetime

    lags = []
    for rec in records:
        for ct_id in ("ct_main_lighting_ct1", "ct_main_lighting_ct2"):
            sample = rec["measurements"].get(ct_id)
            if not sample or sample["repeated"]:
                continue
            # The honest "vintage" of a sample: how long after the physical
            # instant it describes was it published.  A 30 s extra poll delay
            # must show up here, and must not show up for the other channel.
            stamped = datetime.fromisoformat(sample["state_timestamp"])
            start = datetime.fromisoformat(rec["scenario"]["start_time"])
            lag = (stamped - start).total_seconds() - float(sample["sample_time_s"])
            lags.append((ct_id, lag))
    delayed = [lag for ct_id, lag in lags if ct_id == "ct_main_lighting_ct1"]
    normal = [lag for ct_id, lag in lags if ct_id == "ct_main_lighting_ct2"]
    assert delayed and normal
    assert sum(delayed) / len(delayed) > sum(normal) / len(normal) + 20.0
    # Exponential inter-arrival delay: the *median* is the robust statement.
    import statistics

    assert statistics.median(delayed) > statistics.median(normal) + 15.0


# --------------------------------------------------------------------------- #
# New ground-truth profiles
# --------------------------------------------------------------------------- #


def _profile(cls, **metadata):
    device = SimDevice(
        device_id="probe",
        name="Probe",
        sim_type=SimDeviceType.MOTOR,
        phase=Phase.L1,
        circuits={"ct": 1.0},
        nominal_power_w=800.0,
        ha_entity_id="switch.probe",
        dedicated_ct=True,
        metadata=metadata,
    )
    return cls(device=device, seed=42, horizon_s=3600.0)


def test_a_motor_profile_has_an_inrush_that_decays_to_the_steady_state() -> None:
    profile: MotorProfile = _profile(
        MotorProfile,
        startup_w=4000.0, startup_s=4.0, standby_w=0.0, windows=[(300.0, 900.0)],
    )
    assert profile.power(299.0) == 0.0
    assert profile.power(300.0) == pytest.approx(4000.0)
    assert profile.power(302.0) == pytest.approx(2400.0)
    assert profile.power(400.0) == pytest.approx(800.0)


def test_a_step_profile_evaluates_an_explicit_schedule() -> None:
    profile: StepProfile = _profile(
        StepProfile, steps=[(0.0, 0.0), (300.0, 3600.0), (1500.0, 11000.0)]
    )
    assert profile.power(0.0) == 0.0
    assert profile.power(300.0) == 3600.0
    assert profile.power(1500.0) == 11000.0


def test_a_hot_tub_profile_uses_explicit_cycles() -> None:
    profile: HotTubProfile = _profile(
        HotTubProfile, cycle=[(600.0, 1200.0, 500.0)], standby_w=0.0
    )
    assert profile.power(599.0) == 0.0
    assert profile.power(600.0) == 500.0
    assert profile.power(1199.0) == 500.0
    assert profile.power(1200.0) == 0.0


def test_new_device_types_build_working_profiles() -> None:
    for sim_type in (SimDeviceType.MOTOR, SimDeviceType.STEP):
        device = SimDevice(
            device_id=f"dev_{sim_type.value}",
            name=sim_type.value,
            sim_type=sim_type,
            phase=Phase.L1,
            circuits={"ct": 1.0},
            nominal_power_w=500.0,
            ha_entity_id="switch.probe",
            metadata={"steps": [(0.0, 500.0)]} if sim_type is SimDeviceType.STEP else {},
        )
        profiles = build_profiles([device], seed=3, horizon_s=600.0)
        assert profiles[device.device_id].power(0.0) >= 0.0


# --------------------------------------------------------------------------- #
# Determinism of the new controls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"spike_windows": {"ct_main_lighting_ct1": [(300.0, 315.0, 5000.0)]}},
        {"missing_windows": {"ct_main_lighting_ct1": [(300.0, 900.0)]}},
        {"polarity_flips": {"ct_main_lighting_ct1": 600.0}},
        {"channel_delay_s": {"ct_main_lighting_ct1": 25.0}},
    ],
    ids=["spikes", "gaps", "flip", "delay"],
)
def test_new_controls_are_deterministic(kwargs: dict) -> None:
    def run():
        cfg = SimulatorConfig(seed=101, minutes=12, interval=15,
                              simultaneous_event_batches=0, **kwargs)
        return json.dumps(_clean(HouseholdScenario(cfg).to_list()), sort_keys=True)

    assert run() == run()


def test_a_factor_scenario_is_deterministic() -> None:
    topo = build_real_home_topology()
    device = next(d for d in topo.devices if d.device_id == "water_heater")
    device.circuits = {"ct_main_water_heater_ct1": 1.0}
    topo.channels = [c for c in topo.channels if c.ct_id != "ct_main_water_heater_ct2"]
    channel = RefossFactorChannel(
        ct_id="ct_main_water_heater_ct1",
        device_id="water_heater",
        metered_leg=Phase.L1,
        timeline=FactorTimeline.step(
            600.0,
            FactorEpoch(multiplier=1.0, applied_by=FactorOwner.USER),
            FactorEpoch(multiplier=2.0, applied_by=FactorOwner.USER),
        ),
    )

    def run():
        scenario = HouseholdScenario(
            SimulatorConfig(seed=55, minutes=20, interval=15,
                            factor_channels={channel.ct_id: channel},
                            simultaneous_event_batches=0),
            topology=topo,
        )
        return json.dumps(_clean(scenario.to_list()), sort_keys=True)

    assert run() == run()
    # The step timeline resolves the factor per sample.
    assert channel.factor_at(0.0) == 1.0
    assert channel.factor_at(600.0) == 2.0


# --------------------------------------------------------------------------- #
# CLI: strata are reachable from the shell, defaults are unchanged
# --------------------------------------------------------------------------- #


def test_cli_lists_every_buildable_stratum() -> None:
    import subprocess
    import sys
    from pathlib import Path

    from tools.simulator.labhouse import available_strata

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--list-strata"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    assert result.stdout.split() == list(available_strata())


def test_cli_builds_a_stratum_and_refuses_conflicting_flags() -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    ok = subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli",
         "--stratum", "factor2", "--seed", "1", "--minutes", "3", "--interval", "30"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    records = [json.loads(line) for line in ok.stdout.splitlines() if line.strip()]
    assert records
    # A stratum fixes its own wiring: an extra impairment flag is a mistake, not
    # something to apply on top, so it must be refused rather than ignored.
    bad = subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli",
         "--stratum", "factor2", "--invert-ct", "ct_main_l1"],
        cwd=repo, capture_output=True, text=True,
    )
    assert bad.returncode != 0
    assert "--invert-ct" in bad.stderr


def test_cli_stratum_output_is_deterministic(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    cmd = [sys.executable, "-m", "tools.simulator.cli", "--stratum", "shared",
           "--seed", "9", "--minutes", "3", "--interval", "30", "--out"]
    subprocess.run(cmd + [str(tmp_path / "a")], cwd=repo, check=True, capture_output=True)
    subprocess.run(cmd + [str(tmp_path / "b")], cwd=repo, check=True, capture_output=True)
    assert (tmp_path / "a" / "household.jsonl").read_text() == (
        tmp_path / "b" / "household.jsonl"
    ).read_text()
    assert "stratum: shared" in (tmp_path / "a" / "README.txt").read_text()
