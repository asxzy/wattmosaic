"""Tests for the synthetic household simulator itself.

These must pass unconditionally: the simulator is our own deliverable and has
no dependency on other agents' modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.cli import _clean
from tools.simulator.sensors import QualityLevel
from tools.simulator.topology import REAL_HOME_TOPOLOGY, build_real_home_topology

REQUIRED_DEVICE_TYPES = {
    "binary", "standby", "dimmable", "variable_speed", "hvac",
    "refrigerator", "washing_machine", "dishwasher", "dryer",
    "double_pole", "unknown",
}


def test_topology_mirrors_the_reference_household() -> None:
    topo = REAL_HOME_TOPOLOGY
    ids = {c.ct_id for c in topo.channels}
    # Synthetic reference household, structurally identical to a two-panel
    # multi-channel CT install (see tools/dev/make_reference_house.py).
    assert "ct_main_l1" in ids and "ct_main_l2" in ids
    assert "ct_sub_l1" in ids and "ct_sub_l2" in ids
    entity = {c.ct_id: c.entity_id for c in topo.channels}
    assert entity["ct_main_l1"] == "sensor.panel_a_ct1_power"
    assert entity["ct_sub_network_cabinet"] == "sensor.panel_b_network_cabinet_power"
    assert entity["ct_main_water_heater_ct1"] == "sensor.panel_a_water_heater_ct1_power"
    # 18 channels per panel (CT1/CT2 + 16 named), as in the reference audit.
    assert sum(1 for c in topo.channels if c.panel == "main") == 18
    assert sum(1 for c in topo.channels if c.panel == "sub") == 18
    # Reference median cadence.
    assert topo.channels[0].expected_interval_s == pytest.approx(15.7)


def test_topology_covers_all_required_device_classes() -> None:
    types = {d.sim_type.value for d in REAL_HOME_TOPOLOGY.devices}
    assert types >= REQUIRED_DEVICE_TYPES
    assert any(d.double_pole for d in REAL_HOME_TOPOLOGY.devices)
    unknown = [d for d in REAL_HOME_TOPOLOGY.devices if d.sim_type.value == "unknown"]
    assert unknown, "unknown loads are required"
    # Unknown loads must not be attributable.
    assert all(not d.metadata.get("attributable", True) for d in unknown)


def test_nested_cts_are_never_summed() -> None:
    """Sub panel mains nest under main panel mains."""
    topo = REAL_HOME_TOPOLOGY
    cmap = topo.channel_map()
    assert cmap["ct_sub_l1"].parent_ct_id == "ct_main_l1"
    assert cmap["ct_sub_l2"].parent_ct_id == "ct_main_l2"
    powers = {d.device_id: 100.0 for d in topo.devices}
    ct = topo.true_ct_powers(powers)
    # Main L1 includes sub L1, so main > sub and main != sub + main.
    assert ct["ct_main_l1"] > ct["ct_sub_l1"]
    # Naive sum of all mains would double count the sub-feed.
    naive = ct["ct_main_l1"] + ct["ct_main_l2"] + ct["ct_sub_l1"] + ct["ct_sub_l2"]
    true_main = ct["ct_main_l1"] + ct["ct_main_l2"]
    assert naive > true_main


def test_double_pole_legs_sum_to_device_power() -> None:
    topo = REAL_HOME_TOPOLOGY
    ct = topo.true_ct_powers({d.device_id: 0.0 for d in topo.devices}
                             | {"water_heater": 6000.0})
    assert ct["ct_main_water_heater_ct1"] == pytest.approx(3000.0)
    assert ct["ct_main_water_heater_ct2"] == pytest.approx(3000.0)
    assert ct["ct_main_water_heater_ct1"] + ct["ct_main_water_heater_ct2"] == pytest.approx(6000.0)


def test_ground_truth_is_independent_of_measurements() -> None:
    """Impairment settings must not change any ground-truth value."""
    clean = HouseholdScenario(SimulatorConfig(seed=11, minutes=5, interval=15,
                                              quality=QualityLevel.EXCELLENT))
    awful = HouseholdScenario(SimulatorConfig(seed=11, minutes=5, interval=15,
                                              quality=QualityLevel.POOR))
    clean_recs = clean.to_list()
    awful_recs = awful.to_list()
    assert [r["ground_truth"] for r in clean_recs] == [r["ground_truth"] for r in awful_recs]
    # ...but measurements differ (noise actually applied).
    assert [r["measurements"] for r in clean_recs] != [r["measurements"] for r in awful_recs]


def test_determinism_for_fixed_seed() -> None:
    a = HouseholdScenario(seed=99, minutes=8, interval=20).to_list()
    b = HouseholdScenario(seed=99, minutes=8, interval=20).to_list()
    assert json.dumps(_clean(a), sort_keys=True) == json.dumps(_clean(b), sort_keys=True)


def test_different_seeds_differ() -> None:
    a = HouseholdScenario(seed=1, minutes=5).to_list()
    b = HouseholdScenario(seed=2, minutes=5).to_list()
    assert [r["ground_truth"]["devices"] for r in a] != [r["ground_truth"]["devices"] for r in b]


def test_record_schema_has_three_timestamps_and_metadata() -> None:
    recs = HouseholdScenario(seed=5, minutes=3, interval=20).to_list()
    assert recs
    for rec in recs[:5]:
        assert {"timestamp", "t_s", "scenario", "ground_truth", "measurements",
                "latest_measurements", "ha_states", "events"} <= set(rec)
        assert rec["scenario"]["seed"] == 5
        assert "median" not in rec["scenario"]  # sanity: no stray metric fields
        for m in rec["measurements"].values():
            assert m["state_timestamp"]
            assert m["received_at"]
            assert "physical_timestamp" in m
            assert "value_w" in m and "quality" in m and "repeated" in m
            # state_timestamp must not be later than received_at.
            assert m["state_timestamp"] <= m["received_at"]


def test_ha_states_carry_entity_and_change_times() -> None:
    rec = next(iter(HouseholdScenario(seed=3, minutes=2, interval=20).records()))
    assert rec["ha_states"]
    for state in rec["ha_states"].values():
        assert state["entity_id"]
        assert state["last_changed"] and state["last_updated"]
        assert "attributes" in state


def test_stale_and_missing_are_observable() -> None:
    cfg = SimulatorConfig(
        seed=21, minutes=30, interval=15, quality=QualityLevel.POOR,
        outage_windows={"ct_main_l1": [(300.0, 900.0)]},
    )
    recs = HouseholdScenario(cfg).to_list()
    qualities = [m.get("quality") for r in recs for m in r["measurements"].values()]
    assert any(q in ("stale", "degraded") for q in qualities), qualities
    repeated = [m for r in recs for m in r["measurements"].values() if m["repeated"]]
    assert repeated, "poor quality must produce repeated/stale values"
    # Outage window produces an explicit missing-quality observation.
    assert any(
        m.get("quality") == "missing"
        for r in recs for m in r["measurements"].values()
    )


def test_polarity_inversion_flips_sign_and_is_declared() -> None:
    cfg = SimulatorConfig(seed=31, minutes=10, interval=20,
                         inverted_channels=("ct_sub_l1",))
    recs = HouseholdScenario(cfg).to_list()
    inverted = [r["measurements"]["ct_sub_l1"] for r in recs
                if "ct_sub_l1" in r["measurements"] and not r["measurements"]["ct_sub_l1"]["repeated"]]
    assert inverted
    assert all(m["polarization"] == "inverted" for m in inverted)
    assert any(m["value_w"] < 0 for m in inverted)
    # Ground truth sign is untouched.
    assert all(r["ground_truth"]["ct_powers_w"]["ct_sub_l1"] >= 0 for r in recs)


def test_measurement_drift_accumulates() -> None:
    cfg = SimulatorConfig(seed=41, minutes=60, interval=10,
                         drifting_channels={"ct_main_l1": 50_000.0})
    recs = HouseholdScenario(cfg).to_list()
    # Compare delivered vs true channel power over time.
    early, late = [], []
    for r in recs:
        m = r["measurements"].get("ct_main_l1")
        if m and not m["repeated"] and m["true_channel_power_w"] > 500:
            ratio = m["value_w"] / m["true_channel_power_w"]
            (early if r["t_s"] < 900 else late).append(ratio)
    assert early and late
    assert sum(late) / len(late) > sum(early) / len(early) * 1.01


def test_simultaneous_events_exist_and_are_simultaneous() -> None:
    s = HouseholdScenario(seed=77, minutes=90, interval=15)
    assert s.simultaneous_anchors
    for anchor in s.simultaneous_anchors:
        t = anchor["t"]
        group = anchor["device_ids"]
        assert len(group) >= 2
        for dev in group:
            before = s.device_power(dev, t - 1.0)
            after = s.device_power(dev, t + 1.0)
            assert after - before > 50.0, (dev, before, after)


def test_concurrency_levels_are_reachable() -> None:
    recs = HouseholdScenario(seed=13, minutes=120, interval=15).to_list()
    levels = {r["ground_truth"]["concurrency"] for r in recs}
    assert len(levels) >= 3


def test_energy_accumulates_monotonically() -> None:
    recs = HouseholdScenario(seed=17, minutes=20, interval=30).to_list()
    energies = [r["ground_truth"]["device_energy_kwh"]["water_heater"] for r in recs]
    assert energies == sorted(energies)
    assert energies[-1] > 0


def test_cli_writes_deterministic_output(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    cmd = [sys.executable, "-m", "tools.simulator.cli", "--seed", "123",
           "--minutes", "6", "--interval", "30", "--out"]
    subprocess.run(cmd + [str(out_a)], cwd=repo, check=True, capture_output=True)
    subprocess.run(cmd + [str(out_b)], cwd=repo, check=True, capture_output=True)

    manifest_a = json.loads((out_a / "manifest.json").read_text())
    manifest_b = json.loads((out_b / "manifest.json").read_text())
    assert manifest_a["sha256"] == manifest_b["sha256"]
    assert manifest_a["record_count"] == manifest_b["record_count"] == 12
    assert (out_a / "samples.jsonl").read_text() == (out_b / "samples.jsonl").read_text()
    assert (out_a / "household.jsonl").read_text() == (out_b / "household.jsonl").read_text()

    # Changing the seed changes the hash.
    out_c = tmp_path / "c"
    subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--seed", "124",
         "--minutes", "6", "--interval", "30", "--out", str(out_c)],
        cwd=repo, check=True, capture_output=True,
    )
    manifest_c = json.loads((out_c / "manifest.json").read_text())
    assert manifest_c["sha256"] != manifest_a["sha256"]


def test_cli_json_format_and_topology_dump(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    out = tmp_path / "json"
    subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--seed", "1",
         "--minutes", "2", "--interval", "30", "--format", "json", "--out", str(out)],
        cwd=repo, check=True, capture_output=True,
    )
    doc = json.loads((out / "household.json").read_text())
    assert doc["records"] and doc["records"][0]["scenario"]["seed"] == 1

    res = subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--print-topology"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    topo = json.loads(res.stdout)
    assert len(topo["channels"]) == 36
    assert len(topo["devices"]) == len(REAL_HOME_TOPOLOGY.devices)


def test_cli_handles_impairment_flags(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    out = tmp_path / "imp"
    subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--seed", "2", "--minutes", "10",
         "--interval", "30", "--quality", "poor", "--invert-ct", "ct_sub_l1",
         "--drift-ct", "ct_main_l1=1000", "--outage", "ct_main_l2=60:180",
         "--out", str(out)],
        cwd=repo, check=True, capture_output=True,
    )
    assert (out / "household.jsonl").exists()
    lines = (out / "household.jsonl").read_text().splitlines()
    # Poor quality has a mean 6 s delivery delay plus jitter, so the first
    # record is not guaranteed to contain every channel.  Scan the opening
    # records instead of assuming tick 0 is complete.
    early = [json.loads(line) for line in lines[:4]]
    assert any(
        "ct_sub_l1" in rec["measurements"] or "ct_sub_l1" in rec["latest_measurements"]
        for rec in early
    )


def test_bad_impairment_flag_is_rejected(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        [sys.executable, "-m", "tools.simulator.cli", "--drift-ct", "garbage"],
        cwd=repo, capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "invalid --drift-ct" in (res.stderr + res.stdout)


def _relabel_topology(topo):
    """Return a copy of ``topo`` with every human-facing label replaced.

    Structure — circuits, parents, phases, power — is preserved exactly; only the
    ids, names and entity ids change.  Used to prove that a rename is a rename.
    """
    import copy as _copy

    from tools.simulator.topology import HouseholdTopology

    dev_ids = {d.device_id: f"anon_dev_{i:03d}" for i, d in enumerate(topo.devices)}
    ct_ids = {c.ct_id: f"anon_ct_{i:03d}" for i, c in enumerate(topo.channels)}

    devices = _copy.deepcopy(topo.devices)
    channels = _copy.deepcopy(topo.channels)
    for dev in devices:
        dev.device_id = dev_ids[dev.device_id]
        dev.name = "Anonymous Load"
        dev.ha_entity_id = "sensor.anonymous"
        dev.seed_key = ""  # re-assigned from declaration order
        dev.circuits = {ct_ids[k]: v for k, v in dev.circuits.items()}
    for ch in channels:
        ch.seed_key = ""
        if ch.parent_ct_id:
            ch.parent_ct_id = ct_ids[ch.parent_ct_id]
        if ch.paired_ct_id:
            ch.paired_ct_id = ct_ids[ch.paired_ct_id]
        ch.ct_id = ct_ids[ch.ct_id]
        ch.entity_id = f"sensor.{ch.ct_id}"
        ch.name = "Anonymous CT"
    return HouseholdTopology(channels=channels, devices=devices, notes=list(topo.notes)), ct_ids


def test_a_rename_cannot_change_any_measurement() -> None:
    """Renaming a load or channel must not move a single measured watt.

    The simulator seeds each load's waveform from a *structural* key, so the
    ground truth and every delivered sample depend only on topology structure.
    Before that decoupling the seed came from the display id, and a pure
    privacy rename silently re-realised the whole scenario - which moved every
    published accuracy number (one unrelated rename shifted ``humidifier_2``
    MAE from 10.45 W to 20.44 W).

    This is the negative verification for that fix: relabel everything, then
    require the measurements to be byte-identical.
    """
    base_topo = build_real_home_topology()
    renamed_topo, ct_ids = _relabel_topology(base_topo)

    def run(topo):
        cfg = SimulatorConfig(seed=202, minutes=15, interval=15, quality=QualityLevel.GOOD)
        return list(HouseholdScenario(cfg, topology=topo).records())

    base_records = run(base_topo)
    renamed_records = run(renamed_topo)
    assert len(base_records) == len(renamed_records)

    for base, renamed in zip(base_records, renamed_records, strict=True):
        assert base["timestamp"] == renamed["timestamp"]
        base_values = {
            ct_ids[ct_id]: m["value_w"] for ct_id, m in base["measurements"].items()
        }
        renamed_values = {ct_id: m["value_w"] for ct_id, m in renamed["measurements"].items()}
        assert base_values == renamed_values, base["timestamp"]
        assert base["ground_truth"]["main_total_w"] == renamed["ground_truth"]["main_total_w"]
        base_dev = {
            anon: base["ground_truth"]["devices"][orig]["power_w"]
            for orig, anon in _device_relabel_map(base_topo).items()
        }
        renamed_dev = {
            dev_id: entry["power_w"]
            for dev_id, entry in renamed["ground_truth"]["devices"].items()
        }
        assert base_dev == renamed_dev, base["timestamp"]


def _device_relabel_map(topo):
    return {d.device_id: f"anon_dev_{i:03d}" for i, d in enumerate(topo.devices)}


def test_topology_supports_inverted_channels() -> None:
    topo = build_real_home_topology(inverted_channels=("ct_main_l1",))
    assert topo.channel_map()["ct_main_l1"].polarity.value == "inverted"
    assert topo.channel_map()["ct_main_l2"].polarity.value == "normal"


def test_scenario_summary_is_measurement_free() -> None:
    s = HouseholdScenario(seed=8, minutes=10, interval=15)
    summary = s.summary()
    assert summary["devices"] >= 30
    assert summary["channels"] == 36
    assert summary["simultaneous_event_anchors"] >= 1
    assert summary["midpoint_main_w"] > 0


# --------------------------------------------------------------------------- #
# WP-B8 / X07 simulator extensions
# --------------------------------------------------------------------------- #
def test_ha_state_changes_carry_a_context() -> None:
    """Every HA state carries the top-level HA context (id/parent/user)."""
    rec = next(iter(HouseholdScenario(seed=3, minutes=2, interval=20).records()))
    assert rec["ha_states"]
    for state in rec["ha_states"].values():
        ctx = state["context"]
        assert set(ctx) == {"id", "parent_id", "user_id"}
        assert ctx["id"]


def test_explicit_ha_contexts_are_copied_onto_states_and_events() -> None:
    """A fixture-provided context (automation burst / decoy) survives verbatim."""
    ctx = {"id": "ctx-1", "parent_id": "parent-automation", "user_id": None}
    cfg = SimulatorConfig(seed=3, minutes=30, interval=20, ha_contexts={"tv": ctx})
    recs = HouseholdScenario(cfg).to_list()
    assert recs[0]["ha_states"]["media_player.tv"]["context"] == ctx
    events = [e for r in recs for e in r["events"] if e["device_id"] == "tv"]
    assert events
    assert all(e["context"]["parent_id"] == "parent-automation" for e in events)


def test_push_on_change_suppresses_unchanged_samples() -> None:
    """A flat reader emits no sample while its reported truth is unchanged."""
    from tools.simulator.topology import (
        CTType,
        HouseholdTopology,
        Phase,
        SimChannel,
        SimDevice,
        SimDeviceType,
    )

    # A constant load: its physical truth never changes, so push-on-change must
    # produce a single sample then go silent for the rest of the horizon.
    topo = HouseholdTopology(
        channels=[SimChannel("ct_const", "sensor.const", "Const", CTType.MAIN, Phase.L1)],
        devices=[SimDevice("dev_const", "Const", SimDeviceType.STEP, Phase.L1,
                           {"ct_const": 1.0}, 500.0, "switch.const",
                           metadata={"steps": [(0.0, 500.0)]})],
    )
    explicit_cfg = SimulatorConfig(seed=5, minutes=10, interval=5,
                                   quality=QualityLevel.EXCELLENT,
                                   channel_intervals={"ct_const": 2.0})
    push_cfg = SimulatorConfig(seed=5, minutes=10, interval=5,
                               quality=QualityLevel.EXCELLENT,
                               channel_intervals={"ct_const": 2.0}, push_on_change=True)
    explicit = [r["measurements"]["ct_const"] for r in
                HouseholdScenario(explicit_cfg, topology=topo).to_list()
                if "ct_const" in r["measurements"]]
    pushed = [r["measurements"]["ct_const"] for r in
              HouseholdScenario(push_cfg, topology=topo).to_list()
              if "ct_const" in r["measurements"]]
    assert len(explicit) > 10
    assert 0 < len(pushed) < len(explicit)
    # Every surviving sample carries the liveness timestamp.
    assert all(m["last_reported"] for m in pushed)


def test_measurement_window_ms_is_declared_on_samples() -> None:
    """A per-reader averaging window is reported as observation metadata."""
    cfg = SimulatorConfig(seed=5, minutes=3, interval=20,
                          measurement_window_ms={"ct_main_l1": 1500.0})
    rec = next(iter(HouseholdScenario(cfg).records()))
    sample = rec["measurements"]["ct_main_l1"]
    assert sample["measurement_window_ms"] == 1500.0
    other = rec["measurements"]["ct_main_l2"]
    assert other["measurement_window_ms"] is None


def test_window_averaging_smeears_a_step() -> None:
    """A windowed reported value moves gradually across a physical step."""
    # A step profile whose single transition is inside the horizon.
    from tools.simulator.scenario import HouseholdScenario as HS
    from tools.simulator.topology import (
        CTType,
        HouseholdTopology,
        Phase,
        SimChannel,
        SimDevice,
        SimDeviceType,
    )

    topo = HouseholdTopology(
        channels=[
            SimChannel("ct_x", "sensor.x", "X", CTType.MAIN, Phase.L1),
        ],
        devices=[
            SimDevice("dev_x", "X", SimDeviceType.STEP, Phase.L1,
                      {"ct_x": 1.0}, 1000.0, "switch.x",
                      metadata={"steps": [(0.0, 0.0), (300.0, 1000.0)]}),
        ],
    )
    window_ms = 4000.0
    cfg = SimulatorConfig(seed=7, minutes=8, interval=5, quality=QualityLevel.EXCELLENT,
                          channel_intervals={"ct_x": 2.0},
                          measurement_window_ms={"ct_x": window_ms})
    recs = list(HS(cfg, topology=topo).records())
    just_before = [r["measurements"]["ct_x"]["value_w"] for r in recs
                   if 290 <= r["t_s"] < 300]
    just_after = [r["measurements"]["ct_x"]["value_w"] for r in recs
                  if 300 <= r["t_s"] < 310]
    assert just_before and just_after
    # The step is smeared: just after the transition the reported value is
    # strictly between the before-level and the full 1000 W level.
    early_after = just_after[0]
    assert 0.0 < early_after < 1000.0, (just_before, just_after)


def test_late_backfill_arrives_after_its_sample_time() -> None:
    """A backfilled sample's receipt is later than its physical sample time."""
    cfg = SimulatorConfig(seed=5, minutes=8, interval=10, quality=QualityLevel.EXCELLENT,
                          late_backfill_s={"ct_main_l1": 25.0})
    recs = HouseholdScenario(cfg).to_list()
    late = [m for r in recs for m in r["measurements"].values()
            if m["ct_id"] == "ct_main_l1"]
    assert late
    assert all(
        datetime.fromisoformat(m["received_at"]) >
        datetime.fromisoformat(m["state_timestamp"])
        for m in late
    )


