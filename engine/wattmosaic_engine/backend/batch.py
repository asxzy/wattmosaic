"""Backend result shape and construction defaults.

The constants used to live at the top of ``integration.py``; they move here with
the backend split (X03) and are re-exported from
:mod:`wattmosaic_engine.integration` for existing importers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import InferenceResult, Reconciliation

#: How many recent inference results may accumulate before the aggregator is
#: flushed even when no cycle boundary is reached.
DEFAULT_FLUSH_EVERY_CYCLES = 12
#: DEPRECATED (realtime learning): retained only for constructor/config
#: compatibility.  Training is event-driven — it fires when new matured samples
#: arrive — so this value no longer throttles anything.  ``0`` means realtime.
DEFAULT_TRAIN_INTERVAL_S = 0.0
#: Bound on remembered HA events awaiting a learning pass.
DEFAULT_EVENT_BUFFER = 512
#: How far back crash recovery replays stored inference results.
RECOVERY_LOOKBACK_HOURS = 24.0
#: Bound on remembered reanalysis timestamps (one entry per pass).
DEFAULT_REANALYSIS_HISTORY = 64


@dataclass(slots=True)
class BackendBatch:
    """Shape ``main.py`` expects from a backend."""

    results: list[InferenceResult]
    reconciliation: Reconciliation
    diagnostics: dict[str, Any]


__all__ = [
    "BackendBatch",
    "DEFAULT_EVENT_BUFFER",
    "DEFAULT_FLUSH_EVERY_CYCLES",
    "DEFAULT_REANALYSIS_HISTORY",
    "DEFAULT_TRAIN_INTERVAL_S",
    "RECOVERY_LOOKBACK_HOURS",
]
