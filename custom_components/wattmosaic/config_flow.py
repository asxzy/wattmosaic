"""Config and options flows for WattMosaic.

The flows are intentionally thin: all validation/merging logic lives in
``flow_logic.py`` (pure Python, unit-tested), and all HTTP lives in
``api_client.py``.  This file only wires Home Assistant's flow API to those
two building blocks.

Wizard order (identical for setup and options):

1. ``user`` / ``init``   – engine URL + token (setup) or polling settings (options)
2. ``settings``          – polling and freshness thresholds (setup only)
3. ``channels``          – per-CT role / phase / polarity / pairing / device
4. ``mapping``           – device suggestions for still-unmapped dedicated CTs
5. ``confirm``           – explicit confirmation before anything is committed

Nothing is written to the engine until step 5, and uncertain assignments stay
``candidate`` unless the user ticks the confirmation box.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api_client import EngineClient, describe_engine_error, redact_token_for_display
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_CONFIGURE_DEVICE,
    CONF_CT_ASSIGNMENTS,
    CONF_ENERGY_ENTITIES,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    CONF_TOPOLOGY_CONFIRMED,
    CONF_TOPOLOGY_VERSION,
    CONF_UNAVAILABLE_AFTER,
    CONNECTIVITY_LEVELS,
    CT_ROLE_DEDICATED,
    DEVICE_TYPES,
    DOMAIN,
    LIGHT_TYPES,
    MAX_SCAN_INTERVAL,
    MAX_STALE_AFTER,
    MAX_UNAVAILABLE_AFTER,
    MIN_SCAN_INTERVAL,
    MIN_STALE_AFTER,
    MIN_UNAVAILABLE_AFTER,
    MONITORING_LEVELS,
    NAME,
    POWER_BEHAVIORS,
)
from .flow_logic import (
    ENERGY_ENTITY_FIELD,
    assignment_defaults,
    assignment_store,
    build_topology_update,
    candidate_labels,
    connection_defaults,
    device_candidates,
    device_configuration_command,
    discovered_base_url,
    energy_entity_choice,
    mapping_summary,
    normalise_assignment,
    normalise_channels,
    normalise_device_configuration,
    normalise_device_list,
    normalise_settings,
    panel_shared_tokens,
    resolve_confirmation,
    settings_defaults,
    stored_energy_choice,
    suggested_pair,
    uncertainty_summary,
    validate_assignments,
    validate_connection,
    validate_device_configuration,
    validate_settings,
)
from .util import normalise_base_url

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Selectors / schemas
# --------------------------------------------------------------------------- #


def _role_select() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value="main", label="Main incoming feed"),
                SelectOptionDict(value="branch", label="Branch circuit"),
                SelectOptionDict(value="dedicated_device", label="Dedicated device"),
                SelectOptionDict(value="unknown", label="Unknown / unassigned"),
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _phase_select() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value="l1", label="L1 (leg 1)"),
                SelectOptionDict(value="l2", label="L2 (leg 2)"),
                SelectOptionDict(value="both", label="Both (double-pole 240 V)"),
                SelectOptionDict(value="unknown", label="Unknown"),
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _polarity_select() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(value="normal", label="Normal"),
                SelectOptionDict(value="inverted", label="Inverted"),
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _number_selector(low: int, high: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=low,
            max=high,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _watts_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="W",
        )
    )


def _count_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=1, max=200, step=1, mode=NumberSelectorMode.BOX)
    )


def _enum_select(values: tuple[str, ...], labels: dict[str, str] | None = None) -> SelectSelector:
    """Dropdown over a mirrored contract enum, with human labels where known."""
    options = [
        SelectOptionDict(value=value, label=(labels or {}).get(value, value.replace("_", " ")))
        for value in values
    ]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


_DEVICE_TYPE_LABELS = {
    "light": "Light",
    "light_group": "Light group / fixture",
    "fan": "Fan",
    "pump": "Pump",
    "hvac_heat": "Heating",
    "hvac_cool": "Cooling",
    "hvac_combined": "Combined HVAC",
    "water_heater": "Water heater",
    "appliance": "Appliance",
    "refrigeration": "Refrigeration",
    "ev_charger": "EV charger",
    "electronics": "Electronics",
    "socket": "Socket / plug",
    "motor": "Motor",
    "humidifier": "Humidifier",
    "dehumidifier": "Dehumidifier",
    "sauna": "Sauna",
    "hot_tub": "Hot tub",
    "other": "Other",
    "unknown": "Unknown",
}

_CONNECTIVITY_LABELS = {
    "connected": "Connected to Home Assistant",
    "not_connected": "Not connected (no HA entity)",
    "unknown": "Unknown",
}

_MONITORING_LABELS = {
    "dedicated": "Dedicated CT",
    "shared": "Shared circuit",
    "home_total_only": "Home total only",
    "unmonitored": "Unmonitored",
    "unknown": "Unknown (derive from topology)",
}

_BEHAVIOR_LABELS = {
    "fixed": "Fixed watts",
    "variable": "Variable (dimmed/speed)",
    "complex": "Complex controller",
    "unknown": "Unknown",
}

_LIGHT_TYPE_LABELS = {
    "led": "LED",
    "incandescent": "Incandescent",
    "halogen": "Halogen",
    "cfl": "CFL",
    "fluorescent": "Fluorescent",
    "smart_bulb": "Smart bulb",
    "unknown": "Unknown",
}


def _device_config_schema(*, energy_default: bool = True) -> vol.Schema:
    """Flat form for one device's configuration.

    Every numeric field is optional: an absent value means "not stated", and the
    engine must never read a blank as zero watts.

    ``energy_default`` is the *stored* per-device choice.  It must be honoured:
    the form always submits the checkbox, so showing an unconditional default of
    ``True`` would silently re-enable an entity the user deliberately turned off
    the moment they edited any other field on the same device.
    """
    return vol.Schema(
        {
            vol.Required("device_type", default="unknown"): _enum_select(
                DEVICE_TYPES, _DEVICE_TYPE_LABELS
            ),
            vol.Required("connectivity", default="unknown"): _enum_select(
                CONNECTIVITY_LEVELS, _CONNECTIVITY_LABELS
            ),
            vol.Required("monitoring", default="unknown"): _enum_select(
                MONITORING_LEVELS, _MONITORING_LABELS
            ),
            vol.Required("behavior", default="unknown"): _enum_select(
                POWER_BEHAVIORS, _BEHAVIOR_LABELS
            ),
            vol.Optional("state_entity_ids", default=""): TextSelector(),
            vol.Optional("mode_entity_ids", default=""): TextSelector(),
            vol.Optional("expected_on_w"): _watts_selector(),
            vol.Optional("expected_standby_w"): _watts_selector(),
            # Light detail is always shown but only sent when the device type
            # allows it; it is the strongest confidence lever for lamps.
            vol.Optional("bulb_count"): _count_selector(),
            vol.Optional("light_type", default="unknown"): _enum_select(
                LIGHT_TYPES, _LIGHT_TYPE_LABELS
            ),
            vol.Optional("watts_per_bulb_on"): _watts_selector(),
            vol.Optional("controller_mode_entity_id", default=""): TextSelector(),
            vol.Optional("controller_load_entity_ids", default=""): TextSelector(),
            # Integration-side: whether to create the kWh entity for this load.
            # Defaults on, matching powercalc's `create_energy_sensor`.
            vol.Optional(ENERGY_ENTITY_FIELD, default=energy_default): BooleanSelector(),
        }
    )


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SCAN_INTERVAL, default=defaults["scan_interval"]
            ): _number_selector(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL),
            vol.Required(
                CONF_STALE_AFTER, default=defaults["stale_after"]
            ): _number_selector(MIN_STALE_AFTER, MAX_STALE_AFTER),
            vol.Required(
                CONF_UNAVAILABLE_AFTER, default=defaults["unavailable_after"]
            ): _number_selector(MIN_UNAVAILABLE_AFTER, MAX_UNAVAILABLE_AFTER),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Settings plus the optional per-device configuration branch.

    The checkbox defaults to ``False``, so an options visit that does not tick it
    follows exactly the previous wizard.
    """
    fields: dict[Any, Any] = dict(_settings_schema(defaults).schema)
    fields[vol.Optional(CONF_CONFIGURE_DEVICE, default=False)] = BooleanSelector()
    return vol.Schema(fields)