def test_reader_clock_skew_shifts_state_timestamp_only() -> None:
    """Clock skew moves the reported state time, not the physical sample time."""
    base_cfg = SimulatorConfig(seed=5, minutes=8, interval=10, quality=QualityLevel.EXCELLENT)
    skew_cfg = SimulatorConfig(seed=5, minutes=8, interval=10, quality=QualityLevel.EXCELLENT,
                               reader_clock_skew_s={"ct_main_l1": 30.0})
    base = [m for r in HouseholdScenario(base_cfg).to_list() for m in r["measurements"].values()
            if m["ct_id"] == "ct_main_l1"]
    skewed = [m for r in HouseholdScenario(skew_cfg).to_list() for m in r["measurements"].values()
              if m["ct_id"] == "ct_main_l1"]
    assert len(base) == len(skewed) and base
    # Same physical sample instants (and therefore the same values).
    assert [m["value_w"] for m in base] == [m["value_w"] for m in skewed]
    for b, s in zip(base, skewed, strict=True):
        state_delta = (datetime.fromisoformat(s["state_timestamp"]) -
                       datetime.fromisoformat(b["state_timestamp"])).total_seconds()
        assert state_delta == 30.0
        # The physical sample time is untouched by the skew.
        assert b["sample_time_s"] == s["sample_time_s"]
