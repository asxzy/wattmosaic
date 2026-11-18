"""Re-export facade for the inference backend.

The backend moved into the :mod:`wattmosaic_engine.backend` package (task X03):
``backend/__init__.py`` holds ``WattMosaicBackend`` and
``load_inference_backend``, while the behaviour is split by responsibility into
``backend/inference.py``, ``backend/learning.py``, ``backend/audit.py`` and
``backend/reanalysis.py``.

Everything this module used to export is still importable from here, so callers
and tests that do ``from wattmosaic_engine.integration import WattMosaicBackend``
keep working unchanged.
"""

from __future__ import annotations

from .backend import (
    BackendBatch,
    InferenceBackendUnavailable,
    WattMosaicBackend,
    build_backend,
    load_inference_backend,
    log,
)
from .backend.batch import (
    DEFAULT_EVENT_BUFFER,
    DEFAULT_FLUSH_EVERY_CYCLES,
    DEFAULT_REANALYSIS_HISTORY,
    DEFAULT_TRAIN_INTERVAL_S,
    RECOVERY_LOOKBACK_HOURS,
)

#: The module-level helpers that used to live here are still importable from this
#: path, beside the code that now owns them.
from .backend.inference import (
    _channel_type,
    _has_fresh_measurement,
    _measurement_age,
    _quality_text,
    _source_text,
    _stale_reason,
)
from .backend.learning import _event_attributes, _event_entity, _event_state, _event_time

__all__ = [
    "BackendBatch",
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_FLUSH_EVERY_CYCLES",
    "DEFAULT_REANALYSIS_HISTORY",
    "DEFAULT_TRAIN_INTERVAL_S",
    "InferenceBackendUnavailable",
    "RECOVERY_LOOKBACK_HOURS",
    "WattMosaicBackend",
    "_channel_type",
    "_event_attributes",
    "_event_entity",
    "_event_state",
    "_event_time",
    "_has_fresh_measurement",
    "_measurement_age",
    "_quality_text",
    "_source_text",
    "_stale_reason",
    "build_backend",
    "load_inference_backend",
    "log",
]