def _connection_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL, default=defaults["base_url"]): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            # Never pre-fill the token: an empty value means "keep the existing
            # one" (reconfigure) or "no token" (setup).
            vol.Optional(CONF_API_TOKEN, default=""): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _device_options(devices: list[dict[str, Any]]) -> list[SelectOptionDict]:
    options = [SelectOptionDict(value="", label="— none —")]
    for device in devices:
        device_id = str(device.get("device_id"))
        label = f"{device.get('name') or device_id} ({device_id})"
        options.append(SelectOptionDict(value=device_id, label=label))
    return options


async def _probe_engine(
    hass: Any,
    base_url: str,
    api_token: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, str]]:
    """Validate connectivity and fetch topology + devices.

    Returns ``(topology, devices, errors)``.  ``errors`` uses the flow error
    keys from ``strings.json``; the token is never included.
    """
    client = EngineClient(
        async_get_clientsession(hass), base_url, api_token=api_token
    )
    try:
        health = await client.health()
        snapshot = await client.snapshot()
    except Exception as err:  # noqa: BLE001 - mapped to a flow error key
        key = describe_engine_error(err)
        _LOGGER.debug("WattMosaic connection probe failed: %s", key)
        return None, [], {"base": key}

    if not isinstance(health, dict) or not isinstance(snapshot, dict):
        return None, [], {"base": "invalid_response"}

    # Topology/devices enrich the wizard but are not required to connect: a
    # brand-new engine may not have a topology yet.
    try:
        topology = await client.topology()
    except Exception:  # noqa: BLE001
        topology = None
    try:
        devices = await client.devices()
    except Exception:  # noqa: BLE001
        devices = None

    return (
        topology if isinstance(topology, dict) else {},
        normalise_device_list(devices),
        {},
    )


