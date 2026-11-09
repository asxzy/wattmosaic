"""Startup topology discovery: a fresh install must not silently do nothing.

Defect #39.  The engine shipped a ``discover_candidate_topology`` function that
the documentation promised (``docs/installation.md`` §5: "WattMosaic discovers CT
channels and proposes device mappings") but which nothing ever called.  With no
topology stored, ``run_inference_once`` returns ``{"skipped": "no topology
configured"}``, that value was never logged, and ``/health`` reported ``ok``.  A
real installation therefore looked healthy while emitting no device at all — the
user saw only the global sensors and the CT-level entities.

Two independent failures had to be fixed together, and both are covered here
because either one alone leaves the product broken:

1. **Nothing bootstrapped a topology.**  ``bootstrap_topology`` now runs once at
   startup when the database has none, stores a *candidate*, and logs the
   outcome.  Health reports ``topology.configured`` and degrades when it is false.

2. **Discovery itself produced an unusable topology.**  Its role classifier
   matched the substring ``"main"``, so every load whose name contained it was
   classified as an incoming service feed: on a real 36-channel installation it
   emitted 21 mains.  It also emitted *zero* devices, which
   ``_validate_topology`` rejects outright (``topology must contain at least one
   device``), so even a wired-up call would have 400'd.

The fixtures below are synthetic and installation-neutral.  They encode the
*shape* of a real installation (a monitor with a feed pair, a double-pole load
and separate single circuits) without any household identifier.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import CTType, Polarity, TopologyStatus
from wattmosaic_engine.ingestion.discovery import discover_candidate_topology
from wattmosaic_engine.ingestion.ha_source import ConnectionState
from wattmosaic_engine.main import (
    TOPOLOGY_BOOTSTRAP_WAIT_S,
    EngineCore,
)
from wattmosaic_engine.storage.database import Database

# --------------------------------------------------------------------------- #
# Fixtures: a synthetic multi-channel monitor
# --------------------------------------------------------------------------- #


def _power(entity_id: str, value: str = "10.0") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "friendly_name": entity_id,
        },
    }


def _energy(entity_id: str, value: str = "1.0") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "friendly_name": entity_id,
        },
    }


#: A monitor named neutrally, with the entity-id prefix deliberately *different*
#: from its display name (real installations do this).
MONITOR_ID = "monitor_device_1"
MONITOR_NAME = "Basement Panel"

#: Object ids: the panel feed pair, a two-pole load, one three-way light family
#: (which must NOT be paired — that is defect #28), and a plain single circuit.
FIXTURE_ENTITY_IDS = [
    "sensor.basement_panel_ct1_power",
    "sensor.basement_panel_ct2_power",
    "sensor.basement_panel_water_heater_ct1_power",
    "sensor.basement_panel_water_heater_ct2_power",
    "sensor.basement_panel_circuit_ct1_power",
    "sensor.basement_panel_circuit_ct2_power",
    "sensor.basement_panel_circuit_ct3_power",
    "sensor.basement_panel_workshop_plug_power",
    "sensor.basement_panel_total_power",
]


def _registries(platform: str = "some_monitor") -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "platform": platform,
                "name": entity_id,
                "device_id": MONITOR_ID,
            }
            for entity_id in FIXTURE_ENTITY_IDS
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }


def _states() -> list[dict[str, Any]]:
    return [_power(entity_id) for entity_id in FIXTURE_ENTITY_IDS]


def _discover() -> Any:
    return discover_candidate_topology(_states(), _registries())


# --------------------------------------------------------------------------- #
# Discovery must produce a *usable* topology
# --------------------------------------------------------------------------- #


def test_discovery_never_treats_a_load_named_main_as_a_feed() -> None:
    """The original bug: ``"main"`` in an id made a load look like a feed.

    A load whose name merely contains ``main`` (``main_area_ac``) must stay a
    load.  This is what produced 21 mains out of 36 channels in production.
    """
    states = [
        _power("sensor.panel_ct1_power"),
        _power("sensor.panel_ct2_power"),
        _power("sensor.panel_main_area_ac_power"),
        _power("sensor.panel_main_area_pump_power"),
    ]
    registries = {
        "entities": [
            {"entity_id": s["entity_id"], "device_id": MONITOR_ID, "platform": "m"}
            for s in states
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    topology = discover_candidate_topology(states, registries)

    mains = [c for c in topology.channels if c.ct_type == CTType.MAIN]
    assert len(mains) == 2, (
        "only the panel's own ct1/ct2 feed pair is a main; "
        f"got {[(c.ct_id, c.ct_type) for c in topology.channels]}"
    )
    assert {c.entity_id for c in mains} == {
        "sensor.panel_ct1_power",
        "sensor.panel_ct2_power",
    }
    assert all(
        c.ct_type != CTType.MAIN
        for c in topology.channels
        if "main_area" in c.entity_id
    ), "a load named 'main_area_*' was classified as a service feed"


def test_discovery_emits_devices_so_the_topology_is_accepted() -> None:
    """``_validate_topology`` rejects a topology with no devices.

    Discovery used to return ``devices=[]``, so even a wired-up call would have
    been refused by the API with HTTP 400.
    """
    topology = _discover()
    assert topology.devices, "a topology without devices is rejected by the API"
    device_ids = {d.device_id for d in topology.devices}
    assert len(device_ids) == len(topology.devices), "duplicate device ids emitted"


def test_every_non_main_channel_is_owned_by_a_device() -> None:
    """A channel with no device is measured but never attributed.

    This is the invariant that makes discovery *useful*: a CT that no device
    claims still appears in the topology and still carries power, but the engine
    has nothing to attribute it to, so no entity is produced for it.
    """
    topology = _discover()
    owned = {
        ct_id
        for device in topology.devices
        for ct_id in (*device.dedicated_ct_ids, *device.branch_ct_ids)
    }
    load_channels = {
        c.ct_id for c in topology.channels if c.ct_type != CTType.MAIN
    }
    unowned = load_channels - owned
    assert not unowned, f"load channels with no owning device: {sorted(unowned)}"

    # Stated as a count too, so losing a whole class of devices is caught even
    # when the remaining ones happen to cover their own channels.
    assert len(topology.devices) == 5, (
        "expected one device per load: water heater (2-pole), three separate "
        f"lighting circuits, workshop plug; got {[d.device_id for d in topology.devices]}"
    )


def test_discovery_emits_exactly_one_feed_pair() -> None:
    topology = _discover()
    mains = [c for c in topology.channels if c.ct_type == CTType.MAIN]
    assert len(mains) == 2
    first, second = mains
    assert first.paired_ct_id == second.ct_id
    assert second.paired_ct_id == first.ct_id


def test_discovery_emits_every_channel_exactly_once() -> None:
    """The panel ``total`` aggregate is not a circuit.

    Counting it would add a phantom channel equal to the panel total and
    double-count every other CT on the monitor.
    """
    topology = _discover()
    entity_ids = [c.entity_id for c in topology.channels]
    assert len(entity_ids) == len(set(entity_ids)), "a channel was emitted twice"
    assert "sensor.basement_panel_total_power" not in entity_ids, (
        "the monitor's own total was discovered as a circuit"
    )
    # Feed pair + water heater pair + lights ct1/ct2/ct3 + workshop plug
    assert len(topology.channels) == 2 + 2 + 3 + 1


def test_discovery_pairs_a_genuine_two_pole_load() -> None:
    topology = _discover()
    by_entity = {c.entity_id: c for c in topology.channels}
    l1 = by_entity["sensor.basement_panel_water_heater_ct1_power"]
    l2 = by_entity["sensor.basement_panel_water_heater_ct2_power"]
    assert l1.ct_type == CTType.BRANCH
    assert l2.ct_type == CTType.BRANCH
    assert l1.paired_ct_id == l2.ct_id
    assert l2.paired_ct_id == l1.ct_id

    heater = [d for d in topology.devices if len(d.branch_ct_ids) == 2]
    assert len(heater) == 1, "the two-pole load must be one device owning both legs"
    assert heater[0].double_pole is True


def test_reversed_leg_of_a_two_pole_load_is_attributed_to_the_right_channel() -> None:
    """The leg reading must follow the leg, not the loop iteration order.

    A double-pole device is emitted from one entity's turn through the loop; if
    the attributes were attached positionally, a reversed CT2 would be recorded
    against CT1 and the wrong leg would be negated on every inference cycle.
    """
    states = [
        _power("sensor.panel_ct1_power", "500.0"),
        _power("sensor.panel_ct2_power", "480.0"),
        _power("sensor.panel_water_heater_ct1_power", "900.0"),
        _power("sensor.panel_water_heater_ct2_power", "-900.0"),
        _power("sensor.panel_workshop_plug_power", "40.0"),
    ]
    registries = {
        "entities": [
            {"entity_id": s["entity_id"], "device_id": MONITOR_ID, "platform": "m"}
            for s in states
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    topology = discover_candidate_topology(states, registries)
    by_entity = {c.entity_id: c for c in topology.channels}

    assert by_entity["sensor.panel_water_heater_ct1_power"].polarity == Polarity.NORMAL, (
        "the positive leg was marked inverted"
    )
    assert by_entity["sensor.panel_water_heater_ct2_power"].polarity == Polarity.INVERTED, (
        "the reversed leg was not detected; it will be clipped to 0 W"
    )


def test_discovery_does_not_fuse_three_separate_lighting_circuits() -> None:
    """Defect #28: ``circuit_ct1/ct2/ct3`` are three loads sharing a name stem.

    Pairing the first two would fuse two unrelated circuits into one device and
    leave the third colliding with it.
    """
    topology = _discover()
    lights = [
        c for c in topology.channels if "circuit" in c.entity_id
    ]
    assert len(lights) == 3
    assert all(c.paired_ct_id is None for c in lights), (
        "three lighting circuits were treated as double-pole legs: "
        f"{[(c.ct_id, c.paired_ct_id) for c in lights]}"
    )
    # A single CT measures one conductor, not one appliance.  ``dedicated_device``
    # would assert exactly one device on the row, which discovery cannot know:
    # three such rows in the reference installation each resolved several
    # independent fixtures.  ``branch`` is the claim the evidence supports, and
    # it is also what lets auto-placement consider the row at all.
    assert all(c.ct_type == CTType.BRANCH for c in lights)
    # Every lighting circuit keeps its own leg in the ct id, so ids stay distinct.
    assert len({c.ct_id for c in lights}) == 3


def test_a_single_channel_circuit_is_not_declared_a_private_measurement() -> None:
    """The misclassification that hid 14 placeable fixtures.

    ``dedicated_device`` means "exactly one logical device".  Discovery's only
    evidence against that is "this channel was not part of a ct1/ct2 pair", which
    is equally true of every one-CT lighting circuit.  On the reference install
    ``lights_ct1`` was declared dedicated while resolving **seven** independent
    fixtures, and ``lights_ct2`` peaked at 221% of what its three claimants
    summed to -- proof the row carried loads nobody had declared.

    The consequence was not cosmetic: ``plan_placements`` refuses a
    ``dedicated_device`` row as another device's measurement, so every accepted
    candidate was refused and auto-placement could never create anything.
    """
    topology = _discover()
    singles = [
        c
        for c in topology.channels
        if c.ct_type not in (CTType.MAIN,)
        and c.paired_ct_id is None
        and "circuit" not in c.entity_id
    ]
    assert singles, "fixture should contain a single-channel non-pair load"
    for channel in singles:
        assert CTType(channel.ct_type) is CTType.BRANCH, (
            f"{channel.ct_id} is a shared circuit but was declared "
            f"{channel.ct_type}"
        )
        assert channel.device_id is None, (
            "a shared branch row must not name one device as its owner"
        )
    owners = {d.device_id: d for d in topology.devices}
    for device in owners.values():
        assert not device.dedicated_ct_ids or all(
            ct in {c.ct_id for c in topology.channels if CTType(c.ct_type) is CTType.DEDICATED_DEVICE}
            for ct in device.dedicated_ct_ids
        )


def test_discovery_ignores_an_isolated_smart_plug() -> None:
    """A smart plug is not CT hardware.

    Promoting ``sensor.washer_power`` to a CT channel would fabricate a circuit
    the installation does not have.
    """
    states = [_power("sensor.washer_power")]
    registries = {
        "entities": [
            {"entity_id": "sensor.washer_power", "device_id": "plug1", "platform": "matter"}
        ],
        "devices": [{"id": "plug1", "name": "Washer Plug"}],
    }
    topology = discover_candidate_topology(states, registries)
    assert topology.channels == []
    assert topology.devices == []


def test_discovery_ignores_energy_only_entities() -> None:
    """kWh counters are not CT channels; watts are."""
    states = [_energy("sensor.basement_panel_energy")]
    registries = {
        "entities": [
            {
                "entity_id": "sensor.basement_panel_energy",
                "device_id": MONITOR_ID,
                "platform": "m",
            }
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    assert discover_candidate_topology(states, registries).channels == []


def test_discovery_never_proposes_wattmosaic_own_estimates() -> None:
    """ADR-0015 / defect #32: the engine must not measure its own answer."""
    states = [
        _power("sensor.panel_ct1_power"),
        _power("sensor.panel_ct2_power"),
        _power("sensor.fridge_estimated_power"),
        _power("sensor.panel_lights_estimated_power"),
    ]
    registries = {
        "entities": [
            {"entity_id": "sensor.panel_ct1_power", "device_id": MONITOR_ID, "platform": "m"},
            {"entity_id": "sensor.panel_ct2_power", "device_id": MONITOR_ID, "platform": "m"},
            {"entity_id": "sensor.fridge_estimated_power", "device_id": "d2", "platform": "wattmosaic"},
            {"entity_id": "sensor.panel_lights_estimated_power", "device_id": "d3", "platform": "wattmosaic"},
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    topology = discover_candidate_topology(states, registries)
    ids = {c.entity_id for c in topology.channels}
    assert "sensor.fridge_estimated_power" not in ids
    assert "sensor.panel_lights_estimated_power" not in ids
    assert "sensor.panel_ct1_power" in ids, "a real CT must still be discovered"


def test_discovery_detects_a_reversed_ct_from_its_reading() -> None:
    """A CT installed backwards must not be published as a clean zero.

    With the default ``normal`` polarity a negative reading is clipped to zero
    and reported as ``direct_measured`` 0 W with ``quality: good`` — a silent
    lie that also hides that channel's whole share of the residual.  Discovery
    therefore derives polarity from the observed sign and records the evidence.
    """
    states = [
        _power("sensor.panel_ct1_power", "500.0"),
        _power("sensor.panel_ct2_power", "480.0"),
        _power("sensor.panel_water_heater_power", "-120.0"),
        _power("sensor.panel_workshop_plug_power", "40.0"),
    ]
    registries = {
        "entities": [
            {"entity_id": s["entity_id"], "device_id": MONITOR_ID, "platform": "m"}
            for s in states
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    topology = discover_candidate_topology(states, registries)
    by_entity = {c.entity_id: c for c in topology.channels}

    reversed_ct = by_entity["sensor.panel_water_heater_power"]
    assert reversed_ct.polarity == Polarity.INVERTED, (
        "a negative reading was accepted as normal polarity; it will be clipped "
        "to 0 W and reported as a good measurement"
    )
    assert "polarity_evidence" in reversed_ct.metadata
    # A positive channel must not be flipped.
    assert by_entity["sensor.panel_workshop_plug_power"].polarity == Polarity.NORMAL
    # Still a candidate: a single snapshot is not proof.
    assert reversed_ct.topology_status == TopologyStatus.CANDIDATE


def test_discovery_result_is_a_candidate_never_confirmed() -> None:
    topology = _discover()
    assert topology.status == TopologyStatus.CANDIDATE
    assert all(c.topology_status == TopologyStatus.CANDIDATE for c in topology.channels)
    assert all(d.topology_status == TopologyStatus.CANDIDATE for d in topology.devices)
    assert all(
        "state_entity_ids" not in (d.metadata or {})
        for d in topology.devices
    ), "discovery must not invent device state links"


# --------------------------------------------------------------------------- #
# Startup wiring: the part that was entirely missing
# --------------------------------------------------------------------------- #


class _StubSource:
    """Stands in for ``HASource`` without touching a socket."""

    def __init__(self, states: list[dict[str, Any]], registries: dict[str, Any]) -> None:
        self._states = states
        self._registries = registries
        self.waited: list[float] = []

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return list(self._states)

    async def registries(self) -> dict[str, Any]:
        return dict(self._registries)

    async def start(self, **_kwargs: Any) -> None:
        return None

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        self.waited.append(timeout)
        return True


class _LateSource(_StubSource):
    """A source whose states only exist after the first connection is primed.

    This reproduces the real startup ordering: ``hasource.start()`` schedules a
    background task and returns immediately, so the first
    ``state_snapshot()`` can legitimately be empty.
    """

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        self.waited.append(timeout)
        self._states = _states()
        self._registries = _registries()
        return True


class _HealthySource(_StubSource):
    """A source that reports connected, so topology is the only variable."""

    def __init__(self) -> None:
        super().__init__([], {})
        self.connected = True
        self.last_event_at = None
        self.state = ConnectionState.CONNECTED


def _core(tmp_path: Path) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(database_path=str(tmp_path / "wm.db"), ha_token="not-used")
    return EngineCore(config, db), db


def _bootstrap(core: EngineCore, db: Database, tmp_path: Path) -> dict[str, Any]:
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        return asyncio.run(core.bootstrap_topology())
    finally:
        db.close()


def test_bootstrap_stores_a_candidate_when_the_database_is_empty(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    result = _bootstrap(core, db, tmp_path)

    assert result.get("status") == "candidate", result
    assert result["channels"] > 0 and result["devices"] > 0, result

    stored = Database(tmp_path / "wm.db").load_topology()
    assert stored is not None, "bootstrap did not persist a topology"
    assert stored.status == TopologyStatus.CANDIDATE
    assert len(stored.channels) == result["channels"]
    assert len(stored.devices) == result["devices"]


def test_bootstrap_never_overwrites_an_existing_topology(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    existing = discover_candidate_topology(_states(), _registries())
    # A visible marker that survives the round-trip: bootstrap must leave this
    # exact payload in place.
    first = existing.devices[0]
    existing = existing.model_copy(
        update={
            "devices": [
                first.model_copy(update={"name": "operator-supplied device"}),
                *existing.devices[1:],
            ]
        }
    )
    db.save_topology(existing)

    result = _bootstrap(core, db, tmp_path)
    assert result.get("skipped") == "topology already configured", result

    stored = Database(tmp_path / "wm.db").load_topology()
    assert stored is not None
    assert stored.devices[0].name == "operator-supplied device", (
        "bootstrap replaced a topology that already existed"
    )


def test_bootstrap_distinguishes_a_monitor_with_no_circuits(tmp_path: Path) -> None:
    """A monitor exposing only its feed pair is not "no monitor installed"."""
    core, db = _core(tmp_path)
    feeds = ["sensor.panel_ct1_power", "sensor.panel_ct2_power"]
    core.hasource = _StubSource(
        [_power(e) for e in feeds],
        {
            "entities": [
                {"entity_id": e, "device_id": "m1", "platform": "m"} for e in feeds
            ],
            "devices": [{"id": "m1", "name": "Panel"}],
        },
    )  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
    finally:
        db.close()

    assert "skipped" in result, result
    reason = str(result["skipped"]).lower()
    assert "no load circuits" in reason, reason
    assert "no multi-channel energy monitor" not in reason


def test_bootstrap_reports_when_no_monitor_is_present(tmp_path: Path) -> None:
    """No monitor is a real condition the user must be told about."""
    core, db = _core(tmp_path)
    core.hasource = _StubSource([_power("sensor.washer_power")], {  # type: ignore[assignment]
        "entities": [
            {"entity_id": "sensor.washer_power", "device_id": "plug1", "platform": "matter"}
        ],
        "devices": [{"id": "plug1", "name": "Washer Plug"}],
    })
    try:
        result = asyncio.run(core.bootstrap_topology())
    finally:
        db.close()

    assert "skipped" in result, result
    assert "monitor" in str(result["skipped"]).lower()
    assert core.topology_bootstrap_error, "the reason must be retained for /health"


def test_bootstrap_can_be_disabled_by_configuration(tmp_path: Path) -> None:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(
        database_path=str(tmp_path / "wm.db"),
        ha_token="not-used",
        topology_bootstrap=False,
    )
    core = EngineCore(config, db)
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology())
    finally:
        db.close()

    assert result == {"skipped": "topology bootstrap disabled by configuration"}
    assert core.topology_cache is None, "disabled bootstrap must not load a topology"



def test_health_reports_a_missing_topology_as_degraded(tmp_path: Path) -> None:
    """The engine must not say ``ok`` while it has nothing to infer.

    Everything else is made healthy here — backend attached, Home Assistant
    connected, database fine — so the *only* possible cause of a non-ok status is
    the missing topology.  A weaker test passed because the backend happened to
    be absent, which is not the condition being guarded.
    """
    core, db = _core(tmp_path)
    try:
        core.inference_backend = object()
        core.hasource = _HealthySource()  # type: ignore[assignment]
        health = core.health()
        assert health.details["topology"]["configured"] is False
        assert health.status != "ok", (
            "a topology-less engine reported healthy even though it has nothing "
            "to infer; that is the silent failure the defect was about"
        )
    finally:
        db.close()


def test_health_reports_a_configured_topology(tmp_path: Path) -> None:
    """Positive control: the same check must pass once a topology exists."""
    core, db = _core(tmp_path)
    try:
        db.save_topology(discover_candidate_topology(_states(), _registries()))
        core.topology_cache = None
        health = core.health()
        topology = health.details["topology"]
        assert topology["configured"] is True
        assert topology["status"] == "candidate"
        assert topology["channels"] > 0
    finally:
        db.close()


def test_bootstrap_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """A restart must reuse the discovered topology, not duplicate it."""
    core, db = _core(tmp_path)
    first = _bootstrap(core, db, tmp_path)
    assert first.get("status") == "candidate"

    core2, db2 = _core(tmp_path)
    core2.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        second = asyncio.run(core2.bootstrap_topology())
    finally:
        db2.close()
    assert second.get("skipped") == "topology already configured"


def test_engine_start_runs_topology_bootstrap(tmp_path: Path) -> None:
    """The regression that mattered: startup never called discovery at all.

    Every other test here calls ``bootstrap_topology`` directly, so without this
    one the method could exist and never run — which is exactly the shipped
    defect.
    """
    core, db = _core(tmp_path)
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        asyncio.run(core.start(start_loops=False))
        stored = Database(tmp_path / "wm.db").load_topology()
        assert stored is not None, (
            "engine.start() never bootstrapped a topology; a fresh install would "
            "ingest every sample and emit nothing"
        )
        assert stored.channels and stored.devices
    finally:
        db.close()


def test_engine_start_waits_for_the_connection_before_discovering(tmp_path: Path) -> None:
    """The regression that mattered at startup: discovery ran too early.

    ``hasource.start()`` returns as soon as the connection task is *scheduled*,
    so an immediate discovery call reads an empty cache.  This drives the real
    ``start()`` path and asserts the bounded wait was actually requested, which
    pinning ``bootstrap_topology`` in isolation cannot catch.
    """
    core, db = _core(tmp_path)
    source = _LateSource([], {})
    core.hasource = source  # type: ignore[assignment]
    try:
        asyncio.run(core.start(start_loops=False))
        stored = Database(tmp_path / "wm.db").load_topology()
        assert stored is not None, (
            "engine.start() ran discovery before the first state snapshot arrived"
        )
        assert stored.channels and stored.devices
        assert source.waited, "engine.start() never waited for the first connection"
        assert source.waited[0] > 0, "engine.start() waited for zero seconds"
    finally:
        db.close()


def test_bootstrap_waits_for_the_first_connection_before_discovering(tmp_path: Path) -> None:
    """Startup must wait for the first state snapshot.

    ``hasource.start()`` only schedules the connection and returns, so an
    immediate discovery call sees an empty cache.  Without the bounded wait and
    the deferred outcome, a genuine fresh install would report "no monitor" even
    though one is configured, and would stay topology-less until a restart.
    """
    core, db = _core(tmp_path)
    core.hasource = _LateSource([], {})  # type: ignore[assignment]
    try:
        result = asyncio.run(
            core.bootstrap_topology(wait_connected_s=TOPOLOGY_BOOTSTRAP_WAIT_S)
        )
    finally:
        db.close()

    assert result.get("status") == "candidate", result
    assert result["channels"] > 0 and result["devices"] > 0
    assert core.hasource.waited, "startup did not wait for the first connection"


def test_bootstrap_defers_instead_of_claiming_no_monitor(tmp_path: Path) -> None:
    """An empty state list is a connection problem, not missing hardware.

    Reporting "no multi-channel energy monitor" here would send the user hunting
    for a CT monitor they already own.  The two conditions must be distinguishable.
    """
    core, db = _core(tmp_path)
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
    finally:
        db.close()

    assert "deferred" in result, result
    assert "monitor" not in str(result["deferred"]).lower(), (
        "an unreachable Home Assistant was reported as missing hardware"
    )
    assert "home assistant" in str(result["deferred"]).lower()


def test_inference_retries_discovery_once_the_connection_appears(tmp_path: Path) -> None:
    """A topology must not be lost forever because startup lost the race.

    The engine keeps serving while Home Assistant is down or slow.  Once states
    become available, the inference cycle must bootstrap rather than return the
    silent skip indefinitely.
    """
    core, db = _core(tmp_path)
    # Startup: Home Assistant is not up yet, so discovery defers.
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
        assert core._active_topology() is None
        assert core.topology_bootstrap_error

        # The connection comes up later; the next cycle must recover.
        core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
        core._last_topology_attempt = 0.0  # bypass the rate limit for this cycle
        result = asyncio.run(core.run_inference_once())

        assert result.get("skipped") != "no topology configured", result
        stored = Database(tmp_path / "wm.db").load_topology()
        assert stored is not None, "the engine never recovered after Home Assistant came up"
        assert stored.channels and stored.devices
    finally:
        db.close()


def test_discovery_retry_is_rate_limited(tmp_path: Path) -> None:
    """Discovery scans every state, so it must not run on every 5 s cycle."""
    core, db = _core(tmp_path)
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        first = asyncio.run(core.maybe_bootstrap_topology())
        second = asyncio.run(core.maybe_bootstrap_topology())
        assert first is not None, "the first attempt must run"
        assert second is None, "retries were not rate limited"
    finally:
        db.close()


def test_missing_topology_is_logged_not_silent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``run_inference_once`` used to return a silent skip every interval."""
    import logging

    core, db = _core(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="wattmosaic.main"):
            asyncio.run(core.run_inference_once())
            asyncio.run(core.run_inference_once())
        warnings = [
            r for r in caplog.records
            if "no topology configured" in r.getMessage().lower()
        ]
        assert warnings, "a missing topology produced no log line at all"
        assert len(warnings) == 1, (
            f"the warning must be emitted once, not every cycle (got {len(warnings)})"
        )
    finally:
        db.close()
