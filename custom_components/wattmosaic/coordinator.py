"""DataUpdateCoordinator for WattMosaic.

Imports Home Assistant, so this module is *not* imported by the test suite
(``homeassistant`` is not installed on the development machine).  All of the
decision logic it relies on lives in :mod:`custom_components.wattmosaic.util`,
which is pure Python and fully tested.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import EngineClient, EngineError
from .const import (
    CONF_DEVICE_LINKS,
    CONF_ENERGY_ENTITIES,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    CONF_UNAVAILABLE_AFTER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_AFTER,
    DEFAULT_UNAVAILABLE_AFTER,
    DOMAIN,
)
from .entity_values import explicit_device_link, merge_energy_choice
from .state_links import (
    LinkResolution,
    companion_config_changes,
    companion_config_fields,
    registry_device_ids,
    registry_rows,
    resolve_links,
)
from .util import (
    coerce_v3_snapshot,
    device_entities_available,
    is_engine_unavailable,
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
    #: Contract-3.0 group estimates, keyed by stable ``group_id`` (H1 step 4).
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: When a group was last seen, so a disappeared group can stay available
    #: (but valueless) for 7 days instead of being deleted.
    group_last_seen: dict[str, datetime] = field(default_factory=dict)

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return self.snapshot.get("results_by_id", {})

    @property
    def group_estimates(self) -> dict[str, dict[str, Any]]:
        return self.groups

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
    * A malformed body never raises: :func:`coerce_v3_snapshot` degrades it.
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
        self._group_last_seen: dict[str, datetime] = {}
        self._poll_count: int = 0
        self._companion_last_sync: datetime | None = None

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

    # -- device links -------------------------------------------------------- #

    def resolve_state_links(
        self, engine_devices: Iterable[str] | None = None
    ) -> LinkResolution:
        """Turn ``CONF_DEVICE_LINKS`` into explicit per-device state entity ids.

        This is the *only* place the Home Assistant entity registry is consulted
        for learning evidence, and it is deliberately in the integration: the
        engine may run outside Home Assistant and must never import
        ``homeassistant.helpers``.  Only the resolved entity ids cross the
        boundary.

        ``engine_devices`` is the engine's device id list; it defaults to the
        device metadata this coordinator already polls from ``GET /devices``,
        so a link to a device the engine does not know is *reported* rather
        than silently dropped.  Measurement entities are refused on the
        registry's own attributes (domain, device class, unit).
        """
        links = {**self.entry.data, **self.entry.options}.get(CONF_DEVICE_LINKS)
        if not isinstance(links, Mapping) or not links:
            return LinkResolution()

        rows: list[dict[str, Any]] = []
        known_registry: list[str] | None = None
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            rows = registry_rows(registry, entries_for_device=er.async_entries_for_device)
            known_registry = registry_device_ids(registry)
        except Exception:  # noqa: BLE001 - a registry hiccup must not block setup
            # Resolution without the registry still accepts entity-id links; a
            # device id then resolves to nothing and is *reported* as such
            # rather than silently dropped.
            _LOGGER.debug("WattMosaic entity registry read failed", exc_info=True)

        return resolve_links(
            links,
            entities=rows,
            engine_devices=(
                list(engine_devices) if engine_devices is not None else list(self._device_meta)
            ),
            registry_device_ids=known_registry,
        )

    async def sync_companion_entities(self, *, force: bool = False) -> int:
        """Declare same-device V/I/PF entity ids for CT endpoints (ADR-0044).

        The engine never reads Home Assistant's registries (ADR-0028), so this
        integration resolves the companion entity ids from the entity registry —
        exactly as :meth:`resolve_state_links` does — and sends only plain entity
        ids through the endpoint config API.  Registry access lives here, in the
        integration; :func:`state_links.companion_config_changes` decides what is
        a real change.

        Runs at most once per hour from the update cycle; ``force`` is used for
        the once-after-setup call.  Every failure is swallowed at debug level:
        a registry hiccup, a missing route on an older engine, or a refused write
        must never break a poll.  Returns the number of endpoints updated.
        """
        now = utcnow()
        if not force and self._companion_last_sync is not None:
            if (now - self._companion_last_sync) < timedelta(hours=1):
                return 0
        self._companion_last_sync = now

        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
            rows = registry_rows(registry, entries_for_device=er.async_entries_for_device)
        except Exception:  # noqa: BLE001 - a registry hiccup must not block a poll
            _LOGGER.debug("WattMosaic companion entity registry read failed", exc_info=True)
            return 0

        try:
            payload = await self.client.endpoints()
        except Exception as err:  # noqa: BLE001 - older engines may lack the route
            _LOGGER.debug("WattMosaic endpoints fetch for companions failed: %s", err)
            return 0

        endpoints = payload.get("endpoints") if isinstance(payload, Mapping) else None
        if not isinstance(endpoints, list):
            return 0

        updated = 0
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            kind = endpoint.get("kind")
            entity_id = endpoint.get("entity_id")
            endpoint_id = endpoint.get("endpoint_id")
            if kind != "ct" or not isinstance(entity_id, str) or not isinstance(endpoint_id, str):
                continue
            try:
                proposed = companion_config_fields(entity_id, rows)
                revisions = endpoint.get("revisions")
                current = max(
                    (r for r in (revisions or []) if isinstance(r, Mapping)),
                    key=lambda r: int(r.get("revision", 0)),
                    default={},
                )
                changes = companion_config_changes(current, proposed)
                if not changes:
                    continue
                await self.client.update_endpoint_config(endpoint_id, changes)
                updated += 1
            except Exception as err:  # noqa: BLE001 - one endpoint must not stop the rest
                _LOGGER.debug(
                    "WattMosaic companion sync for endpoint %s failed: %s",
                    endpoint_id,
                    err,
                )
        return updated

    @property
    def engine_topology(self) -> dict[str, Any] | None:
        """The engine's topology status as reported by ``/health``.

        Read from ``details.topology`` rather than by calling ``GET /topology``:
        the health probe is already polled every cycle, so this costs no extra
        request.  ``None`` means the engine did not report a topology (one that
        has none configured) — callers must treat that as "unknown", never as
        "candidate".
        """
        data = self.data
        health = data.health if data is not None else None
        details = health.get("details") if isinstance(health, Mapping) else None
        topology = details.get("topology") if isinstance(details, Mapping) else None
        if not isinstance(topology, Mapping):
            return None
        if not topology.get("configured", True):
            # A topology-less engine still reports a reason; keep the dict so
            # diagnostics can explain it, but there is no status to confirm.
            return dict(topology)
        return dict(topology)

    @property
    def topology_structural_edges(self) -> int:
        """Number of STRUCTURAL edges the engine currently publishes (ADR-0043).

        Topology follows the evidence in both directions; there is no terminal
        confirmation.  The health payload's ``structural_edges`` count is the
        engine's word, with ``state == 'structural'`` as a legacy fallback.
        """
        topology = self.engine_topology or {}
        count = topology.get("structural_edges")
        if count is not None:
            try:
                return max(0, int(count))
            except (TypeError, ValueError):
                pass
        state = str(topology.get("state") or topology.get("status") or "").strip().lower()
        return 1 if state == "structural" else 0

    def device_result(self, device_id: str) -> dict[str, Any] | None:
        if self.data is None:
            return None
        return self.data.device(device_id)

    def visible_groups(self, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Groups that still deserve an entity, with their availability.

        A group currently in the snapshot is available.  One that disappeared
        stays *unavailable* (not deleted) until its 7-day retention lapses, so a
        transient publication gap cannot orphan the entity's statistics.
        """
        from .const import GROUP_RETAIN_AFTER_DISAPPEAR_S
        from .util import group_still_retained

        data = self.data
        published = data.groups if data is not None else {}
        last_seen = (data.group_last_seen if data is not None else {}) or {}
        result: dict[str, dict[str, Any]] = {}
        for group_id, value in published.items():
            result[group_id] = {**value, "available": True}
        for group_id, seen_at in last_seen.items():
            if group_id in result:
                continue
            if group_still_retained(
                seen_at, now=now, retain_after_s=GROUP_RETAIN_AFTER_DISAPPEAR_S
            ):
                result[group_id] = {
                    "group_id": group_id,
                    "available": False,
                    "point_w": None,
                    "member_ids": [],
                }
        return result

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
            self.client.snapshot_v3(),
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
                    groups=self._last.groups,
                    group_last_seen=self._last.group_last_seen,
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
                groups=self._last.groups,
                group_last_seen=self._last.group_last_seen,
            )
            _LOGGER.warning("WattMosaic snapshot poll failed (using cached data): %s", error_text)
            return self._last

        # ADR-0044: declare same-device V/I/PF entity ids for CT readers, at most
        # once an hour.  Best-effort: a registry/API failure is logged at debug
        # inside the sync and never breaks this poll.
        await self.sync_companion_entities()

        snapshot = coerce_v3_snapshot(snapshot_raw)
        groups = snapshot.get("groups", {})
        # Remember when each group was last published.  A group that
        # disappears keeps its last_seen so the sensor layer can keep the
        # entity unavailable (not deleted) for the retention window.
        for group_id in groups:
            self._group_last_seen[group_id] = now

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
            groups=groups,
            group_last_seen=dict(self._group_last_seen),
        )

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
