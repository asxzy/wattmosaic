"""WattMosaic Home Assistant integration.

Thin REST client + entity publisher for the local WattMosaic engine.  All
inference, learning and topology decisions live in the engine; this integration
never actuates devices and never writes state into HA (master spec §7.2, D3).

HA is imported here, so this module is *not* imported by the test suite.  All
unit-tested logic lives in pure-Python modules (``util``, ``api_client``,
``flow_logic``, ``service_logic``, ``entity_values``).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import EngineAuthError, EngineClient, EngineError, EngineUnavailable
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DOMAIN,
    PLATFORMS,
    RESPONSE_SERVICES,
    SERVICE_START_LEARNING,
    SERVICES,
)
from .coordinator import WattMosaicCoordinator
from .service_logic import (
    ConfirmationRequired,
    ServiceValidationError,
    build_command_payload,
    service_result_summary,
    validate_service_call,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): vol.Any(str, None),
        vol.Optional("sample_id"): vol.Any(str, None),
        vol.Optional("confirm"): vol.Any(bool, None),
        vol.Optional("parameters"): vol.Any(dict, None),
    },
    extra=vol.ALLOW_EXTRA,
)


# --------------------------------------------------------------------------- #
# Setup / teardown
# --------------------------------------------------------------------------- #


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WattMosaic from a config entry."""
    config = {**entry.data, **entry.options}
    base_url = config.get(CONF_BASE_URL)
    if not base_url:
        _LOGGER.error("WattMosaic entry %s has no base_url", entry.entry_id)
        return False

    client = EngineClient(
        async_get_clientsession(hass),
        base_url,
        api_token=config.get(CONF_API_TOKEN),
    )
    coordinator = WattMosaicCoordinator(hass, entry, client)

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(f"WattMosaic engine not ready: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and unregister global services if last entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            _async_unregister_services(hass)
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload cleanly when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


def _async_register_services(hass: HomeAssistant) -> None:
    """Register services once per HA instance."""
    if hass.services.has_service(DOMAIN, SERVICE_START_LEARNING):
        return

    for service in SERVICES:
        schema = SERVICE_SCHEMA
        supports_response = (
            SupportsResponse.OPTIONAL
            if service in RESPONSE_SERVICES
            else SupportsResponse.NONE
        )

        async def _handler(call: ServiceCall, _service: str = service) -> dict[str, Any] | None:
            return await _async_handle_service(hass, _service, call)

        hass.services.async_register(
            DOMAIN,
            service,
            _handler,
            schema=schema,
            supports_response=supports_response,
        )


def _async_unregister_services(hass: HomeAssistant) -> None:
    for service in SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _device_identifier(registry: Any, identifier: tuple[str, str]) -> Any:
    """The main device carrying ``identifier``, or ``None``.

    Written as an iteration rather than ``device_registry.async_get_device()``:
    that helper is deprecated in Home Assistant 2026.11 (removed in 2027.8) and
    logs a deprecation warning for custom integrations, while its replacements
    (``async_get_devices`` / ``async_get_device_by_identifier``) do not exist in
    the 2024.11 minimum this integration supports.

    ``registry.devices`` must be *iterated*, not used as a mapping: Home
    Assistant's compatibility view also deprecates ``.values()``, ``.get()`` and
    device-id membership (removed in 2027.9), and merely iterating is the
    supported form on both versions.
    """
    for device in registry.devices:
        if identifier in device.identifiers:
            return device
    return None


def _resolve_entry(hass: HomeAssistant, device_id: str | None) -> tuple[str, dict[str, Any]]:
    """Pick the config entry (single-entry installs are the common case).

    With several engines configured, ``device_id`` is matched against the HA
    device registry.  Devices created by this integration use the identifier
    ``<entry_id>_<engine_device_id>``, so the suffix is what we match on.
    """
    entries: dict[str, Any] = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("WattMosaic is not configured")

    if len(entries) == 1:
        entry_id, data = next(iter(entries.items()))
        return entry_id, data

    if device_id:
        registry = dr.async_get(hass)
        for entry_id in entries:
            identifier = (DOMAIN, f"{entry_id}_{device_id}")
            if _device_identifier(registry, identifier) is not None:
                return entry_id, entries[entry_id]
        # Also honour an exact engine identifier if a user passed it directly.
        # A device now belongs to a single config entry, so ``config_entry_id``
        # is the non-deprecated form (``config_entries`` is removed in 2027.10).
        device = _device_identifier(registry, (DOMAIN, device_id))
        if device is not None and device.config_entry_id in entries:
            return device.config_entry_id, entries[device.config_entry_id]

    _LOGGER.warning(
        "Multiple WattMosaic engines are configured and the target could not be "
        "resolved; using the first entry"
    )
    entry_id, data = next(iter(entries.items()))
    return entry_id, data


async def _async_handle_service(
    hass: HomeAssistant,
    service: str,
    call: ServiceCall,
) -> dict[str, Any] | None:
    """Validate, forward and translate one service call."""
    data = dict(call.data)
    try:
        validated = validate_service_call(service, data)
    except ConfirmationRequired as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="confirmation_required",
            translation_placeholders={"service": service},
        ) from err
    except ServiceValidationError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders={"service": service},
        ) from err

    _, entry_data = _resolve_entry(hass, validated.get("device_id"))
    client: EngineClient = entry_data["client"]
    payload = build_command_payload(service, validated)

    try:
        result = await client.command(
            service,
            device_id=validated.get("device_id"),
            sample_id=validated.get("sample_id"),
            parameters=payload["parameters"],
        )
    except EngineAuthError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="engine_rejected",
            translation_placeholders={"detail": "authentication failed"},
        ) from err
    except EngineUnavailable as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="engine_unavailable",
        ) from err
    except EngineError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="engine_rejected",
            translation_placeholders={"detail": str(err)},
        ) from err

    if isinstance(result, dict) and result.get("ok") is False:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="engine_rejected",
            translation_placeholders={"detail": service_result_summary(result)},
        )

    # A command can change learning/model state; refresh so entities follow.
    coordinator: WattMosaicCoordinator = entry_data["coordinator"]
    await coordinator.async_request_refresh()

    if service in RESPONSE_SERVICES:
        # For export/diagnostics, return the full payload from the engine.
        if isinstance(result, dict):
            data_payload = result.get("data")
            if isinstance(data_payload, dict) and data_payload:
                return data_payload
            return result
        return {"result": result}

    if isinstance(result, dict):
        _LOGGER.debug("WattMosaic service %s: %s", service, service_result_summary(result))
    return None
