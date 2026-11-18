"""Inference half of ``WattMosaicBackend.process``: solve, energy, diagnostics.

Owned by I7/Z2 after X03."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from ..contracts import InferenceResult, QualityStatus, Reconciliation, Source, Topology
from ..device_links import STATE_OVERRIDES_KEY, ambiguous_entities, entity_links
from ..disaggregation.engine import InferenceBatch
from ..electrical.topology import build_graph
from .batch import BackendBatch

log = logging.getLogger("wattmosaic.integration")


class InferenceMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    def process(
        self,
        window: Any,
        *,
        device_states: Any,
        topology: Topology,
        models: Any = None,
        events: Iterable[Any] | None = None,
    ) -> BackendBatch:
        cycle_events: list[Any] = list(events) if events is not None else []
        if cycle_events:
            for event in cycle_events:
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
                    timestamp=self._clock(),
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
        self._analyze(window, topology, results, cycle_events)
        self._maybe_reanalyze(results, topology)

        diagnostics = dict(batch.diagnostics or {})
        diagnostics["energy"] = self._energy_summary()
        diagnostics["learning"] = self._learning_summary()
        diagnostics["audit"] = self._audit_summary()
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

        * **No direct measurement.**  There is no direct measurement path any
          more, so the engine yields nothing for every circuit.  This records
          that fact and *why* so a dashboard can say "0 W direct because this
          device has no private channel" instead of showing a bare zero.
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
                elif isinstance(reading, Mapping):
                    reasons = reading.get("reasons")
                    missing_direct[device_id] = "direct_reading_unusable" + (
                        f":{','.join(str(r) for r in reasons)}"
                        if isinstance(reasons, Sequence) and not isinstance(reasons, str) and reasons
                        else ""
                    )
                else:
                    kinds = sorted({
                        str(_channel_type(graph, cid))
                        for cid in graph.attribution_ct_ids(device_id)
                    })
                    missing_direct[device_id] = (
                        "no_private_channel"
                        + (f":attribution_ct_types={'/'.join(kinds)}" if kinds else ":no_attribution_ct")
                    )

            result = by_device.get(device_id)
            if result is not None and not _has_fresh_measurement(result, max_age):
                no_fresh[device_id] = _stale_reason(result, max_age)

        # Structural guard, computed from the *graph and the readings* rather
        # than from the loop's own branches above: every device the graph says
        # is capable of a direct reading must be either measured or named in
        # ``missing_direct``.  If a future edit narrows that loop, a capable
        # device can no longer vanish from the explanation without this
        # reporting it.
        #
        # No device is capable: the direct measurement path was deleted along
        # with the channel role that gated it, so every device is explained by
        # ``missing_direct`` instead.  The guard and the verdict below stay so
        # the payload keeps its shape for its consumers.
        capable: set[str] = set()
        measured = {
            did
            for did, reading in direct_map.items()
            if isinstance(reading, Mapping)
            and reading.get("usable")
            and reading.get("power_w") is not None
        }
        unexplained = sorted(capable - measured - set(missing_direct))
        # A 0 W direct count is only acceptable when *every* device that has no
        # usable direct reading says why (``missing_direct``).  The count alone
        # cannot distinguish "this install has no row that could be read
        # directly" -- a legitimate, now universal outcome -- from "the direct
        # path silently produced nothing", which was a defect when the path
        # still existed.
        if not capable:
            availability = "no_direct_capable_device"
        elif unexplained:
            availability = "unexplained_unavailable"
        elif measured:
            availability = "measured"
        else:
            availability = "explained_unavailable"

        return {
            "direct_measured_count": direct_count,
            "missing_direct": missing_direct,
            "missing_direct_count": len(missing_direct),
            "direct_capable_count": len(capable),
            "direct_availability": availability,
            "direct_unavailable_explained": not unexplained,
            "unexplained_direct_devices": unexplained,
            "no_fresh_measurement": no_fresh,
            "no_fresh_measurement_count": len(no_fresh),
        }

    def _associations(self, topology: Topology) -> dict[str, tuple[str, tuple[str, ...]]]:
        """``entity_id -> (device_id, ct_ids)`` derived only from the topology."""
        return entity_links(topology)

    def status(self) -> dict[str, Any]:
        learning = self.learning_status()
        return {
            "backend": "WattMosaicBackend",
            "disaggregation": type(self.engine).__name__,
            "model_registry": self.registry is not None,
            "learning": self.learning is not None,
            "learning_active": self.learning_active,
            # ``effective_state`` is the honest one: ``learning`` only when
            # samples are actually being accepted, ``blocked`` when every sample
            # is rejected.  ``learning_active`` above stays the loop's own flag.
            "learning_status": learning,
            "learning_effective_state": learning["effective_state"],
            "energy_aggregation": self.aggregator is not None,
            "cycles": self.cycles,
            "samples_collected": self.samples_collected,
            "samples_accepted": self.samples_accepted,
            "samples_rejected": self.samples_rejected,
            "training_attempts": self.training_attempts,
            "models_promoted": self.models_promoted,
            "energy_updates": self.energy_updates,
            "last_train_at": None if self.last_train_at is None else self.last_train_at.isoformat(),
            "audit": self._audit_summary(),
            "last_error": self.last_error,
        }

    def set_learning_active(self, active: bool) -> None:
        self.learning_active = bool(active)

    def close(self) -> None:
        """Flush anything still open (called from the engine shutdown path)."""
        self.flush_energy()


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
