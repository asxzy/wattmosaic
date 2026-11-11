"""Orchestrator-owned integration: wire the real inference backend to the engine.

`main.py` (owned by another workstream) calls ``backend.process(window, ...)``.
The electrical/disaggregation/learning packages (owned by other workstreams)
expose ``DisaggregationEngine.infer(window, device_states=..., models=...,
topology=...)`` and a ``ModelRegistry``.  This adapter is the single place where
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
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .contracts import (
    InferenceResult,
    QualityStatus,
    Reconciliation,
    Source,
    Topology,
    utcnow,
)
from .device_links import (
    STATE_OVERRIDES_KEY,
    ambiguous_entities,
    device_for_entity,
    entity_links,
)
from .disaggregation.engine import DisaggregationEngine, InferenceBatch
from .disaggregation.events import StepTracker
from .electrical.topology import build_graph
from .models.protocols import window_time as _window_time

log = logging.getLogger("wattmosaic.integration")

#: How many recent inference results may accumulate before the aggregator is
#: flushed even when no cycle boundary is reached.
DEFAULT_FLUSH_EVERY_CYCLES = 12
#: Minimum wall-clock gap between automatic training attempts per backend.
DEFAULT_TRAIN_INTERVAL_S = 3600.0
#: Bound on remembered HA events awaiting a learning pass.
DEFAULT_EVENT_BUFFER = 512
#: How far back crash recovery replays stored inference results.
RECOVERY_LOOKBACK_HOURS = 24.0


@dataclass(slots=True)
class BackendBatch:
    """Shape ``main.py`` expects from a backend."""

    results: list[InferenceResult]
    reconciliation: Reconciliation
    diagnostics: dict[str, Any]


def _event_state(event: Any) -> str | None:
    raw = event.get("new_state") if isinstance(event, Mapping) else getattr(event, "new_state", None)
    return None if raw is None else str(raw)


def _event_entity(event: Any) -> str | None:
    raw = event.get("entity_id") if isinstance(event, Mapping) else getattr(event, "entity_id", None)
    return None if raw is None else str(raw)


def _event_attributes(event: Any) -> dict[str, Any]:
    raw = event.get("attributes") if isinstance(event, Mapping) else getattr(event, "attributes", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _event_time(event: Any) -> datetime | None:
    raw = (
        event.get("occurred_at")
        if isinstance(event, Mapping)
        else getattr(event, "occurred_at", None)
    )
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class WattMosaicBackend:
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
    ) -> None:
        # The synchroniser exposes only a point-in-time snapshot (no per-channel
        # history, cadence or previous cycle).  Without a tracker the CT step
        # detector would never see two comparable samples, so production
        # inference would silently degrade to state-only attribution.  The
        # tracker is per-backend, append-only and bounded, so one process
        # restart loses at most ``max_samples`` cycles of history.
        self.tracker = StepTracker()
        self.engine = DisaggregationEngine(
            max_measurement_age_s=max_measurement_age_s, tracker=self.tracker
        )
        self.database = database
        self.registry: Any | None = None
        self.aggregator: Any | None = None
        self.learning: Any | None = None
        self.learning_active = bool(learning_enabled)
        self.train_interval_s = max(60.0, float(train_interval_s))
        self.flush_every_cycles = max(1, int(flush_every_cycles))
        self.max_measurement_age_s = float(max_measurement_age_s)

        self.cycles = 0
        self.samples_collected = 0
        self.samples_accepted = 0
        self.training_attempts = 0
        self.models_promoted = 0
        self.energy_updates = 0
        self.last_train_at: datetime | None = None
        self.last_error: str | None = None

        self._pending_events: deque[Any] = deque(maxlen=max(16, int(event_buffer)))
        self._cycles_since_flush = 0
        self._ambiguous: dict[str, list[str]] = {}
        self._topology_signature: tuple[int, int] | None = None

        if database is not None:
            try:
                from .models.registry import ModelRegistry

                self.registry = ModelRegistry(database)
            except Exception:  # noqa: BLE001 - model layer is optional
                log.warning("model registry unavailable; running without learned models")
                self.registry = None
            try:
                from .aggregation.aggregator import EnergyAggregator

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
            if learning_enabled:
                try:
                    from .learning.pipeline import LearningPipeline
                    from .learning.thresholds import LearningThresholds

                    thresholds = LearningThresholds(stale_after_s=max_measurement_age_s)
                    self.learning = LearningPipeline(
                        database,
                        thresholds=thresholds,
                        registry=self.registry,
                    )
                except Exception:  # noqa: BLE001 - learning is optional
                    log.warning("learning pipeline unavailable; no model training")
                    self.learning = None

    # -- the interface main.py calls -------------------------------------- #
    def process(
        self,
        window: Any,
        *,
        device_states: Any,
        topology: Topology,
        models: Any = None,
        events: Iterable[Any] | None = None,
    ) -> BackendBatch:
        if events is not None:
            for event in events:
                self._pending_events.append(event)

        self._refresh_links(topology)
        registry = models if models is not None else self.registry
        try:
            batch: InferenceBatch = self.engine.infer(
                window,
                device_states=device_states or {},
                models=registry,
                topology=topology,
            )
        except Exception as exc:  # noqa: BLE001 - never kill the inference loop
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("disaggregation failed; returning empty batch")
            # Events stay buffered: they are still valid evidence for a later
            # cycle, and dropping them would silently lose learning data.
            return BackendBatch(
                results=[],
                reconciliation=Reconciliation(
                    timestamp=utcnow(),
                    topology_version=topology.version,
                    quality_status="bad",
                    explanation=[f"inference_failed: {type(exc).__name__}"],
                ),
                diagnostics={"error": str(exc), "topology_version": topology.version},
            )

        results = list(batch.results)
        self.cycles += 1
        self.last_error = None

        self._update_energy(results)
        self._learn(window, topology)

        diagnostics = dict(batch.diagnostics or {})
        diagnostics["energy"] = self._energy_summary()
        diagnostics["learning"] = self._learning_summary()
        gaps = self._measurement_gap_summary(topology, results, batch.diagnostics or {})
        diagnostics["measurement_gaps"] = gaps
        self._explain_measurement_gaps(batch.reconciliation, gaps)
        return BackendBatch(
            results=results,
            reconciliation=batch.reconciliation,
            diagnostics=diagnostics,
        )

    def _refresh_links(self, topology: Topology) -> None:
        """Recompute entity ambiguity only when the topology actually changed."""
        signature = (int(getattr(topology, "version", 0) or 0), len(topology.devices))
        if signature == self._topology_signature:
            return
        self._topology_signature = signature
        self._ambiguous = ambiguous_entities(topology)
        if self._ambiguous:
            log.warning(
                "topology declares %d entity(ies) for more than one device; "
                "their samples will be rejected as ambiguous",
                len(self._ambiguous),
            )
        self._apply_state_overrides(topology)

    def _apply_state_overrides(self, topology: Topology) -> None:
        """Push per-device state-label corrections into the sample filter.

        The disaggregation engine reads these straight from the topology; the
        learning filter needs them handed over, because it judges a sample's
        sign from the state label.  Without this, a spa reporting ``Cooling``
        at 22 W would teach the model that cooling is a 4.5 kW event.
        """
        pipeline = self.learning
        if pipeline is None:
            return
        overrides: dict[str, dict[str, str]] = {}
        for device in topology.devices:
            metadata = device.metadata if isinstance(device.metadata, Mapping) else {}
            raw = metadata.get(STATE_OVERRIDES_KEY)
            if not isinstance(raw, Mapping):
                continue
            cleaned = {
                str(label).strip().lower(): str(meaning).strip().lower()
                for label, meaning in raw.items()
                if str(meaning).strip().lower() in {"on", "off"}
            }
            if cleaned:
                overrides[device.device_id] = cleaned
        try:
            pipeline.filter.set_state_overrides(overrides)
        except Exception as exc:  # noqa: BLE001 - learning must not break inference
            log.warning("failed to apply state overrides: %s", exc)

    # -- energy ----------------------------------------------------------- #
    def _update_energy(self, results: Sequence[InferenceResult]) -> None:
        """Feed results to the aggregator and publish a cumulative total.

        ``InferenceResult.energy_kwh`` is what the HA ``total_increasing``
        sensor publishes, so it is the running lifetime total for the device —
        not a per-cycle delta.  Bucket rows in ``energy_aggregates`` remain the
        authoritative per-interval series.

        Each result carries the age of the measurement it was derived from in
        ``measurement_age_seconds``.  It is handed to the aggregator so a value
        that went stale inside the window (a CT that stopped updating keeps
        being aligned for up to the staleness limit) cannot integrate frozen
        watts into kWh.
        """
        aggregator = self.aggregator
        if aggregator is None:
            return
        for result in results:
            try:
                aggregator.add_result(
                    result, measurement_age_s=_measurement_age(result)
                )
                total = aggregator.current_total_kwh(result.device_id)
            except Exception as exc:  # noqa: BLE001 - never kill inference
                self.last_error = f"aggregation: {type(exc).__name__}: {exc}"
                log.warning("energy aggregation failed for %s: %s", result.device_id, exc)
                continue
            if total is None:
                continue
            result.energy_kwh = round(max(0.0, float(total)), 6)
            self.energy_updates += 1
        self._cycles_since_flush += 1
        if self._cycles_since_flush >= self.flush_every_cycles:
            self.flush_energy()

    def flush_energy(self, now: datetime | None = None) -> list[Any]:
        """Persist open buckets and close idle device runs."""
        self._cycles_since_flush = 0
        aggregator = self.aggregator
        if aggregator is None:
            return []
        try:
            return list(aggregator.flush(now=now))
        except Exception as exc:  # noqa: BLE001 - persistence failures are logged
            self.last_error = f"flush: {type(exc).__name__}: {exc}"
            log.warning("energy flush failed: %s", exc)
            return []

    def _energy_summary(self) -> dict[str, Any]:
        aggregator = self.aggregator
        if aggregator is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "cycles_since_flush": self._cycles_since_flush,
            "updates": self.energy_updates,
            "bucket_sizes": list(aggregator.bucket_sizes),
        }

    @staticmethod
    def _explain_measurement_gaps(
        reconciliation: Reconciliation, gaps: Mapping[str, Any]
    ) -> None:
        """Append the measurement-gap explanation to ``Reconciliation.explanation``.

        Idempotent: the same sentence is never added twice, so a pinned
        reconciliation object cannot accumulate duplicates.
        """
        explanation = reconciliation.explanation
        missing = int(gaps.get("missing_direct_count") or 0)
        stale = int(gaps.get("no_fresh_measurement_count") or 0)
        if missing:
            reason_counts: dict[str, int] = {}
            for reason in (gaps.get("missing_direct") or {}).values():
                key = str(reason).split(":", 1)[0]
                reason_counts[key] = reason_counts.get(key, 0) + 1
            detail = ",".join(
                f"{key}×{count}" for key, count in sorted(reason_counts.items())
            )
            note = f"no_direct_measurement:{missing} device(s) ({detail})"
            if note not in explanation:
                explanation.append(note)
        if stale:
            note = f"no_fresh_measurement:{stale} device(s)"
            if note not in explanation:
                explanation.append(note)

    def _measurement_gap_summary(
        self,
        topology: Topology,
        results: Sequence[InferenceResult],
        engine_diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Explain a device-level 0 W that is not a real zero.

        Two distinct situations produced unexplained zeros in production:

        * **No direct measurement.**  A circuit is metered by a ``branch`` CT,
          which ``resolve_direct`` never reads (it requires a
          ``DEDICATED_DEVICE`` channel), so the Level-1 path yields nothing even
          when the device is the only thing on the circuit.  The type gate
          belongs to the electrical layer; this records the fact and *why* so a
          dashboard can say "0 W direct because all 21 circuits are classified
          branch" instead of showing a bare zero.
        * **No fresh measurement.**  The device has an attribution row but the
          CT value is stale/bad/missing, so nothing current describes it.  Such
          a device still counts towards an apparently healthy quality score,
          which is why it is surfaced as a first-class reason.

        No value is invented here: this is a read-only explanation built from
        the topology graph, the batch's own direct measurements and the results.
        """
        direct = engine_diagnostics.get("direct_measurements")
        direct_map: dict[str, Any] = direct if isinstance(direct, Mapping) else {}
        direct_count = sum(
            1
            for reading in direct_map.values()
            if isinstance(reading, Mapping)
            and reading.get("usable")
            and reading.get("power_w") is not None
        )
        by_device = {r.device_id: r for r in results}

        try:
            graph = build_graph(topology)
        except Exception:  # noqa: BLE001 - an explanation must never break inference
            graph = None

        missing_direct: dict[str, str] = {}
        no_fresh: dict[str, str] = {}
        max_age = float(self.max_measurement_age_s)

        for device in topology.devices:
            device_id = device.device_id
            reading = direct_map.get(device_id)
            if not (isinstance(reading, Mapping) and reading.get("usable") and reading.get("power_w") is not None):
                if graph is None:
                    missing_direct[device_id] = "topology_graph_unavailable"
                elif graph.dedicated_channel_for(device_id) is None:
                    kinds = sorted({
                        str(_channel_type(graph, cid))
                        for cid in graph.attribution_ct_ids(device_id)
                    })
                    missing_direct[device_id] = (
                        "no_dedicated_device_ct"
                        + (f":attribution_ct_types={'/'.join(kinds)}" if kinds else ":no_attribution_ct")
                    )
                elif isinstance(reading, Mapping):
                    reasons = reading.get("reasons")
                    missing_direct[device_id] = "direct_reading_unusable" + (
                        f":{','.join(str(r) for r in reasons)}"
                        if isinstance(reasons, Sequence) and not isinstance(reasons, str) and reasons
                        else ""
                    )
                else:
                    missing_direct[device_id] = "direct_reading_absent_from_window"

            result = by_device.get(device_id)
            if result is not None and not _has_fresh_measurement(result, max_age):
                no_fresh[device_id] = _stale_reason(result, max_age)

        return {
            "direct_measured_count": direct_count,
            "missing_direct": missing_direct,
            "missing_direct_count": len(missing_direct),
            "no_fresh_measurement": no_fresh,
            "no_fresh_measurement_count": len(no_fresh),
        }

    # -- learning --------------------------------------------------------- #
    def _learn(self, window: Any, topology: Topology) -> None:
        """Collect samples once their post-event window has actually elapsed.

        ``SampleCollector.build`` needs readings *after* the transition (it
        compares a pre-event baseline with a post-settle event level).  Building
        a sample at the very cycle the event arrives would see only the baseline
        half and be rejected for missing measurement, which is exactly what the
        live audit's 15.7 s cadence makes unavoidable.  Events therefore wait
        until the collector's own window length has passed.

        The age check is deliberately *event-relative*, never wall-clock: a
        buffered backlog replayed after a disconnection still matures normally.
        """
        pipeline = self.learning
        if pipeline is None or not self.learning_active:
            return
        now = _window_time(window)
        mature: list[Any] = []
        still_pending: list[Any] = []
        settle_s = float(getattr(pipeline.collector.thresholds, "settle_s", 5.0))
        window_s = float(pipeline.collector.window_s)
        for event in self._pending_events:
            at = _event_time(event)
            if at is not None and (now - at).total_seconds() >= window_s + settle_s:
                mature.append(event)
            else:
                still_pending.append(event)
        self._pending_events.clear()
        self._pending_events.extend(still_pending)
        try:
            # ``consider`` both records the aligned window (the baseline/event
            # history every sample is built from) and builds candidates.  It
            # must run every cycle even when no event is mature, otherwise the
            # collector has no history at all and every sample is rejected for
            # missing measurement.
            samples = pipeline.consider(
                [self._bridge_event(event, topology) for event in mature],
                window,
            )
        except Exception as exc:  # noqa: BLE001 - learning must not break inference
            self.last_error = f"learning: {type(exc).__name__}: {exc}"
            log.warning("learning sample collection failed: %s", exc)
            return
        if not samples and not getattr(pipeline, "last_rejected", None):
            return
        rejected = list(getattr(pipeline, "last_rejected", []) or [])
        self.samples_collected += len(samples) + len(rejected)
        self.samples_accepted += len(samples)
        self._maybe_train(topology)

    def _maybe_train(self, topology: Topology) -> None:
        pipeline = self.learning
        if pipeline is None:
            return
        now = utcnow()
        if (
            self.last_train_at is not None
            and (now - self.last_train_at).total_seconds() < self.train_interval_s
        ):
            return
        # Only stamp the throttle when a device was actually ready.  Stamping it
        # on the first cycle (when almost no samples exist yet) would suppress
        # training for a full interval after the data finally arrived.
        ready_devices: list[Any] = []
        for device in topology.devices:
            try:
                accepted = pipeline.stored_samples(device.device_id, accepted_only=True)
                ready, reason = pipeline.trainer.should_train(
                    device.device_id, accepted, pipeline.registry.versions(device.device_id)
                )
                if not ready:
                    log.debug("no training for %s: %s", device.device_id, reason)
                    continue
                ready_devices.append(device)
            except Exception as exc:  # noqa: BLE001 - per-device isolation
                log.warning("training readiness check failed for %s: %s", device.device_id, exc)
        if not ready_devices:
            return
        self.last_train_at = now
        for device in ready_devices:
            try:
                self.training_attempts += 1
                candidate, decision = pipeline.retrain_device(device.device_id, device=device)
                if candidate is None:
                    continue
                if decision is not None and decision.promoted:
                    self.models_promoted += 1
                    log.info(
                        "promoted model %s v%s for %s",
                        candidate.model_type,
                        candidate.version,
                        device.device_id,
                    )
                else:
                    log.info(
                        "candidate model for %s kept for review: %s",
                        device.device_id,
                        getattr(decision, "reason", "unknown"),
                    )
            except Exception as exc:  # noqa: BLE001 - per-device isolation
                log.warning("training failed for %s: %s", device.device_id, exc)

    def _bridge_event(self, event: Any, topology: Topology) -> dict[str, Any]:
        """Attach the topology's device + CT association to an HA event.

        The HA event carries ``entity_id``; WattMosaic keys everything by its own
        ``DeviceTopology.device_id``.  The link is declared in the topology
        (``device_links``): a device owns its CT entities and any state entities
        listed in ``metadata["state_entity_ids"]``.  Names are never compared and
        the HA device registry is never consulted, because a guessed association
        would fabricate attribution.
        """
        entity_id = _event_entity(event)
        link = device_for_entity(topology, entity_id)
        attributes = _event_attributes(event)
        attributes["topology_quality"] = self._topology_quality(topology)
        ambiguous = self._ambiguous.get(entity_id or "")
        if ambiguous:
            attributes["topology_candidates"] = len(ambiguous)
            attributes["topology_quality"] = "ambiguous"
        if link is None:
            return {
                "device_id": entity_id or "unknown",
                "entity_id": entity_id,
                "new_state": _event_state(event),
                "attributes": attributes,
                "occurred_at": _event_time(event),
            }
        device_id, ct_ids = link
        attributes["supporting_ct_ids"] = list(ct_ids)
        return {
            "device_id": device_id,
            "entity_id": entity_id,
            "new_state": _event_state(event),
            "attributes": attributes,
            "occurred_at": _event_time(event),
        }

    def _associations(self, topology: Topology) -> dict[str, tuple[str, tuple[str, ...]]]:
        """``entity_id -> (device_id, ct_ids)`` derived only from the topology."""
        return entity_links(topology)

    @staticmethod
    def _topology_quality(topology: Topology) -> str:
        status = getattr(topology, "status", None)
        value = getattr(status, "value", status)
        return "confirmed" if str(value) == "confirmed" else "candidate"

    def _learning_summary(self) -> dict[str, Any]:
        pipeline = self.learning
        if pipeline is None:
            return {"enabled": False}
        rejected = getattr(pipeline, "last_rejected", []) or []
        return {
            "enabled": True,
            "active": self.learning_active,
            "samples_collected": self.samples_collected,
            "samples_accepted": self.samples_accepted,
            "samples_rejected_last_batch": len(rejected),
            "training_attempts": self.training_attempts,
            "models_promoted": self.models_promoted,
            "last_train_at": None if self.last_train_at is None else self.last_train_at.isoformat(),
        }

    # -- lifecycle hooks the facade may call ------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "backend": "WattMosaicBackend",
            "disaggregation": type(self.engine).__name__,
            "model_registry": self.registry is not None,
            "learning": self.learning is not None,
            "learning_active": self.learning_active,
            "energy_aggregation": self.aggregator is not None,
            "cycles": self.cycles,
            "samples_collected": self.samples_collected,
            "samples_accepted": self.samples_accepted,
            "training_attempts": self.training_attempts,
            "models_promoted": self.models_promoted,
            "energy_updates": self.energy_updates,
            "last_train_at": None if self.last_train_at is None else self.last_train_at.isoformat(),
            "last_error": self.last_error,
        }

    def set_learning_active(self, active: bool) -> None:
        self.learning_active = bool(active)

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


