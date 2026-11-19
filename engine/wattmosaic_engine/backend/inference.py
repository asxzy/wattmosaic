"""Inference half of ``WattMosaicBackend.process``: solve, energy, diagnostics.

Owned by I7/Z2 after X03.  I7 adds the v3 path next to the legacy one: the pure
entry points :func:`~wattmosaic_engine.inference.orchestrator.infer_group` /
:func:`~wattmosaic_engine.inference.orchestrator.infer_steady` turn event groups
and held readings into :class:`JointInferenceResult`s.  The legacy engine path
below stays working until Z2 removes it; the v3 path is selected only when the
caller supplies the typed v3 inputs (``event_group`` / ``readings`` plus an
``ObservationOperator``), never by a global flag.

Production code here never imports ``runtime.fakes``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from ..contracts import DeviceEstimate, ReconciliationV3
from ..device_links import STATE_OVERRIDES_KEY, ambiguous_entities, entity_links
from .batch import BackendBatch

log = logging.getLogger("wattmosaic.integration")


# --------------------------------------------------------------------------- #
# v3 orchestration path (I7).  These helpers are pure: every time comes from an
# injected clock, every cross-area value from a typed contract/Protocol.  They
# are deliberately additive so the legacy ``process`` below is unchanged.
# --------------------------------------------------------------------------- #


def infer_group_v3(
    group: Any,
    operator: Any,
    models: Any,
    residual: Any,
    history: Any,
    matches: Any,
    versions: Any,
    clock: Any,
) -> Any:
    """Call the pure v3 event-delta entry point with injected inputs."""
    from ..inference.orchestrator import infer_group

    return infer_group(
        group, operator, models, residual, history, matches, versions, clock
    )


def infer_steady_v3(
    readings: Mapping[str, Any],
    operator: Any,
    models: Any,
    residual: Any,
    established: Any,
    versions: Any,
    clock: Any,
) -> Any:
    """Call the pure v3 steady-state entry point with injected inputs."""
    from ..inference.orchestrator import infer_steady

    return infer_steady(
        readings, operator, models, residual, established, versions, clock
    )


class InferenceMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    def process(
        self,
        window: Any,
        *,
        device_states: Any,
        topology: Any = None,
        models: Any = None,
        events: Iterable[Any] | None = None,
    ) -> BackendBatch:
        # Z2: the legacy 5 s absolute solve is gone.  Production inference now
        # runs per event group through the three-loop runtime (P6), which calls
        # ``infer_group``/``infer_steady`` directly.  This compatibility shim
        # keeps the backend constructible for diagnostics and old callers but
        # never fabricates a per-device watt value.
        #
        # ZI: the legacy InferenceResult/Reconciliation contracts are removed.
        # The backend's neutral handoff now uses the v3 DeviceEstimate list and
        # nullable ReconciliationV3, so no v2-only shape is constructed here.
        return BackendBatch(
            results=[],
            reconciliation=ReconciliationV3(),
            diagnostics={"legacy_path_removed": True},
        )

    def _process_legacy_unused(
        self,
        window: Any,
        *,
        device_states: Any,
        topology: Any = None,
        models: Any = None,
        events: Iterable[Any] | None = None,
    ) -> BackendBatch:
        cycle_events: list[Any] = list(events) if events is not None else []
        if cycle_events:
            for event in cycle_events:
                self._pending_events.append(event)
        self._refresh_links(topology)
        results: list[Any] = []
        self.cycles += 1
        self.last_error = None
        self._update_energy(results)
        self._learn(window, topology)
        self._analyze(window, topology, results, cycle_events)
        self._maybe_reanalyze(results, topology)
        diagnostics: dict[str, Any] = {}
        diagnostics["energy"] = self._energy_summary()
        diagnostics["learning"] = self._learning_summary()
        diagnostics["audit"] = self._audit_summary()
        # Legacy helper retained for API compatibility only; callers must use
        # the three-loop runtime for inference.
        return BackendBatch(
            results=results,
            reconciliation=ReconciliationV3(),
            diagnostics=diagnostics,
        )

    def _refresh_links(self, topology: Any) -> None:
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

    def _apply_state_overrides(self, topology: Any) -> None:
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

    def _update_energy(self, results: Sequence[DeviceEstimate]) -> None:
        """Feed v3 device estimates to the aggregator (a no-op shim).

        ZI: the legacy ``InferenceResult.energy_kwh`` handoff is removed.  A v3
        :class:`~wattmosaic_engine.contracts.DeviceEstimate` carries ``point_w``,
        not a device energy total, so the three-loop runtime owns energy; this
        backend compatibility shim never fabricates a watt or kWh value.
        """

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
        reconciliation: ReconciliationV3, gaps: Mapping[str, Any]
    ) -> None:
        """Explain measurement gaps on the v3 nullable reconciliation.

        ``ReconciliationV3`` has no mutable ``explanation`` list (absence is a
        first-class ``None`` on every term), so this compatibility shim reduces
        the explanation to a read-only dict returned by
        :meth:`_measurement_gap_summary` and keeps no hidden state.  The v3
        surface never fabricates a 0 W to stand in for a missing reading.
        """

    def _measurement_gap_summary(
        self,
        topology: Any,
        results: Sequence[DeviceEstimate],
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
        by_device = {r.subject_id: r for r in results}

        graph = None  # legacy topology graph removed in Z2

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

    def _associations(self, topology: Any) -> dict[str, tuple[str, tuple[str, ...]]]:
        """``entity_id -> (device_id, ct_ids)`` derived only from the topology."""
        return entity_links(topology)

    def status(self) -> dict[str, Any]:
        learning = self.learning_status()
        return {
            "backend": "WattMosaicBackend",
            "disaggregation": "three-loop-v3",
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


def _measurement_age(result: DeviceEstimate) -> float | None:
    """Age of the measurement behind a v3 estimate, or ``None`` if not stated.

    A v3 ``DeviceEstimate`` has no measurement-age field; its provenance lives
    in ``evidence_refs``.  An absent age is honest ``None`` — never a fabricated
    ``0`` — and the estimator degrades to "no age stated".
    """
    raw = getattr(result, "measurement_age_s", None)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0.0 else None


def _has_fresh_measurement(result: DeviceEstimate, max_age_s: float) -> bool:
    """Whether a v3 estimate describes a current, usable observation of a load.

    A non-identifiable / UNKNOWN estimate has ``point_w is None`` and contributes
    no observation (HB §18), so it is never counted as a fresh measurement.
    """
    if getattr(result, "point_w", None) is None:
        return False
    age = _measurement_age(result)
    return not (age is not None and age > max_age_s)


def _stale_reason(result: DeviceEstimate, max_age_s: float) -> str:
    if getattr(result, "point_w", None) is None:
        return "estimate_unknown"
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
