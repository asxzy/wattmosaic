"""`LabHouse`: documented, seeded builders for every scenario stratum.

A stratum is a *reusable household situation* a test can ask for by name:
"give me the two-loads-one-breaker case", "give me the ×2 factor case", "give me
a CT whose reading flips sign mid-run".  Each builder returns a :class:`LabHouse`
which exposes exactly three things:

``scenario``
    the underlying :class:`~tools.simulator.scenario.HouseholdScenario`;
``observations()``
    the observable frame only (see :mod:`tools.simulator.frames`);
``ground_truth()``
    the physical frame, for assertions.

Algorithms must be given ``observations()`` and nothing else.  The seam is
enforced by key inspection in ``tests/unit/test_simulator_fixtures.py``, not by
convention.

Household shape
---------------
Every stratum is built on the reference installation, so no stratum quietly
invents a house shape that no other test sees:

* Panel A (main) and Panel B (a *nested* sub-feed of Panel A).
* 18 CTs per panel: CT1/CT2 mains plus 16 named branch channels, i.e. **36
  channels** total — the "third feed group" shape of the reference install.
  ``channel_counts()`` reports the split so a test can assert 16 branch CTs per
  panel rather than hard-code a total.
* L1/L2 split-phase, 120 V branch loads and 240 V double-pole loads.
* Dedicated and shared circuits, standby loads, and unmetered ("unknown") loads
  that can only ever appear as residual.

Determinism
-----------
Every builder takes ``seed`` and passes it straight to the simulator, whose
ground truth is a pure function of it.  Two calls with the same name and seed
produce byte-identical records; ``LabHouse.signature()`` is the SHA-256 of the
observation stream, so a test can pin it cheaply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from wattmosaic_engine.contracts import Phase, Polarity

from .factors import FactorOwner, FactorTimeline, RefossFactorChannel
from .frames import observation_stream, truth_stream
from .scenario import HouseholdScenario, SimulatorConfig
from .sensors import QualityLevel
from .topology import (
    CTType,
    HouseholdTopology,
    SimChannel,
    SimDevice,
    SimDeviceType,
    build_real_home_topology,
)

DEFAULT_SEED = 42
DEFAULT_MINUTES = 60.0
DEFAULT_INTERVAL = 15.0


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StratumPlan:
    """What a stratum promises to contain, and what a test may assert.

    The plan is data so it can be checked: :func:`validate_strata` builds every
    stratum and fails loudly if a device, channel or behaviour named here is
    missing from the built house.
    """

    name: str
    description: str
    required_devices: tuple[str, ...] = ()
    required_channels: tuple[str, ...] = ()
    required_flags: tuple[str, ...] = ()
    expectations: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "required_devices": list(self.required_devices),
            "required_channels": list(self.required_channels),
            "required_flags": list(self.required_flags),
            "expectations": dict(self.expectations),
        }


# --------------------------------------------------------------------------- #
# LabHouse
# --------------------------------------------------------------------------- #


@dataclass
class LabHouse:
    """One built stratum: a scenario plus its two separated frames."""

    plan: StratumPlan
    scenario: HouseholdScenario
    seed: int
    notes: list[str] = field(default_factory=list)

    # -- frames ------------------------------------------------------------ #
    def records(self) -> Iterator[dict[str, Any]]:
        return self.scenario.records()

    def to_list(self) -> list[dict[str, Any]]:
        return list(self.scenario.records())

    def observations(self, records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """The observable frame for every record.  No truth keys survive."""
        return observation_stream(records if records is not None else self.records())

    def ground_truth(self, records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """The physical frame for every record.  Never passed to an algorithm."""
        return truth_stream(records if records is not None else self.records())

    # -- shape ------------------------------------------------------------- #
    @property
    def topology(self) -> HouseholdTopology:
        return self.scenario.topology

    def device_ids(self) -> list[str]:
        return [d.device_id for d in self.scenario.devices]

    def channel_ids(self) -> list[str]:
        return [c.ct_id for c in self.topology.channels]

    def flags(self) -> set[str]:
        """Machine-readable statements about a built stratum."""
        flags: set[str] = set()
        cfg = self.scenario.config
        if self.plan.name:
            flags.add(f"stratum:{self.plan.name}")
        if cfg.factor_channels:
            flags.add("has_factor_channels")
        if cfg.inverted_channels:
            flags.add("has_inverted_channels")
        if cfg.polarity_flips:
            flags.add("has_polarity_flip")
        if cfg.missing_windows:
            flags.add("has_missing_intervals")
        if cfg.spike_windows:
            flags.add("has_spikes")
        if cfg.channel_delay_s:
            flags.add("has_delayed_ct")
        if cfg.channel_intervals:
            flags.add("has_irregular_sampling")
        if cfg.state_lag_s:
            flags.add("has_state_lag")
        if cfg.outage_windows:
            flags.add("has_outages")
        if cfg.quality is not QualityLevel.GOOD:
            flags.add(f"quality:{cfg.quality.value}")
        if count_unknown := sum(
            1 for d in self.scenario.devices if d.sim_type is SimDeviceType.UNKNOWN
        ):
            flags.add(f"unknown_loads:{count_unknown}")
        if self.has_nested_subpanel():
            flags.add("nested_subpanel")
        return flags

    def has_nested_subpanel(self) -> bool:
        panels = {c.ct_id: c.panel for c in self.topology.channels}
        return any(
            c.parent_ct_id is not None and panels.get(c.parent_ct_id) != c.panel
            for c in self.topology.channels
        )

    def channel_counts(self) -> dict[str, int]:
        """Channels per panel, and the *branch* count behind each panel's feed.

        "Branch" is structural, not a name or type: a channel is a panel's own
        branch when its parent is in the same panel.  That makes the count
        correct for the sub panel too, whose CT1/CT2 are typed ``branch`` (they
        are a nested feed) and whose 16 children hang off them.
        """
        panels = {c.ct_id: c.panel for c in self.topology.channels}
        counts: dict[str, int] = {}
        branch: dict[str, int] = {}
        for channel in self.topology.channels:
            panel = channel.panel
            counts[panel] = counts.get(panel, 0) + 1
            if (
                channel.parent_ct_id is not None
                and panels.get(channel.parent_ct_id) == panel
            ):
                branch[panel] = branch.get(panel, 0) + 1
        out = dict(counts)
        out["total"] = sum(counts.values())
        for panel in counts:
            out[f"{panel}_branch"] = branch.get(panel, 0)
        return out

    def config_metadata(self) -> dict[str, Any]:
        meta = self.scenario.config.as_metadata()
        meta["stratum"] = self.plan.name
        meta["stratum_notes"] = list(self.notes)
        return meta

    # -- determinism ------------------------------------------------------- #
    def signature(self, records: list[dict[str, Any]] | None = None) -> str:
        """SHA-256 over the observation stream.  Equal seeds ⇒ equal digest."""
        payload = self.observations(records)
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "stratum": self.plan.name,
            "seed": self.seed,
            "devices": len(self.scenario.devices),
            "channels": len(self.topology.channels),
            "channel_counts": self.channel_counts(),
            "flags": sorted(self.flags()),
            "expectations": dict(self.plan.expectations),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# Topology helpers
# --------------------------------------------------------------------------- #


def base_topology() -> HouseholdTopology:
    """A fresh copy of the reference two-panel topology."""
    return build_real_home_topology()


def _find_device(topology: HouseholdTopology, device_id: str) -> SimDevice:
    for device in topology.devices:
        if device.device_id == device_id:
            return device
    raise KeyError(f"unknown device {device_id!r}")


def _set_circuits(
    topology: HouseholdTopology, device_id: str, circuits: dict[str, float]
) -> None:
    device = _find_device(topology, device_id)
    device.circuits = dict(circuits)


def _replace_device(topology: HouseholdTopology, device: SimDevice) -> None:
    for index, existing in enumerate(topology.devices):
        if existing.device_id == device.device_id:
            topology.devices[index] = device
            return
    topology.devices.append(device)


def _add_channel(topology: HouseholdTopology, channel: SimChannel) -> None:
    topology.channels.append(channel)


def _reindex(topology: HouseholdTopology) -> HouseholdTopology:
    """Recompute nesting depth and re-assign structural seed keys."""
    cmap = {c.ct_id: c for c in topology.channels}
    for channel in topology.channels:
        depth = 0
        cursor = channel.parent_ct_id
        while cursor:
            depth += 1
            if cursor not in cmap:  # pragma: no cover - defensive
                raise ValueError(f"channel {channel.ct_id} has unknown parent {cursor}")
            cursor = cmap[cursor].parent_ct_id
        channel._depth = depth
    for device in topology.devices:
        device.seed_key = ""
    for channel in topology.channels:
        channel.seed_key = ""
    topology.assign_seed_keys()
    return topology


def _factor2_topology() -> tuple[HouseholdTopology, dict[str, RefossFactorChannel]]:
    """One leg of the 240 V tub metered, ×2 declared by the user.

    This is the *end-to-end* Refoss shape: the conductor carries half the load,
    the monitor's declared factor is ×2, and the published value is a two-leg
    estimate.  The appliance stays a 240 V double-pole device in the topology.
    """
    topology = base_topology()
    device = _find_device(topology, "water_heater")
    # Only the L1 conductor is physically metered.
    device.circuits = {"ct_main_water_heater_ct1": 1.0}
    device.metadata = {**device.metadata, "simulated_factor": "x2", "metered_leg": "l1"}
    channel = topology.channel_map()["ct_main_water_heater_ct1"]
    channel.paired_ct_id = None
    channel.ct_type = CTType.DEDICATED_DEVICE
    # Drop the unmetered second leg so no phantom channel can be read.
    topology.channels = [
        c for c in topology.channels if c.ct_id != "ct_main_water_heater_ct2"
    ]
    devices = {
        "ct_main_water_heater_ct1": RefossFactorChannel(
            ct_id="ct_main_water_heater_ct1",
            device_id="water_heater",
            metered_leg=Phase.L1,
            timeline=FactorTimeline.constant(
                2.0,
                applied_by=FactorOwner.USER,
                published=False,
                note="Refoss publishes one leg; user declares x2",
            ),
            leg_share=0.5,
            note="only the L1 conductor is metered",
        )
    }
    return _reindex(topology), devices


# --------------------------------------------------------------------------- #
# Stratum builders
# --------------------------------------------------------------------------- #


def _cfg(**kwargs: Any) -> SimulatorConfig:
    params: dict[str, Any] = {
        "seed": kwargs.pop("seed", DEFAULT_SEED),
        "minutes": kwargs.pop("minutes", DEFAULT_MINUTES),
        "interval": kwargs.pop("interval", DEFAULT_INTERVAL),
    }
    params.update(kwargs)
    return SimulatorConfig(**params)


def _dedicated(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="dedicated",
        description=(
            "Named loads each metered by their own CT: the vehicle charger, the "
            "water heater pair, the underfloor loop. One device per CT."
        ),
        required_devices=("vehicle_charger", "water_heater", "underfloor_loop"),
        required_channels=("ct_main_vehicle_charger",),
        required_flags=("stratum:dedicated",),
        expectations={
            "dedicated": True,
            "summing": "each dedicated CT is one device; summing devices is valid",
        },
    )
    topology = base_topology()
    keep = {
        "vehicle_charger", "water_heater", "underfloor_loop", "unknown_load_l1",
        "unknown_load_l2",
    }
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _shared(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="shared",
        description=(
            "Two utility loads on one breaker. Both must be attributable from the "
            "shared CT step, and neither may be given the other's power."
        ),
        required_devices=("utility_1", "utility_2"),
        required_channels=("ct_main_utility_plug_ct1",),
        expectations={"shared": True, "min_devices_on_one_ct": 2},
    )
    topology = base_topology()
    keep = {"utility_1", "utility_2", "utility_3", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _standby(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="standby",
        description=(
            "Always-on loads with a small baseline and rare active windows: the "
            "hot water tank recirculator and the network cabinet."
        ),
        required_devices=("hot_water_tank", "network_cabinet"),
        expectations={"standby_present": True, "never_reported_as_zero": True},
    )
    topology = base_topology()
    keep = {"hot_water_tank", "network_cabinet", "monitor", "unknown_load_l2"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _unknown_load(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="unknown_load",
        description=(
            "Loads attached directly to a panel main with no branch CT. They can "
            "only ever appear as residual power, never as a device."
        ),
        required_devices=("unknown_load_l1", "unknown_load_l2"),
        expectations={"unmetered": True, "must_stay_residual": True},
    )
    topology = base_topology()
    keep = {"unknown_load_l1", "unknown_load_l2", "lighting_1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _dynamic(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="dynamic",
        description=(
            "Continuously varying loads: dimmable lighting on a brightness curve "
            "and a variable-speed humidifier. Direct measurement must follow the "
            "value rather than a learned constant."
        ),
        required_devices=("lighting_1", "humidifier_1"),
        expectations={"dynamic": True, "track_value_not_constant": True},
    )
    topology = base_topology()
    keep = {"lighting_1", "lighting_2", "humidifier_1", "network_cabinet", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _factor2(seed: int, minutes: float, interval: float) -> LabHouse:
    topology, factors = _factor2_topology()
    plan = StratumPlan(
        name="factor2",
        description=(
            "Refoss end-to-end shape: a 240 V appliance with only one leg "
            "metered, and a user-declared x2 factor on that CT. The conductor "
            "reading is half the appliance; the published value is the whole "
            "load estimate."
        ),
        required_devices=("water_heater",),
        required_channels=("ct_main_water_heater_ct1",),
        required_flags=("has_factor_channels",),
        expectations={
            "factor": 2.0,
            "applied_by": "user",
            "metered_leg": "l1",
            "leg_share": 0.5,
            "double_count_risk": "the other leg must not be added",
        },
    )
    keep = {"water_heater", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            factor_channels=dict(factors),
        ),
        topology=_reindex(topology),
    ), seed=seed, notes=["ct_main_water_heater_ct2 does not exist: only L1 is metered"])


def _noisy_legs(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="noisy_legs",
        description=(
            "Both legs of a 240 V load are metered but the readings are noisy. "
            "The leg SUM must stay within tolerance of the device power; noise "
            "must not be mistaken for asymmetry."
        ),
        required_devices=("water_heater",),
        required_channels=("ct_main_water_heater_ct1", "ct_main_water_heater_ct2"),
        required_flags=("quality:degraded",),
        expectations={"leg_sum_is_device_power": True, "noise_not_asymmetry": True},
    )
    topology = base_topology()
    keep = {"water_heater", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            quality=QualityLevel.DEGRADED,
            channel_quality={
                "ct_main_water_heater_ct1": QualityLevel.POOR,
                "ct_main_water_heater_ct2": QualityLevel.POOR,
            },
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _unbalanced(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="unbalanced",
        description=(
            "A 240 V load whose physical legs are genuinely asymmetric (70/30). "
            "The two leg CTs must still be summed; the declared phase split must "
            "not be used to 'correct' two real readings."
        ),
        required_devices=("water_heater",),
        required_channels=("ct_main_water_heater_ct1", "ct_main_water_heater_ct2"),
        expectations={"physical_split_l1": 0.7, "declared_split_ignored": True},
    )
    topology = base_topology()
    _set_circuits(
        topology,
        "water_heater",
        {"ct_main_water_heater_ct1": 0.7, "ct_main_water_heater_ct2": 0.3},
    )
    keep = {"water_heater", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _automation_burst(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="automation_burst",
        description=(
            "Many loads switch at the same instant because one automation runs "
            "them. The aggregate step must not be attributed to a single device. "
            "Short horizon, dense simultaneous batches."
        ),
        required_flags=("stratum:automation_burst",),
        expectations={"simultaneous_events": True, "min_group_size": 2},
    )
    topology = base_topology()
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            simultaneous_event_batches=12,
        ),
        topology=topology,
    ), seed=seed)


def _ambiguous_pair(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="ambiguous_pair",
        description=(
            "Two devices with the same nominal power on one CT that always switch "
            "together. The correct result is AMBIGUOUS/UNKNOWN, never two "
            "independent estimates."
        ),
        required_devices=("amb_a", "amb_b"),
        required_channels=("ct_main_utility_plug_ct1",),
        expectations={"ambiguous": True, "must_not_split": True},
    )
    topology = base_topology()
    twin = SimDevice(
        device_id="amb_a",
        name="Ambiguous Load A",
        sim_type=SimDeviceType.STEP,
        phase=Phase.L1,
        circuits={"ct_main_utility_plug_ct1": 1.0},
        nominal_power_w=900.0,
        ha_entity_id="switch.amb_a",
        metadata={
            "steps": [(0.0, 0.0), (300.0, 900.0), (1800.0, 0.0)],
            "shared_branch": "ct_main_utility_plug_ct1",
        },
    )
    twin_b = SimDevice(
        device_id="amb_b",
        name="Ambiguous Load B",
        sim_type=SimDeviceType.STEP,
        phase=Phase.L1,
        circuits={"ct_main_utility_plug_ct1": 1.0},
        nominal_power_w=900.0,
        ha_entity_id="switch.amb_b",
        metadata={
            "steps": [(0.0, 0.0), (300.0, 900.0), (1800.0, 0.0)],
            "shared_branch": "ct_main_utility_plug_ct1",
        },
    )
    topology.devices = [
        d for d in topology.devices
        if d.device_id in {"unknown_load_l1", "lighting_1"}
    ]
    topology.devices.extend([twin, twin_b])
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=max(minutes, 40.0), interval=interval,
            simultaneous_event_batches=0,
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _hvac_modulation(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="hvac_modulation",
        description=(
            "A thermostat-driven compressor: duty-cycle modulation plus periodic "
            "defrost spikes. Power must be tracked per sample, not averaged into "
            "a constant."
        ),
        required_devices=("air_conditioner", "central_hvac"),
        expectations={"modulation": True, "track_per_sample": True},
    )
    topology = base_topology()
    keep = {"air_conditioner", "central_hvac", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _ev_rate_change(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="ev_rate_change",
        description=(
            "The EV charger steps from 3.6 kW to 11 kW and back. The dedicated "
            "channel must follow the rate immediately; no device run may split."
        ),
        required_devices=("vehicle_charger",),
        required_channels=("ct_main_vehicle_charger",),
        expectations={"rate_steps_w": [3600.0, 11000.0], "follows_rate": True},
    )
    topology = base_topology()
    device = _find_device(topology, "vehicle_charger")
    _replace_device(topology, SimDevice(
        device_id=device.device_id,
        name=device.name,
        sim_type=SimDeviceType.STEP,
        phase=device.phase,
        circuits=dict(device.circuits),
        nominal_power_w=11000.0,
        ha_entity_id=device.ha_entity_id,
        dedicated_ct=device.dedicated_ct,
        double_pole=device.double_pole,
        metadata={
            "steps": [
                (0.0, 0.0),
                (300.0, 3600.0),
                (1500.0, 11000.0),
                (2400.0, 3600.0),
                (3000.0, 0.0),
            ],
            "standby_w": 5.0,
        },
    ))
    keep = {"vehicle_charger", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _fridge_cycling(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="fridge_cycling",
        description=(
            "Compressor duty cycle with a periodic defrost heater. The cycling is "
            "physical load, not a device event stream driving learning samples."
        ),
        required_devices=("refrigerator_1", "refrigerator_2"),
        expectations={"duty_cycle": True, "defrost_spike": True},
    )
    topology = base_topology()
    keep = {"refrigerator_1", "refrigerator_2", "unknown_load_l2"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _motor_startup(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="motor_startup",
        description=(
            "A pump with a short high inrush that decays to a steady running "
            "load. The steady state must be learned; the inrush must not inflate "
            "the model or the energy integral."
        ),
        required_devices=("pump",),
        required_channels=("ct_main_pump",),
        expectations={"startup_w": 4000.0, "steady_w": 800.0, "startup_s": 4.0},
    )
    topology = base_topology()
    topology.channels.append(
        SimChannel(
            ct_id="ct_main_pump",
            entity_id="sensor.panel_a_pump_power",
            name="Panel A Pump",
            ct_type=CTType.DEDICATED_DEVICE,
            phase=Phase.L1,
            parent_ct_id="ct_main_l1",
            panel="main",
        )
    )
    topology.devices = [
        d for d in topology.devices if d.device_id in {"unknown_load_l1", "lighting_1"}
    ]
    topology.devices.append(
        SimDevice(
            device_id="pump",
            name="Well Pump",
            sim_type=SimDeviceType.MOTOR,
            phase=Phase.L1,
            circuits={"ct_main_pump": 1.0},
            nominal_power_w=800.0,
            ha_entity_id="switch.pump",
            dedicated_ct=True,
            metadata={"startup_w": 4000.0, "startup_s": 4.0, "standby_w": 0.0,
                      "windows": [(300.0, 900.0), (3300.0, 3900.0)]},
        )
    )
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval), topology=_reindex(topology)
    ), seed=seed)


def _delayed_ct(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="delayed_ct",
        description=(
            "One CT's samples arrive seconds late while the device state flips on "
            "time. The lag must be handled by age/vintage, never by treating the "
            "late value as fresh."
        ),
        required_channels=("ct_sub_office_outlet",),
        required_flags=("has_delayed_ct",),
        expectations={"delay_s": 20.0, "state_not_delayed": True},
    )
    topology = base_topology()
    keep = {"washing_machine", "dishwasher", "tv", "unknown_load_l2"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            channel_delay_s={"ct_sub_office_outlet": 20.0},
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _irregular_sampling(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="irregular_sampling",
        description=(
            "Per-channel cadences that do not divide the record interval: 5 s, "
            "15.7 s, 30 s, 60 s. Windows must be sized from observed cadence."
        ),
        required_flags=("has_irregular_sampling",),
        expectations={"cadences_s": [5.0, 15.7, 30.0, 60.0]},
    )
    topology = base_topology()
    keep = {"lighting_1", "lighting_2", "lighting_3", "network_cabinet", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            channel_intervals={
                "ct_main_lighting_ct1": 5.0,
                "ct_main_lighting_ct2": 15.7,
                "ct_main_lighting_ct3": 30.0,
                "ct_sub_network_cabinet": 60.0,
            },
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _missing_intervals(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="missing_intervals",
        description=(
            "One channel goes silent for several minutes: no sample at all, as "
            "opposed to a stale repeat. A gap must accrue no energy and must not "
            "be read as 0 W."
        ),
        required_channels=("ct_main_lighting_ct1",),
        required_flags=("has_missing_intervals",),
        expectations={"gap_s": [900.0, 1500.0], "no_energy_across_gap": True},
    )
    topology = base_topology()
    keep = {"lighting_1", "lighting_2", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            missing_windows={"ct_main_lighting_ct1": [(900.0, 1500.0)]},
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _spikes(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="spikes",
        description=(
            "Measurement-only impulses on a channel: the reported value jumps for "
            "a few seconds while ground truth does not move. A spike must not be "
            "learned, attributed, or charged as device energy."
        ),
        required_channels=("ct_main_air_conditioner",),
        required_flags=("has_spikes",),
        expectations={"spike_w": 6000.0, "truth_unaffected": True},
    )
    topology = base_topology()
    _set_circuits(topology, "air_conditioner", {"ct_main_air_conditioner": 1.0})
    keep = {"air_conditioner", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            spike_windows={
                "ct_main_air_conditioner": [(600.0, 615.0, 6000.0), (1800.0, 1815.0, 6000.0)]
            },
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _main_subpanel_overlap(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="main_subpanel_overlap",
        description=(
            "The full reference installation: Panel B is a nested sub-feed of "
            "Panel A, so its mains are contained in Panel A's. Summing every main "
            "double counts the sub-panel."
        ),
        required_channels=("ct_main_l1", "ct_main_l2", "ct_sub_l1", "ct_sub_l2"),
        required_flags=("nested_subpanel",),
        expectations={"nested": True, "do_not_sum_all_mains": True},
    )
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(seed=seed, minutes=minutes, interval=interval),
        topology=base_topology(),
    ), seed=seed)


def _reversed_orientation(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="reversed_orientation",
        description=(
            "A CT is re-clamped the wrong way round mid-run: the reading flips "
            "sign while the declared polarity stays normal. That must be flagged "
            "as an orientation change, not clipped to 0 W or read as export."
        ),
        required_channels=("ct_main_lighting_ct2",),
        required_flags=("has_polarity_flip",),
        expectations={"flip_at_s": 1200.0, "declared_polarity": "normal"},
    )
    topology = base_topology()
    keep = {"lighting_1", "lighting_2", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            polarity_flips={"ct_main_lighting_ct2": 1200.0},
        ),
        topology=_reindex(topology),
    ), seed=seed)


def _negative_ct(seed: int, minutes: float, interval: float) -> LabHouse:
    plan = StratumPlan(
        name="negative_ct",
        description=(
            "A physically inverted CT for the whole run: negative readings with "
            "polarity declared inverted. The sign must be corrected once, and "
            "true export must stay negative."
        ),
        required_channels=("ct_sub_l1",),
        required_flags=("has_inverted_channels",),
        expectations={"inverted_channel": "ct_sub_l1", "sign_corrected_once": True},
    )
    topology = build_real_home_topology(inverted_channels=("ct_sub_l1",))
    channel = topology.channel_map()["ct_sub_l1"]
    assert channel.polarity is Polarity.INVERTED
    keep = {"refrigerator_1", "refrigerator_2", "unknown_load_l1"}
    topology.devices = [d for d in topology.devices if d.device_id in keep]
    return LabHouse(plan=plan, scenario=HouseholdScenario(
        _cfg(
            seed=seed, minutes=minutes, interval=interval,
            inverted_channels=("ct_sub_l1",),
        ),
        topology=_reindex(topology),
    ), seed=seed)


Builder = Callable[[int, float, float], LabHouse]

#: Every buildable stratum, keyed by the name the test matrix uses.
STRATA: dict[str, Builder] = {
    "dedicated": _dedicated,
    "shared": _shared,
    "standby": _standby,
    "unknown_load": _unknown_load,
    "dynamic": _dynamic,
    "factor2": _factor2,
    "noisy_legs": _noisy_legs,
    "unbalanced": _unbalanced,
    "automation_burst": _automation_burst,
    "ambiguous_pair": _ambiguous_pair,
    "hvac_modulation": _hvac_modulation,
    "ev_rate_change": _ev_rate_change,
    "fridge_cycling": _fridge_cycling,
    "motor_startup": _motor_startup,
    "delayed_ct": _delayed_ct,
    "irregular_sampling": _irregular_sampling,
    "missing_intervals": _missing_intervals,
    "spikes": _spikes,
    "main_subpanel_overlap": _main_subpanel_overlap,
    "reversed_orientation": _reversed_orientation,
    "negative_ct": _negative_ct,
}


def available_strata() -> tuple[str, ...]:
    """Every stratum name, sorted.  Missing names fail the coverage test."""
    return tuple(sorted(STRATA))


def build_scenario(
    name: str,
    seed: int = DEFAULT_SEED,
    *,
    minutes: float = DEFAULT_MINUTES,
    interval: float = DEFAULT_INTERVAL,
) -> LabHouse:
    """Build the named stratum.

    ``KeyError`` names the valid strata rather than silently substituting one:
    a typo would otherwise turn a coverage test into a no-op.
    """
    try:
        builder = STRATA[name]
    except KeyError:
        raise KeyError(
            f"unknown stratum {name!r}; available: {list(available_strata())}"
        ) from None
    return builder(seed, minutes, interval)


def validate_strata(
    seed: int = DEFAULT_SEED,
    *,
    minutes: float = 12.0,
    interval: float = 30.0,
) -> list[StratumPlan]:
    """Build every stratum and check its own plan is satisfied.

    Returns the plans of the strata that built.  Raises ``AssertionError``
    naming the first violation, so a builder that silently stops producing its
    defining feature fails loudly instead of passing vacuously.
    """
    plans: list[StratumPlan] = []
    for name in available_strata():
        house = build_scenario(name, seed, minutes=minutes, interval=interval)
        records = house.to_list()
        assert records, f"{name}: builder produced no records"
        device_ids = set(house.device_ids())
        channel_ids = set(house.channel_ids())
        flags = house.flags()

        missing_devices = [d for d in house.plan.required_devices if d not in device_ids]
        assert not missing_devices, (
            f"{name}: plan requires devices {missing_devices} absent from "
            f"the built house ({sorted(device_ids)})"
        )
        missing_channels = [c for c in house.plan.required_channels if c not in channel_ids]
        assert not missing_channels, (
            f"{name}: plan requires channels {missing_channels} absent from "
            f"the built house"
        )
        missing_flags = [f for f in house.plan.required_flags if f not in flags]
        assert not missing_flags, (
            f"{name}: plan requires flags {missing_flags}; got {sorted(flags)}"
        )
        plans.append(house.plan)
    return plans


__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_MINUTES",
    "DEFAULT_SEED",
    "LabHouse",
    "STRATA",
    "StratumPlan",
    "available_strata",
    "base_topology",
    "build_scenario",
    "validate_strata",
]
