"""Electrical topology of the simulated home.

The topology mirrors the *synthetic reference household* also published as
``data/examples/reference_house/`` — never a real installation:

* Two energy-meter panels: **Panel A** and **Panel B**.
* Each panel exposes CT1/CT2 as panel mains plus 16 named branch channels.
* The Panel B is a *sub-feed* of the Panel A.  Its mains are nested
  children of the Panel A mains, so naively adding every CT double counts.
* Several circuits are double-pole 240 V loads exposed as ``<Name> CT1`` /
  ``<Name> CT2`` pairs (Water Heater, Utility Plug, Deck Heater, Upper Plug).
  Each leg carries half the load.
* Branch circuits are named generically (Lighting 1-3, Server Rack, Heat
  Recovery Ventilator, ...).

This module deliberately separates:

* **channels** — CT measurement points and their parent/child nesting;
* **devices** — logical loads and the leaf CTs they contribute to;
* **unknown loads** — loads attached directly to a panel main with no
  branch CT, so they can only appear as residual power.

Attachment of *shared-branch* devices (washer, dryer, dishwasher, kitchen
appliances) is a modelling assumption, not an audit-confirmed fact; the real
audit only confirms the circuit names.  ``metadata["confidence"]`` records that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from wattmosaic_engine.contracts import CTType, Phase, Polarity


class SimDeviceType(str, Enum):
    BINARY = "binary"
    STANDBY = "standby"
    DIMMABLE = "dimmable"
    VARIABLE_SPEED = "variable_speed"
    HVAC = "hvac"
    REFRIGERATOR = "refrigerator"
    WASHING_MACHINE = "washing_machine"
    DISHWASHER = "dishwasher"
    DRYER = "dryer"
    DOUBLE_POLE = "double_pole"
    MOTOR = "motor"
    STEP = "step"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SimChannel:
    ct_id: str
    entity_id: str
    name: str
    ct_type: CTType
    phase: Phase
    parent_ct_id: str | None = None
    paired_ct_id: str | None = None
    panel: str = "main"
    polarity: Polarity = Polarity.NORMAL
    resolution_w: float = 0.001
    expected_interval_s: float = 15.7
    reliability: float = 1.0
    #: Position-independent identity of *this measurement point*, used to seed
    #: its simulated noise.  Assigned from declaration order by
    #: :meth:`HouseholdTopology.assign_seed_keys`.  Never a household label: a
    #: rename must not change a single measured value.
    seed_key: str = ""

    @property
    def depth(self) -> int:  # filled by HouseholdTopology
        return self._depth

    _depth: int = field(default=0, repr=False)


@dataclass(slots=True)
class SimDevice:
    device_id: str
    name: str
    sim_type: SimDeviceType
    phase: Phase
    circuits: dict[str, float]
    nominal_power_w: float
    ha_entity_id: str
    dedicated_ct: bool = False
    double_pole: bool = False
    metadata: dict = field(default_factory=dict)
    #: Identity used to seed this load's synthetic behaviour.  Assigned from
    #: declaration order, never from ``device_id``: renaming a load (for
    #: privacy, or a clearer label) must leave the simulated physics untouched.
    seed_key: str = ""


@dataclass(slots=True)
class HouseholdTopology:
    channels: list[SimChannel]
    devices: list[SimDevice]
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.assign_seed_keys()

    def assign_seed_keys(self) -> None:
        """Give every load and channel a stable, label-independent seed key.

        The simulator derives each load's waveform and each channel's noise from
        a seed key.  Using the *display* id for that made a rename (a privacy
        relabel, or a clearer name) silently re-realise the whole scenario, so
        every published accuracy number moved.  Deriving the key from
        declaration order instead keeps the physics pinned to structure: two
        topologies that differ only in labels produce identical measurements.

        Keys are assigned on every construction so a topology built by hand in a
        test behaves the same as the module-level one.
        """
        for index, device in enumerate(self.devices):
            if not device.seed_key:
                device.seed_key = f"load_{index:03d}"
        for index, channel in enumerate(self.channels):
            if not channel.seed_key:
                channel.seed_key = f"ct_{index:03d}"

    def channel_map(self) -> dict[str, SimChannel]:
        return {c.ct_id: c for c in self.channels}

    def device_map(self) -> dict[str, SimDevice]:
        return {d.device_id: d for d in self.devices}

    def leaf_channels(self) -> list[str]:
        parents = {c.parent_ct_id for c in self.channels if c.parent_ct_id}
        return [c.ct_id for c in self.channels if c.ct_id not in parents]

    def main_channel_ids(self) -> list[str]:
        return [c.ct_id for c in self.channels if c.ct_type == CTType.MAIN]

    def true_ct_powers(self, device_powers: dict[str, float]) -> dict[str, float]:
        """Aggregate ground-truth device power into every CT channel.

        Leaf channels receive ``coefficient * device_power``.  Parents receive
        the sum of their children.  A double-pole device contributes 0.5 to
        each leg, so the two legs sum to the device total and are never
        double counted.
        """
        out: dict[str, float] = {c.ct_id: 0.0 for c in self.channels}
        for dev in self.devices:
            p = float(device_powers.get(dev.device_id, 0.0))
            for ct_id, coeff in dev.circuits.items():
                out[ct_id] = out.get(ct_id, 0.0) + coeff * p
        # Propagate children upward; depth ensures children are complete first.
        by_depth = sorted(self.channels, key=lambda c: c._depth, reverse=True)
        for ch in by_depth:
            if ch.parent_ct_id:
                out[ch.parent_ct_id] = out.get(ch.parent_ct_id, 0.0) + out.get(ch.ct_id, 0.0)
        return out

    def panel_totals(self, ct_powers: dict[str, float]) -> dict[str, float]:
        """Root and nested-feed totals, derived from structure, not names.

        * ``main`` — channels with no parent (the whole-home feed legs).
        * ``sub``  — channels whose parent is in another panel (a nested feed).

        Both are sums over *every* matching channel, so adding, removing or
        renaming a panel does not need a code change here.
        """
        panels = {c.ct_id: c.panel for c in self.channels}
        roots = [c.ct_id for c in self.channels if c.parent_ct_id is None]
        nested = [
            c.ct_id
            for c in self.channels
            if c.parent_ct_id is not None
            and panels.get(c.parent_ct_id) != c.panel
        ]
        return {
            "main": round(sum(ct_powers.get(c, 0.0) for c in roots), 4),
            "sub": round(sum(ct_powers.get(c, 0.0) for c in nested), 4),
        }

    # ------------------------------------------------------------------ #
    # Contract bridge
    # ------------------------------------------------------------------ #
    def to_contract_topology(self, *, confirmed: bool = True):
        """Export this simulated topology as the authoritative contract.

        The result is a ``wattmosaic_engine.contracts.Topology`` so tests and
        the real engine consume exactly the same schema as production.

        Modelling decisions required by the contract's validation rules:

        * A **dedicated** CT is typed ``dedicated_device`` and carries the
          owning ``device_id``; it is listed in ``dedicated_ct_ids`` only.
        * A sub-panel feed is a ``branch`` CT whose parent is the main-panel
          line.  Nested ``main`` CTs are illegal because mains are summed.
        * 240 V loads are ``double_pole=True`` with ``phase=both``.
        * Unmetered ("unknown") loads have no CT and are **excluded** from the
          device list: they can only ever be residual power, so giving them a
          device row would invite fabricated attribution.
        """
        from wattmosaic_engine.contracts import (
            CTChannel,
            DeviceTopology,
            Topology,
            TopologyStatus,
        )

        status = TopologyStatus.CONFIRMED if confirmed else TopologyStatus.CANDIDATE
        devices = [d for d in self.devices if d.metadata.get("attributable", True)]
        owner_of: dict[str, str] = {}
        for dev in devices:
            if dev.dedicated_ct:
                for ct_id in dev.circuits:
                    owner_of[ct_id] = dev.device_id

        channels = [
            CTChannel(
                ct_id=c.ct_id,
                entity_id=c.entity_id,
                name=c.name,
                ct_type=c.ct_type,
                phase=c.phase,
                parent_ct_id=c.parent_ct_id,
                polarity=c.polarity,
                device_id=owner_of.get(c.ct_id),
                paired_ct_id=c.paired_ct_id,
                resolution_w=c.resolution_w,
                expected_interval_s=c.expected_interval_s,
                reliability=c.reliability,
                topology_status=status,
                metadata={"panel": c.panel, "simulated": True},
            )
            for c in self.channels
        ]
        device_rows = [
            DeviceTopology(
                device_id=d.device_id,
                name=d.name,
                dedicated_ct_ids=sorted(d.circuits) if d.dedicated_ct else [],
                branch_ct_ids=[] if d.dedicated_ct else sorted(d.circuits),
                phase=d.phase,
                double_pole=d.double_pole,
                topology_status=status,
                metadata={
                    **d.metadata,
                    "simulated": True,
                    "circuits": dict(d.circuits),
                    "device_type": d.sim_type.value,
                    "nominal_power_w": d.nominal_power_w,
                },
            )
            for d in devices
        ]
        return Topology(
            version=1,
            status=status,
            channels=channels,
            devices=device_rows,
            notes=list(self.notes),
        )


def _ch(
    ct_id: str,
    entity_id: str,
    name: str,
    ct_type: CTType,
    phase: Phase,
    parent: str | None = None,
    *,
    panel: str = "main",
    paired: str | None = None,
    polarity: Polarity = Polarity.NORMAL,
    resolution: float = 0.001,
    interval: float = 15.7,
) -> SimChannel:
    return SimChannel(
        ct_id=ct_id,
        entity_id=entity_id,
        name=name,
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        paired_ct_id=paired,
        panel=panel,
        polarity=polarity,
        resolution_w=resolution,
        expected_interval_s=interval,
    )


MP = "sensor.panel_a_"
SP = "sensor.panel_b_"


def build_real_home_topology(*, inverted_channels: tuple[str, ...] = ()) -> HouseholdTopology:
    """Build the audited two-panel topology.

    ``inverted_channels`` lets a scenario mark physically reversed CTs.  The
    channel's ``polarity`` is then ``inverted`` and its readings are negative.
    """
    inv = set(inverted_channels)
    channels: list[SimChannel] = [
        # --- Panel A -------------------------------------------------- #
        _ch("ct_main_l1", MP + "ct1_power", "Panel A CT1 (L1)", CTType.MAIN, Phase.L1),
        _ch("ct_main_l2", MP + "ct2_power", "Panel A CT2 (L2)", CTType.MAIN, Phase.L2),
        _ch(
            "ct_main_underfloor_loop", MP + "underfloor_loop_power",
            "Panel A Underfloor Loop", CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1",
        ),
        _ch(
            "ct_main_vehicle_charger", MP + "vehicle_charger_power",
            "Panel A Vehicle Charger", CTType.DEDICATED_DEVICE, Phase.BOTH, "ct_main_l1",
        ),
        _ch(
            "ct_main_water_heater_ct1", MP + "water_heater_ct1_power",
            "Panel A Water Heater CT1", CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1",
            paired="ct_main_water_heater_ct2",
        ),
        _ch(
            "ct_main_water_heater_ct2", MP + "water_heater_ct2_power",
            "Panel A Water Heater CT2", CTType.DEDICATED_DEVICE, Phase.L2, "ct_main_l2",
            paired="ct_main_water_heater_ct1",
        ),
        _ch(
            "ct_main_utility_plug_ct1", MP + "utility_plug_ct1_power",
            "Panel A Kitchen Plug CT1", CTType.BRANCH, Phase.L1, "ct_main_l1",
            paired="ct_main_utility_plug_ct2",
        ),
        _ch(
            "ct_main_utility_plug_ct2", MP + "utility_plug_ct2_power",
            "Panel A Kitchen Plug CT2", CTType.BRANCH, Phase.L2, "ct_main_l2",
            paired="ct_main_utility_plug_ct1",
        ),
        _ch("ct_main_lighting_ct1", MP + "lighting_ct1_power", "Panel A Lighting Bank 1",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1"),
        _ch("ct_main_lighting_ct2", MP + "lighting_ct2_power", "Panel A Lighting Bank 2",
            CTType.DEDICATED_DEVICE, Phase.L2, "ct_main_l2"),
        _ch("ct_main_lighting_ct3", MP + "lighting_ct3_power", "Panel A Lighting Bank 3",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1"),
        _ch("ct_main_air_conditioner", MP + "air_conditioner_power", "Panel A Air Conditioner",
            CTType.DEDICATED_DEVICE, Phase.BOTH, "ct_main_l1"),
        _ch("ct_main_central_hvac", MP + "central_hvac_power", "Panel A Central HVAC",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1"),
        _ch("ct_main_space_heater", MP + "space_heater_power", "Panel A Space Heater", CTType.DEDICATED_DEVICE, Phase.BOTH, "ct_main_l2"),
        _ch(
            "ct_main_patio_heater_ct1", MP + "patio_heater_ct1_power",
            "Panel A Patio Heater CT1", CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1",
            paired="ct_main_patio_heater_ct2",
        ),
        _ch(
            "ct_main_patio_heater_ct2", MP + "patio_heater_ct2_power",
            "Panel A Patio Heater CT2", CTType.DEDICATED_DEVICE, Phase.L2, "ct_main_l2",
            paired="ct_main_patio_heater_ct1",
        ),
        _ch("ct_main_upper_ac", MP + "upper_ac_power", "Panel A Upper AC",
            CTType.DEDICATED_DEVICE, Phase.BOTH, "ct_main_l1"),
        _ch("ct_main_upper_central_hvac", MP + "upper_central_hvac_power",
            "Panel A Upper HVAC", CTType.DEDICATED_DEVICE, Phase.L1, "ct_main_l1"),

        # --- Panel B (sub-feed of the main panel) ---------------------- #
        _ch("ct_sub_l1", SP + "ct1_power", "Panel B CT1 (L1) [sub-feed]", CTType.BRANCH, Phase.L1,
            "ct_main_l1", panel="sub"),
        _ch("ct_sub_l2", SP + "ct2_power", "Panel B CT2 (L2) [sub-feed]", CTType.BRANCH, Phase.L2,
            "ct_main_l2", panel="sub"),
        _ch("ct_sub_refrigerator_1", SP + "refrigerator_1_power", "Panel B Refrigerator 1",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_refrigerator_2", SP + "refrigerator_2_power", "Panel B Refrigerator 2",
            CTType.DEDICATED_DEVICE, Phase.L2, "ct_sub_l2", panel="sub"),
        _ch("ct_sub_hot_water_tank", SP + "hot_water_tank_power", "Panel B Hot Water Tank",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_utility_sink", SP + "utility_sink_power", "Panel B Utility Sink",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_workshop_outlet", SP + "workshop_outlet_power", "Panel B Workshop Outlet",
            CTType.BRANCH, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_guest_bath_unit", SP + "guest_bath_unit_power", "Panel B Guest Bath Unit",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_humidifier_1", SP + "humidifier_1_power",
            "Panel B Humidifier 1", CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_office_outlet", SP + "office_outlet_power", "Panel B Office Outlet",
            CTType.BRANCH, Phase.L2, "ct_sub_l2", panel="sub"),
        _ch("ct_sub_garden_outlet", SP + "garden_outlet_power", "Panel B Garden Outlet",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_display_panel", SP + "display_panel_power", "Panel B Wall Display",
            CTType.DEDICATED_DEVICE, Phase.L2, "ct_sub_l2", panel="sub"),
        _ch("ct_sub_network_cabinet", SP + "network_cabinet_power", "Panel B Network Cabinet",
            CTType.DEDICATED_DEVICE, Phase.L2, "ct_sub_l2", panel="sub"),
        _ch("ct_sub_eave_outlet", SP + "eave_outlet_power", "Panel B Eave Outlet",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_ventilation_unit", SP + "ventilation_unit_power", "Panel B Ventilation Unit",
            CTType.DEDICATED_DEVICE, Phase.L1, "ct_sub_l1", panel="sub"),
        _ch("ct_sub_humidifier_2", SP + "humidifier_2_power",
            "Panel B Humidifier 2", CTType.DEDICATED_DEVICE, Phase.L2, "ct_sub_l2", panel="sub"),
        _ch(
            "ct_sub_upper_plug_ct1", SP + "upper_plug_ct1_power",
            "Panel B Upper Floor Plug CT1", CTType.BRANCH, Phase.L1, "ct_sub_l1",
            panel="sub", paired="ct_sub_upper_plug_ct2",
        ),
        _ch(
            "ct_sub_upper_plug_ct2", SP + "upper_plug_ct2_power",
            "Panel B Upper Floor Plug CT2", CTType.BRANCH, Phase.L2, "ct_sub_l2",
            panel="sub", paired="ct_sub_upper_plug_ct1",
        ),
    ]

    # Physical CT reversal: reading sign flips, polarity metadata records it.
    for ch in channels:
        if ch.ct_id in inv:
            ch.polarity = Polarity.INVERTED

    devices: list[SimDevice] = [
        # --- Main panel -------------------------------------------------- #
        SimDevice("underfloor_loop", "Underfloor Loop", SimDeviceType.BINARY,
                  Phase.L1, {"ct_main_underfloor_loop": 1.0}, 1500.0,
                  "switch.underfloor_loop", dedicated_ct=True),
        SimDevice("vehicle_charger", "Vehicle Charger", SimDeviceType.VARIABLE_SPEED,
                  Phase.BOTH, {"ct_main_vehicle_charger": 1.0}, 7200.0, "sensor.vehicle_charger_power",
                  dedicated_ct=True, double_pole=True),
        SimDevice("water_heater", "Water Heater", SimDeviceType.DOUBLE_POLE, Phase.BOTH,
                  {"ct_main_water_heater_ct1": 0.5, "ct_main_water_heater_ct2": 0.5}, 6000.0,
                  "climate.water_heater", dedicated_ct=True, double_pole=True,
                  metadata={"pair": ["ct_main_water_heater_ct1", "ct_main_water_heater_ct2"]}),
        SimDevice("utility_1", "Utility Load 1", SimDeviceType.BINARY,
                  Phase.L1, {"ct_main_utility_plug_ct1": 1.0}, 900.0,
                  "switch.utility_1",
                  metadata={"shared_branch": "ct_main_utility_plug_ct1", "confidence": "assumed"}),
        SimDevice("utility_2", "Utility Load 2", SimDeviceType.BINARY,
                  Phase.L1, {"ct_main_utility_plug_ct1": 1.0}, 1500.0,
                  "switch.utility_2",
                  metadata={"shared_branch": "ct_main_utility_plug_ct1", "confidence": "assumed"}),
        SimDevice("utility_3", "Utility Load 3", SimDeviceType.BINARY,
                  Phase.L2, {"ct_main_utility_plug_ct2": 1.0}, 800.0,
                  "switch.utility_3",
                  metadata={"shared_branch": "ct_main_utility_plug_ct2", "confidence": "assumed"}),
        SimDevice("utility_4", "Utility Load 4", SimDeviceType.BINARY,
                  Phase.L2, {"ct_main_utility_plug_ct2": 1.0}, 1200.0,
                  "switch.utility_4",
                  metadata={"shared_branch": "ct_main_utility_plug_ct2", "confidence": "assumed"}),
        SimDevice("lighting_1", "Lighting Circuit 1", SimDeviceType.DIMMABLE,
                  Phase.L1, {"ct_main_lighting_ct1": 1.0}, 240.0,
                  "light.lighting_1", dedicated_ct=True),
        SimDevice("lighting_2", "Lighting Circuit 2", SimDeviceType.DIMMABLE,
                  Phase.L2, {"ct_main_lighting_ct2": 1.0}, 480.0,
                  "light.lighting_2", dedicated_ct=True),
        SimDevice("lighting_3", "Lighting Circuit 3", SimDeviceType.DIMMABLE,
                  Phase.L1, {"ct_main_lighting_ct3": 1.0}, 180.0,
                  "light.lighting_3", dedicated_ct=True),
        SimDevice("air_conditioner", "Air Conditioner", SimDeviceType.HVAC, Phase.BOTH,
                  {"ct_main_air_conditioner": 1.0}, 3500.0, "climate.air_conditioner",
                  dedicated_ct=True, double_pole=True),
        SimDevice("central_hvac", "Central HVAC", SimDeviceType.HVAC, Phase.L1,
                  {"ct_main_central_hvac": 1.0}, 900.0, "climate.central_hvac",
                  dedicated_ct=True),
        SimDevice("space_heater", "Space Heater", SimDeviceType.BINARY, Phase.BOTH,
                  {"ct_main_space_heater": 1.0}, 4500.0, "switch.space_heater",
                  dedicated_ct=True, double_pole=True),
        SimDevice("patio_heater", "Patio Heater", SimDeviceType.DOUBLE_POLE,
                  Phase.BOTH, {"ct_main_patio_heater_ct1": 0.5,
                               "ct_main_patio_heater_ct2": 0.5}, 3000.0,
                  "climate.patio_heater", dedicated_ct=True, double_pole=True,
                  metadata={"pair": ["ct_main_patio_heater_ct1", "ct_main_patio_heater_ct2"]}),
        SimDevice("upper_ac", "Upper AC", SimDeviceType.HVAC, Phase.BOTH,
                  {"ct_main_upper_ac": 1.0}, 3200.0, "climate.upper_ac",
                  dedicated_ct=True, double_pole=True),
        SimDevice("upper_central_hvac", "Upper Central HVAC", SimDeviceType.HVAC, Phase.L1,
                  {"ct_main_upper_central_hvac": 1.0}, 800.0, "climate.upper_central_hvac",
                  dedicated_ct=True),

        # --- Sub panel --------------------------------------------------- #
        SimDevice("refrigerator_1", "Refrigerator 1", SimDeviceType.REFRIGERATOR, Phase.L1,
                  {"ct_sub_refrigerator_1": 1.0}, 120.0, "sensor.refrigerator_1_power",
                  dedicated_ct=True),
        SimDevice("refrigerator_2", "Refrigerator 2", SimDeviceType.REFRIGERATOR, Phase.L2,
                  {"ct_sub_refrigerator_2": 1.0}, 120.0, "sensor.refrigerator_2_power",
                  dedicated_ct=True),
        SimDevice("hot_water_tank", "Hot Water Tank", SimDeviceType.STANDBY, Phase.L1,
                  {"ct_sub_hot_water_tank": 1.0}, 250.0, "sensor.hot_water_tank_power", dedicated_ct=True),
        SimDevice("utility_sink", "Utility Sink", SimDeviceType.BINARY, Phase.L1,
                  {"ct_sub_utility_sink": 1.0}, 900.0, "switch.utility_sink",
                  dedicated_ct=True),
        SimDevice("dryer", "Dryer", SimDeviceType.DRYER, Phase.L1,
                  {"ct_sub_workshop_outlet": 1.0}, 5200.0, "sensor.dryer",
                  metadata={"shared_branch": "ct_sub_workshop_outlet", "confidence": "assumed"}),
        SimDevice("freezer", "Freezer", SimDeviceType.REFRIGERATOR, Phase.L1,
                  {"ct_sub_workshop_outlet": 1.0}, 90.0, "sensor.freezer",
                  metadata={"shared_branch": "ct_sub_workshop_outlet", "confidence": "assumed"}),
        SimDevice("guest_bath_unit", "Guest Bath Unit", SimDeviceType.BINARY, Phase.L1,
                  {"ct_sub_guest_bath_unit": 1.0}, 800.0, "switch.guest_bath_unit",
                  dedicated_ct=True),
        SimDevice("humidifier_1", "Humidifier 1", SimDeviceType.VARIABLE_SPEED,
                  Phase.L1, {"ct_sub_humidifier_1": 1.0}, 120.0,
                  "sensor.humidifier_1", dedicated_ct=True),
        SimDevice("washing_machine", "Washing Machine", SimDeviceType.WASHING_MACHINE, Phase.L2,
                  {"ct_sub_office_outlet": 1.0}, 2200.0, "sensor.washing_machine",
                  metadata={"shared_branch": "ct_sub_office_outlet", "confidence": "assumed"}),
        SimDevice("dishwasher", "Dishwasher", SimDeviceType.DISHWASHER, Phase.L2,
                  {"ct_sub_office_outlet": 1.0}, 1800.0, "sensor.dishwasher",
                  metadata={"shared_branch": "ct_sub_office_outlet", "confidence": "assumed"}),
        SimDevice("tv", "TV", SimDeviceType.BINARY, Phase.L2,
                  {"ct_sub_office_outlet": 1.0}, 130.0, "media_player.tv",
                  metadata={"shared_branch": "ct_sub_office_outlet", "confidence": "assumed"}),
        SimDevice("garden_outlet", "Garden Outlet", SimDeviceType.BINARY, Phase.L1,
                  {"ct_sub_garden_outlet": 1.0}, 500.0, "switch.garden_outlet",
                  dedicated_ct=True),
        SimDevice("wall_display_panel", "Wall Display", SimDeviceType.BINARY, Phase.L2,
                  {"ct_sub_display_panel": 1.0}, 35.0, "switch.wall_display_panel",
                  dedicated_ct=True),
        SimDevice("network_cabinet", "Network Cabinet", SimDeviceType.VARIABLE_SPEED, Phase.L2,
                  {"ct_sub_network_cabinet": 1.0}, 300.0, "sensor.network_cabinet_power",
                  dedicated_ct=True),
        SimDevice("eave_outlet", "Eave Outlet", SimDeviceType.BINARY, Phase.L1,
                  {"ct_sub_eave_outlet": 1.0}, 60.0, "switch.eave_outlet",
                  dedicated_ct=True),
        SimDevice("ventilation_unit", "Heat Recovery Ventilator", SimDeviceType.VARIABLE_SPEED, Phase.L1,
                  {"ct_sub_ventilation_unit": 1.0}, 160.0, "fan.ventilation_unit",
                  dedicated_ct=True),
        SimDevice("humidifier_2", "Humidifier 2", SimDeviceType.BINARY,
                  Phase.L2, {"ct_sub_humidifier_2": 1.0}, 1200.0,
                  "switch.humidifier_2", dedicated_ct=True),
        SimDevice("workstation", "Workstation", SimDeviceType.BINARY, Phase.L1,
                  {"ct_sub_upper_plug_ct1": 1.0}, 220.0, "switch.workstation",
                  metadata={"shared_branch": "ct_sub_upper_plug_ct1", "confidence": "assumed"}),
        SimDevice("monitor", "Monitor", SimDeviceType.STANDBY, Phase.L1,
                  {"ct_sub_upper_plug_ct1": 1.0}, 45.0, "switch.monitor",
                  metadata={"shared_branch": "ct_sub_upper_plug_ct1", "confidence": "assumed"}),
        SimDevice("laptop", "Laptop", SimDeviceType.BINARY, Phase.L2,
                  {"ct_sub_upper_plug_ct2": 1.0}, 65.0, "sensor.laptop",
                  metadata={"shared_branch": "ct_sub_upper_plug_ct2", "confidence": "assumed"}),
        SimDevice("printer", "Printer", SimDeviceType.STANDBY, Phase.L2,
                  {"ct_sub_upper_plug_ct2": 1.0}, 400.0, "switch.printer",
                  metadata={"shared_branch": "ct_sub_upper_plug_ct2", "confidence": "assumed"}),

        # --- Unmetered residual loads (no branch CT at all) -------------- #
        SimDevice("unknown_load_l1", "Unmetered Load L1", SimDeviceType.UNKNOWN, Phase.L1,
                  {"ct_main_l1": 1.0}, 800.0, "sensor.unknown_load_l1",
                  metadata={"attributable": False}),
        SimDevice("unknown_load_l2", "Unmetered Load L2", SimDeviceType.UNKNOWN, Phase.L2,
                  {"ct_main_l2": 1.0}, 900.0, "sensor.unknown_load_l2",
                  metadata={"attributable": False}),
    ]

    # Compute nesting depth (children before parents).
    {c.parent_ct_id for c in channels if c.parent_ct_id}
    for ch in channels:
        depth = 0
        cur = ch.parent_ct_id
        cmap = {c.ct_id: c for c in channels}
        while cur:
            depth += 1
            cur = cmap[cur].parent_ct_id
        ch._depth = depth

    # Sanity: every device circuit must exist.
    known = {c.ct_id for c in channels}
    for dev in devices:
        missing = set(dev.circuits) - known
        if missing:
            raise ValueError(f"device {dev.device_id} references unknown CTs {sorted(missing)}")

    return HouseholdTopology(
        channels=channels,
        devices=devices,
        notes=[
            "Panel B CT1/CT2 are nested inside Panel A CT1/CT2 (reference topology).",
            "Do not sum Panel A and Panel B mains: that double counts the sub-feed.",
            "Double-pole loads: each leg CT carries half the device power.",
            "Shared-branch device placement is assumed, not measured.",
            "Unknown loads attach directly to a panel main and have no branch CT.",
            "Reference CT median state-update cadence: 15.7 s.",
        ],
    )


REAL_HOME_TOPOLOGY = build_real_home_topology()