# --------------------------------------------------------------------------- #
# Result-level measurement freshness
# --------------------------------------------------------------------------- #


def _measurement_age(result: InferenceResult) -> float | None:
    """Age of the measurement behind ``result``, or ``None`` if not stated."""
    raw = getattr(result, "measurement_age_seconds", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0.0 else None


def _quality_text(result: InferenceResult) -> str:
    quality = result.quality_status
    return quality.value if hasattr(quality, "value") else str(quality)


def _source_text(result: InferenceResult) -> str:
    source = result.source
    return source.value if hasattr(source, "value") else str(source)


def _has_fresh_measurement(result: InferenceResult, max_age_s: float) -> bool:
    """Whether this result is a current, usable observation of the load.

    ``Source.UNKNOWN`` and placeholder values never count: they are the exact
    readings a "no fresh measurement" count exists to reveal.
    """
    if _source_text(result) == Source.UNKNOWN.value:
        return False
    provenance = result.provenance
    if isinstance(provenance, Mapping) and provenance.get("power_w_is_placeholder"):
        return False
    if _quality_text(result) in (QualityStatus.BAD.value, QualityStatus.MISSING.value):
        return False
    age = _measurement_age(result)
    return not (age is not None and age > max_age_s)


def _stale_reason(result: InferenceResult, max_age_s: float) -> str:
    if _source_text(result) == Source.UNKNOWN.value:
        return "source_unknown"
    provenance = result.provenance
    if isinstance(provenance, Mapping) and provenance.get("power_w_is_placeholder"):
        return "power_w_is_placeholder"
    quality = _quality_text(result)
    if quality in (QualityStatus.BAD.value, QualityStatus.MISSING.value):
        return f"quality_{quality}"
    age = _measurement_age(result)
    if age is not None and age > max_age_s:
        return f"measurement_age_{age:.0f}s_exceeds_{max_age_s:.0f}s"
    return "no_usable_measurement"


def _channel_type(graph: Any, ct_id: str) -> str:
    """The ``CTType`` value of one channel, or ``unknown``."""
    try:
        channel = graph.channels.get(ct_id)
    except Exception:  # noqa: BLE001 - explanation only
        return "unknown"
    if channel is None:
        return "unknown"
    ct_type = getattr(channel, "ct_type", None)
    return str(ct_type.value if hasattr(ct_type, "value") else ct_type or "unknown")


__all__ = ["BackendBatch", "WattMosaicBackend", "build_backend"]
