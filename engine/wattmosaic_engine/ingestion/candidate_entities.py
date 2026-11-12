"""Which Home Assistant entities *could* be a fixture on a shared circuit?

Why this module exists
----------------------
Auto placement was starved of candidates.  The population it reasoned about came
from :func:`wattmosaic_engine.main._linkable_state_entities`, which reads only
the entities the *topology* already declares as runtime signals.  On a real
installation that is one device's entities, so a shared lighting circuit could
never be resolved: every light on it was "unknown to the topology" and was
silently absent from the analysis.  The engine looked like it had found nothing
when in fact it had looked at nothing.

The honest population is the set of Home Assistant entities whose **domain and
declared role** can prove runtime -- ``light``, ``switch``, ``fan``,
``input_boolean`` -- minus the ones that are already accounted for (declared by
the user, or the engine's own output).  That is a structural rule, not a
heuristic: it never consults a friendly name, an area, a manufacturer or a
registry device *to decide membership*.  Those fields travel with a candidate
only so the UI can label it.

Two things this module deliberately does **not** do
---------------------------------------------------
1. **It never invents a device.**  A candidate is an entity that *could* be a
   load; nothing here asserts that it is on any circuit.  Only measured
   co-movement (``ingestion.correlation``) may claim that.
2. **It never infers from names.**  A missing registry row is not
   disqualifying: a ``light.*`` state with no registry device is still a
   candidate, because the domain alone is the evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts import Topology
from ..device_links import CONFIG_LOAD_ENTITIES_KEY
from ..entity_evidence import classify_entity, domain_of
from .correlation import CANDIDATE_DOMAINS
from .normalize import is_own_entity

#: Refusal labels.  Stable strings, because the API reports them verbatim and a
#: client may key its explanation off them.
REASON_MODE_ENTITY = "mode_entity"
REASON_NOT_A_LOAD_DOMAIN = "not_a_load_domain"
REASON_ALREADY_DECLARED = "already_declared_state_entity"
REASON_WATTMOSAIC_ENTITY = "wattmosaic_entity"

#: The three topology metadata keys that declare a device's runtime signals.
#: Kept identical to ``main._linkable_state_entities`` on purpose: rule 2 says a
#: declared entity is not a candidate, and re-using the same key set is the only
#: way "declared" means the same thing in both places.
DECLARED_STATE_ENTITY_KEYS = (
    "state_entity_ids",
    "load_entity_ids",
    CONFIG_LOAD_ENTITIES_KEY,
)


@dataclass(slots=True, frozen=True)
class CandidateEntity:
    """One entity that could be a fixture on a shared circuit.

    ``registry_device_id``, ``manufacturer``, ``area_id`` and ``friendly_name``
    are **display metadata only**.  They are shown next to the measurement; they
    never decide whether the entity is a candidate.
    """

    entity_id: str
    domain: str
    friendly_name: str
    registry_device_id: str | None = None
    manufacturer: str | None = None
    area_id: str | None = None
    #: Human-readable reasons this entity was considered at all.  Shown, never
    #: parsed.
    evidence: tuple[str, ...] = field(default_factory=tuple)


def _object_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1] if "." in entity_id else entity_id


def _registry_rows(registries: Mapping[str, Any] | None, key: str) -> dict[str, Mapping[str, Any]]:
    """Index a registry list by its id field, ignoring anything malformed.

    ``HASource`` caches ``{"entities": [...], "devices": [...], "areas": [...]}``
    (see ``ingestion.discovery``), but a failed registry fetch stores an error
    object instead of a list.  Both shapes are tolerated: a missing registry is
    normal and must not remove a candidate.
    """
    raw = registries.get(key) if isinstance(registries, Mapping) else None
    rows: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw, Iterable):
        return rows
    id_key = "entity_id" if key == "entities" else "id"
    for entry in raw:
        if isinstance(entry, Mapping) and entry.get(id_key):
            rows[str(entry[id_key])] = entry
    return rows


def declared_state_entities(topology: Topology | None) -> set[str]:
    """Entities the topology already declares as runtime signals for a device.

    An entity a person (or the mapping wizard) already linked is not a question
    to be answered again.  The key set matches ``main._linkable_state_entities``
    exactly; this helper lives here so the candidate module can be tested without
    the engine process.
    """
    out: set[str] = set()
    if topology is None:
        return out
    for device in topology.devices:
        for key in DECLARED_STATE_ENTITY_KEYS:
            raw = (device.metadata or {}).get(key)
            if isinstance(raw, str):
                out.add(raw)
            elif isinstance(raw, Iterable):
                out.update(str(value) for value in raw if value)
    return {entity_id for entity_id in out if entity_id}


def _state_rows(states: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index the state snapshot by entity id; the last row wins on duplicates."""
    out: dict[str, Mapping[str, Any]] = {}
    for row in states:
        if not isinstance(row, Mapping):
            continue
        entity_id = row.get("entity_id")
        if isinstance(entity_id, str) and entity_id:
            out[entity_id] = row
    return out


