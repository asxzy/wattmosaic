"""EngineCore responsibilities, split out of :mod:`wattmosaic_engine.main`.

``main`` keeps ``EngineCore`` composition, the CLI and logging; every
behaviour lives in one mixin here.  Because the mixins share ``self`` with the
composed class, state and call sites are unchanged by the split.

Package facade: this module is written once (task X03) and re-exports each
mixin so later waves own whole files without editing it.  Row helpers from
:mod:`wattmosaic_engine.core.rows` are re-exported too, and are also kept
importable from ``wattmosaic_engine.main`` for existing callers.
"""

from __future__ import annotations

from .diagnostics_api import DiagnosticsApiMixin, _prom_label
from .discovery_api import (
    TOPOLOGY_BOOTSTRAP_WAIT_S,
    TOPOLOGY_RETRY_INTERVAL_S,
    DiscoveryApiMixin,
    _is_numeric_state,
)
from .mapping_api import MappingApiMixin
from .models_api import ModelsApiMixin
from .rows import (
    _json_list,
    _json_obj,
    _safe_float,
    row_to_inference_result,
    row_to_reconciliation,
)
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
    NESTING_RESAMPLE_GRID,
    TopologyApiMixin,
    _as_utc,
    _forward_fill,
    _linkable_state_entities,
    _sample_grid,
    _sampling_cadence_s,
)

__all__ = [
    "ComponentStatus",
    "DiagnosticsApiMixin",
    "DiscoveryApiMixin",
    "MAINTENANCE_INTERVAL_S",
    "MappingApiMixin",
    "ModelsApiMixin",
    "NESTING_RESAMPLE_GRID",
    "RuntimeMixin",
    "SAMPLE_BATCH_SIZE",
    "SAMPLE_FLUSH_INTERVAL_S",
    "SnapshotMixin",
    "TOPOLOGY_BOOTSTRAP_WAIT_S",
    "TOPOLOGY_RETRY_INTERVAL_S",
    "TopologyApiMixin",
    "_as_utc",
    "_event_time",
    "_forward_fill",
    "_is_numeric_state",
    "_json_list",
    "_json_obj",
    "_linkable_state_entities",
    "_prom_label",
    "_safe_float",
    "_safe_history_row",
    "_sample_grid",
    "_sampling_cadence_s",
    "row_to_inference_result",
    "row_to_reconciliation",
]
