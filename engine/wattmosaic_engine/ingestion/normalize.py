"""Unit and timestamp normalization helpers for Home Assistant states.

The rules are deliberately small and explicit:

* Power is normalised to **watts**; kW is multiplied by 1000.
* Energy is normalised to **kWh**; Wh is divided by 1000.
* Only ``W``, ``kW``, ``kWh`` and ``Wh`` are accepted by the ``Measurement``
  contract.  Anything else is rejected rather than guessed.
* Sign is preserved.  A negative reading (export / reverse flow) is never
  clamped.
"""

from __future__ import annotations

import fnmatch
import math
from collections.abc import Iterable
from datetime import UTC, datetime

POWER_UNITS = ("W", "kW")
ENERGY_UNITS = ("kWh", "Wh")

#: ``device_class`` values that indicate a power/energy reading.
POWER_DEVICE_CLASSES = ("power",)
ENERGY_DEVICE_CLASSES = ("energy",)

_UNIT_ALIASES = {
    "w": "W",
    "watt": "W",
    "watts": "W",
    "kw": "kW",
    "kilowatt": "kW",
    "kilowatts": "kW",
    "kwh": "kWh",
    "wh": "Wh",
    "watt_hour": "Wh",
    "watt-hours": "Wh",
}


def canonical_unit(unit: str | None) -> str | None:
    """Return the contract spelling of ``unit`` or ``None`` if unsupported."""
    if not unit or not isinstance(unit, str):
        return None
    return _UNIT_ALIASES.get(unit.strip().lower())


def normalize_unit(value: float, unit: str | None) -> tuple[float, str]:
    """Convert ``value`` into the engine base unit.

    Returns ``(normalized_value, base_unit)`` where ``base_unit`` is ``"W"`` for
    power and ``"kWh"`` for energy.  Raises ``ValueError`` for unsupported or
    ambiguous units rather than silently treating them as watts.
    """
    canon = canonical_unit(unit)
    if canon is None:
        raise ValueError(f"unsupported unit: {unit!r}")
    if canon == "kW":
        return float(value) * 1000.0, "W"
    if canon == "W":
        return float(value), "W"
    if canon == "Wh":
        return float(value) / 1000.0, "kWh"
    return float(value), "kWh"


def is_finite_number(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def parse_float(raw: object) -> float | None:
    """Parse a state string into a finite float.

    Home Assistant states are strings; some integrations emit ``"unavailable"``,
    ``"unknown"``, empty strings, or numbers with units.  Only a plain finite
    number is accepted.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(float(raw)) else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_state_timestamp(raw: object) -> datetime | None:
    """Parse an ISO-8601 HA timestamp into an aware UTC datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


#: Integration domain that owns WattMosaic's own estimate entities.
OWN_DOMAIN = "wattmosaic"

#: Id prefixes of WattMosaic's own global sensors.  Used only as a fallback for
#: when the entity registry is unavailable, because a device-level estimate is
#: named after the device (``sensor.<device>_estimated_power``) and carries no
#: marker in its id.
OWN_ENTITY_PREFIXES = ("sensor.wattmosaic_",)


def is_own_entity(entity_id: str, platform: str | None = None) -> bool:
    """Return True when ``entity_id`` is WattMosaic's own output.

    The engine must never ingest its own estimates.  Doing so feeds predictions
    back in as if they were physical measurements, and it makes topology
    discovery offer the estimates as candidate CT channels — a circuit that
    measures its own answer.

    The entity registry's ``platform`` field is authoritative: Home Assistant
    sets it to the integration that created the entity.  The id prefix is a
    fallback for the case where the registry could not be loaded, and covers
    only the engine's own global sensors.
    """
    if platform is not None and str(platform).strip().lower() == OWN_DOMAIN:
        return True
    name = entity_id.lower()
    return any(name.startswith(prefix) for prefix in OWN_ENTITY_PREFIXES)


def entity_allowed(
    entity_id: str,
    *,
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
) -> bool:
    """Apply allow/deny patterns.

    Patterns are case-insensitive ``fnmatch`` globs matched against the entity
    id.  Deny wins over allow.  An empty allow list means "allow everything".
    """
    name = entity_id.lower()

    def matches(pattern: str) -> bool:
        return fnmatch.fnmatchcase(name, pattern.strip().lower())

    deny_list = [p for p in (deny or ()) if p and p.strip()]
    if any(matches(p) for p in deny_list):
        return False
    allow_list = [p for p in (allow or ()) if p and p.strip()]
    if not allow_list:
        return True
    return any(matches(p) for p in allow_list)


__all__ = [
    "ENERGY_DEVICE_CLASSES",
    "ENERGY_UNITS",
    "OWN_DOMAIN",
    "OWN_ENTITY_PREFIXES",
    "POWER_DEVICE_CLASSES",
    "POWER_UNITS",
    "canonical_unit",
    "entity_allowed",
    "is_finite_number",
    "is_own_entity",
    "normalize_unit",
    "parse_float",
    "parse_state_timestamp",
]
