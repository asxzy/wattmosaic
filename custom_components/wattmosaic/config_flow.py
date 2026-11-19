"""Config and options flows for WattMosaic (v3 setup path).

The flows are intentionally thin: all validation/merging logic lives in
``flow_logic.py`` (pure Python, unit-tested), and all HTTP lives in
``api_client.py``.  This file only wires Home Assistant's flow API to those
two building blocks.

Wizard order (identical for setup and options):

1. ``user`` / ``hassio``  – engine URL + token (setup), or settings (options)
2. ``settings``           – polling / freshness (setup only)
3. ``endpoints``          – discovered endpoints: optional user-declared parents
   (CTs) and single-subject scope (candidate plugs)
4. ``done``               – commit the plan: ``POST /topology/edges`` and
   ``POST /endpoints/{id}/scope`` only

Devices are automatic (DEC-04), so there are deliberately no per-device forms
and no per-CT channel wizard.  Engine-inferred parents are shown as *suggestions*
and are written only when the user accepts them (DEC-13); nothing is confirmed
silently.  No v2 topology PUT or blanket topology-confirmation POST is issued
by this setup flow.
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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api_client import EngineClient, describe_engine_error, redact_token_for_display
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    CONF_UNAVAILABLE_AFTER,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MAX_STALE_AFTER,
    MAX_UNAVAILABLE_AFTER,
    MIN_SCAN_INTERVAL,
    MIN_STALE_AFTER,
    MIN_UNAVAILABLE_AFTER,
    NAME,
)
from .flow_logic import (
    build_endpoint_setup_plan,
    connection_defaults,
    discovered_base_url,
    endpoint_choices,
    normalise_endpoints,
    normalise_settings,
    settings_defaults,
    suggested_parents,
    validate_connection,
    validate_settings,
)
from .util import normalise_base_url

_LOGGER = logging.getLogger(__name__)


async def _v3_post(client: EngineClient, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """POST ``body`` to a v3 path through the engine client.

    ``EngineClient`` deliberately exposes only named endpoint methods, so the
    setup flow uses its generic request primitive here.  This keeps the token
    and contract headers (and error scrubbing) in one place and lets the flow
    write v3 edges / scope confirmations without a new client method (which is
    owned by a different task).
    """
    return await client._request("POST", path, json_body=dict(body), expect=(dict, list))


# --------------------------------------------------------------------------- #
# Selectors / schemas
# --------------------------------------------------------------------------- #


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


async def _probe_engine(
    hass: Any,
    base_url: str,
    api_token: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate connectivity to the engine.

    Returns ``(topology, errors)``.  ``errors`` uses the flow error keys from
    ``strings.json``; the token is never included.  The topology read is
    best-effort: a brand-new engine may not have a topology yet, and the v3
    endpoints the wizard edits are fetched separately by ``_wizard_fetch_v3``.
    """
    client = EngineClient(async_get_clientsession(hass), base_url, api_token=api_token)
    try:
        health = await client.health()
        snapshot = await client.snapshot()
    except Exception as err:  # noqa: BLE001 - mapped to a flow error key
        key = describe_engine_error(err)
        _LOGGER.debug("WattMosaic connection probe failed: %s", key)
        return {}, {"base": key}

    if not isinstance(health, dict) or not isinstance(snapshot, dict):
        return {}, {"base": "invalid_response"}

    topology: Any = None
    try:
        topology = await client.topology()
    except Exception:  # noqa: BLE001 - never block connection on this
        topology = None
    # The confirmation screen reports how many measurement endpoints (CT
    # channels and metering plugs) the engine already knows.
    endpoints: Any = None
    try:
        endpoints = await client._request("GET", "/endpoints", expect=(dict, list))
    except Exception:  # noqa: BLE001 - never block connection on this
        endpoints = None
    result = dict(topology) if isinstance(topology, dict) else {}
    result["endpoint_count"] = len(normalise_endpoints(endpoints))
    return result, {}


# --------------------------------------------------------------------------- #
# Shared endpoint wizard
# --------------------------------------------------------------------------- #


