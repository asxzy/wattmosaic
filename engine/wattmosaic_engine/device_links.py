"""Entity ↔ device linking derived strictly from the topology.

Why this module exists
----------------------
Home Assistant events arrive keyed by ``entity_id`` (``switch.pump``), while
every inference and learning contract is keyed by WattMosaic's
``DeviceTopology.device_id``.  Without an explicit, declared link:

* ``EngineCore.device_states`` is keyed by raw entity ids, so the
  disaggregation engine's ``_state_text(device_states, device_id)`` lookup
  always misses and state-transition inference is dead code in production;
* :class:`~wattmosaic_engine.learning.sample_collector.SampleCollector` never
  sees a supporting CT, so every learning sample is rejected as
  ``missing_measurement`` and no model can ever be trained.

The link is **declarative, never inferred**.  A device owns:

1. its CT channels (``dedicated_ct_ids`` + ``branch_ct_ids``) — the entity id
   on each :class:`~wattmosaic_engine.contracts.CTChannel` is a *power* entity;
2. zero or more state entities listed in
   ``DeviceTopology.metadata["state_entity_ids"]`` — the ``switch``/``climate``/
   ``binary_sensor`` entities whose state describes whether the device is on;
3. zero or more state entities the *user* declared in configuration, kept under
   ``metadata["config_load_entity_ids"]`` so they never overwrite the
   evidence-derived list above;
4. zero or more state entities the *user* declared by linking the device to a
   Home Assistant device, kept under ``metadata["link_state_entity_ids"]``.

The last key exists because a device link stores an HA **device** id, which is
not an entity at all.  Home Assistant's entity registry is the only place that
can expand it, and the engine may run outside Home Assistant, so the integration
resolves it and writes the resulting entity ids here.  The engine only ever
reads the topology; it never learns what a device registry is.  Without this
key the live install's only link (``dev_refoss_main_panel_hot_tub`` →
``9a2b4caee1c94761ae1c0403873d39b3``) linked nothing but two CT *power*
entities, so every real state change was discarded at the
``power_events_not_state`` bridge and ``samples_seen`` stayed at zero.

Name similarity and HA areas are deliberately **not** used: guessing an
association would fabricate attribution, which the spec forbids.  An entity that
no device claims stays unlinked and any sample built from it is rejected with a
recorded reason.  The HA device registry is not consulted *here* either — this
module stays host-agnostic and reads only the declared metadata keys above.

A power entity is *measurable*, not *state-bearing*
--------------------------------------------------
Both kinds are linked, but they are not interchangeable.  A CT power entity
(``sensor.water_heater_power``) reports **watts**; its value is a measurement.  It must
never be written into ``device_states``: that slot holds the device's runtime
state (``on``/``off``/``Heating``).  Numeric readings never reach the event path
(they are consumed as measurements), but availability changes do — and writing
``"unavailable"`` there replaces ``"off"``, so the next genuine ``"off"`` reads as
a transition and one bridge blip manufactures two phantom transitions.  A power
entity is measurable, not state-bearing.  :func:`runtime_state_entities`
separates the two, and any caller that maintains device state must use it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import DeviceTopology, Topology

#: Metadata key holding the HA entities that describe a device's own state, as
#: derived from mapping *evidence*.  Evidence-owned: configuration never rewrites
#: it (ADR-0011).
STATE_ENTITIES_KEY = "state_entity_ids"
#: Metadata key holding runtime state entities resolved from a Home Assistant
#: **device** link (``device_links: {engine_device: ha_device_id}``).  Written by
#: the integration, which alone can consult the HA entity registry; the engine
#: reads it as a declarative state source and never resolves it itself.
LINK_STATE_ENTITIES_KEY = "link_state_entity_ids"
#: Metadata key holding runtime entities the *user* declared in configuration.
#: Separate from ``STATE_ENTITIES_KEY`` so removing a configuration restores the
#: evidence-derived list exactly.
CONFIG_LOAD_ENTITIES_KEY = "config_load_entity_ids"
#: Metadata key holding per-device corrections to the meaning of state labels,
#: as ``{label: "on"|"off"}``.  Needed because one global label table cannot
#: describe every integration: a spa in a typical installation reports
#: ``Cooling`` while drawing 22 W and ``Heating`` while drawing 4.5 kW, and the
#: global table treats ``cooling`` as ON.  Declarative and per device — never
#: guessed.
STATE_OVERRIDES_KEY = "state_overrides"


def _as_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out
    return []


def state_entity_ids(device: DeviceTopology | Mapping[str, Any]) -> list[str]:
    """Declared state entities for one device (metadata, contract-validated)."""
    metadata = (
        device.get("metadata") if isinstance(device, Mapping) else getattr(device, "metadata", None)
    )
    if not isinstance(metadata, Mapping):
        return []
    return _as_sequence(metadata.get(STATE_ENTITIES_KEY))


def config_load_entity_ids(device: DeviceTopology | Mapping[str, Any]) -> list[str]:
    """Runtime entities the *user* declared in configuration (ADR-0011)."""
    metadata = (
        device.get("metadata") if isinstance(device, Mapping) else getattr(device, "metadata", None)
    )
    if not isinstance(metadata, Mapping):
        return []
    return _as_sequence(metadata.get(CONFIG_LOAD_ENTITIES_KEY))


def link_state_entity_ids(device: DeviceTopology | Mapping[str, Any]) -> list[str]:
    """Runtime state entities resolved from a Home Assistant device link.

    A ``device_links`` entry stores an HA *device* id, which is not an entity.
    The integration expands it through the entity registry and records the
    result under :data:`LINK_STATE_ENTITIES_KEY`; this module only reads that
    declaration, so the engine never needs registry knowledge and stays
    runnable outside Home Assistant.
    """
    metadata = (
        device.get("metadata") if isinstance(device, Mapping) else getattr(device, "metadata", None)
    )
    if not isinstance(metadata, Mapping):
        return []
    return _as_sequence(metadata.get(LINK_STATE_ENTITIES_KEY))


def runtime_state_entities(device: DeviceTopology | Mapping[str, Any]) -> list[str]:
    """Entities whose *state* may be written into ``device_states``.

    This is the union of evidence-derived, configuration-declared and
    link-resolved runtime entities, and it deliberately excludes CT power
    entities.  A power entity is a measurement source (``entity_links`` still
    links it so a learning sample can find its CTs), but its value is watts.
    Numeric readings never reach the event path — they are consumed as
    measurements — yet availability changes (``unavailable``, ``unknown``,
    ``restored``) do, and writing one of those as the device's state replaces
    ``"off"`` with ``"unavailable"``; the next real ``"off"`` then reads as a
    transition, so one blip manufactures two phantom transitions.
    """
    return list(
        dict.fromkeys(
            [
                *state_entity_ids(device),
                *config_load_entity_ids(device),
                *link_state_entity_ids(device),
            ]
        )
    )


def _ct_ids(device: DeviceTopology | Mapping[str, Any]) -> list[str]:
    if isinstance(device, Mapping):
        return [
            *_as_sequence(device.get("dedicated_ct_ids")),
            *_as_sequence(device.get("branch_ct_ids")),
        ]
    return [*device.dedicated_ct_ids, *device.branch_ct_ids]


def entity_links(topology: Topology | None) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``entity_id -> (device_id, supporting ct_ids)`` from the topology alone.

    Both a device's runtime state entities and the entities of its CT channels
    are linked.  A power CT entity is included because it names the device's
    measurement CTs, which is what makes a learning sample measurable.  It is
    **not** a state source — see :func:`runtime_state_entities`.

    When two devices claim the same entity the first declaration wins and the
    ambiguity is *not* resolved silently — :func:`ambiguous_entities` reports it
    so the caller can surface it instead of guessing.
    """
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    if topology is None:
        return out
    channels = topology.channel_map()
    for device in topology.devices:
        cts = tuple(_ct_ids(device))
        entities = runtime_state_entities(device)
        for ct_id in cts:
            channel = channels.get(ct_id)
            if channel is not None and channel.entity_id:
                entities.append(channel.entity_id)
        seen: set[str] = set()
        for entity in entities:
            if entity in seen:
                continue
            seen.add(entity)
            out.setdefault(entity, (device.device_id, cts))
    return out


