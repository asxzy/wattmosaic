"""Device-registry wiring for the WattMosaic engine (hub) device.

Two Home Assistant eras have to be supported at once:

* **2024.11 – 2026.7** — ``DeviceInfo`` / ``async_get_or_create`` accept the
  ``via_device=(domain, identifier)`` pair and resolve the link themselves.
* **2026.8 and later** — ``via_device`` is deprecated in favour of
  ``via_device_id``, a *device registry id*.  Setting the deprecated pair makes
  Home Assistant report the calling integration; when that report happens inside
  a frame with ``ReportBehavior.ERROR`` it is **raised** as ``RuntimeError``,
  which aborts entity creation instead of merely logging.

That second case is not hypothetical: it took 276 of 322 sensors offline on a
live installation when a registry operation placed the entity re-add inside the
``config`` integration's frame.

Passing *both* keys raises ``HomeAssistantError``, so exactly one is emitted.
The choice is made from the registry's own signature rather than from a version
string, because the capability is what actually matters.

``via_device_id`` also requires the hub to be registered *first*, so
:func:`async_create_engine_device` runs from ``async_setup_entry`` before any
platform is forwarded.
"""

from __future__ import annotations

import inspect
from functools import lru_cache
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MANUFACTURER, NAME, VERSION
from .entity_values import via_device_kwargs


def engine_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """The identifier pair naming this entry's engine (hub) device."""
    return (DOMAIN, f"engine_{entry.entry_id}")


#: ``inspect.signature`` is not free and every entity asks; the answer cannot
#: change while the process lives.
@lru_cache(maxsize=1)
def supports_via_device_id() -> bool:
    """Whether this Home Assistant accepts ``via_device_id``.

    Read from the registry method that the ``DeviceInfo`` mapping is eventually
    splatted into, so the answer is exactly the set of keywords Home Assistant
    will accept.  ``via_device_id`` appears there in 2026.8; before that only
    ``via_device`` exists.
    """
    parameters = inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
    return "via_device_id" in parameters


def async_create_engine_device(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: Any
) -> str:
    """Register the engine device and return its registry id.

    Idempotent: ``async_get_or_create`` returns the existing device when one
    already carries the identifier pair.
    """
    registry = dr.async_get(hass)
    health = {}
    data = getattr(coordinator, "data", None)
    if data is not None and getattr(data, "health", None):
        health = data.health
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={engine_identifier(entry)},
        name=f"{NAME} Engine",
        manufacturer=MANUFACTURER,
        model="Local inference engine",
        sw_version=health.get("engine_version", VERSION),
        configuration_url=coordinator.client.base_url,
    )
    return device.id


def via_device_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The via-device keyword mapping for this entry's child devices.

    Returns ``{"via_device_id": ...}`` on Home Assistant versions that support
    it, and the legacy ``{"via_device": (domain, identifier)}`` pair otherwise.
    Empty when the hub id is somehow unavailable while the new key is required:
    an absent link is better than emitting the deprecated pair, which is the
    exact call that aborts entity creation in a strict reporting frame.
    """
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}
    return via_device_kwargs(
        supports_via_device_id=supports_via_device_id(),
        hub_device_id=stored.get("engine_device_id"),
        hub_identifier=engine_identifier(entry),
    )