# --------------------------------------------------------------------------- #
# Shared assignment wizard
# --------------------------------------------------------------------------- #


class _AssignmentWizard:
    """Channels -> mapping -> confirm, shared by setup and options.

    Subclasses provide ``_wizard_client()`` and ``_wizard_finish(...)``.
    """

    _topology: dict[str, Any]
    _channels: list[dict[str, Any]]
    _devices: list[dict[str, Any]]
    _assignments: dict[str, dict[str, Any]]
    _channel_index: int
    _skipped: set[str]
    _errors: dict[str, str]

    # -- hooks ------------------------------------------------------------- #

    def _wizard_client(self) -> EngineClient:  # pragma: no cover - abstract
        raise NotImplementedError

    def _wizard_finish(self, confirmed: bool, version: Any) -> ConfigFlowResult:
        raise NotImplementedError  # pragma: no cover - abstract

    # -- state ------------------------------------------------------------- #

    def _init_wizard(
        self,
        topology: dict[str, Any],
        devices: list[dict[str, Any]],
        assignments: dict[str, dict[str, Any]],
    ) -> None:
        self._topology = topology or {}
        self._channels = normalise_channels(self._topology)
        self._devices = devices or []
        self._assignments = assignments
        self._channel_index = 0
        self._skipped = set()
        self._errors = {}

    # -- step: channels ---------------------------------------------------- #

    def _current_channel(self) -> dict[str, Any] | None:
        if 0 <= self._channel_index < len(self._channels):
            return self._channels[self._channel_index]
        return None

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        channel = self._current_channel()
        if channel is None:
            return await self.async_step_mapping()

        ct_id = str(channel["ct_id"])

        if user_input is not None:
            merged = {
                **assignment_defaults(channel, self._assignments.get(ct_id)),
                **{
                    key: user_input[key]
                    for key in (
                        "role",
                        "phase",
                        "polarity",
                        "paired_ct_id",
                        "device_id",
                        "enabled",
                    )
                    if key in user_input
                },
            }
            self._assignments[ct_id] = normalise_assignment(merged, channel)
            self._channel_index += 1
            next_channel = self._current_channel()
            if next_channel is None:
                # All channels assigned: move on to mapping/confirmation.
                return await self.async_step_mapping()
            channel = next_channel
            ct_id = str(channel["ct_id"])

        defaults = assignment_defaults(channel, self._assignments.get(ct_id))
        pair_suggestion = suggested_pair(channel, self._channels) or ""

        pair_options = [SelectOptionDict(value="", label="— none —")]
        for other in self._channels:
            other_id = str(other.get("ct_id"))
            if other_id == ct_id:
                continue
            pair_options.append(
                SelectOptionDict(
                    value=other_id,
                    label=f"{other.get('name') or other_id} ({other_id})",
                )
            )

        schema = vol.Schema(
            {
                vol.Required("role", default=defaults["role"]): _role_select(),
                vol.Required("phase", default=defaults["phase"]): _phase_select(),
                vol.Required("polarity", default=defaults["polarity"]): _polarity_select(),
                vol.Optional(
                    "paired_ct_id", default=defaults["paired_ct_id"] or pair_suggestion
                ): SelectSelector(
                    SelectSelectorConfig(options=pair_options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("device_id", default=defaults["device_id"] or ""): SelectSelector(
                    SelectSelectorConfig(
                        options=_device_options(self._devices), mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional("enabled", default=defaults["enabled"]): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="channels",
            data_schema=schema,
            errors=self._errors,
            description_placeholders={
                "channel_name": str(channel.get("name") or ct_id),
                "channel_id": ct_id,
                "position": f"{self._channel_index + 1} / {len(self._channels)}",
                "entity_id": str(channel.get("entity_id") or ""),
                "pair_suggestion": pair_suggestion or "none",
            },
        )

    # -- step: mapping ----------------------------------------------------- #

    def _unmapped_channels(self) -> list[dict[str, Any]]:
        """Only dedicated CTs without a device need a mapping prompt."""
        unmapped: list[dict[str, Any]] = []
        for channel in self._channels:
            ct_id = str(channel["ct_id"])
            if ct_id in self._skipped:
                continue
            current = normalise_assignment(
                self._assignments.get(ct_id) or assignment_defaults(channel), channel
            )
            if current["role"] != CT_ROLE_DEDICATED:
                continue
            if current["device_id"] is None:
                unmapped.append(channel)
        return unmapped

    def _mapping_schema(self, labels: list[tuple[str, str]]) -> vol.Schema:
        options = [SelectOptionDict(value="", label="— no automatic candidate —")]
        options.extend(SelectOptionDict(value=value, label=label) for value, label in labels)
        return vol.Schema(
            {
                vol.Optional("candidate", default=""): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("manual_override", default=""): TextSelector(),
                vol.Optional("skip", default=False): BooleanSelector(),
            }
        )

    def _mapping_placeholders(
        self, channel: dict[str, Any], labels: list[tuple[str, str]]
    ) -> dict[str, str]:
        ct_id = str(channel.get("ct_id"))
        return {
            "channel_name": str(channel.get("name") or ct_id),
            "channel_id": ct_id,
            "candidate_count": str(len(labels)),
            "candidates": "\n".join(f"- {label}" for _, label in labels)
            or "No candidates found.",
        }

    def _candidate_choices(
        self, channel: dict[str, Any]
    ) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
        # Filter the panel/monitor prefix, which every channel shares.  Matching
        # on it would suggest the monitor itself for every circuit, and it is
        # derived from the channels so it works for any hardware.
        candidates = device_candidates(
            self._devices, channel, shared_tokens=panel_shared_tokens(self._channels)
        )
        return candidate_labels(candidates), candidates

    async def async_step_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        unmapped = self._unmapped_channels()
        if not unmapped:
            return await self.async_step_confirm()

        channel = unmapped[0]
        ct_id = str(channel["ct_id"])
        labels, _ = self._candidate_choices(channel)

        if user_input is not None:
            if user_input.get("skip"):
                self._skipped.add(ct_id)
                return await self.async_step_mapping()

            chosen = (user_input.get("manual_override") or "").strip() or (
                user_input.get("candidate") or ""
            )
            if not chosen:
                # Never invent a mapping; re-ask with an explanation.
                self._errors = {"base": "invalid_mapping"}
                return self.async_show_form(
                    step_id="mapping",
                    data_schema=self._mapping_schema(labels),
                    errors=self._errors,
                    description_placeholders=self._mapping_placeholders(channel, labels),
                )

            current = self._assignments.get(ct_id) or assignment_defaults(channel)
            self._assignments[ct_id] = normalise_assignment(
                {**current, "device_id": chosen}, channel
            )
            return await self.async_step_mapping()

        return self.async_show_form(
            step_id="mapping",
            data_schema=self._mapping_schema(labels),
            errors=self._errors,
            description_placeholders=self._mapping_placeholders(channel, labels),
        )

    # -- step: confirm ----------------------------------------------------- #

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = validate_assignments(self._assignments, self._channels)
        uncertain = uncertainty_summary(self._assignments)

        if user_input is not None:
            confirmed, error_key = resolve_confirmation(
                errors, uncertain, bool(user_input.get("confirm_uncertain"))
            )
            if error_key is not None:
                self._errors = {"base": error_key}
            elif confirmed is not None:
                version: Any = None
                try:
                    updated = build_topology_update(
                        self._topology,
                        self._assignments,
                        confirmed=confirmed,
                        notes=[
                            "Updated from the Home Assistant integration "
                            "configuration wizard."
                        ],
                    )
                    client = self._wizard_client()
                    await client.put_topology(updated)
                    if confirmed:
                        await client.confirm_topology(updated.get("version"))
                    version = updated.get("version")
                except Exception as err:  # noqa: BLE001
                    self._errors = {"base": describe_engine_error(err)}
                    _LOGGER.debug("WattMosaic topology update failed: %s", self._errors)
                else:
                    return self._wizard_finish(confirmed, version)

        schema = vol.Schema(
            {
                vol.Optional(
                    "summary",
                    default="\n".join(mapping_summary(self._channels, self._assignments)),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)),
                vol.Optional("confirm_uncertain", default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=schema,
            errors=self._errors,
            description_placeholders={
                "uncertain_count": str(len(uncertain)),
                "uncertain_list": "\n".join(uncertain) or "None",
                "validation_errors": ", ".join(errors) or "None",
            },
        )


# --------------------------------------------------------------------------- #
# Config flow
# --------------------------------------------------------------------------- #


class WattMosaicConfigFlow(_AssignmentWizard, ConfigFlow, domain=DOMAIN):
    """Full setup: connection, polling settings, CT assignment, confirmation."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_url: str = ""
        self._api_token: str | None = None
        self._settings: dict[str, int] = settings_defaults(None)
        self._init_wizard({}, [], {})

    # -- wizard hooks ------------------------------------------------------ #

    def _wizard_client(self) -> EngineClient:
        return EngineClient(
            async_get_clientsession(self.hass), self._base_url, api_token=self._api_token
        )

    def _wizard_finish(self, confirmed: bool, version: Any) -> ConfigFlowResult:
        data = {
            CONF_BASE_URL: self._base_url,
            CONF_API_TOKEN: self._api_token,
            **self._settings,
            CONF_CT_ASSIGNMENTS: assignment_store(self._assignments),
            CONF_TOPOLOGY_CONFIRMED: confirmed,
            CONF_TOPOLOGY_VERSION: version,
        }
        return self.async_create_entry(title=f"{NAME} ({self._base_url})", data=data)

    # -- step 1: connection ------------------------------------------------ #

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle App-install discovery from the Supervisor.

        The App lists ``wattmosaic`` under ``discovery:`` in its ``config.yaml``
        and POSTs its own host and port to the Supervisor.  Home Assistant then
        starts this flow with the app's name, slug and the announced config,
        which lets the user add the integration by confirming one screen —
        nothing to look up, nothing to type.

        The announced host is only a hint.  It is never trusted: the engine is
        actually probed before an entry is created, so a stale message (the app
        was uninstalled, or the Supervisor restarted while the engine was down)
        produces the normal ``cannot_connect`` error instead of a broken entry.
        """
        config = discovery_info.config if isinstance(discovery_info.config, dict) else {}
        base_url = discovered_base_url(config)
        if base_url is None:
            # The announcement carried no usable host. It is useless, but the
            # manual path still works, so hand over to it instead of dead-ending
            # the user in an abort with nothing to do.
            return await self.async_step_user()

        # One entry per engine URL.  The user step uses the same key, so a
        # discovery arriving after a manual setup aborts instead of duplicating.
        await self.async_set_unique_id(base_url)
        self._abort_if_unique_id_configured()

        self._base_url = base_url
        self._api_token = None
        self._settings = settings_defaults(None)
        self._init_wizard({}, [], {})
        self.context["title_placeholders"] = {
            "name": discovery_info.name or NAME,
            "url": base_url,
        }
        # Probe now: the confirmation step must state whether the engine actually
        # answered, not merely that an app announced itself.
        topology, devices, probe_errors = await _probe_engine(self.hass, base_url, None)
        self._probe_errors = probe_errors
        if not probe_errors:
            self._init_wizard(topology or {}, devices, {})
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One-click confirmation of a discovered engine."""
        errors = getattr(self, "_probe_errors", {})
        if user_input is not None:
            if not errors:
                # Nothing to assign means the engine has no topology yet; the
                # rest of the wizard is skipped exactly like the manual path.
                if not self._channels:
                    return self._wizard_finish(False, None)
                return await self.async_step_settings()
            # Let the user retry the probe without leaving the discovery flow.
            topology, devices, errors = await _probe_engine(
                self.hass, self._base_url, None
            )
            self._probe_errors = errors
            if not errors:
                self._init_wizard(topology or {}, devices, {})
                return await self.async_step_hassio_confirm()

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "url": self._base_url,
                "app_name": str(self.context.get("title_placeholders", {}).get("name") or NAME),
                "channel_count": str(len(self._channels)),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_connection(
                user_input.get(CONF_BASE_URL), user_input.get(CONF_API_TOKEN)
            )
            if not errors:
                base_url = normalise_base_url(user_input.get(CONF_BASE_URL))
                assert base_url is not None
                # Abort before touching the network if already configured.
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()

                token = (user_input.get(CONF_API_TOKEN) or "").strip() or None
                topology, devices, probe_errors = await _probe_engine(
                    self.hass, base_url, token
                )
                if probe_errors:
                    errors = probe_errors
                else:
                    self._base_url = base_url
                    self._api_token = token
                    self._init_wizard(topology or {}, devices, {})
                    return await self.async_step_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(connection_defaults(None)),
            errors=errors,
            description_placeholders={
                "token_status": redact_token_for_display(self._api_token),
            },
        )

    # -- step 2: polling settings ----------------------------------------- #

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_settings(
                user_input.get(CONF_SCAN_INTERVAL),
                user_input.get(CONF_STALE_AFTER),
                user_input.get(CONF_UNAVAILABLE_AFTER),
            )
            if not errors:
                self._settings = normalise_settings(user_input)
                if not self._channels:
                    # No CT channels on the engine yet: nothing to assign.
                    return self._wizard_finish(False, None)
                return await self.async_step_channels()

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(self._settings),
            errors=errors,
            description_placeholders={"channel_count": str(len(self._channels))},
        )

    # -- reconfigure ------------------------------------------------------- #

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_connection(
                user_input.get(CONF_BASE_URL), user_input.get(CONF_API_TOKEN)
            )
            if not errors:
                base_url = normalise_base_url(user_input.get(CONF_BASE_URL))
                assert base_url is not None
                # An empty field keeps the stored token (never echoed back).
                token = (user_input.get(CONF_API_TOKEN) or "").strip() or current.get(
                    CONF_API_TOKEN
                )
                _, _, probe_errors = await _probe_engine(self.hass, base_url, token)
                if probe_errors:
                    errors = probe_errors
                else:
                    # Allow changing the URL, but never collide with another
                    # entry (the current entry is excluded).
                    duplicate = any(
                        other.entry_id != entry.entry_id and other.unique_id == base_url
                        for other in self.hass.config_entries.async_entries(DOMAIN)
                    )
                    if duplicate:
                        errors = {"base": "already_configured"}
                    else:
                        return self.async_update_reload_and_abort(
                            entry,
                            unique_id=base_url,
                            data_updates={
                                CONF_BASE_URL: base_url,
                                CONF_API_TOKEN: token,
                            },
                        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(connection_defaults(current)),
            errors=errors,
            description_placeholders={
                "token_status": redact_token_for_display(current.get(CONF_API_TOKEN)),
            },
        )


# --------------------------------------------------------------------------- #
# Options flow
# --------------------------------------------------------------------------- #


class WattMosaicOptionsFlow(_AssignmentWizard, OptionsFlow):
    """Reconfigure polling and CT assignments without re-adding the entry."""

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        self._entry_arg = config_entry
        self._settings: dict[str, int] = settings_defaults(None)
        self._device_config_device_id: str = ""
        self._energy_entities: dict[str, bool] = {}
        self._init_wizard({}, [], {})

    # -- hooks ------------------------------------------------------------- #

    @property
    def _entry(self) -> ConfigEntry:
        entry = self._entry_arg
        if entry is None:
            # HA (>= 2024.11) injects ``config_entry`` on the flow object.
            entry = self.config_entry
        return entry  # type: ignore[return-value]

    def _config(self) -> dict[str, Any]:
        return {**self._entry.data, **self._entry.options}

    def _wizard_client(self) -> EngineClient:
        config = self._config()
        return EngineClient(
            async_get_clientsession(self.hass),
            config[CONF_BASE_URL],
            api_token=config.get(CONF_API_TOKEN),
        )

    def _wizard_finish(self, confirmed: bool, version: Any) -> ConfigFlowResult:
        return self.async_create_entry(
            title="",
            data={
                **self._settings,
                CONF_CT_ASSIGNMENTS: assignment_store(self._assignments),
                CONF_TOPOLOGY_CONFIRMED: confirmed,
                CONF_TOPOLOGY_VERSION: version,
                CONF_ENERGY_ENTITIES: self._merged_energy_choices(),
            },
        )

    # -- step: settings ---------------------------------------------------- #

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        config = self._config()
        stored = config.get(CONF_CT_ASSIGNMENTS) or {}
        assignments = {
            str(ct_id): normalise_assignment(raw)
            for ct_id, raw in stored.items()
            if isinstance(raw, dict)
        }
        self._settings = settings_defaults(config)
        self._errors = {}
        # Keep the stored assignments available to the optional device step so
        # finishing that branch cannot silently drop an existing CT mapping.
        self._assignments = assignments

        if user_input is not None:
            errors = validate_settings(
                user_input.get(CONF_SCAN_INTERVAL),
                user_input.get(CONF_STALE_AFTER),
                user_input.get(CONF_UNAVAILABLE_AFTER),
            )
            if errors:
                self._errors = errors
            else:
                self._settings = normalise_settings(user_input)
                if user_input.get(CONF_CONFIGURE_DEVICE):
                    # Optional branch: configure one device, then finish.
                    return await self.async_step_device_config()
                client = self._wizard_client()
                try:
                    topology = await client.topology()
                except Exception as err:  # noqa: BLE001
                    self._errors = {"base": describe_engine_error(err)}
                else:
                    try:
                        devices = await client.devices()
                    except Exception:  # noqa: BLE001
                        devices = []
                    self._init_wizard(
                        topology if isinstance(topology, dict) else {},
                        normalise_device_list(devices),
                        assignments,
                    )
                    if not self._channels:
                        return self._wizard_finish(False, None)
                    return await self.async_step_channels()

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self._settings),
            errors=self._errors,
            description_placeholders={"channel_count": str(len(self._channels))},
        )

    # -- step: per-device configuration ------------------------------------ #

    async def async_step_device_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the one device to configure.

        A dropdown of engine device ids plus a free-text id, because a device can
        exist in the engine before it appears in the list (or the list endpoint
        may be unavailable while the engine is otherwise healthy).
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            chosen = str(user_input.get("device_id") or "").strip() or str(
                user_input.get("custom_device_id") or ""
            ).strip()
            if not chosen:
                errors["base"] = "device_required"
            else:
                self._device_config_device_id = chosen
                return await self.async_step_device_config_details()

        devices: list[dict[str, Any]] = []
        try:
            devices = normalise_device_list(await self._wizard_client().devices())
        except Exception as err:  # noqa: BLE001 - the free-text id still works
            _LOGGER.debug("WattMosaic device list unavailable: %s", describe_engine_error(err))

        schema = vol.Schema(
            {
                vol.Optional("device_id", default=""): SelectSelector(
                    SelectSelectorConfig(
                        options=_device_options(devices), mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional("custom_device_id", default=""): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="device_config",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device_count": str(len(devices)),
                "device_ids": ", ".join(d["device_id"] for d in devices) or "none reported",
            },
        )

    async def async_step_device_config_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit one flat configuration form and send it as a command."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = validate_device_configuration(user_input)
            if not errors:
                parameters = normalise_device_configuration(user_input)
                choice = energy_entity_choice(user_input)
                if choice is not None:
                    self._energy_entities[self._device_config_device_id] = choice
                if not parameters:
                    # The form expressed no engine-side knowledge, only the
                    # local energy choice; there is nothing to send.
                    return self._device_config_finish()
                command_body = device_configuration_command(
                    self._device_config_device_id, parameters
                )
                try:
                    result = await self._wizard_client().command(
                        command_body["command"],
                        device_id=command_body["device_id"],
                        parameters=command_body["parameters"],
                    )
                except Exception as err:  # noqa: BLE001
                    errors = {"base": describe_engine_error(err)}
                else:
                    if isinstance(result, dict) and result.get("ok") is False:
                        # The engine refused the command; surface it, never
                        # pretend the configuration was stored.
                        self._errors = {
                            "base": "engine_rejected",
                        }
                        errors = self._errors
                    else:
                        return self._device_config_finish()

        return self.async_show_form(
            step_id="device_config_details",
            data_schema=_device_config_schema(
                energy_default=self._stored_energy_choice()
            ),
            errors=errors,
            description_placeholders={
                "device_id": self._device_config_device_id,
            },
        )

    def _device_config_finish(self) -> ConfigFlowResult:
        """Persist options without disturbing the stored topology bookkeeping."""
        config = self._config()
        return self.async_create_entry(
            title="",
            data={
                **self._settings,
                CONF_CT_ASSIGNMENTS: assignment_store(self._assignments),
                CONF_TOPOLOGY_CONFIRMED: config.get(CONF_TOPOLOGY_CONFIRMED, False),
                CONF_TOPOLOGY_VERSION: config.get(CONF_TOPOLOGY_VERSION),
                CONF_ENERGY_ENTITIES: self._merged_energy_choices(),
            },
        )

    def _merged_energy_choices(self) -> dict[str, bool]:
        """The full opt-in map, for ``async_create_entry``.

        Home Assistant replaces an entry's ``options`` wholesale
        (``OptionsFlowManager.async_finish_flow`` calls
        ``async_update_entry(options=result["data"])``), so anything missing
        from the submitted data is destroyed.  Every completion path must
        therefore rebuild the whole map: the stored choices, coerced through the
        shared rule, with this flow's edits applied.

        Dropping this from either path would make an unrelated save — say, a new
        scan interval — silently re-enable every energy entity a user turned off.
        """
        stored = self._config().get(CONF_ENERGY_ENTITIES)
        merged = {
            str(device_id): stored_energy_choice(stored, str(device_id))
            for device_id in (stored if isinstance(stored, Mapping) else {})
        }
        merged.update(self._energy_entities)
        return merged

    def _stored_energy_choice(self) -> bool:
        """The already-stored energy decision for the device being edited."""
        return stored_energy_choice(
            self._config().get(CONF_ENERGY_ENTITIES), self._device_config_device_id
        )


@callback
def async_get_options_flow(config_entry: ConfigEntry) -> WattMosaicOptionsFlow:
    return WattMosaicOptionsFlow(config_entry)
