"""Binary sensors for WattMosaic (running status, engine reachability, learning).

Decision logic lives in ``entity_values.py`` (pure Python, tested).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, NAME, VERSION
from .coordinator import WattMosaicCoordinator
from .entity_defs import DEVICE_BINARY_SENSORS, GLOBAL_BINARY_SENSORS
from .entity_values import device_values, global_values
from .util import utcnow

_LOGGER = logging.getLogger(__name__)

_DEVICE_CLASS_MAP = {
    "running": BinarySensorDeviceClass.RUNNING,
    "connectivity": BinarySensorDeviceClass.CONNECTIVITY,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WattMosaicCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[BinarySensorEntity] = [
        WattMosaicGlobalBinarySensor(coordinator, entry, key) for key in GLOBAL_BINARY_SENSORS
    ]
    known: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        if coordinator.data is None:
            return
        new_entities: list[BinarySensorEntity] = []
        for device_id in coordinator.data.results:
            if device_id in known:
                continue
            known.add(device_id)
            new_entities.extend(
                WattMosaicDeviceBinarySensor(coordinator, entry, device_id, key)
                for key in DEVICE_BINARY_SENSORS
            )
        if new_entities:
            async_add_entities(new_entities)

    _add_new_devices()
    async_add_entities(entities)
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class _Base(CoordinatorEntity[WattMosaicCoordinator], BinarySensorEntity):
    """Shared wiring for every WattMosaic binary sensor.

    ``BinarySensorEntity`` is required: a plain ``Entity`` never consults
    ``is_on`` and reports ``unknown`` permanently.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: WattMosaicCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"engine_{entry.entry_id}")},
            name=f"{NAME} Engine",
            manufacturer=MANUFACTURER,
            model="Local inference engine",
            sw_version=coordinator.data.health.get("engine_version", VERSION)
            if coordinator.data and coordinator.data.health
            else VERSION,
            configuration_url=coordinator.client.base_url,
        )


class WattMosaicGlobalBinarySensor(_Base):
    def __init__(
        self,
        coordinator: WattMosaicCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry)
        definition = GLOBAL_BINARY_SENSORS[key]
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_global_{key}"
        self._attr_device_class = _DEVICE_CLASS_MAP.get(definition.get("device_class") or "")
        self._attr_icon = definition.get("icon")

    def _values(self) -> dict[str, Any]:
        return global_values(
            self.coordinator.data.snapshot if self.coordinator.data else None,
            self.coordinator.data.health if self.coordinator.data else None,
            engine_available=self.coordinator.engine_available,
            now=utcnow(),
            stale_after=self.coordinator.stale_after,
        )

    @property
    def available(self) -> bool:
        # These convey health, so they are always meaningful.
        return True

    @property
    def is_on(self) -> bool:
        values = self._values()
        if self._key == "engine_reachable":
            return bool(self.coordinator.engine_available)
        return bool(values.get("learning_active"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._key == "engine_reachable":
            data = self.coordinator.data
            last_success = data.last_success if data else self.coordinator.last_success
            last_error = data.last_error if data else self.coordinator.last_error
            return {
                "engine_url": self.coordinator.client.base_url,
                "last_success": last_success.isoformat() if last_success else None,
                "last_error": last_error,
            }
        return {}


class WattMosaicDeviceBinarySensor(_Base):
    def __init__(
        self,
        coordinator: WattMosaicCoordinator,
        entry: ConfigEntry,
        device_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry)
        definition = DEVICE_BINARY_SENSORS[key]
        self._device_id = device_id
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{key}"
        self._attr_device_class = _DEVICE_CLASS_MAP.get(definition.get("device_class") or "")
        self._attr_icon = definition.get("icon")
        name = coordinator.device_names.get(device_id) or device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{device_id}")},
            name=name,
            manufacturer=MANUFACTURER,
            model="Inferred load",
            via_device=(DOMAIN, f"engine_{entry.entry_id}"),
        )

    def _values(self) -> dict[str, Any]:
        return device_values(
            self.coordinator.device_result(self._device_id),
            engine_available=self.coordinator.engine_available,
            now=utcnow(),
            stale_after=self.coordinator.stale_after,
        )

    @property
    def available(self) -> bool:
        return bool(self._values().get("running_available"))

    @property
    def is_on(self) -> bool | None:
        if not self.available:
            return None
        return bool(self._values().get("running"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.coordinator.device_result(self._device_id) or {}
        return {
            "engine_device_id": self._device_id,
            "power_w": result.get("power_w"),
            "source": result.get("source"),
            "quality_status": result.get("quality_status"),
        }