def _friendly_name(
    entity_id: str,
    state: Mapping[str, Any],
    entity_entry: Mapping[str, Any],
) -> str:
    """Display name, in order of authority; never used to classify."""
    attributes = state.get("attributes")
    if isinstance(attributes, Mapping):
        name = attributes.get("friendly_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    for key in ("name", "original_name"):
        name = entity_entry.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return _object_id(entity_id)


def _device_class(state: Mapping[str, Any]) -> str | None:
    attributes = state.get("attributes")
    value = attributes.get("device_class") if isinstance(attributes, Mapping) else None
    return str(value) if value else None


def candidate_state_entities(
    states: Iterable[Mapping[str, Any]],
    registries: Mapping[str, Any] | None,
    topology: Topology | None = None,
) -> tuple[list[CandidateEntity], dict[str, str]]:
    """Entities that COULD be a fixture on a shared circuit.

    Returns ``(candidates sorted by entity_id, refused_map entity_id -> reason)``.
    Nothing is dropped silently: an entity that was looked at and declined is in
    the refusal map with the rule that declined it.

    Inclusion needs all four rules (contract §2):

    1. the domain is one whose state *is* runtime (``CANDIDATE_DOMAINS``);
    2. the entity is not already declared by the topology;
    3. the entity is not WattMosaic's own output;
    4. ``classify_entity`` calls its role ``load`` (ADR-0012), so a
       ``climate``/``water_heater`` mode entity is refused as ``mode_entity``.
    """
    states_by_id = _state_rows(states)
    entities = _registry_rows(registries, "entities")
    devices = _registry_rows(registries, "devices")
    declared = declared_state_entities(topology)

    candidates: list[CandidateEntity] = []
    refused: dict[str, str] = {}

    for entity_id in sorted(states_by_id):
        state = states_by_id[entity_id]
        entity_entry = entities.get(entity_id, {})
        platform = entity_entry.get("platform")
        domain = domain_of(entity_id)

        if entity_id in declared:
            refused[entity_id] = REASON_ALREADY_DECLARED
            continue
        if is_own_entity(entity_id, str(platform) if platform is not None else None):
            refused[entity_id] = REASON_WATTMOSAIC_ENTITY
            continue

        # The domain-independent role check comes first so a ``climate`` entity
        # is refused as a *mode* rather than as a generic non-load domain: the
        # reason is what tells the user why that entity can never prove runtime.
        evidence = classify_entity(
            entity_id, device_class=_device_class(state), declared_load=False
        )
        if evidence.role == "mode":
            refused[entity_id] = REASON_MODE_ENTITY
            continue
        if evidence.role != "load" or domain not in CANDIDATE_DOMAINS:
            refused[entity_id] = REASON_NOT_A_LOAD_DOMAIN
            continue

        device_id = entity_entry.get("device_id")
        device = devices.get(str(device_id)) if device_id else {}
        manufacturer = device.get("manufacturer") if device else None
        area_id = entity_entry.get("area_id") or (device.get("area_id") if device else None)

        label = f"HA {domain} entity"
        if not device_id:
            evidence_text = (f"{label} (no registry device)",)
        elif manufacturer:
            evidence_text = (label, str(manufacturer))
        else:
            evidence_text = (label,)

        candidates.append(
            CandidateEntity(
                entity_id=entity_id,
                domain=domain,
                friendly_name=_friendly_name(entity_id, state, entity_entry),
                registry_device_id=str(device_id) if device_id else None,
                manufacturer=str(manufacturer) if manufacturer else None,
                area_id=str(area_id) if area_id else None,
                evidence=evidence_text,
            )
        )

    return candidates, dict(sorted(refused.items()))


__all__ = [
    "CandidateEntity",
    "DECLARED_STATE_ENTITY_KEYS",
    "REASON_ALREADY_DECLARED",
    "REASON_MODE_ENTITY",
    "REASON_NOT_A_LOAD_DOMAIN",
    "REASON_WATTMOSAIC_ENTITY",
    "candidate_state_entities",
    "declared_state_entities",
]
