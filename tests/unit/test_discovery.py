"""Discovery must never offer WattMosaic's own estimates as CT channels.

Topology discovery reads every power entity in Home Assistant.  The integration
publishes ``sensor.<device>_estimated_power``, which is a power entity in watts,
so without an explicit exclusion discovery proposes the engine's own answer back
to the user as if it were an independent measurement of the same circuit.

These tests use the real discovery function with a synthetic registry, so they
fail if the exclusion is removed rather than merely restating it.
"""

from __future__ import annotations

from wattmosaic_engine.ingestion.discovery import discover_candidate_topology


def _power_state(entity_id: str, value: str = "1.0") -> dict:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "friendly_name": entity_id,
        },
    }


def _registry(entries: dict[str, str], *, device_id: str | None = None) -> dict:
    """Build a minimal registry.

    ``device_id`` groups every entity onto one hardware device.  Discovery only
    treats power entities belonging to a *multi-channel monitor* as CT channels,
    so the real-CT assertions need a shared device; passing ``None`` leaves the
    entities ungrouped (and therefore not CT hardware).
    """
    return {
        "entities": [
            {"entity_id": eid, "platform": platform, "name": eid, "device_id": device_id}
            for eid, platform in entries.items()
        ],
        "devices": (
            [{"id": device_id, "name": "Test Monitor"}] if device_id is not None else []
        ),
    }


def test_discovery_excludes_own_estimate_channels() -> None:
    """A real CT is discovered; the engine's own estimate on the same monitor is not."""
    states = [
        _power_state("sensor.panel_ct1_power"),
        _power_state("sensor.panel_ct2_power"),
        _power_state("sensor.panel_water_heater_power"),
        _power_state("sensor.fridge_estimated_power"),
    ]
    registry = _registry(
        {
            "sensor.panel_ct1_power": "some_monitor",
            "sensor.panel_ct2_power": "some_monitor",
            "sensor.panel_water_heater_power": "some_monitor",
            "sensor.fridge_estimated_power": "wattmosaic",
        },
        device_id="monitor",
    )

    topology = discover_candidate_topology(states, registry)
    ids = {c.entity_id for c in topology.channels}

    assert "sensor.panel_ct1_power" in ids, "a real CT must still be discovered"
    assert "sensor.fridge_estimated_power" not in ids, (
        "the engine's own estimate must never be proposed as a CT channel"
    )


def test_discovery_excludes_own_global_sensors_without_registry() -> None:
    states = [_power_state("sensor.wattmosaic_engine_total_incoming_power")]
    topology = discover_candidate_topology(states, None)
    assert topology.channels == [], (
        "engine-owned global sensors are identifiable from their id alone"
    )


def test_discovery_keeps_third_party_power_entities() -> None:
    """The exclusion must be narrow: other integrations are unaffected.

    A monitor whose channels come from unrelated integrations is still
    discovered; only WattMosaic's own output is filtered out.
    """
    states = [
        _power_state("sensor.panel_ct1_power"),
        _power_state("sensor.panel_ct2_power"),
        _power_state("sensor.template_load_power"),
        _power_state("sensor.other_power"),
    ]
    registry = _registry(
        {
            "sensor.panel_ct1_power": "matter",
            "sensor.panel_ct2_power": "matter",
            "sensor.template_load_power": "template",
            "sensor.other_power": "powercalc",
        },
        device_id="monitor",
    )
    topology = discover_candidate_topology(states, registry)
    assert {c.entity_id for c in topology.channels} == {s["entity_id"] for s in states}
