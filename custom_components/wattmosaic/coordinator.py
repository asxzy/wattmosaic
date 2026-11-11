"""DataUpdateCoordinator for WattMosaic.

Imports Home Assistant, so this module is *not* imported by the test suite
(``homeassistant`` is not installed on the development machine).  All of the
decision logic it relies on lives in :mod:`custom_components.wattmosaic.util`,
which is pure Python and fully tested.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import EngineClient, EngineError
from .const import (
    CONF_CT_ASSIGNMENTS,
    CONF_DEVICE_LINKS,
    CONF_ENERGY_ENTITIES,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    CONF_TOPOLOGY_CONFIRMED,
    CONF_TOPOLOGY_VERSION,
    CONF_UNAVAILABLE_AFTER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_AFTER,
    DEFAULT_UNAVAILABLE_AFTER,
    DOMAIN,
)
from .entity_values import explicit_device_link, merge_energy_choice
from .flow_logic import TOPOLOGY_PUSH_TIMEOUT, TopologyPushState, async_replay_topology
from .util import (
    coerce_snapshot,
    device_entities_available,
    is_engine_unavailable,
    sanitize_text,
    utcnow,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class WattMosaicData:
    """Normalised engine state used by every entity."""

    snapshot: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    received_at: datetime | None = None
    last_success: datetime | None = None
    engine_available: bool = False
    last_error: str | None = None
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return self.snapshot.get("results_by_id", {})

    @property
    def reconciliation(self) -> dict[str, Any]:
        return self.snapshot.get("reconciliation", {})

    @property
    def quality(self) -> dict[str, Any] | None:
        return self.snapshot.get("quality")

    def device(self, device_id: str) -> dict[str, Any] | None:
        return self.results.get(device_id)

    def device_name(self, device_id: str) -> str:
        meta = self.devices.get(device_id) or {}
        name = meta.get("name") or meta.get("friendly_name")
        return str(name) if name else device_id

    def device_area(self, device_id: str) -> str | None:
        meta = self.devices.get(device_id) or {}
        area = meta.get("area_id")
        return str(area) if isinstance(area, str) and area else None


class WattMosaicCoordinator(DataUpdateCoordinator[WattMosaicData]):
    """Polls ``/snapshot`` (every cycle) and ``/health`` (every cycle).

    Failure semantics
    -----------------
    * A malformed body never raises: :func:`coerce_snapshot` degrades it.
    * A transient network error inside ``unavailable_after`` keeps the last
      snapshot but marks it stale (entities that publish *current* values go
      ``unavailable`` while energy/status entities keep their last total).
    * Beyond ``unavailable_after`` the coordinator raises ``UpdateFailed`` so
      HA marks the whole entry as not ready.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EngineClient,
    ) -> None:
        config = {**entry.data, **entry.options}
        self.scan_interval = int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self.stale_after = int(config.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER))
        self.unavailable_after = int(
            config.get(CONF_UNAVAILABLE_AFTER, DEFAULT_UNAVAILABLE_AFTER)
        )
        self.client = client
        self.entry = entry
        self._last: WattMosaicData = WattMosaicData()
        self._device_meta: dict[str, dict[str, Any]] = {}
        self._poll_count: int = 0
        self.topology_push = TopologyPushState()
        #: Set once ``async_setup_entry`` has run the first replay check.  Polls
        #: before that (the first refresh) must not push: setup owns that call,
        #: and a write must not be able to delay or fail platform setup.
        self._topology_checked_once = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.scan_interval),
            config_entry=entry,
        )

    # -- device metadata ---------------------------------------------------- #

    @property
    def device_meta(self) -> dict[str, dict[str, Any]]:
        return self._device_meta

    @property
    def device_names(self) -> dict[str, str]:
        return {
            device_id: str(meta.get("name") or meta.get("friendly_name") or device_id)
            for device_id, meta in self._device_meta.items()
        }

    @property
    def device_areas(self) -> dict[str, str]:
        return {
            device_id: str(meta["area_id"])
            for device_id, meta in self._device_meta.items()
            if isinstance(meta.get("area_id"), str) and meta["area_id"]
        }

    def device_configuration(self, device_id: str) -> dict[str, Any] | None:
        """The engine's stated configuration for one device, if any.

        ``None`` means "the engine said nothing", which is distinct from "the
        engine said nothing is set": every opt-in decision treats missing
        configuration as "use the default", never as "disable".

        The energy opt-in is stored in this config entry rather than the engine,
        because it is a Home Assistant concern; it is layered on top of whatever
        the engine reported.
        """
        meta = self._device_meta.get(device_id) or {}
        return merge_energy_choice(
            meta.get("configuration"),
            self.entry.options.get(CONF_ENERGY_ENTITIES),
            device_id,
        )

    def explicit_device_link(self, device_id: str) -> str | None:
        """The Home Assistant device the user linked to this engine device.

        A declaration in the config entry, not something the engine knows: the
        engine has no concept of a Home Assistant device id, and sending one over
        the API would make a placement decision look like electrical knowledge.
        """
        return explicit_device_link(
            {**self.entry.data, **self.entry.options}.get(CONF_DEVICE_LINKS),
            device_id,
        )

    def device_id_for_identifier(
        self, identifier: tuple[str, str]
    ) -> str | None:
        """The HA device id this integration registered for ``identifier``.

        Used to move a measurement entity back onto its own WattMosaic device
        when no real device resolves any more.  Looked up from the registry
        rather than remembered, because the id is Home Assistant's, not ours.
        """
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(self.hass)
        # ``registry.devices`` is iterated, not used as a mapping: the mapping
        # view is deprecated for custom integrations (see ``__init__.py``).
        for device in registry.devices:
            if identifier in device.identifiers:
                return device.id
        return None

    # -- availability ------------------------------------------------------- #

    @property
    def engine_available(self) -> bool:
        """True when the last successful poll is within ``unavailable_after``."""
        return not is_engine_unavailable(
            self._last.last_success if self._last else None,
            unavailable_after=self.unavailable_after,
        )

    @property
    def last_success(self) -> datetime | None:
        """Timestamp of the last successful poll (survives ``UpdateFailed``)."""
        return self._last.last_success

    @property
    def last_error(self) -> str | None:
        """Most recent error text (secrets already scrubbed by the client)."""
        return self._last.last_error

    # -- topology replay ----------------------------------------------------- #

    async def async_ensure_topology_pushed(self) -> TopologyPushState:
        """Replay the stored topology declaration onto the engine if needed.

        The config entry is the *only* record of the user's confirmed CT
        nesting/aliasing: the engine's topology is a derived table that a
        database reset or a discovery bootstrap can silently replace with a
        parentless candidate.  This replays the declaration, and it is safe to
        call on every poll because :func:`flow_logic.async_replay_topology`
        decides whether anything must be written.

        Never raises: a replay failure is recorded on :attr:`topology_push` and
        retried on the next poll.  Setup and entity availability must not depend
        on the engine accepting a write.
        """
        config = {**self.entry.data, **self.entry.options}
        assignments = config.get(CONF_CT_ASSIGNMENTS) or {}
        if not isinstance(assignments, Mapping) or not assignments:
            # A fresh install has no declaration.  Never fabricate one.
            self.topology_push.reason = "no_assignments"
            self._topology_checked_once = True
            return self.topology_push

        observed = None
        if self._last is not None:
            observed = (self._last.reconciliation or {}).get("topology_version")

        # ``async_replay_topology`` catches every engine error itself; the
        # ``except`` here only covers a programming error so a poll can never
        # die because of the audit path.
        try:
            self.topology_push = await asyncio.wait_for(
                async_replay_topology(
                    self.client,
                    assignments,
                    confirmed=bool(config.get(CONF_TOPOLOGY_CONFIRMED)),
                    stored_version=config.get(CONF_TOPOLOGY_VERSION),
                    state=self.topology_push,
                    observed_version=observed,
                ),
                timeout=TOPOLOGY_PUSH_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001 - a replay must never break setup
            self.topology_push.error = sanitize_text(err) or "topology replay failed"
            _LOGGER.debug("WattMosaic topology replay did not complete", exc_info=True)
        self._topology_checked_once = True
        return self.topology_push

    @property
    def topology_push_diagnostics(self) -> dict[str, Any]:
        """Secret-free audit trail of the last topology replay."""
        return self.topology_push.as_diagnostics()

    @property
    def topology_pushed(self) -> bool:
        """Whether the stored declaration has been accepted by the engine."""
        return self.topology_push.pushed

    def device_result(self, device_id: str) -> dict[str, Any] | None:
        if self.data is None:
            return None
        return self.data.device(device_id)

    def device_available(self, device_id: str, *, now: datetime | None = None) -> bool:
        """Whether a per-device *current measurement* may be published."""
        result = self.device_result(device_id)
        return device_entities_available(
            result_present=result is not None,
            result_timestamp=result.get("timestamp") if result else None,
            quality_status=result.get("quality_status") if result else None,
            engine_available=self.engine_available,
            now=now,
            stale_after=self.stale_after,
        )

    def summary_stale(self, *, now: datetime | None = None) -> bool:
        """Global measure of whether the snapshot itself is old."""
        from .util import is_stale

        if self.data is None:
            return True
        return is_stale(self.data.received_at, now=now, stale_after=self.stale_after)

    # -- update ------------------------------------------------------------- #

    async def _async_update_data(self) -> WattMosaicData:
        snapshot_error: Exception | None = None
        health: dict[str, Any] = {}
        snapshot_raw: Any = None

        snapshot_result, health_result = await asyncio.gather(
            self.client.snapshot(),
            self.client.health(),
            return_exceptions=True,
        )

        if isinstance(health_result, Exception):
            _LOGGER.debug("WattMosaic health probe failed: %s", health_result)
        elif isinstance(health_result, dict):
            health = health_result

        if isinstance(snapshot_result, Exception):
            snapshot_error = snapshot_result
        else:
            snapshot_raw = snapshot_result

        now = utcnow()

        if snapshot_raw is None:
            error_text = _describe(snapshot_error)
            if is_engine_unavailable(self._last.last_success, now=now, unavailable_after=self.unavailable_after):
                self._last = WattMosaicData(
                    snapshot=self._last.snapshot,
                    health=health,
                    received_at=self._last.received_at,
                    last_success=self._last.last_success,
                    engine_available=False,
                    last_error=error_text,
                    devices=self._device_meta,
                )
                raise UpdateFailed(f"engine unavailable: {error_text}")
            # Transient: keep the previous data, but do not refresh timestamps
            # so staleness logic naturally kicks in for current-value entities.
            self._last = WattMosaicData(
                snapshot=self._last.snapshot,
                health=health or self._last.health,
                received_at=self._last.received_at,
                last_success=self._last.last_success,
                engine_available=True,
                last_error=error_text,
                devices=self._device_meta,
            )
            _LOGGER.warning("WattMosaic snapshot poll failed (using cached data): %s", error_text)
            return self._last

        snapshot = coerce_snapshot(snapshot_raw)

        # Device metadata (names/areas) changes rarely; refresh it periodically
        # rather than on every poll, and never let it break an update.
        self._poll_count += 1
        if self._poll_count == 1 or self._poll_count % 20 == 0:
            try:
                from .flow_logic import normalise_device_list

                raw_devices = await self.client.devices()
                self._device_meta = {
                    item["device_id"]: item for item in normalise_device_list(raw_devices)
                }
            except Exception:  # noqa: BLE001 - metadata only
                _LOGGER.debug("WattMosaic device metadata refresh failed", exc_info=True)

        received = snapshot.get("timestamp") or now
        self._last = WattMosaicData(
            snapshot=snapshot,
            health=health,
            received_at=received,
            last_success=now,
            engine_available=True,
            last_error=None,
            devices=self._device_meta,
        )

        # Re-check the stored topology declaration against the revision the
        # engine just reported.  Skipped on the very first refresh: setup runs
        # that check itself, after a successful poll, so a write can never
        # delay or fail platform setup.  Subsequent polls re-check, which is a
        # no-op unless the engine's version changed, a previous push failed, or
        # the declaration differs.  A failure is recorded for diagnostics and
        # retried next poll: it must never turn a good snapshot into
        # `UpdateFailed`.
        if self._topology_checked_once:
            try:
                await self.async_ensure_topology_pushed()
            except Exception:  # noqa: BLE001 - replay must never break the poll
                _LOGGER.debug("WattMosaic topology replay check failed", exc_info=True)

        return self._last

    # -- service plumbing --------------------------------------------------- #

    async def async_send_command(self, command: str, **kwargs: Any) -> dict[str, Any]:
        """Forward a service command and request a refresh afterwards."""
        result = await self.client.command(command, **kwargs)
        await self.async_request_refresh()
        return result


def _describe(error: Exception | None) -> str:
    if error is None:
        return "unknown error"
    if isinstance(error, EngineError):
        return str(error)
    return f"{type(error).__name__}: {error}"