class _EndpointWizard:
    """Endpoints -> done, shared by setup and options.

    Subclasses provide ``_wizard_client()`` and ``_wizard_finish(...)``.
    ``_endpoints``, ``_parent_suggestions`` and ``_endpoint_choices`` are the
    entire v3 state: there are no CT assignments or device configurations.
    """

    _endpoints: list[dict[str, Any]]
    _parent_suggestions: dict[str, list[str]]
    _endpoint_choices: dict[str, dict[str, Any]]
    _errors: dict[str, str]

    # -- hooks ------------------------------------------------------------- #

    def _wizard_client(self) -> EngineClient:  # pragma: no cover - abstract
        raise NotImplementedError

    def _wizard_finish(self, confirmed: bool, version: Any) -> ConfigFlowResult:
        raise NotImplementedError  # pragma: no cover - abstract

    async def _wizard_fetch_v3(self, client: EngineClient) -> bool:
        """Fetch v3 endpoints + topology suggestions.  True when usable.

        The endpoint read is required for the v3 flow; topology edges are
        best-effort and only feed the parent *suggestions* the user may accept.
        """
        endpoints: Any = None
        topology: Any = None
        try:
            endpoints = await client._request("GET", "/endpoints", expect=(dict, list))
        except Exception:  # noqa: BLE001 - no endpoints means nothing to edit
            endpoints = None
        try:
            topology = await client.topology()
        except Exception:  # noqa: BLE001
            topology = None
        self._endpoints = normalise_endpoints(endpoints)
        self._parent_suggestions = suggested_parents(
            topology.get("edges") or [] if isinstance(topology, Mapping) else []
        )
        self._endpoint_choices = {}
        return bool(self._endpoints)

    # -- state ------------------------------------------------------------- #

    def _init_wizard(self) -> None:
        self._endpoints = []
        self._parent_suggestions = {}
        self._endpoint_choices = {}
        self._errors = {}

    # -- step: endpoints --------------------------------------------------- #

    async def async_step_endpoints(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List discovered endpoints and capture user-declared structure.

        For each CT the user may pick a parent; an engine PROVISIONAL parent is
        preselected *as a suggestion* and must be left checked to be accepted as
        a user edge.  For each candidate plug the user may say which single
        device it measures.  Nothing is written here — the done step commits the
        plan (DEC-13).
        """
        # ``user_input`` is the flat form: keys are ``parent::<id>``,
        # ``accept::<id>`` and ``scope::<id>``.  Fold them back into the nested
        # per-endpoint shape the pure helpers consume.
        submitted: dict[str, dict[str, Any]] = {}
        if user_input is not None:
            for endpoint in self._endpoints:
                eid = str(endpoint["endpoint_id"])
                entry: dict[str, Any] = {}
                if f"parent::{eid}" in user_input:
                    entry["parent_endpoint_id"] = user_input[f"parent::{eid}"]
                if f"accept::{eid}" in user_input:
                    entry["parent_accepted"] = bool(user_input[f"accept::{eid}"])
                elif f"parent::{eid}" in user_input:
                    # A user explicitly typed a parent: that is a declaration.
                    entry["parent_accepted"] = bool(user_input[f"parent::{eid}"])
                if f"scope::{eid}" in user_input:
                    entry["scope_subject_id"] = user_input[f"scope::{eid}"]
                submitted[eid] = entry

        self._endpoint_choices = endpoint_choices(
            self._endpoints, submitted, suggestions=self._parent_suggestions
        )
        if user_input is not None:
            return await self.async_step_done()

        # One form for all endpoints, with a stable field per endpoint id.  A
        # parent select carries the engine suggestion as its default so the user
        # can leave it checked to accept it as a user edge (DEC-13); a plug gets
        # a "which device does it measure" select, defaulted to the discovery
        # proposal.  Neither is written until the done step builds the plan.
        fields: dict[Any, Any] = {}
        rows: list[str] = []
        for endpoint in self._endpoints:
            eid = str(endpoint["endpoint_id"])
            choice = self._endpoint_choices.get(eid, {})
            label = str(endpoint.get("display_name") or eid)
            if endpoint.get("kind") in ("smart_plug", "appliance_monitor"):
                subject = choice.get("suggested_scope_subject") or ""
                rows.append(f"{label} [{eid}]: measures one appliance? suggested={subject or 'none'}")
                fields[vol.Optional(f"scope::{eid}", default=str(subject))] = TextSelector()
            elif str(choice.get("suggested_parent") or ""):
                parent = str(choice["suggested_parent"])
                rows.append(f"{label} [{eid}]: suggested parent {parent}")
                fields[vol.Optional(f"accept::{eid}", default=False)] = BooleanSelector()
                fields[vol.Optional(f"parent::{eid}", default=parent)] = TextSelector()
            else:
                rows.append(f"{label} [{eid}]: optional parent (none suggested)")
                fields[vol.Optional(f"parent::{eid}", default="")] = TextSelector()

        schema = vol.Schema(fields) if fields else vol.Schema({})
        return self.async_show_form(
            step_id="endpoints",
            data_schema=schema,
            errors=self._errors,
            description_placeholders={
                "endpoint_count": str(len(self._endpoints)),
                "endpoint_rows": "\n".join(rows) or "None",
            },
        )

    async def async_step_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Commit the v3 write plan: user edges, scope, then entity links.

        Only user-declared CONTAINS edges and accepted plug scopes are written.
        There is deliberately no v2 topology PUT and no blanket
        topology-confirmation POST here: a declared parent is authoritative
        (DEC-10) and an inferred parent is never written as a declaration unless
        the user accepted it (DEC-13).  A plug scope is confirmed only when the
        user selected a device.
        """
        plan = build_endpoint_setup_plan(self._endpoint_choices)
        client = self._wizard_client()
        try:
            for item in plan:
                await _v3_post(client, str(item["path"]), dict(item["body"]))
        except Exception as err:  # noqa: BLE001
            self._errors = {"base": describe_engine_error(err)}
            _LOGGER.debug("WattMosaic v3 endpoint setup failed: %s", self._errors)
            return self.async_show_form(
                step_id="done",
                data_schema=vol.Schema({}),
                errors=self._errors,
                description_placeholders={"write_count": str(len(plan))},
            )
        return self._wizard_finish(False, None)


# --------------------------------------------------------------------------- #
# Config flow
# --------------------------------------------------------------------------- #


class WattMosaicConfigFlow(_EndpointWizard, ConfigFlow, domain=DOMAIN):
    """Full setup: connection, polling settings, endpoints, done."""

    VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WattMosaicOptionsFlow:
        """Return the options flow for an existing entry.

        This *must* be a method on the config-flow class: Home Assistant's
        ``OptionsFlowManager.async_create_flow`` calls
        ``handler.async_get_options_flow(entry)`` on the registered handler and
        ``ConfigFlow.async_supports_options_flow`` decides ``supports_options``
        by comparing ``cls.async_get_options_flow`` with the base method.
        """
        return WattMosaicOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._base_url: str = ""
        self._api_token: str | None = None
        self._settings: dict[str, int] = settings_defaults(None)
        self._init_wizard()

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
        }
        return self.async_create_entry(title=f"{NAME} ({self._base_url})", data=data)

    # -- step 1: connection ------------------------------------------------ #

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle App-install discovery from the Supervisor.

        The announced host is only a hint: the engine is probed before an entry
        is created, so a stale message produces the normal connection error
        instead of a broken entry.
        """
        config = discovery_info.config if isinstance(discovery_info.config, dict) else {}
        base_url = discovered_base_url(config)
        if base_url is None:
            # The announcement carried no usable host; hand over to the manual
            # path instead of dead-ending the user in an abort.
            return await self.async_step_user()

        # One entry per engine URL.  The user step uses the same key.
        await self.async_set_unique_id(base_url)
        self._abort_if_unique_id_configured()

        self._base_url = base_url
        self._api_token = None
        self._settings = settings_defaults(None)
        self._init_wizard()
        self.context["title_placeholders"] = {
            "name": discovery_info.name or NAME,
            "url": base_url,
        }
        # Probe now: the confirmation step must state whether the engine actually
        # answered, not merely that an app announced itself.
        topology, probe_errors = await _probe_engine(self.hass, base_url, None)
        self._probe_errors = probe_errors
        self._probe_channels = int((topology or {}).get("endpoint_count") or 0)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One-click confirmation of a discovered engine."""
        errors = getattr(self, "_probe_errors", {})
        if user_input is not None:
            if not errors:
                return await self.async_step_settings()
            # Let the user retry the probe without leaving the discovery flow.
            topology, errors = await _probe_engine(self.hass, self._base_url, None)
            self._probe_errors = errors
            self._probe_channels = int((topology or {}).get("endpoint_count") or 0)
            if not errors:
                return await self.async_step_hassio_confirm()

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "url": self._base_url,
                "app_name": str(self.context.get("title_placeholders", {}).get("name") or NAME),
                "channel_count": str(getattr(self, "_probe_channels", 0)),
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
                _, probe_errors = await _probe_engine(self.hass, base_url, token)
                if probe_errors:
                    errors = probe_errors
                else:
                    self._base_url = base_url
                    self._api_token = token
                    self._init_wizard()
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
                await self._wizard_fetch_v3(self._wizard_client())
                if not self._endpoints:
                    # No endpoints to edit: nothing to write.
                    return self._wizard_finish(False, None)
                return await self.async_step_endpoints()

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(self._settings),
            errors=errors,
            description_placeholders={"endpoint_count": str(len(self._endpoints))},
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
                _, probe_errors = await _probe_engine(self.hass, base_url, token)
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


