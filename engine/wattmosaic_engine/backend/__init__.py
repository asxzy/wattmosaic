"""Orchestrator-owned integration backend, split out of ``integration.py`` (X03).

``main.py`` calls ``backend.process(window, ...)``.  The
electrical/disaggregation/learning packages (owned by other workstreams) expose
``DisaggregationEngine.infer(window, device_states=..., models=...,
topology=...)`` and a ``ModelRegistry``.  This package is the single place where
those two contracts meet, so neither owner has to know the other's shape.

It also owns the *production* learning and aggregation wiring:

* :class:`~wattmosaic_engine.aggregation.EnergyAggregator` integrates every
  inference result into per-device energy buckets and detected device runs, and
  supplies the cumulative ``energy_kwh`` total that the HA ``total_increasing``
  sensor publishes.  Without this the field was always ``None`` and the sensor
  never advanced.
* :class:`~wattmosaic_engine.learning.LearningPipeline` collects, filters,
  persists and (when enough accepted samples exist) trains/validates/promotes
  device models.  Without this ``learning_active`` advertised a loop that did
  not exist and ``model_estimated`` power could never become non-zero.

Design rules honoured here:
* one device failure never aborts a batch (the engine already isolates; this
  adapter additionally guards the whole call);
* missing model registry is not fatal — inference runs with explicit
  ``model_estimated`` gaps rather than fabricating;
* reconciliation from the engine is passed through unchanged, residual included;
* HA events are only associated with a device when the **topology explicitly
  names the CT(s) that meter it**.  No name-similarity guessing is used, and an
  unconfirmed topology is recorded as such so the sample filter rejects it.

The behaviour itself lives in one mixin per responsibility
(:mod:`~wattmosaic_engine.backend.inference`,
:mod:`~wattmosaic_engine.backend.learning`,
:mod:`~wattmosaic_engine.backend.audit`,
:mod:`~wattmosaic_engine.backend.reanalysis`).  ``WattMosaicBackend`` composes
them, so state and call sites are unchanged by the split.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..contracts import utcnow
from .audit import AuditMixin
from .batch import (
    DEFAULT_EVENT_BUFFER,
    DEFAULT_FLUSH_EVERY_CYCLES,
    DEFAULT_REANALYSIS_HISTORY,
    DEFAULT_TRAIN_INTERVAL_S,
    RECOVERY_LOOKBACK_HOURS,
    BackendBatch,
)
from .inference import InferenceMixin
from .learning import LearningMixin
from .loader import InferenceBackendUnavailable, load_inference_backend
from .reanalysis import ReanalysisMixin

log = logging.getLogger("wattmosaic.integration")

__all__ = [
    "BackendBatch",
    "InferenceBackendUnavailable",
    "WattMosaicBackend",
    "build_backend",
    "load_inference_backend",
]


class _DisabledAuditConfig:
    """All audit families disabled after the Z2 cutover."""

    write_records = False
    write_isolation = False
    write_association = False
    write_circuits = False
    write_residual = False
    write_hierarchy = False
    algorithm_version = "disabled@z2"


class WattMosaicBackend(
    InferenceMixin, LearningMixin, AuditMixin, ReanalysisMixin
):
    """Concrete inference backend for the engine process.

    Parameters
    ----------
    database:
        Used to build the model registry, the energy aggregator and the learning
        pipeline.  Optional so the adapter can run in tests without storage.
    learning_enabled:
        Whether to construct the learning pipeline.  Pausing learning later via
        :attr:`learning_active` stops sample collection without discarding the
        pipeline or its persisted state.
    clock:
        Source of "now" for everything this backend timestamps.  Defaults to
        :func:`~wattmosaic_engine.contracts.utcnow`; injected by replay and by
        tests that need deterministic timestamps.
    """

    def __init__(
        self,
        database: Any | None = None,
        *,
        max_measurement_age_s: float = 120.0,
        learning_enabled: bool = True,
        train_interval_s: float = DEFAULT_TRAIN_INTERVAL_S,
        flush_every_cycles: int = DEFAULT_FLUSH_EVERY_CYCLES,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
        reanalysis_interval_s: float = 0.0,
        reanalysis_lookback_s: float = 21600.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._clock = clock
        # Z2: the 5 s full-solve path is gone.  The backend is now only the
        # aggregation / learning / diagnostics adapter for the three-loop
        # runtime; there is no DisaggregationEngine and no step tracker.
        self.database = database
        self.registry: Any | None = None
        self.aggregator: Any | None = None
        self.learning: Any | None = None
        self.learning_active = bool(learning_enabled)
        # DEPRECATED (realtime learning): no longer a throttle.  Kept as a public
        # attribute so callers that set it still work; 0 means realtime.  Training
        # is now event-driven via a per-device "last trained to" watermark.
        self.train_interval_s = max(0.0, float(train_interval_s))
        self.flush_every_cycles = max(1, int(flush_every_cycles))
        self.max_measurement_age_s = float(max_measurement_age_s)

        self.cycles = 0
        self.training_attempts = 0
        self.models_promoted = 0
        self.energy_updates = 0
        self.last_train_at: datetime | None = None
        #: Realtime learning: per-device watermark of the newest sample timestamp
        #: already fed to a training pass.  A device is retrained only when a
        #: *new* matured sample exists beyond this point — an event-driven
        #: debounce that replaces the old wall-clock interval without the
        #: runaway cost of retraining on every one of the 5 s inference cycles.
        self._trained_to: dict[str, datetime] = {}
        self.last_error: str | None = None

        self._pending_events: deque[Any] = deque(maxlen=max(16, int(event_buffer)))
        self._cycles_since_flush = 0
        self._ambiguous: dict[str, list[str]] = {}
        self._topology_signature: tuple[int, int] | None = None
        #: Quality of the topology the last learning pass judged samples against
        #: (``confirmed`` / ``candidate``).  Reported in ``learning_status`` so a
        #: ``candidate`` topology is explained before the first rejection lands.
        self._topology_quality_seen: str | None = None

        # -- analysis pass -------------------------------------------------- #
        # The six inference modules (isolation, association, circuits, factors,
        # residual, hierarchy) only ran in their own unit tests before this: no
        # production call site ordered them, so on a live install none of their
        # artifacts existed.  The pass runs on this backend's existing inference
        # loop -- it starts no thread, task or timer of its own -- and writes its
        # versioned ``InferenceRecord`` audit trail beside the existing output.
        #: DEPRECATED as a throttle (realtime learning): ``0`` (the default) means
        #: reanalysis fires as soon as relevant matured evidence exists.  A
        #: positive value is now only a short *debounce*, not a 3600 s gate.
        self.reanalysis_interval_s = max(0.0, float(reanalysis_interval_s))
        self.reanalysis_lookback_s = max(60.0, float(reanalysis_lookback_s))
        self.reanalysis_attempts = 0
        #: Event-driven reanalysis watermark: the newest stored device-event time
        #: already re-derived.  The pass fires only when newer matured evidence
        #: exists (or once at startup when there is evidence to derive).  ``None``
        #: means no pass has run yet, so the first evidence-bearing cycle fires.
        self.last_reanalysis_at: datetime | None = None
        self._reanalysis_evidence_seen: datetime | None = None
        self.last_audit_report: Any | None = None
        # Z2: the legacy AuditPass is gone.  Keep a minimal disabled config and
        # a null audit so the read-only diagnostics still report honestly.
        self.audit: Any | None = None
        self.audit_config: Any = _DisabledAuditConfig()
        self._reanalysis_history: deque[datetime] = deque(
            maxlen=DEFAULT_REANALYSIS_HISTORY
        )

        if database is not None:
            try:
                from ..models.registry import ModelRegistry

                self.registry = ModelRegistry(database)
            except Exception:  # noqa: BLE001 - model layer is optional
                log.warning("model registry unavailable; running without learned models")
                self.registry = None
            try:
                from ..aggregation.aggregator import EnergyAggregator

                self.aggregator = EnergyAggregator(
                    database, stale_after_s=max_measurement_age_s
                )
                # A crash can leave a bucket half written.  Replaying stored
                # inference results repairs it deterministically (upserts
                # overwrite in place), which is also what makes the cumulative
                # total continuous across a restart.
                try:
                    self.aggregator.recompute_incomplete_buckets(
                        lookback_hours=RECOVERY_LOOKBACK_HOURS
                    )
                except Exception as exc:  # noqa: BLE001 - recovery is best effort
                    log.warning("energy bucket recovery failed: %s", exc)
            except Exception:  # noqa: BLE001 - aggregation is optional
                log.warning("energy aggregator unavailable; energy_kwh will stay null")
                self.aggregator = None
            # Z2: the legacy learning pipeline is gone; incremental learning
            # now runs in the background worker (L6).  ``self.learning`` stays
            # None and the status surface reports learning as off here.
            self.learning = None


def build_backend(config: Any, database: Any | None = None) -> WattMosaicBackend:
    """Construct the backend with config-derived settings."""
    return WattMosaicBackend(
        database,
        max_measurement_age_s=float(getattr(config, "stale_after_s", 120.0)),
        learning_enabled=bool(getattr(config, "learning_enabled", True)),
        train_interval_s=float(getattr(config, "train_interval_s", 0.0)),
        reanalysis_interval_s=float(
            getattr(config, "reanalysis_interval_s", 0.0)
        ),
        reanalysis_lookback_s=float(
            getattr(config, "reanalysis_lookback_s", 21600.0)
        ),
    )
