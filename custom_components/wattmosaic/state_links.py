"""Resolve a ``device_links`` entry to the device's *state* entities.

Why this lives in the integration
---------------------------------
``CONF_DEVICE_LINKS`` is ``{engine_device_id: ha_device_id}``.  It was written
for entity placement: the power/energy sensors of an inferred load are shown on
the appliance's own Home Assistant device.  Two things were then conflated:

* **placement** — where a measurement is displayed (an HA device id is fine);
* **evidence** — which entity proves the device is *running*.

Only the second can produce a learning sample, and an HA device id is not an
entity id at all.  A live install declared
``dev_refoss_main_panel_hot_tub → 9a2b4caee1c94761ae1c0403873d39b3``, the
topology then carried no ``state_entity_ids`` at all, ``entity_links`` linked
only the hot tub's two CT *power* entities, and every real state change was
discarded at the ``power_events_not_state`` bridge — ``samples_seen`` stayed 0
for days.

**Where the registry lookup happens, and why here.**  ``docs/architecture.md``
§1 states the rule this module must respect: *"The integration never computes
power.  It renders what the engine already decided.  This is the single most
important architectural rule."*  The engine may be run outside Home Assistant
(Docker, other hosts), so it must never import ``homeassistant.helpers``.  The
entity registry exists only inside HA, therefore the resolution is done here, at
the integration boundary, and the *result* — explicit entity ids — is written
into the topology metadata the engine already reads.  The engine keeps consuming
one declarative contract and stays host-agnostic.

That is deliberately the opposite direction from placement: an HA device id is a
display concern and never travels to the engine; the entity ids it resolves to
are evidence and do travel.

What is selected, and what is refused
-------------------------------------
A device's state entities are the domains in :data:`STATE_DOMAINS`.  Power,
energy, current and voltage entities are **measurements**, never transitions: a
watch power reading written into ``device_states`` would fabricate an ``on``.
The device's own CT entities are excluded on that evidence explicitly, because a
registry row can lack ``device_class``/``unit_of_measurement`` while the
topology still knows the entity is the CT.

Nothing is guessed.  Names, areas and "looks like a switch" heuristics are not
used: a fabricated binding attributes one appliance's power to another and the
error is invisible downstream.  A device that resolves to no usable state
entity is **reported** (``devices_without_state_entity``) rather than silently
producing nothing — the silent discard is what made the live pipeline look
healthy while it was dead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Metadata key for state entities the *user declared via a device link*.  Kept
#: separate from the evidence-owned ``state_entity_ids`` (ADR-0011) and from
#: ``config_load_entity_ids`` (an engine-side device configuration) so removing a
#: link restores the other two lists exactly.
LINK_STATE_ENTITIES_KEY = "link_state_entity_ids"

#: Domains whose entity state describes whether the device is running.
STATE_DOMAINS: frozenset[str] = frozenset(
    {
        "binary_sensor",
        "switch",
        "input_boolean",
        "sensor",
        "climate",
        "fan",
        "light",
        "media_player",
        "water_heater",
        "cover",
    }
)

#: Domains that are always a *measurement*, even when listed as a state source.
MEASUREMENT_DOMAINS: frozenset[str] = frozenset(
    {"number", "input_number", "counter"}
)

#: Registry ``device_class`` values that mark a measurement rather than a state.
MEASUREMENT_DEVICE_CLASSES: frozenset[str] = frozenset(
    {
        "power",
        "energy",
        "current",
        "voltage",
        "reactive_power",
        "apparent_power",
        "power_factor",
        "frequency",
        "battery",
        "temperature",
        "humidity",
        "illuminance",
        "pressure",
        "monetary",
        "gas",
        "water",
        "volume",
    }
)

#: Registry units that mark a measurement.  Kept narrow on purpose: ``°C`` on a
#: ``sensor`` is a reading, not a runtime state.
MEASUREMENT_UNITS: frozenset[str] = frozenset(
    {"w", "kw", "wh", "kwh", "mwh", "a", "ma", "v", "mv", "var", "va", "hz", "%"}
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class LinkResolution:
    """Explicit state ids per engine device, plus every refusal, named."""

    #: ``{engine_device_id: (state_entity_id, ...)}``.  A device absent from this
    #: mapping has no usable state entity and appears in
    #: :attr:`devices_without_state_entity` instead.
    state_entities: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: One record per device whose link produced nothing usable.  Diagnostics
    #: render this verbatim so an operator can act on it.
    devices_without_state_entity: tuple[dict[str, Any], ...] = ()
    #: How many non-empty ``device_links`` entries were examined.  Zero links vs
    #: one link that resolved to nothing are very different diagnoses, and the
    #: engine can see neither.
    links_declared: int = 0

    def state_entity_ids(self, device_id: str) -> tuple[str, ...]:
        return tuple(self.state_entities.get(device_id, ()))

    def as_diagnostics(self) -> dict[str, Any]:
        """The public shape used by ``diagnostics.py``."""
        return {
            "links_declared": self.links_declared,
            "state_links": {
                device_id: list(entities)
                for device_id, entities in sorted(self.state_entities.items())
            },
            "devices_without_state_entity": [
                dict(record) for record in self.devices_without_state_entity
            ],
        }


def is_device_registry_id(value: str | None) -> bool:
    """True for an HA device id: 32 lowercase hex, no domain, no dot."""
    return bool(value) and bool(_HEX32.match(str(value).strip().lower()))


def registry_rows(registry: Any, *, entries_for_device: Any = None) -> list[dict[str, Any]]:
    """Normalise an HA entity registry into the plain rows this module consumes.

    ``registry`` is the Home Assistant entity registry (or any object exposing
    ``entities``).  ``entries_for_device(registry, device_id)`` is
    ``er.async_entries_for_device`` when the caller has it; otherwise every
    entity is read, which is fine for the small registries this runs on and for
    the fakes used in tests.

    Only plain values are extracted, so the resolution rules stay testable
    without Home Assistant and the HA objects never leak past this function.
    """
    collected: list[Any]
    if entries_for_device is not None:
        rows_by_device: list[Any] = []
        for device_id in registry_device_ids(registry):
            try:
                rows_by_device.extend(entries_for_device(registry, device_id) or [])
            except Exception:  # noqa: BLE001 - one bad device must not abort
                continue
        collected = rows_by_device
    else:
        raw = getattr(registry, "entities", None)
        # A real registry exposes ``entities`` as a mapping of entity id ->
        # entry, so iterating it yields *keys*.  ``values()`` is what carries
        # the objects; a plain list (the fakes) passes through unchanged.
        collected = list(raw.values()) if isinstance(raw, Mapping) else list(raw or [])

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in collected:
        entity_id = _text(getattr(entry, "entity_id", None))
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        out.append(
            {
                "entity_id": entity_id,
                "device_id": _text(getattr(entry, "device_id", None)),
                "domain": _domain_of(entity_id),
                "device_class": _text(getattr(entry, "device_class", None)),
                "unit": _text(getattr(entry, "unit_of_measurement", None)),
                "entity_category": _text(getattr(entry, "entity_category", None)),
                "disabled": bool(getattr(entry, "disabled", False)),
                "hidden": bool(getattr(entry, "hidden", False)),
                "hidden_by": getattr(entry, "hidden_by", None),
            }
        )
    return out


def registry_device_ids(registry: Any) -> list[str]:
    """Every device id named by an entity in ``registry`` (plain strings)."""
    raw = getattr(registry, "entities", None)
    # Same mapping-vs-list distinction as :func:`registry_rows`: a real registry
    # maps entity id -> entry, so iterating it would yield keys, not objects.
    entries = list(raw.values()) if isinstance(raw, Mapping) else list(raw or [])
    ids: set[str] = set()
    for entry in entries:
        device_id = _text(getattr(entry, "device_id", None))
        if device_id:
            ids.add(device_id)
    return sorted(ids)


def ct_entities_by_device(topology: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """``{device_id: [ct entity ids]}`` from a topology payload.

    These are *measurements*.  They are passed to :func:`resolve_links` so a
    registry row that carries no ``device_class``/``unit_of_measurement`` still
    cannot be mistaken for a state transition: the topology already knows the
    entity is a CT.
    """
    if not isinstance(topology, Mapping):
        return {}
    channels = {
        _text(channel.get("ct_id")): _text(channel.get("entity_id"))
        for channel in (topology.get("channels") or [])
        if isinstance(channel, Mapping)
    }
    out: dict[str, list[str]] = {}
    for device in topology.get("devices") or []:
        if not isinstance(device, Mapping):
            continue
        device_id = _text(device.get("device_id"))
        if not device_id:
            continue
        for key in ("dedicated_ct_ids", "branch_ct_ids"):
            for ct_id in device.get(key) or []:
                entity_id = channels.get(_text(ct_id))
                if entity_id and entity_id not in out.setdefault(device_id, []):
                    out[device_id].append(entity_id)
    return out


def device_ids(topology: Mapping[str, Any] | None) -> list[str]:
    """Every ``device_id`` in a topology payload."""
    if not isinstance(topology, Mapping):
        return []
    return [
        _text(device.get("device_id"))
        for device in (topology.get("devices") or [])
        if isinstance(device, Mapping) and _text(device.get("device_id"))
    ]


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].lower() if "." in entity_id else ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_measurement_row(row: Mapping[str, Any]) -> bool:
    """Whether one registry row describes a measurement, not a runtime state."""
    if _text(row.get("domain")).lower() in MEASUREMENT_DOMAINS:
        return True
    if _text(row.get("device_class")).lower() in MEASUREMENT_DEVICE_CLASSES:
        return True
    unit = _text(row.get("unit")).lower() or _text(row.get("unit_of_measurement")).lower()
    return bool(unit) and unit in MEASUREMENT_UNITS


def _usable(row: Mapping[str, Any]) -> bool:
    if row.get("disabled") is True:
        return False
    if row.get("hidden") is True or row.get("hidden_by") is not None:
        return False
    # ``diagnostic``/``config`` entities describe the integration, not the
    # appliance.  A "firmware update" binary_sensor is not a runtime state.
    return not _text(row.get("entity_category"))


def _state_entities_for_device(
    device_id: str,
    entities: Iterable[Mapping[str, Any]],
    *,
    excluded: set[str],
    known_device: bool | None,
) -> tuple[tuple[str, ...], str, str]:
    """``(selected, reason, detail)`` for one HA device id."""
    rows = [
        row
        for row in entities
        if _text(row.get("device_id")) == device_id and _text(row.get("entity_id"))
    ]
    if not rows and known_device is False:
        return (
            (),
            "device_not_in_registry",
            "no Home Assistant device with this id exists any more",
        )
    measurement = 0
    domain_refused: list[str] = []
    selected: list[str] = []
    for row in rows:
        entity_id = _text(row.get("entity_id"))
        domain = _text(row.get("domain")).lower() or _domain_of(entity_id)
        if entity_id in excluded:
            measurement += 1
            continue
        if not _usable(row):
            continue
        if _is_measurement_row({**row, "domain": domain}):
            measurement += 1
            continue
        if domain not in STATE_DOMAINS:
            domain_refused.append(entity_id)
            continue
        selected.append(entity_id)

    unique = tuple(dict.fromkeys(selected))
    if unique:
        return unique, "", ""
    if measurement:
        return (
            (),
            "only_measurement_entities",
            f"{measurement} candidate entities are CT/power/energy measurements",
        )
    if domain_refused:
        return (
            (),
            "not_a_state_domain",
            f"no entity in {sorted(STATE_DOMAINS)}; refused {sorted(domain_refused)}",
        )
    return (), "no_state_entities", "the device has no enabled state-bearing entity"


def resolve_links(
    links: Mapping[str, Any] | None,
    *,
    entities: Iterable[Mapping[str, Any]],
    engine_devices: Iterable[str],
    ct_entity_ids: Mapping[str, Iterable[str]] | None = None,
    registry_device_ids: Iterable[str] | None = None,
) -> LinkResolution:
    """Turn the stored link map into explicit per-device state entity ids.

    ``entities`` is a registry-shaped iterable (``entity_id``, ``device_id``,
    optionally ``domain``/``device_class``/``unit``/``disabled``/``entity_category``).
    It is plain data so this module stays importable and testable without Home
    Assistant.  ``ct_entity_ids`` is the topology's own per-device measurement
    entities; they are excluded even when the registry carries no attributes.
    ``registry_device_ids``, when supplied, lets a vanished device be reported as
    such rather than as a device with no state entities.

    An entity-id link is still accepted unchanged (backward compatibility): old
    entries stored an entity id, and refusing them would break working installs.
    """
    known_devices = {str(d) for d in engine_devices}
    known_registry_devices = (
        None if registry_device_ids is None else {str(d) for d in registry_device_ids}
    )
    ct_map = {
        str(device): {str(e) for e in ents}
        for device, ents in (ct_entity_ids or {}).items()
    }
    rows = list(entities)

    resolved: dict[str, tuple[str, ...]] = {}
    problems: list[dict[str, Any]] = []
    declared = 0

    for raw_device_id, raw_link in sorted((links or {}).items()):
        device_id = _text(raw_device_id)
        link = _text(raw_link)
        if not device_id or not link:
            continue
        declared += 1
        if device_id not in known_devices:
            problems.append(
                {
                    "device_id": device_id,
                    "link": link,
                    "link_kind": "device" if is_device_registry_id(link) else "entity",
                    "reason": "engine_device_not_in_topology",
                    "detail": "the engine topology has no such device_id",
                }
            )
            continue

        if is_device_registry_id(link):
            selected, reason, detail = _state_entities_for_device(
                link.lower(),
                rows,
                excluded=ct_map.get(device_id, set()),
                known_device=(
                    None
                    if known_registry_devices is None
                    else link.lower() in known_registry_devices
                ),
            )
            if not selected:
                problems.append(
                    {
                        "device_id": device_id,
                        "link": link,
                        "link_kind": "device",
                        "reason": reason,
                        "detail": detail,
                    }
                )
                continue
            resolved[device_id] = selected
            continue

        # An entity-id link: unchanged behaviour, but still checked so a stored
        # power entity cannot pretend to be a state source.
        if not _ENTITY_ID.match(link):
            problems.append(
                {
                    "device_id": device_id,
                    "link": link,
                    "link_kind": "entity",
                    "reason": "unrecognised_link",
                    "detail": "not an HA device id (32 hex) or an entity id",
                }
            )
            continue
        domain = _domain_of(link)
        row = next(
            (r for r in rows if _text(r.get("entity_id")) == link), None
        )
        measurement = link in ct_map.get(device_id, set()) or (
            row is not None and _is_measurement_row({**row, "domain": domain})
        )
        if measurement:
            problems.append(
                {
                    "device_id": device_id,
                    "link": link,
                    "link_kind": "entity",
                    "reason": "measurement_entity",
                    "detail": "a CT/power/energy entity is a measurement, not a state",
                }
            )
            continue
        if domain not in STATE_DOMAINS:
            problems.append(
                {
                    "device_id": device_id,
                    "link": link,
                    "link_kind": "entity",
                    "reason": "not_a_state_domain",
                    "detail": f"domain '{domain}' does not describe runtime state",
                }
            )
            continue
        resolved[device_id] = (link,)

    return LinkResolution(
        state_entities=resolved,
        devices_without_state_entity=tuple(problems),
        links_declared=declared,
    )


def apply_state_entities(device: dict[str, Any], entity_ids: Iterable[str]) -> bool:
    """Write/retract the link-owned state ids on one device dict, in place.

    Returns whether the device changed.  Only :data:`LINK_STATE_ENTITIES_KEY` is
    touched, so ``state_entity_ids`` (mapping evidence) and
    ``config_load_entity_ids`` (engine device configuration) survive byte for
    byte: clearing a link can never destroy provenance.
    """
    wanted = [str(e) for e in entity_ids if str(e)]
    metadata = dict(device.get("metadata") or {})
    if wanted:
        if metadata.get(LINK_STATE_ENTITIES_KEY) == wanted:
            return False
        metadata[LINK_STATE_ENTITIES_KEY] = wanted
    elif LINK_STATE_ENTITIES_KEY in metadata:
        metadata.pop(LINK_STATE_ENTITIES_KEY, None)
    else:
        return False
    device["metadata"] = metadata
    return True


def apply_state_link_metadata(
    topology: Mapping[str, Any] | None,
    resolution: LinkResolution,
) -> tuple[dict[str, Any], bool]:
    """Write/retract :data:`LINK_STATE_ENTITIES_KEY` on every topology device.

    Returns ``(updated_topology, changed)``.  The input is never mutated: the
    engine's topology is versioned and replaced atomically, so a caller must
    send the returned copy.
    """
    updated = dict(topology or {})
    raw_devices = updated.get("devices")
    devices: list[dict[str, Any]] = []
    changed = False
    if isinstance(raw_devices, list):
        for item in raw_devices:
            if not isinstance(item, Mapping):
                continue
            device = dict(item)
            device_id = _text(device.get("device_id"))
            if apply_state_entities(device, resolution.state_entities.get(device_id, ())):
                changed = True
            devices.append(device)
    if devices:
        updated["devices"] = devices
    return updated, changed


def state_link_signature(resolution: LinkResolution) -> str:
    """Stable fingerprint of a resolution, used to bound topology rewrites.

    Sorted by device id and by entity id, so the *order* the registry happened
    to return entities in cannot trigger a write.
    """
    parts = [
        f"{device_id}={','.join(sorted(resolution.state_entity_ids(device_id)))}"
        for device_id in sorted(resolution.state_entities)
    ]
    problems = ";".join(
        f"{record.get('device_id')}:{record.get('reason')}"
        for record in sorted(
            resolution.devices_without_state_entity, key=lambda r: str(r.get("device_id"))
        )
    )
    return "|".join(parts) + (f"||{problems}" if problems else "")


__all__ = [
    "LINK_STATE_ENTITIES_KEY",
    "LinkResolution",
    "MEASUREMENT_DEVICE_CLASSES",
    "MEASUREMENT_DOMAINS",
    "MEASUREMENT_UNITS",
    "STATE_DOMAINS",
    "apply_state_entities",
    "apply_state_link_metadata",
    "ct_entities_by_device",
    "device_ids",
    "is_device_registry_id",
    "registry_rows",
    "registry_device_ids",
    "resolve_links",
    "state_link_signature",
]