class WattMosaicOptionsFlow(_EndpointWizard, OptionsFlow):
    """Edit polling settings and the endpoint/parent declarations only.

    Devices are automatic (DEC-04); there is no per-device configuration step
    and no per-CT channel wizard.  Every key the endpoint editor does not own is
    preserved in ``options`` byte for byte, because Home Assistant replaces the
    whole options mapping on save.
    """

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        self._entry_arg = config_entry
        self._settings: dict[str, int] = settings_defaults(None)
        self._init_wizard()

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
        # Preserve every unrelated option (energy choices, device links, the
        # migration's v3 endpoint plan, ...) rather than dropping them: HA
        # replaces options wholesale, so omission would silently delete them.
        config = self._config()
        return self.async_create_entry(title="", data={**config, **self._settings})

    # -- step: settings + endpoints ---------------------------------------- #

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        config = self._config()
        self._settings = settings_defaults(config)
        self._init_wizard()

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
                try:
                    await self._wizard_fetch_v3(self._wizard_client())
                except Exception as err:  # noqa: BLE001 - report, never crash
                    self._errors = {"base": describe_engine_error(err)}
                else:
                    if not self._endpoints:
                        return self._wizard_finish(False, None)
                    return await self.async_step_endpoints()

        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(self._settings),
            errors=self._errors,
            description_placeholders={"endpoint_count": str(len(self._endpoints))},
        )
