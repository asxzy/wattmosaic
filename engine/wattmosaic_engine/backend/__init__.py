"""The engine's backend adapter for the three-loop runtime (contract 3.0).

Inference runs per event group in the three-loop runtime (P6), which calls
:func:`~wattmosaic_engine.inference.orchestrator.infer_group` /
:func:`~wattmosaic_engine.inference.orchestrator.infer_steady` directly, and
learning runs in the background :class:`~wattmosaic_engine.learning.worker.LearningWorker`.
This adapter only owns what is left around them:

* the :class:`~wattmosaic_engine.aggregation.EnergyAggregator`, whose
  incomplete buckets are repaired at startup and flushed at shutdown;
* the L6 fast-path hook (:meth:`LearningMixin.on_confirmed_evidence`) and the
  honest learning status;
* a plain self-description for ``/status`` and diagnostics.

The legacy 5 s ``process`` solve, its analysis pass (isolation, association,
circuits, hierarchy, residual), the sample-based learning pipeline and the
legacy model registry were removed at the contract-3.0 cutover.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..contracts import utcnow
from .learning import LearningMixin
from .loader import InferenceBackendUnavailable, load_inference_backend

log = logging.getLogger("wattmosaic.integration")

#: How far back crash recovery replays stored results into energy buckets.
RECOVERY_LOOKBACK_HOURS = 24.0

__all__ = [
    "InferenceBackendUnavailable",
    "RECOVERY_LOOKBACK_HOURS",
    "WattMosaicBackend",
    "build_backend",
    "load_inference_backend",
]


class WattMosaicBackend(LearningMixin):
    """Concrete backend adapter for the engine process.

    Parameters
    ----------
    database:
        Used to build the energy aggregator.  Optional so the adapter can run in
        tests without storage.
    learning_enabled:
        Initial value of :attr:`learning_active`.
    clock:
        Source of "now" for everything this backend timestamps.
    """

    def __init__(
        self,
        database: Any | None = None,
        *,
        max_measurement_age_s: float = 120.0,
        learning_enabled: bool = True,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._clock = clock
        self.database = database
        self.aggregator: Any | None = None
        self.learning = None
        self.learning_active = bool(learning_enabled)
        self.max_measurement_age_s = float(max_measurement_age_s)
        self.last_error: str | None = None

        if database is not None:
            try:
                from ..aggregation.aggregator import EnergyAggregator

                self.aggregator = EnergyAggregator(
                    database, stale_after_s=max_measurement_age_s
                )
                # A crash can leave a bucket half written.  Replaying stored
                # results repairs it deterministically (upserts overwrite in
                # place), which keeps the cumulative total continuous.
                try:
                    self.aggregator.recompute_incomplete_buckets(
                        lookback_hours=RECOVERY_LOOKBACK_HOURS
                    )
                except Exception as exc:  # noqa: BLE001 - recovery is best effort
                    log.warning("energy bucket recovery failed: %s", exc)
            except Exception:  # noqa: BLE001 - aggregation is optional
                log.warning("energy aggregator unavailable")
                self.aggregator = None

    def flush_energy(self, now: datetime | None = None) -> list[Any]:
        """Persist open buckets and close idle device runs."""
        aggregator = self.aggregator
        if aggregator is None:
            return []
        try:
            return list(aggregator.flush(now=now))
        except Exception as exc:  # noqa: BLE001 - persistence failures are logged
            self.last_error = f"flush: {type(exc).__name__}: {exc}"
            log.warning("energy flush failed: %s", exc)
            return []

    def status(self) -> dict[str, Any]:
        learning = self.learning_status()
        return {
            "backend": "WattMosaicBackend",
            "disaggregation": "three-loop-v3",
            "learning": self.learning is not None,
            "learning_active": self.learning_active,
            "learning_status": learning,
            "learning_effective_state": learning["effective_state"],
            "energy_aggregation": self.aggregator is not None,
            "last_error": self.last_error,
        }

    def close(self) -> None:
        """Flush anything still open (called from the engine shutdown path)."""
        self.flush_energy()


def build_backend(config: Any, database: Any | None = None) -> WattMosaicBackend:
    """Construct the backend with config-derived settings."""
    return WattMosaicBackend(
        database,
        max_measurement_age_s=float(getattr(config, "stale_after_s", 120.0)),
        learning_enabled=bool(getattr(config, "learning_enabled", True)),
    )
