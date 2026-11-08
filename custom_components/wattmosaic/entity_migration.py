"""Display-name changes that must not orphan long-term statistics.

Home Assistant derives a ``has_entity_name`` entity's ``entity_id`` from the
device name plus the translated entity name.  When a translated name changes, a
*new* install simply gets the new id -- but an existing install keeps the old
registry entry, so the entity is now named differently from its id and, worse,
the published name no longer matches anything the user can find.

The dangerous direction is creating a second entity: if the integration stops
producing the old name, HA never re-derives it, it just sits there stale.  If it
starts producing the new one, the registry may allocate a *different* id unless
the old entry is renamed first.  Either way the recorder's statistics stay keyed
by the old id.

The only correct move is an explicit registry rename.  The recorder listens for
``EVENT_ENTITY_REGISTRY_UPDATED`` with ``action="update"`` and a ``entity_id``
change, and migrates ``statistics`` / ``statistics_short_term`` rows to the new
``statistic_id``.  Deleting and re-creating would not: removal leaves the
statistics behind and the new entity starts a separate series.

This module is deliberately split so the *decision* is pure and testable without
Home Assistant: :func:`planned_renames` takes ``(entity_id, unique_id)`` pairs and
returns the renames to apply.  :func:`async_migrate_entity_ids` applies them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Ordered ``(old_fragment, new_fragment)`` pairs.  Longest first, so
#: ``_model_estimated_power`` is rewritten before the shorter ``_estimated_power``
#: rule could match inside it.
NAME_FRAGMENT_RENAMES: tuple[tuple[str, str], ...] = (
    ("_model_estimated_power", "_model_power"),
    ("_estimated_energy", "_energy"),
    ("_estimated_power", "_power"),
)


def migrated_object_id(object_id: str) -> str | None:
    """The new object id for a stale one, or ``None`` when nothing changes.

    Only a *suffix* is rewritten: an entity the user renamed by hand keeps its
    name, and a device whose own name contains the word is untouched.

    Home Assistant disambiguates a duplicate entity id by appending ``_2``,
    ``_3`` and so on.  That suffix is *not* part of the name, so it is detached
    before the rule runs and restored afterwards; otherwise the collided entity
    is the one that silently keeps its old id.
    """
    base = object_id
    collision = ""
    match = re.search(r"_(?P<n>[0-9]+)$", object_id)
    if match is not None:
        candidate = object_id[: match.start()]
        # Only treat it as a collision suffix when the remainder still matches a
        # rename rule; otherwise it is a legitimate word or number in the name.
        for old, _new in NAME_FRAGMENT_RENAMES:
            if candidate.endswith(old):
                base = candidate
                collision = match.group(0)
                break
    for old, new in NAME_FRAGMENT_RENAMES:
        if base.endswith(old):
            return base[: -len(old)] + new + collision
    return None


def planned_renames(
    entities: Iterable[Mapping[str, Any]],
    *,
    existing_entity_ids: Iterable[str] = (),
    domain: str = "sensor",
) -> list[tuple[str, str]]:
    """Return ``[(old_entity_id, new_entity_id)]`` renames to perform.

    ``entities`` are registry entries for *this integration only*; the caller is
    responsible for that filter, because renaming an entity the integration does
    not own would corrupt someone else's history.

    A rename is skipped when the target id is already taken.  That means a
    previous run already renamed it, or the user created an entity with the
    target name -- in both cases forcing the rename would either fail or steal
    someone else's id.
    """
    taken = {str(eid) for eid in existing_entity_ids}
    renames: list[tuple[str, str]] = []
    for entry in entities:
        entity_id = entry.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        if not entity_id.startswith(f"{domain}."):
            continue
        # A user-chosen name must be respected: only entities that still carry
        # our own translated name are candidates.
        if entry.get("name") not in (None, ""):
            continue
        object_id = entity_id.split(".", 1)[1]
        new_object_id = migrated_object_id(object_id)
        if new_object_id is None or new_object_id == object_id:
            continue
        target = f"{domain}.{new_object_id}"
        if target in taken:
            continue
        taken.add(target)
        renames.append((entity_id, target))
    return renames


async def async_migrate_entity_ids(hass: Any, entry: Any, *, domain: str) -> list[tuple[str, str]]:
    """Apply :func:`planned_renames` through the entity registry.

    Returns the renames that were performed.  A failure on one entity is logged
    and skipped: a stale id must never stop the integration from starting.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries = [
        {
            "entity_id": item.entity_id,
            "name": item.name,
            "unique_id": item.unique_id,
        }
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]
    taken = {item.entity_id for item in registry.entities.values()}
    renames = planned_renames(entries, existing_entity_ids=taken, domain=domain)
    applied: list[tuple[str, str]] = []
    for old, new in renames:
        try:
            registry.async_update_entity(old, new_entity_id=new)
        except Exception:  # noqa: BLE001 - one bad rename must not block startup
            continue
        applied.append((old, new))
    return applied
