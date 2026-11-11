"""Button entities for WattMosaic.

One button: **Confirm topology**.

Why a button and not a service
-------------------------------
Confirming the topology is the single action that unblocks learning: while the
engine reports ``candidate``, the sample filter rejects every sample as
``ambiguous_topology`` (correctly — the association itself is unverified), so no
model is ever trained.  The engine has always exposed ``POST /topology/confirm``
and the config-flow wizard calls it, but a user who never re-ran the wizard had
no way to reach it.

A ``button`` is the right shape here:

* it is one parameterless user action, so it needs no service schema, no
  ``confirm:`` flag and no device targeting;
* it appears in the UI next to the ``Active learning`` binary sensor that says
  *why* it matters, so the remedy is discoverable from the dashboard itself;
* ``unavailable`` expresses "nothing to do" natively — the button disappears
  from an actionable state once the topology is confirmed.

The press logic lives in :mod:`custom_components.wattmosaic.topology_confirm`
(pure Python, unit-tested); this module is thin Home Assistant wiring: entity
identity, availability, and persisting the flag + reloading the entry.

Never automatic: nothing here runs on setup.  A topology is a hypothesis until a
person confirms it (ADR-0008), and self-confirming would fabricate user intent.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CT_ASSIGNMENTS,
    CONF_TOPOLOGY_VERSION,
    DOMAIN,
    MANUFACTURER,
    NAME,
    VERSION,
)
from .coordinator import WattMosaicCoordinator
from .entity_defs import GLOBAL_BUTTONS
from .topology_confirm import (
    ConfirmAvailability,
    ConfirmResult,
    async_confirm_topology,
    confirm_availability,
    confirmed_entry_updates,
)

_LOGGER = logging.getLogger(__name__)

#: The entity id is pinned.  The ``blocked`` learning status names this exact id
#: as the remedy, so a slug change would make the advice point at nothing.
CONFIRM_TOPOLOGY_ENTITY_ID = "button.wattmosaic_confirm_topology"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WattMosaicCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            WattMosaicConfirmTopologyButton(coordinator, entry, key)
            for key in GLOBAL_BUTTONS
        ]
    )


def _engine_device_info(coordinator: WattMosaicCoordinator, entry: ConfigEntry) -> DeviceInfo:
    health = coordinator.data.health if coordinator.data and coordinator.data.health else {}
    return DeviceInfo(
        identifiers={(DOMAIN, f"engine_{entry.entry_id}")},
        name=f"{NAME} Engine",
        manufacturer=MANUFACTURER,
        model="Local inference engine",
        sw_version=health.get("engine_version", VERSION),
        configuration_url=coordinator.client.base_url,
    )


class WattMosaicConfirmTopologyButton(
    CoordinatorEntity[WattMosaicCoordinator], ButtonEntity
):
    """Confirm the engine's candidate topology using the stored declaration.

    Availability
    ------------
    Available only while the engine reports ``candidate`` **and** the config
    entry stores a declaration to replay.  When the engine is already confirmed
    the entity is ``unavailable``: there is nothing to do, and a button that can
    be pressed with no effect teaches users to distrust the UI.

    Idempotency
    -----------
    A press on an already-confirmed engine performs **no write** (checked before
    the replay), and the replay itself refuses to rewrite a declaration that
    matches.  Pressing twice is therefore safe, and the second press reports
    "already confirmed" instead of creating a second topology revision.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: WattMosaicCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        definition = GLOBAL_BUTTONS[key]
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_button_{key}"
        self._attr_icon = definition.get("icon")
        self._attr_device_info = _engine_device_info(coordinator, entry)
        self._last_result: ConfirmResult | None = None

    # -- entry-side state -------------------------------------------------- #
    @property
    def _config(self) -> dict[str, Any]:
        return {**self._entry.data, **self._entry.options}

    @property
    def _assignments(self) -> Any:
        return self._config.get(CONF_CT_ASSIGNMENTS) or {}

    def _availability(self) -> ConfirmAvailability:
        return confirm_availability(
            self.coordinator.engine_topology,
            self._assignments,
            engine_available=self.coordinator.engine_available,
        )

    # -- entity API -------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return self._availability().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._availability()
        attributes: dict[str, Any] = dict(status.as_attributes())
        attributes["entity_id"] = CONFIRM_TOPOLOGY_ENTITY_ID
        if self._last_result is not None:
            attributes.update(self._last_result.as_attributes())
        return attributes

    async def async_press(self) -> None:
        """Confirm the stored declaration, persist the flag, then reload.

        The entry flag is written only when the engine actually reports
        ``confirmed`` afterwards.  A PUT that landed but was not confirmed must
        not tell the user learning is enabled while the filter still rejects
        every sample.
        """
        assignments = self._assignments
        result = await async_confirm_topology(
            self.coordinator.client,
            assignments,
            stored_version=self._config.get(CONF_TOPOLOGY_VERSION),
            state=self.coordinator.topology_push,
            observed_version=(self.coordinator.data.reconciliation or {}).get("topology_version")
            if self.coordinator.data
            else None,
        )
        self._last_result = result
        _LOGGER.info("WattMosaic topology confirm: %s", result.message)

        if not result.entry_confirmed:
            # Nothing persistent to write.  Refresh so the attributes reflect
            # the engine's real state, and leave the button actionable.
            await self.coordinator.async_request_refresh()
            return

        config = self._config
        if not config.get("topology_confirmed"):
            data, options = confirmed_entry_updates(
                self._entry.data, self._entry.options, version=result.version
            )
            # ``async_update_entry`` does not reload by itself; the update
            # listener in ``__init__`` does.  Reloading is what makes the
            # coordinator replay the declaration as confirmed on the next poll.
            self.hass.config_entries.async_update_entry(
                self._entry, data=data, options=options
            )
        else:
            # The flag was already true (a retry after a failed engine write):
            # the entry needs no change, but the engine state should be picked
            # up immediately rather than on the next scan interval.
            await self.coordinator.async_request_refresh()
