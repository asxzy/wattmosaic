"""P1 — only behavioural HA changes become HA events (HB §1 C, §6.6, §7.1).

Regression for the live install where ~96 % of ``ha_events_v3`` was unlinked
numeric telemetry (a smart plug's current, power factor, apparent power) and
``image``/``camera`` refreshes.  Every reader step overlapped one of those
"events", so P5's groups were telemetry noise.  An HA event is a change of a
control interface or state machine, or of any entity with an
``ha_entity_links`` link; measurement endpoints arrive as readings only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import HAEvent
from wattmosaic_engine.ingestion.ha_source import (
    DEFAULT_HA_EVENT_DOMAINS,
    HASource,
    is_event_domain,
)

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


class _Links:
    def __init__(self, linked: set[str]) -> None:
        self.linked = linked

    def links_for_entity(self, entity_id: str, at: datetime) -> list[dict[str, Any]]:
        if entity_id in self.linked:
            return [{"target_id": "dev_x", "role": "load_state"}]
        if entity_id.endswith("_plug_power"):
            return [{"target_id": "dev_x", "role": "power_measurement"}]
        return []


def _change(entity_id: str, state: str, **attributes: Any) -> dict[str, Any]:
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": T0.isoformat(),
            "data": {
                "entity_id": entity_id,
                "old_state": {"entity_id": entity_id, "state": "x", "attributes": {}},
                "new_state": {
                    "entity_id": entity_id,
                    "state": state,
                    "last_updated": T0.isoformat(),
                    "attributes": attributes,
                },
            },
        },
    }


def _source(linked: set[str] | None = None, **kwargs: Any) -> tuple[HASource, list[HAEvent]]:
    events: list[HAEvent] = []
    source = HASource(
        "http://ha.local:8123",
        "token",
        link_lookup=_Links(linked or set()),
        clock=lambda: T0 + timedelta(seconds=1),
        **kwargs,
    )
    source._on_ha_event = events.append  # type: ignore[assignment]
    return source, events


async def test_numeric_sensor_telemetry_and_images_are_not_ha_events() -> None:
    source, events = _source()
    await source._dispatch(_change("sensor.plug_3_apparent_current", "0.006", unit_of_measurement="A"))
    await source._dispatch(_change("sensor.plug_3_power_factor", "0.41", device_class="power_factor"))
    await source._dispatch(_change("sensor.date_time", "2026-11-19, 21:38"))
    await source._dispatch(_change("image.doorbell_snapshot", "2026-11-20T03:38:00"))
    await source._dispatch(_change("camera.porch", "idle"))
    assert events == []
    assert source.stats["ha_events_filtered"] == 5


async def test_state_machine_domains_enum_sensors_and_linked_entities_are_events() -> None:
    source, events = _source(linked={"sensor.kettle_plug_power"})
    await source._dispatch(_change("switch.kettle", "on"))
    await source._dispatch(_change("light.hall", "on", brightness=120))
    await source._dispatch(_change("binary_sensor.dishwasher_door", "off"))
    await source._dispatch(_change("automation.hrv_sync", "on", last_triggered=T0.isoformat()))
    await source._dispatch(_change("sensor.washer_state", "rinse", device_class="enum"))
    # A sensor linked with a behavioural role is an event source whatever its
    # domain; one linked only as a power measurement is a reading, not an event.
    await source._dispatch(_change("sensor.kettle_plug_power", "1800", unit_of_measurement="W"))
    await source._dispatch(_change("sensor.fridge_plug_power", "90", unit_of_measurement="W"))
    assert [e.entity_id for e in events] == [
        "switch.kettle",
        "light.hall",
        "binary_sensor.dishwasher_door",
        "automation.hrv_sync",
        "sensor.washer_state",
        "sensor.kettle_plug_power",
    ]


async def test_event_domains_are_configurable() -> None:
    config = EngineConfig.from_env({"WM_HA_EVENT_DOMAINS": "switch, sensor"}, dotenv_path="/nonexistent/.env")
    assert config.ha_event_domains == ("switch", "sensor")
    source, events = _source(ha_event_domains=config.ha_event_domains)
    await source._dispatch(_change("sensor.plug_current", "0.2", unit_of_measurement="A"))
    await source._dispatch(_change("light.hall", "on"))
    assert [e.entity_id for e in events] == ["sensor.plug_current"]
    assert EngineConfig().ha_event_domains is None


def test_default_domains_exclude_measurements_and_noise() -> None:
    for domain in ("sensor", "image", "camera", "weather", "sun", "update", "person", "device_tracker"):
        assert domain not in DEFAULT_HA_EVENT_DOMAINS
    assert is_event_domain("climate.main_floor", {})
    assert not is_event_domain("sensor.outdoor_temperature", {"device_class": "temperature"})
