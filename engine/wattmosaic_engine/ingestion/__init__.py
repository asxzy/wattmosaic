"""Read-only Home Assistant ingestion.

Public API
----------
``HASource``            WebSocket client that never issues service calls.
``normalize_unit``      Unit conversion helper (kW -> W, Wh -> kWh).
``entity_allowed``      Entity allow/deny filter (fnmatch patterns).
``compute_backoff``     Jittered exponential reconnect delay.
"""

from .ha_source import (
    BACKOFF_BASE_S,
    BACKOFF_MAX_S,
    ConnectionState,
    HASource,
    compute_backoff,
)
from .normalize import (
    ENERGY_UNITS,
    POWER_UNITS,
    entity_allowed,
    normalize_unit,
    parse_state_timestamp,
)

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "ConnectionState",
    "ENERGY_UNITS",
    "HASource",
    "POWER_UNITS",
    "compute_backoff",
    "entity_allowed",
    "normalize_unit",
    "parse_state_timestamp",
]
