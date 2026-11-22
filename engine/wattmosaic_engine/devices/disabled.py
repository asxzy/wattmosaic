"""Sensors disabled by configuration (``WM_DISABLED_SENSORS``).

A user lists endpoint ids or HA entity ids (``fnmatch`` globs, case-insensitive)
that the engine must ignore: a second meter on a feed another meter already
measures, or a power sensor that is not a CT.  :func:`reconcile_disabled_endpoints`
makes the stored endpoints agree with the list at startup:

* a matching endpoint that is enabled is disabled and marked
  ``metadata.disabled_by = "config"`` (with a DECLARED configuration event);
* a matching endpoint that is already disabled (for example a discovery
  candidate) is left as it is and *not* marked, so removing it from the list
  later never enables something the configuration did not disable;
* a marked endpoint that no longer matches is re-enabled and unmarked.

The returned :attr:`DisabledSensors.endpoint_ids` is the set every consumer
excludes (ingestion, discovery, topology inference, panel tree, matrix).  The
rule is driven by the user's list only: nothing here knows any device name.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("wattmosaic.devices")

#: ``metadata.disabled_by`` value written when the configuration disabled it.
DISABLED_BY_CONFIG = "config"


def sensor_matches(patterns: Sequence[str], endpoint_id: str, entity_id: str | None) -> bool:
    """True when any pattern matches the endpoint id or its entity id."""
    names = [str(endpoint_id).lower()]
    if entity_id:
        names.append(str(entity_id).lower())
    for pattern in patterns:
        glob = str(pattern).strip().lower()
        if glob and any(fnmatch.fnmatchcase(name, glob) for name in names):
            return True
    return False


@dataclass(frozen=True)
class DisabledSensors:
    """Outcome of one reconcile pass."""

    #: Every endpoint the configuration matches (excluded everywhere).
    endpoint_ids: frozenset[str] = frozenset()
    #: Their HA entity ids (added to the ingestion deny list).
    entity_ids: frozenset[str] = frozenset()
    #: Endpoints this pass disabled / re-enabled.
    disabled: tuple[str, ...] = ()
    reenabled: tuple[str, ...] = ()
    #: Patterns that matched no endpoint (likely a typo; logged).
    unmatched: tuple[str, ...] = ()
    patterns: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "patterns": list(self.patterns),
            "endpoint_ids": sorted(self.endpoint_ids),
            "disabled": list(self.disabled),
            "reenabled": list(self.reenabled),
            "unmatched": list(self.unmatched),
        }


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json", row.get("metadata"))
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def reconcile_disabled_endpoints(
    repo: Any,
    patterns: Iterable[str],
    *,
    record_event: Any = None,
) -> DisabledSensors:
    """Make stored endpoints agree with the configured disabled list.

    ``repo`` is the entities repository (``endpoints()`` / ``upsert_endpoint``).
    ``record_event(endpoint_id, action)`` is called once per change.  The raw row
    is written back (not the contract model) so ``metadata_json`` survives.
    Idempotent: a second pass with the same list changes nothing.
    """
    wanted = tuple(p for p in (str(x).strip() for x in patterns) if p)
    matched: set[str] = set()
    entities: set[str] = set()
    used: set[str] = set()
    disabled: list[str] = []
    reenabled: list[str] = []
    for row in repo.endpoints():
        endpoint_id = str(row["endpoint_id"])
        entity_id = row.get("entity_id")
        meta = _metadata(row)
        marked = meta.get("disabled_by") == DISABLED_BY_CONFIG
        enabled = bool(row.get("enabled", True))
        hits = [p for p in wanted if sensor_matches((p,), endpoint_id, entity_id)]
        if hits:
            used.update(hits)
            matched.add(endpoint_id)
            if entity_id:
                entities.add(str(entity_id))
            if enabled:
                meta["disabled_by"] = DISABLED_BY_CONFIG
                repo.upsert_endpoint({**row, "enabled": False, "metadata_json": meta})
                disabled.append(endpoint_id)
                if record_event is not None:
                    record_event(endpoint_id, "config.disable")
        elif marked:
            meta.pop("disabled_by", None)
            repo.upsert_endpoint({**row, "enabled": True, "metadata_json": meta})
            reenabled.append(endpoint_id)
            if record_event is not None:
                record_event(endpoint_id, "config.enable")
    unmatched = tuple(p for p in wanted if p not in used)
    result = DisabledSensors(
        endpoint_ids=frozenset(matched),
        entity_ids=frozenset(entities),
        disabled=tuple(disabled),
        reenabled=tuple(reenabled),
        unmatched=unmatched,
        patterns=wanted,
    )
    if disabled or reenabled:
        log.info(
            "disabled sensors reconciled: disabled=%s reenabled=%s",
            ",".join(disabled) or "-",
            ",".join(reenabled) or "-",
        )
    if unmatched:
        log.warning(
            "WM_DISABLED_SENSORS entries match no endpoint: %s", ", ".join(unmatched)
        )
    return result


__all__ = [
    "DISABLED_BY_CONFIG",
    "DisabledSensors",
    "reconcile_disabled_endpoints",
    "sensor_matches",
]
