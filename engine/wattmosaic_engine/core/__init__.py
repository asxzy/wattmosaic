"""EngineCore responsibilities, split out of :mod:`wattmosaic_engine.main`.

``main`` keeps ``EngineCore`` composition, the CLI and logging; every
behaviour lives in one mixin here.  Because the mixins share ``self`` with the
composed class, state and call sites are unchanged by the split.

Package facade: this module is written once (task X03) and re-exports each
mixin so later waves own whole files without editing it.
"""

from __future__ import annotations

from .diagnostics_api import DiagnosticsApiMixin, _prom_label
from .discovery_api import (
    TOPOLOGY_BOOTSTRAP_WAIT_S,
    TOPOLOGY_RETRY_INTERVAL_S,
    DiscoveryApiMixin,
    _is_numeric_state,
)
from .endpoints_api import EndpointsApiMixin
from .mapping_api import MappingApiMixin
from .models_api import ModelsApiMixin
from .runtime import (
    MAINTENANCE_INTERVAL_S,
    SAMPLE_BATCH_SIZE,
    SAMPLE_FLUSH_INTERVAL_S,
    ComponentStatus,
    RuntimeMixin,
    _event_time,
)
from .snapshot import SnapshotMixin, _safe_history_row
from .topology_api import (
    TopologyApiMixin,
)

__all__ = [
    "ComponentStatus",
    "DiagnosticsApiMixin",
    "DiscoveryApiMixin",
    "EndpointsApiMixin",
    "MAINTENANCE_INTERVAL_S",
    "MappingApiMixin",
    "ModelsApiMixin",
    "RuntimeMixin",
    "SAMPLE_BATCH_SIZE",
    "SAMPLE_FLUSH_INTERVAL_S",
    "SnapshotMixin",
    "TOPOLOGY_BOOTSTRAP_WAIT_S",
    "TOPOLOGY_RETRY_INTERVAL_S",
    "TopologyApiMixin",
    "_event_time",
    "_is_numeric_state",
    "_prom_label",
    "_safe_history_row",
]
