"""Sensor entities for WattMosaic.

All decision logic lives in ``entity_values.py`` / ``util.py`` (pure Python,
tested).  This module only wires those values to Home Assistant entities.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MANUFACTURER,
    NAME,
    VERSION,
)
from .coordinator import WattMosaicCoordinator
from .entity_defs import DEVICE_SENSORS, GLOBAL_SENSORS
from .entity_values import (
    BINDABLE_KEYS,
    RECONCILIATION_KEYS,
    binding_device_entity_ids,
    binding_update,
    device_values,
    energy_enabled,
    global_values,
    resolve_binding_target,
)
from .util import published_energy, utcnow

_LOGGER = logging.getLogger(__name__)

_UNIT_MAP = {
    "W": UnitOfPower.WATT,
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "%": PERCENTAGE,
}

_DEVICE_CLASS_MAP = {
    "power": SensorDeviceClass.POWER,
    "energy": SensorDeviceClass.ENERGY,
    "enum": SensorDeviceClass.ENUM,
    "timestamp": SensorDeviceClass.TIMESTAMP,
}

_STATE_CLASS_MAP = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
    "total": SensorStateClass.TOTAL,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up global sensors and per-device sensors."""
    coordinator: WattMosaicCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[SensorEntity] = [
        WattMosaicGlobalSensor(coordinator, entry, key) for key in GLOBAL_SENSORS
    ]
    known_devices: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        if coordinator.data is None:
            return
        new_entities: list[SensorEntity] = []
        for device_id in coordinator.data.results:
            if device_id in known_devices:
                continue
            known_devices.add(device_id)
            configuration = coordinator.device_configuration(device_id)
            for key in DEVICE_SENSORS:
                if key == "energy" and not energy_enabled(configuration):
                    # The user opted this device out of an energy entity.
                    continue
                new_entities.append(
                    WattMosaicDeviceSensor(coordinator, entry, device_id, key)
                )
        if new_entities:
            async_add_entities(new_entities)

    _add_new_devices()
    async_add_entities(entities)
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #


class _WattMosaicBase(CoordinatorEntity[WattMosaicCoordinator], SensorEntity):
    """Shared wiring for every WattMosaic sensor.

    ``SensorEntity`` is not optional: without it Home Assistant sees a plain
    ``Entity``, whose ``state`` property always reports ``unknown`` and never
    consults ``native_value``.  The entities then exist, poll, and carry fresh
    attributes while permanently publishing no value.
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

    @property
    def _engine_available(self) -> bool:
        return self.coordinator.engine_available


class WattMosaicGlobalSensor(_WattMosaicBase):
    """Home-level entity driven by ``Reconciliation``/``HealthResponse``."""

    def __init__(
        self,
        coordinator: WattMosaicCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry)
        definition = GLOBAL_SENSORS[key]
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_global_{key}"
        self._attr_icon = definition.get("icon")
        self._attr_native_unit_of_measurement = _UNIT_MAP.get(definition.get("unit") or "")
        self._attr_device_class = _DEVICE_CLASS_MAP.get(definition.get("device_class") or "")
        self._attr_state_class = _STATE_CLASS_MAP.get(definition.get("state_class") or "")
        if definition.get("options"):
            self._attr_options = list(definition["options"])
        if definition.get("suggested_display_precision") is not None:
            self._attr_suggested_display_precision = definition["suggested_display_precision"]

    def _values(self) -> dict[str, Any]:
        return global_values(
            self.coordinator.data.snapshot if self.coordinator.data else None,
            self.coordinator.data.health if self.coordinator.data else None,
            engine_available=self._engine_available,
            now=utcnow(),
            stale_after=self.coordinator.stale_after,
        )

    @property
    def available(self) -> bool:
        values = self._values()
        if self._key in ("engine_status", "last_update"):
            # These must remain readable so a user can see *why* nothing else
            # has values.  ``engine_status`` reports "unavailable" explicitly.
            return True
        if self._key == "quality_score":
            return bool(values["available"])
        if self._key in RECONCILIATION_KEYS:
            return bool(values["reconciliation_available"])
        return bool(values["available"])

    @property
    def native_value(self) -> Any:
        values = self._values()
        if self._key == "engine_status":
            return values["engine_status"]
        if self._key == "last_update":
            # Always expose the last snapshot time: it is the user's primary
            # clue as to *why* the current-value entities are unavailable.
            return values["last_update"]
        if not self.available:
            return None
        return values.get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "engine_url": self.coordinator.client.base_url,
            "scan_interval_s": self.coordinator.scan_interval,
            "stale_after_s": self.coordinator.stale_after,
            "unavailable_after_s": self.coordinator.unavailable_after,
        }
        data = self.coordinator.data
        if data is not None:
            attributes["last_success"] = (
                data.last_success.isoformat() if data.last_success else None
            )
            attributes["last_error"] = data.last_error
            attributes["topology_version"] = data.reconciliation.get("topology_version")
        else:
            attributes["last_success"] = (
                self.coordinator.last_success.isoformat()
                if self.coordinator.last_success
                else None
            )
            attributes["last_error"] = self.coordinator.last_error
        if self._key == "engine_status" and data is not None:
            attributes["ha_connected"] = data.health.get("ha_connected")
            attributes["database"] = data.health.get("database")
            attributes["queue_depth"] = data.health.get("queue_depth")
            attributes["uptime_seconds"] = data.health.get("uptime_seconds")
        if self._key == "quality_score":
            quality = (data.quality if data else None) or {}
            attributes["issues"] = quality.get("issues", [])
            attributes["channel_count"] = len(quality.get("channels") or {})
        return attributes


class WattMosaicDeviceSensor(_WattMosaicBase, RestoreEntity):
    """One sensor for one engine device.

    Energy is ``total_increasing`` and the engine owns the total.  The
    integration publishes the engine's value unmodified when the reading is
    publishable, so a genuine reset is visible to Home Assistant instead of
    freezing the meter.  The last published total is restored on restart so an
    HA restart never resets the meter.
    """

    def __init__(
        self,
        coordinator: WattMosaicCoordinator,
        entry: ConfigEntry,
        device_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry)
        definition = DEVICE_SENSORS[key]
        self._device_id = device_id
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{key}"
        self._attr_icon = definition.get("icon")
        self._attr_native_unit_of_measurement = _UNIT_MAP.get(definition.get("unit") or "")
        self._attr_device_class = _DEVICE_CLASS_MAP.get(definition.get("device_class") or "")
        self._attr_state_class = _STATE_CLASS_MAP.get(definition.get("state_class") or "")
        if definition.get("options"):
            self._attr_options = list(definition["options"])
        if definition.get("suggested_display_precision") is not None:
            self._attr_suggested_display_precision = definition["suggested_display_precision"]

        name = coordinator.device_names.get(device_id) or device_id
        self._device_identifier = (DOMAIN, f"{entry.entry_id}_{device_id}")
        # Always register on the WattMosaic-owned device first.  The entity ID is
        # derived from the device an entity is created on, so it must come from a
        # name that does not depend on which real device the user's declarations
        # happen to resolve to.  Binding happens in ``async_added_to_hass``.
        self._attr_device_info = DeviceInfo(
            identifiers={self._device_identifier},
            name=name,
            manufacturer=MANUFACTURER,
            model="Inferred load",
            via_device=(DOMAIN, f"engine_{entry.entry_id}"),
            suggested_area=coordinator.device_areas.get(device_id),
        )
        # The *measurement* entities are placed on the user's real device when a
        # declared state entity resolves to one (ADR-0021/0023).  Diagnostic
        # entities stay here on the inferred-load device so provenance remains
        # inspectable in one place and never masquerades as hardware telemetry.
        if self._key in BINDABLE_KEYS:
            self._adopt_registered_device()
        self._energy: float | None = None

    # -- device binding ---------------------------------------------------- #

    def _adopt_registered_device(self) -> None:
        """Keep an already-bound measurement entity on the device it was on.

        Only relevant when the entity registry already holds this unique id, so
        the entity ID is fixed and Home Assistant will not derive it again.
        Without this, every reload would first create the entity on the
        WattMosaic device and then move it back, writing the registry twice and
        briefly showing the entity in the wrong place.

        Entities without a registry entry are untouched: their identity is still
        being derived and must not depend on the binding outcome.
        """
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entity_reg = er.async_get(self.coordinator.hass)
        entity_id = entity_reg.async_get_entity_id("sensor", DOMAIN, self._attr_unique_id)
        if entity_id is None:
            return
        existing = entity_reg.async_get(entity_id)
        if existing is None or existing.device_id is None:
            return
        device_reg = dr.async_get(self.coordinator.hass)
        device_entry = device_reg.async_get(existing.device_id)
        if device_entry is None:
            return
        if self._device_identifier in device_entry.identifiers:
            # Still on our own device; nothing was bound.  Keep ``device_info``
            # so Home Assistant keeps updating that device's metadata.
            return
        # Home Assistant reads ``device_entry`` when ``device_info`` is absent,
        # and only falls back to ``device_info`` when this is None.
        self._attr_device_info = None
        self.device_entry = device_entry

    def _linkable(self, device_id: str) -> bool:
        """Whether a stored link still names a usable, non-WattMosaic device.

        A link is a user declaration, not a guarantee: the device can be deleted
        (or re-created with a new id) long after the options entry was saved.
        Validating here keeps a stale link from pinning an entity to a device
        that no longer exists, and keeps both entity-derived and declared targets
        subject to the same "never host on our own device" rule.
        """
        from homeassistant.helpers import device_registry as dr

        device_entry = dr.async_get(self.hass).async_get(device_id)
        if device_entry is None:
            return False
        return not any(identifier[0] == DOMAIN for identifier in device_entry.identifiers)

    def _binding_target(self) -> str | None:
        """The real HA device this measurement entity should be displayed on, if any.

        A load inferred from a CT often already has a Home Assistant device: the
        smart plug, the thermostat, the EV charger, the Lutron dimmer.  Showing
        the power and energy sensors there is what makes WattMosaic augment an
        existing device instead of inventing a parallel one.

        Only ``BINDABLE_KEYS`` move.  Source, confidence, evidence tier, sample
        count and the other diagnostics stay on the synthetic "Inferred load"
        device so inference provenance remains inspectable in one place and never
        masquerades as hardware telemetry.

        This only *resolves* the target; the move itself happens after the
        entity has been registered (see ``_bind_real_device``).
        """
        if self._key not in BINDABLE_KEYS:
            return None
        explicit = self.coordinator.explicit_device_link(self._device_id)
        if explicit is not None:
            # A declared link names the device directly, so no registry lookup is
            # needed and no ambiguity between sibling entities can arise.  It is
            # validated first: the device may have been deleted from Home
            # Assistant since the link was stored, and writing a stale id would
            # fail the registry update (or, worse, point at a reused id).
            return explicit if self._linkable(explicit) else None
        configuration = self.coordinator.device_configuration(self._device_id)
        candidates = binding_device_entity_ids(configuration)
        if not candidates:
            return None
        from homeassistant.helpers import entity_registry as er

        entity_reg = er.async_get(self.hass)
        registry: dict[str, Any] = {}
        for entity_id in candidates:
            registry_entry = entity_reg.async_get(entity_id)
            if registry_entry is None:
                registry[entity_id] = None
                continue
            registry[entity_id] = {
                "device_id": registry_entry.device_id,
                "config_entry_id": registry_entry.config_entry_id,
            }
        return resolve_binding_target(
            candidates, registry, own_config_entry_id=self._entry.entry_id
        )

    @callback
    def _bind_real_device(self) -> None:
        """Move an already-registered measurement entity to the resolved device.

        This runs *after* Home Assistant has created the entity registry entry,
        never before.  Entity IDs are derived from the device an entity is
        created on, so setting ``device_entry`` in ``__init__`` made the ID
        depend on the binding outcome: the same engine device produced
        ``sensor.<engine name>_estimated_energy`` when unbindable and
        ``sensor.<real device>_estimated_energy`` when bindable.  A device that
        became bindable after the first start therefore changed its entity ID,
        and the recorder does not migrate statistics when an ID is re-derived
        at create time — the old statistics were orphaned.

        Moving an entity between devices through the registry changes only
        ``device_id``.  The entity ID is untouched, and the recorder's
        statistics migration is keyed on renames, so it does not run.
        """
        target = self._binding_target()
        if target is None:
            # No real device resolves any more.  That happens when the user
            # clears a link or removes the state entity that justified one.
            # Without this branch the entity would stay on the old device
            # forever: the move is the only thing that writes ``device_id``, and
            # "nothing to do" previously meant "leave it where it is".
            self._return_to_own_device()
            return
        from homeassistant.helpers import entity_registry as er

        entity_reg = er.async_get(self.hass)
        current = entity_reg.async_get(self.entity_id)
        update = binding_update(
            current.device_id if current is not None else None,
            target,
        )
        if update is None:
            return
        entity_reg.async_update_entity(self.entity_id, **update)

    @callback
    def _return_to_own_device(self) -> None:
        """Move a measurement entity back onto its WattMosaic device.

        Only touches an entity that is currently somewhere else, so an entity
        that was never bound is left alone and no registry write happens on a
        normal reload.  The entity ID never changes: only ``device_id`` is
        written (see :func:`binding_update`).
        """
        from homeassistant.helpers import entity_registry as er

        entity_reg = er.async_get(self.hass)
        current = entity_reg.async_get(self.entity_id)
        if current is None or current.device_id is None:
            return
        own = self.coordinator.device_id_for_identifier(self._device_identifier)
        if own is None or current.device_id == own:
            return
        entity_reg.async_update_entity(self.entity_id, device_id=own)

    # -- lifecycle --------------------------------------------------------- #

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The registry entry exists from here on, so the entity ID is final and a
        # device move can no longer change it.
        self._bind_real_device()
        if self._key == "energy":
            last = await self.async_get_last_state()
            if last is not None and last.state not in (None, "", "unknown", "unavailable"):
                try:
                    restored = float(last.state)
                except (TypeError, ValueError):
                    restored = None
                self._energy = restored if restored is None or restored >= 0 else 0.0
            # Fold the reading already held by the coordinator so the entity has
            # a value immediately instead of waiting for the next poll.
            self._publish_energy()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish the newest engine energy total when it is trustworthy."""
        if self._key == "energy":
            self._publish_energy()
        super()._handle_coordinator_update()

    def _publishable(self, result: dict[str, Any] | None) -> bool:
        """Whether the engine reading may be handed to HA statistics."""
        if result is None:
            return False
        return bool(
            device_values(
                result,
                engine_available=self._engine_available,
                now=utcnow(),
                stale_after=self.coordinator.stale_after,
            )["available"]
        )

    def _publish_energy(self) -> float | None:
        """Fold the newest engine reading into the published total."""
        result = self.coordinator.device_result(self._device_id)
        self._energy = published_energy(
            result.get("energy_kwh") if result else None,
            self._energy,
            publishable=self._publishable(result),
        )
        return self._energy

    # -- values ------------------------------------------------------------ #

    def _values(self) -> dict[str, Any]:
        result = self.coordinator.device_result(self._device_id)
        return device_values(
            result,
            engine_available=self._engine_available,
            now=utcnow(),
            stale_after=self.coordinator.stale_after,
        )

    @property
    def available(self) -> bool:
        result = self.coordinator.device_result(self._device_id)
        if result is None or not self._engine_available:
            return False
        if self._key in (
            "last_inference",
            "data_quality",
            "model_validity",
            "energy",
            "confidence_tier",
        ):
            # Descriptive entities stay readable so the user can see the
            # staleness itself, and a cumulative energy total is not a
            # "current measurement".  Neither ever reports a fresh power value.
            return True
        return bool(self._values()["available"])

    @property
    def native_value(self) -> Any:
        if self._key == "energy":
            # A cumulative total, not a current measurement: the value is kept
            # through a *stale* engine so a gap does not blank the meter.  It is
            # only advanced by a publishable reading, in
            # ``_handle_coordinator_update``; reading it here has no side
            # effects.  An unreachable engine still makes the entity
            # unavailable, like every other time series.
            return self._energy

        values = self._values()
        if self._key in ("last_inference", "data_quality", "model_validity", "confidence_tier"):
            return values.get(self._key)
        if not values.get("available"):
            return None
        return values.get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self.coordinator.device_result(self._device_id) or {}
        attributes: dict[str, Any] = {
            "engine_device_id": self._device_id,
            "confidence_is_calibrated": result.get("confidence_is_calibrated", False),
            "confidence_tier": result.get("confidence_tier", "tier_0_unknown"),
            "supporting_ct_channels": result.get("supporting_ct_channels", []),
            "measurement_age_seconds": result.get("measurement_age_seconds"),
            "stage_status": result.get("stage_status"),
        }
        if self._key == "power":
            attributes["explanation"] = result.get("explanation", [])
            attributes["provenance"] = result.get("provenance", {})
            attributes["model_id"] = result.get("model_id")
            attributes["model_version"] = result.get("model_version")
        if self._key == "confidence_tier":
            attributes["tier_reasons"] = result.get("tier_reasons", [])
            attributes["note"] = (
                "Evidence grade, not a probability. Tier 0 is not zero power."
            )
        if self._key == "confidence":
            attributes["confidence_is_calibrated"] = False
            attributes["note"] = (
                "Engineering score in percent; not a calibrated probability."
            )
        if self._key == "energy":
            attributes["energy_source"] = "engine"
        if self._key == "sample_count":
            attributes["source"] = result.get("source")
        return attributes
