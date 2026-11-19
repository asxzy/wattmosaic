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
the integration boundary, and the *result* — explicit entity ids — is what may
cross to the engine (as v3 ``ha_entity_links`` bodies, see
:func:`build_ha_entity_link_bodies`).  The engine stays host-agnostic.

That is deliberately the opposite direction from placement: an HA device id is a
display concern and never travels to the engine; the entity ids it resolves to
are evidence and do travel.

What is selected, and what is refused
-------------------------------------
A device's state entities are the domains in :data:`STATE_DOMAINS`.  Power,
energy, current and voltage entities are **measurements**, never transitions: a
watch power reading written into ``device_states`` would fabricate an ``on``.
A caller that knows a device's own CT entities can pass them as
``ct_entity_ids`` so they are excluded even when a registry row lacks
``device_class``/``unit_of_measurement``.

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
    Assistant.  ``ct_entity_ids`` (optional) is each device's own measurement
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
                    "detail": "the engine reports no such device_id",
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


def build_ha_entity_link_bodies(
    resolution: LinkResolution,
    *,
    target_kind: str = "device",
    role: str = "state",
) -> list[dict[str, str]]:
    """Build v3 ``ha_entity_links`` write bodies from a :class:`LinkResolution`.

    DEC-08 replaces the four legacy representations of "which entity belongs to
    which device" with one many-to-many table.  Each resolved state entity is a
    link to its engine device with ``target_kind`` and ``role``.  The bodies are
    plain data so they can be posted through the v3 API by the caller; this
    helper never touches Home Assistant or the network.  One entity may link to
    several subjects (one body per pair), which is the point of the v3 shape.

    ``role`` defaults to ``"state"`` because :func:`resolve_links` only ever
    produces runtime-state evidence, never measurement or control links.
    """
    if target_kind not in ("device", "component"):
        raise ValueError("target_kind must be 'device' or 'component'")
    bodies: list[dict[str, str]] = []
    for device_id in sorted(resolution.state_entities):
        for entity_id in resolution.state_entity_ids(device_id):
            bodies.append(
                {
                    "entity_id": entity_id,
                    "target_kind": target_kind,
                    "target_id": device_id,
                    "role": role,
                    "declared_by": "user",
                }
            )
    return bodies


__all__ = [
    "LinkResolution",
    "MEASUREMENT_DEVICE_CLASSES",
    "MEASUREMENT_DOMAINS",
    "MEASUREMENT_UNITS",
    "STATE_DOMAINS",
    "build_ha_entity_link_bodies",
    "is_device_registry_id",
    "registry_rows",
    "registry_device_ids",
    "resolve_links",
    "state_link_signature",
]
