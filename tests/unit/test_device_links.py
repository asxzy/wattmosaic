"""Entity ↔ device linking tests.

The link between HA entities and WattMosaic devices is declared in the
topology.  Guessing it from names or the HA registry would fabricate
attribution, so these tests pin the "declared only" rule and the ambiguity
reporting that replaces silent guessing.
"""

from __future__ import annotations

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.device_links import (
    CONFIG_LOAD_ENTITIES_KEY,
    STATE_ENTITIES_KEY,
    ambiguous_entities,
    config_load_entity_ids,
    device_for_entity,
    entity_links,
    runtime_state_entities,
    state_entity_for_device,
    state_entity_ids,
    state_key,
    supporting_ct_ids,
)


def _topology(*, confirmed: bool = True, ambiguous: bool = False) -> Topology:
    devices = [
        DeviceTopology(
            device_id="dev_sauna",
            name="Sauna",
            dedicated_ct_ids=["ct_sauna"],
            phase=Phase.L1,
            metadata={STATE_ENTITIES_KEY: ["switch.sauna"]},
        ),
        DeviceTopology(
            device_id="dev_fridge",
            name="Fridge",
            dedicated_ct_ids=["ct_fridge"],
            phase=Phase.L2,
            metadata={STATE_ENTITIES_KEY: ["switch.fridge", "binary_sensor.fridge_door"]},
        ),
    ]
    if ambiguous:
        devices.append(
            DeviceTopology(
                device_id="dev_other",
                name="Other",
                dedicated_ct_ids=[],
                phase=Phase.L1,
                metadata={STATE_ENTITIES_KEY: ["switch.fridge"]},
            )
        )
    return Topology(
        status=TopologyStatus.CONFIRMED if confirmed else TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(
                ct_id="ct_sauna",
                entity_id="sensor.sauna_power",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
            ),
            CTChannel(
                ct_id="ct_fridge",
                entity_id="sensor.fridge_power",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L2,
            ),
        ],
        devices=devices,
    )


def test_state_entities_come_from_device_metadata() -> None:
    topology = _topology()
    sauna = next(d for d in topology.devices if d.device_id == "dev_sauna")
    assert state_entity_ids(sauna) == ["switch.sauna"]


def test_state_entities_accept_a_bare_string() -> None:
    device = DeviceTopology(device_id="d", metadata={STATE_ENTITIES_KEY: "switch.one"})
    assert state_entity_ids(device) == ["switch.one"]


def test_state_entities_are_empty_when_undeclared() -> None:
    assert state_entity_ids(DeviceTopology(device_id="d")) == []
    assert state_entity_ids({"device_id": "d"}) == []
    assert state_entity_ids({"device_id": "d", "metadata": {"state_entity_ids": None}}) == []


def test_links_cover_state_entities_and_ct_entities() -> None:
    links = entity_links(_topology())

    assert links["switch.sauna"] == ("dev_sauna", ("ct_sauna",))
    assert links["sensor.sauna_power"] == ("dev_sauna", ("ct_sauna",))
    assert links["binary_sensor.fridge_door"] == ("dev_fridge", ("ct_fridge",))


def test_undeclared_entities_are_not_linked() -> None:
    links = entity_links(_topology())
    assert "switch.unknown_lamp" not in links
    assert device_for_entity(_topology(), "switch.unknown_lamp") is None
    assert supporting_ct_ids(_topology(), "switch.unknown_lamp") == ()


def test_none_topology_links_nothing() -> None:
    assert entity_links(None) == {}
    assert device_for_entity(None, "switch.sauna") is None
    assert state_key(None, "switch.sauna", "on") == ("switch.sauna", "on")


def test_similar_names_are_not_used_to_link() -> None:
    """A device named "Sauna" must not claim ``switch.sauna_light`` by name."""
    assert device_for_entity(_topology(), "switch.sauna_light") is None


