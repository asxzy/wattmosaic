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
from collections.abc import Sequence
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
    SERVICE_DISCOVER_PLACEMENTS,
    SERVICE_START_LEARNING,
    SERVICES,
)
from .coordinator import WattMosaicCoordinator
from .entity_migration import async_migrate_entity_ids
from .flow_logic import placement_request_parameters
from .service_logic import (
    ConfirmationRequired,
    ServiceValidationError,
    build_command_payload,
    choose_entry,
    match_identifier_owners,
    service_result_summary,
    target_device_ids,
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

    # A translated entity name changed, which changes the entity_id Home
    # Assistant derives for new installs.  An existing install must be renamed
    # explicitly so the recorder migrates its long-term statistics; without this
    # a second entity would be created and the old series would be orphaned.
    await async_migrate_entity_ids(hass, entry, domain="sensor")

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


def _device_by_id(registry: Any, device_id: str) -> Any:
    """The registry device with this HA device id, or ``None``.

    Iterated rather than looked up through a mapping for the same compatibility
    reason as :func:`_device_identifier`.
    """
    for device in registry.devices:
        if getattr(device, "id", None) == device_id:
            return device
    return None


def _engine_device_id(
    hass: HomeAssistant, entry_id: str, ha_device_id: str
) -> str | None:
    """The engine device id this entry published for an HA registry device.

    Devices created by this integration carry ``<entry_id>_<engine_device_id>``,
    so the prefix is stripped to recover the id the engine actually knows.
    Returns ``None`` for a device that does not belong to ``entry_id``.
    """
    registry = dr.async_get(hass)
    device = _device_by_id(registry, ha_device_id)
    if device is None or device.config_entry_id != entry_id:
        return None
    prefix = f"{entry_id}_"
    for domain, value in device.identifiers:
        if domain == DOMAIN and value.startswith(prefix):
            return value[len(prefix) :]
    return None


def _resolve_entry(
    hass: HomeAssistant,
    device_id: str | None,
    target_ids: Sequence[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Pick the config entry (single-entry installs are the common case).

    With several engines configured, the target must identify exactly one of
    them.  Accepted forms, in order:

    * a **fully qualified** ``<config_entry_id>_<engine_device_id>`` identifier,
      unambiguous by construction;
    * an **HA device registry id** from ``target.device`` -- that names exactly
      one entry;
    * a **bare engine id** matched against the devices this integration owns.

    A bare engine id is *not* unique in practice: a stable and a development App
    publish the same engine ids, so the same ``dev_...`` string exists in both
    entries.  :func:`choose_entry` therefore raises instead of picking one, which
    is what the old fallback silently did.  Reaching the wrong engine produces a
    plausible answer to a command it never received, and that is worse than an
    error.
    """
    entries: dict[str, Any] = hass.data.get(DOMAIN, {})
    if not entries:
        raise ServiceValidationError("engine_not_configured", "WattMosaic is not configured")

    if len(entries) == 1:
        entry_id, data = next(iter(entries.items()))
        return entry_id, data

    registry = dr.async_get(hass)
    candidates: list[str] = []

    # A target picked in Home Assistant is a registry id, which belongs to one
    # entry by definition.  It takes precedence over a bare engine id because
    # the user picked that exact device.
    for target in target_ids:
        device = _device_by_id(registry, target)
        if device is not None and device.config_entry_id in entries:
            return device.config_entry_id, entries[device.config_entry_id]
        if device is not None and device.config_entry_id is None:
            raise ServiceValidationError(
                "unknown_target",
                f"device {target!r} is not owned by any WattMosaic engine",
            )

    if device_id:
        # Collect every ``(entry, identifier)`` this integration owns, then let
        # the pure helper decide.  It handles the fully qualified form first
        # (which *is* an identifier value and must not be prefixed again) and
        # otherwise reports every entry publishing the bare id, so a collision is
        # visible instead of being resolved by load order.
        owned = [
            (entry_id, value)
            for entry_id in entries
            for device in registry.devices
            for domain, value in device.identifiers
            if domain == DOMAIN and value.startswith(f"{entry_id}_")
        ]
        exact, candidates = match_identifier_owners(
            owned, device_id=device_id, entries=list(entries)
        )
        if exact is not None:
            return exact, entries[exact]

    entry_id = choose_entry(list(entries), candidates, device_id=device_id)
    return entry_id, entries[entry_id]


def _placement_parameters(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Merge the user's stored auto-placement choice into the request.

    The decision itself is made by ``flow_logic.placement_request_parameters``
    (pure and tested); this only finds the live config entry so a toggle takes
    effect on the next call without a reload.
    """
    coordinator = entry_data.get("coordinator")
    entry = getattr(coordinator, "entry", None)
    config: dict[str, Any] = {}
    if entry is not None:
        config = {**entry.data, **entry.options}
    return placement_request_parameters(config, parameters)


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

    target_ids = target_device_ids(data)
    try:
        entry_id, entry_data = _resolve_entry(
            hass, validated.get("device_id"), target_ids
        )
    except ServiceValidationError as err:
        # Routing ambiguity is a user-facing error, never a silent fallback to
        # whichever engine happened to load first: reaching the wrong engine
        # returns a plausible answer to a command it never received.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=err.key,
        ) from err

    client: EngineClient = entry_data["client"]

    # The engine only knows its own device ids, never Home Assistant's.  A target
    # picked in the UI therefore has to be translated back to the engine id this
    # entry published for it; forwarding the registry id would make the engine
    # report "not found" for a device the user just selected.
    if target_ids and not validated.get("device_id"):
        resolved = _engine_device_id(hass, entry_id, target_ids[0])
        if resolved is not None:
            validated["device_id"] = resolved

    payload = build_command_payload(service, validated)
    if service == SERVICE_DISCOVER_PLACEMENTS:
        payload["parameters"] = _placement_parameters(
            hass, entry_data, payload.get("parameters") or {}
        )

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