def ambiguous_entities(topology: Topology | None) -> dict[str, list[str]]:
    """Entities claimed by more than one device (never auto-resolved)."""
    if topology is None:
        return {}
    channels = topology.channel_map()
    claims: dict[str, list[str]] = {}
    for device in topology.devices:
        entities = runtime_state_entities(device)
        for ct_id in _ct_ids(device):
            channel = channels.get(ct_id)
            if channel is not None and channel.entity_id:
                entities.append(channel.entity_id)
        for entity in set(entities):
            claims.setdefault(entity, [])
            if device.device_id not in claims[entity]:
                claims[entity].append(device.device_id)
    return {entity: devices for entity, devices in claims.items() if len(devices) > 1}


def device_for_entity(
    topology: Topology | None, entity_id: str | None
) -> tuple[str, tuple[str, ...]] | None:
    """Link one entity id, or ``None`` when no device declares it."""
    if not entity_id:
        return None
    return entity_links(topology).get(entity_id)


def state_key(
    topology: Topology | None, entity_id: str | None, state: Any
) -> tuple[str, Any]:
    """Return ``(key, state)`` suitable for ``device_states``.

    The key is the declared ``device_id`` when the topology links the entity and
    the raw entity id otherwise.  Passing an unlinked entity through unchanged
    is what lets the engine record it as ``unknown_state_devices`` instead of
    inventing a device for it.

    A CT power entity links for measurement but is *not* a state source: it
    returns its raw entity id so a caller that stores the result cannot overwrite
    the device's runtime state with a watt reading.  Use
    :func:`state_entity_for_device` when the decision matters.
    """
    link = device_for_entity(topology, entity_id)
    if link is None:
        return (entity_id or "", state)
    device_id = link[0]
    if topology is not None and not state_entity_for_device(topology, device_id, entity_id):
        return (entity_id or "", state)
    return (device_id, state)


def supporting_ct_ids(topology: Topology | None, entity_id: str | None) -> tuple[str, ...]:
    link = device_for_entity(topology, entity_id)
    return () if link is None else link[1]


def state_entity_for_device(
    topology: Topology | None, device_id: str, entity_id: str | None
) -> bool:
    """True when ``entity_id`` may be written as ``device_id``'s runtime state.

    A CT power entity links to the device for *measurement* but must never
    overwrite its runtime state (see module docstring).  Declared runtime
    entities — evidence-derived or user-configured — are the only legitimate
    sources.
    """
    if topology is None or not entity_id:
        return False
    device = topology.device_map().get(device_id)
    if device is None:
        return False
    return entity_id in runtime_state_entities(device)


__all__ = [
    "CONFIG_LOAD_ENTITIES_KEY",
    "LINK_STATE_ENTITIES_KEY",
    "STATE_ENTITIES_KEY",
    "STATE_OVERRIDES_KEY",
    "ambiguous_entities",
    "config_load_entity_ids",
    "device_for_entity",
    "entity_links",
    "link_state_entity_ids",
    "runtime_state_entities",
    "state_entity_for_device",
    "state_entity_ids",
    "state_key",
    "supporting_ct_ids",
]