def test_ambiguous_entity_is_reported_not_resolved() -> None:
    topology = _topology(ambiguous=True)
    ambiguous = ambiguous_entities(topology)

    assert ambiguous == {"switch.fridge": ["dev_fridge", "dev_other"]}
    # First declaration wins for lookup, but the ambiguity is visible.
    assert device_for_entity(topology, "switch.fridge") == ("dev_fridge", ("ct_fridge",))


def test_non_ambiguous_topology_reports_nothing() -> None:
    assert ambiguous_entities(_topology()) == {}
    assert ambiguous_entities(None) == {}


def test_state_key_maps_declared_entities_to_device_ids() -> None:
    topology = _topology()
    assert state_key(topology, "switch.sauna", "on") == ("dev_sauna", "on")
    assert state_key(topology, "switch.fridge", "on") == ("dev_fridge", "on")


def test_state_key_never_maps_a_power_entity_to_device_state() -> None:
    """Defect #29: a CT power entity must not become the device's runtime state.

    ``sensor.fridge_power`` links to ``dev_fridge`` for *measurement*, but it is
    not the device's state.  Numeric readings never reach the event path (they are
    consumed as measurements), yet availability changes do — and writing
    ``"unavailable"`` as the state replaces ``"off"``, so the next genuine
    ``"off"`` reads as a transition.
    """
    topology = _topology()
    assert state_key(topology, "sensor.fridge_power", 120.0) == (
        "sensor.fridge_power",
        120.0,
    )
    assert state_entity_for_device(topology, "dev_fridge", "sensor.fridge_power") is False
    assert state_entity_for_device(topology, "dev_fridge", "switch.fridge") is True
    # Availability blips are the reachable case in production.
    assert state_key(topology, "sensor.fridge_power", "unavailable") == (
        "sensor.fridge_power",
        "unavailable",
    )


def test_user_declared_load_entities_are_linked_and_are_state_entities() -> None:
    """A configuration-declared runtime entity must actually reach ingestion.

    ``project_configuration`` writes user declarations to
    ``config_load_entity_ids`` (never into the evidence-owned
    ``state_entity_ids``).  If linking only read the evidence key, the whole
    per-device configuration feature would be inert in production: the engine
    would hold a configured device but never see its switch fire.
    """
    topology = _topology()
    device = topology.device_map()["dev_sauna"]
    device = device.model_copy(
        update={
            "metadata": {
                **device.metadata,
                CONFIG_LOAD_ENTITIES_KEY: ["switch.sauna_configured"],
            }
        }
    )
    topology = topology.model_copy(
        update={
            "devices": [
                device if d.device_id == "dev_sauna" else d for d in topology.devices
            ]
        }
    )
    assert config_load_entity_ids(device) == ["switch.sauna_configured"]
    assert runtime_state_entities(device) == ["switch.sauna", "switch.sauna_configured"]
    assert entity_links(topology)["switch.sauna_configured"] == ("dev_sauna", ("ct_sauna",))
    assert state_entity_for_device(topology, "dev_sauna", "switch.sauna_configured") is True
    # The CT power entity is still linked for measurement, just not for state.
    assert entity_links(topology)["sensor.sauna_power"] == ("dev_sauna", ("ct_sauna",))


def test_state_key_passes_undeclared_entities_through() -> None:
    topology = _topology()
    assert state_key(topology, "switch.elsewhere", "on") == ("switch.elsewhere", "on")
    assert state_key(topology, None, None) == ("", None)


def test_state_key_preserves_device_state_objects() -> None:
    """Non-string states are stored unchanged, not coerced."""

    class State:
        state = "on"

    payload = State()
    assert state_key(_topology(), "switch.sauna", payload) == ("dev_sauna", payload)


def test_confirmed_flag_does_not_affect_linking() -> None:
    """Linking is structural; confirmation only gates learning/training."""
    candidate = _topology(confirmed=False)
    confirmed = _topology(confirmed=True)
    assert entity_links(candidate) == entity_links(confirmed)
