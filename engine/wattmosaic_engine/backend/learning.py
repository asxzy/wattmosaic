"""Learning half of the backend: sample maturation, training and status.

Owned by L6 after X03."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..contracts import Topology
from ..device_links import device_for_entity
from ..learning.reporting import build_learning_status
from ..models.protocols import window_time as _window_time

log = logging.getLogger("wattmosaic.integration")


class LearningMixin:
    """Behaviour for one responsibility of :class:`WattMosaicBackend`."""

    @property
    def _accounting(self) -> Any | None:
        return getattr(self.learning, "accounting", None)

    @property
    def samples_collected(self) -> int:
        """Samples the filter has *evaluated* since start (accepted + rejected)."""
        accounting = self._accounting
        return int(getattr(accounting, "seen", 0) or 0)

    @property
    def samples_accepted(self) -> int:
        """Samples the filter has accepted since start."""
        accounting = self._accounting
        return int(getattr(accounting, "accepted", 0) or 0)

    @property
    def samples_rejected(self) -> int:
        """Samples the filter has rejected since start."""
        accounting = self._accounting
        return int(getattr(accounting, "rejected", 0) or 0)

    def learning_status(self) -> dict[str, Any]:
        """Honest learning status: counters + effective_state + remedy.

        ``effective_state`` is ``learning`` only when the pipeline runs **and**
        accepts samples; ``blocked`` when it runs but rejects all of them (an
        unconfirmed topology is the common cause); ``off`` when the pipeline is
        absent or paused.  O(1) with no per-sample storage.
        """
        pipeline = self.learning
        accounting = self._accounting
        if pipeline is None:
            return build_learning_status(
                enabled=False,
                active=False,
                topology_quality=self._topology_quality_seen,
            )
        snapshot = (
            accounting.snapshot()
            if accounting is not None
            else {"samples_seen": 0, "samples_accepted": 0, "samples_rejected": 0, "rejection_reasons": {}}
        )
        return build_learning_status(
            enabled=True,
            active=bool(self.learning_active),
            samples_seen=int(snapshot.get("samples_seen") or 0),
            samples_accepted=int(snapshot.get("samples_accepted") or 0),
            samples_rejected=int(snapshot.get("samples_rejected") or 0),
            rejection_reasons=snapshot.get("rejection_reasons") or {},
            topology_quality=self._topology_quality_seen,
        )

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
        # Record the topology quality this pass will stamp onto every sample, so
        # ``learning_status`` can explain a 100%-rejection pipeline (an
        # unconfirmed topology rejects by construction) before it has even seen
        # one sample.  One attribute assignment per cycle.
        self._topology_quality_seen = self._topology_quality(topology)
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
        self._maybe_train(topology)

    def _maybe_train(self, topology: Topology) -> None:
        """Realtime, event-driven training on matured samples.

        There is no wall-clock throttle.  A device is trained as soon as a *new*
        matured, accepted sample exists for it — the per-device ``_trained_to``
        watermark is the only debounce, so an inference cycle with no new data
        costs nothing while a cycle that matured new evidence trains immediately.
        Sample maturation (``window_s + settle_s``) and the trainer's per-device
        readiness checks still apply: they are correctness, not a throttle.
        """
        pipeline = self.learning
        if pipeline is None:
            return
        now = self._clock()
        # Only stamp the diagnostic timestamp when a device was actually trained.
        trained_any = False
        for device in topology.devices:
            try:
                accepted = pipeline.stored_samples(device.device_id, accepted_only=True)
                ready, reason = pipeline.trainer.should_train(
                    device.device_id, accepted, pipeline.registry.versions(device.device_id)
                )
                if not ready:
                    log.debug("no training for %s: %s", device.device_id, reason)
                    continue
                # Event-driven debounce: the newest accepted sample timestamp is
                # this device's progress watermark.  Retrain only when a strictly
                # newer matured sample arrived since the last pass.  This is
                # independent of model promotion, so a candidate kept for review
                # does not cause a retrain on every 5 s cycle with the same data.
                newest = max(s.captured_at for s in accepted)
                seen_to = self._trained_to.get(device.device_id)
                if seen_to is not None and newest <= seen_to:
                    log.debug("no new matured samples for %s since %s", device.device_id, seen_to)
                    continue
                self._trained_to[device.device_id] = newest
            except Exception as exc:  # noqa: BLE001 - per-device isolation
                log.warning("training readiness check failed for %s: %s", device.device_id, exc)
                continue
            trained_any = True
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
        if trained_any:
            self.last_train_at = now

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
            "samples_rejected": self.samples_rejected,
            "samples_rejected_last_batch": len(rejected),
            "training_attempts": self.training_attempts,
            "models_promoted": self.models_promoted,
            "last_train_at": None if self.last_train_at is None else self.last_train_at.isoformat(),
        }


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
