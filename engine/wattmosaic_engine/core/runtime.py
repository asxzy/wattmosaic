"""Runtime: EngineCore lifecycle, ingestion callbacks, loops, persist queue.

Owned by P6 after X03 (the three-loop runtime).  All state lives on
``EngineCore`` itself, so every attribute is shared through ``self`` and no call
site changes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from ..api.facade import maybe_await
from ..backend.loader import InferenceBackendUnavailable, load_inference_backend
from ..contracts import DataQualityReport, Measurement, Reconciliation, Topology
from ..device_links import device_for_entity, state_entity_for_device
from ..ingestion.correlation import level_from_attributes
from ..synchronization.aligner import (
    AlignedChannel,
    AlignedWindow,
    ChannelHealth,
    EnergyChannel,
    EnergyWindow,
)
from .discovery_api import TOPOLOGY_BOOTSTRAP_WAIT_S

log = logging.getLogger("wattmosaic.main")

#: Persist-queue drain chunk and the writer loop's flush cadence.
SAMPLE_BATCH_SIZE = 100
SAMPLE_FLUSH_INTERVAL_S = 2.0
#: How often housekeeping (retention, integrity cache) runs.
MAINTENANCE_INTERVAL_S = 60.0


class RuntimeMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    async def start(self, *, start_loops: bool = True) -> None:
        """Start ingestion (and optionally the internal loops).

        ``_run_engine`` passes ``start_loops=False`` because the supervisor owns
        those loops so it can restart them.  Tests and ``--once`` use the default.
        """
        backend = None
        try:
            backend = load_inference_backend(self.config, self.db)
            self.inference_backend = backend
            self.inference_error = None
            # The adapter builds the model registry; share it with the core so
            # /models and model commands use the database.
            registry = getattr(backend, "registry", None)
            if registry is not None:
                self.model_registry = registry
            log.info("inference backend loaded: %s", type(backend).__name__)
            self._sync_learning_state(backend)
        except InferenceBackendUnavailable as exc:
            self.inference_error = str(exc)
            self.learning_active = False
            log.warning("running in degraded mode: %s", exc)

        await self.hasource.start(on_sample=self.on_sample, on_event=self.on_event)
        # The first state snapshot arrives asynchronously from ``_prime()``;
        # waiting a bounded time here is what makes discovery see real entities
        # instead of an empty cache on a genuine fresh install.
        await self.bootstrap_topology(wait_connected_s=TOPOLOGY_BOOTSTRAP_WAIT_S)
        if start_loops:
            self._tasks = [
                asyncio.create_task(self._writer_loop(), name="wm-writer"),
                asyncio.create_task(self._quality_loop(), name="wm-quality"),
                asyncio.create_task(self._inference_loop(), name="wm-inference"),
                asyncio.create_task(self._maintenance_loop(), name="wm-maintenance"),
            ]

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        log.info("stopping engine components")
        try:
            await self.hasource.stop()
        except Exception:  # noqa: BLE001 - shutdown must complete
            log.exception("error stopping HA source")
        self.persist_queue.close()
        # Stop workers first so nothing else enqueues, then drain what is left.
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await asyncio.wait_for(self._drain_persist_queue(), timeout=10.0)
        except TimeoutError:
            log.warning("persistence queue did not drain within 10s")
        except Exception:  # noqa: BLE001 - shutdown must still complete
            log.exception("failed to drain the persistence queue during shutdown")
        # Persist whatever energy buckets / runs are still open so a restart does
        # not lose the tail of the current bucket.
        close_backend = getattr(self.inference_backend, "close", None)
        if callable(close_backend):
            try:
                close_backend()
            except Exception:  # noqa: BLE001
                log.exception("failed to flush the inference backend on shutdown")
        try:
            self.db.checkpoint()
        except Exception:  # noqa: BLE001
            log.exception("database checkpoint failed")
        try:
            self.db.log_system_event("info", "lifecycle", "engine shutdown complete")
        except Exception:  # noqa: BLE001
            log.debug("shutdown system-event write failed", exc_info=True)
        self.latest_quality = self.latest_quality  # keep last value for diagnostics
        log.info("engine stopped")

    async def _drain_persist_queue(self) -> None:
        """Persist everything left in the queue, on the event-loop thread.

        Drain in bounded chunks and yield to the loop between them so shutdown
        stays responsive.
        """
        while True:
            batch: list[tuple[str, Any]] = []
            while len(batch) < SAMPLE_BATCH_SIZE:
                item = self.persist_queue.get_nowait()
                if item is None:
                    break
                batch.append(item)
            if not batch:
                return
            self._persist_batch(batch)
            await asyncio.sleep(0)

    async def on_sample(self, measurement: Measurement) -> None:
        self.samples_ingested += 1
        self.aligner.add(measurement)
        self.persist_queue.put_nowait(("sample", measurement))

    async def on_event(self, event: Any) -> None:
        entity_id = getattr(event, "entity_id", None)
        # Only entities the topology actually declares can produce attribution.
        #
        # HA emits ~30 state changes/second in a typical installation, but only 40
        # entities are declared link targets.  Every undeclared entity used to be
        # buffered for the learning pass and turned into a sample keyed by its
        # raw entity id, which:
        #   * filled the bounded buffer with noise (~2 000 events are needed to
        #     cover the 68 s maturity window, against a 512-event cap, so the
        #     buffer was permanently full and *every* event was evicted before
        #     it could mature — so every sample was rejected);
        #   * wrote ~100 000 `device_events` rows in 5 minutes for entities no
        #     device owns.
        # An unlinked entity is still a real observation, so it is counted — but
        # it is **not** persisted.  Home Assistant emits ~30 state changes/second
        # across ~500 entities in a typical installation, and only 40 are declared
        # link targets.  Writing every undeclared change grew `device_events` at
        # ~1.5 GB/day (99.98 % of all rows) for entities that no device owns and
        # that nothing ever reads: `recent_device_events` is not consulted in
        # production.  The spec requires avoiding unbounded growth, and a run
        # left recording for days must not fill the disk with codebook noise.
        # The counter is the audit signal: it still shows how much traffic the
        # topology fails to explain.
        topology = self._active_topology()
        link = device_for_entity(topology, entity_id) if entity_id else None
        self.events_ingested += 1
        if link is None:
            self.unlinked_events_ignored += 1
            return
        device_id, _ct_ids = link
        # ``device_states`` is consumed by the disaggregation engine, which keys it
        # by ``DeviceTopology.device_id`` — not by the HA entity id.  Storing the
        # entity id made every state lookup miss, so state-transition inference was
        # silently dead in production.
        #
        # Only a *runtime state* entity may write this slot.  A CT power entity
        # (``sensor.water_heater_power``) is linked for measurement, but it is not the
        # device's state: numeric readings never get here (``HASource`` consumes
        # them as measurements), yet availability changes do.  Writing
        # ``"unavailable"`` here replaced ``"off"``, and the next real ``"off"``
        # then read as a transition — one bridge blip manufactured two phantom
        # transitions.  The event is still persisted below; it just cannot claim
        # to be the device's state.
        if not state_entity_for_device(topology, device_id, entity_id):
            self.power_events_not_state += 1
            self.persist_queue.put_nowait(("event", event))
            return
        previous = self.device_states.get(device_id)
        self.device_states[device_id] = event.new_state
        # Record the measured output level alongside the state.  A dimmer's
        # state stays ``on`` across a brightness change, so this is the only
        # place the *amount* is known.  Absent brightness publishes nothing
        # (rather than 100 %), which keeps a non-dimmable fixture indistinguishable
        # from a dimmer that has not dimmed yet.
        level = level_from_attributes(getattr(event, "attributes", None))
        if level is None:
            self.device_levels.pop(device_id, None)
        else:
            self.device_levels[device_id] = level
        # Persist every declared event: the audit trail wants the observation even
        # when it is not a transition.
        self.persist_queue.put_nowait(("event", event))
        if previous is None:
            # First observation of this device in this process.  ``HASource``
            # replays the whole current snapshot at startup, carrying each
            # entity's *old* ``last_updated``; those entries are a state, not a
            # transition.  Queueing them produced samples timestamped hours in
            # the past whose baseline/event window had no CT readings at all, so
            # every one was rejected ``missing_measurement``.  A restart must not
            # invent a transition it did not observe.
            return
        if previous == event.new_state:
            # Attribute churn: not a transition.  The level (above) still moves.
            return
        self._pending_transitions.append(
            {
                "device_id": device_id,
                "previous_state": previous,
                "new_state": event.new_state,
                "occurred_at": _event_time(event),
            }
        )
        # Buffer for the inference backend's learning pass.  Only declared links
        # reach this point, so the buffer holds a handful of events rather than
        # the whole house's noise, and each one can actually mature.
        pending = getattr(self, "_pending_backend_events", None)
        if pending is None:
            pending = self._pending_backend_events = deque(maxlen=512)
        if len(pending) >= (pending.maxlen or 0):
            # The deque silently discards the oldest item.  Count it rather than
            # losing the evidence: a full buffer means samples are being dropped
            # and the status surface must say so.
            self.events_evicted_before_maturity += 1
        pending.append(event)

    async def _writer_loop(self) -> None:
        while not self._stopping:
            try:
                item = await asyncio.wait_for(self.persist_queue.get(), SAMPLE_FLUSH_INTERVAL_S)
            except TimeoutError:
                continue
            batch: list[tuple[str, Any]] = [] if item is None else [item]
            deadline = time.monotonic() + SAMPLE_FLUSH_INTERVAL_S
            while len(batch) < SAMPLE_BATCH_SIZE:
                nxt = self.persist_queue.get_nowait()
                if nxt is None:
                    break
                batch.append(nxt)
                if time.monotonic() > deadline:
                    break
            if batch:
                try:
                    self._persist_batch(batch)
                except Exception:  # noqa: BLE001 - one failed batch is not fatal
                    self.persistence_errors += 1
                    log.exception("failed to persist a batch of %d records", len(batch))

    def _persist_batch(self, batch: list[tuple[str, Any]]) -> None:
        sample_rows: list[dict[str, Any]] = []
        events: list[Any] = []
        for kind, payload in batch:
            if kind == "sample":
                sample_rows.append(
                    {
                        "ct_id": payload.ct_id,
                        "entity_id": payload.entity_id,
                        "value_w": payload.effective_power_w(),
                        "raw_value": payload.value,
                        "unit": payload.unit,
                        "state_timestamp": payload.state_timestamp,
                        "received_at": payload.received_at,
                        "physical_timestamp": payload.physical_timestamp,
                        "quality": payload.quality,
                        "repeated": payload.repeated,
                        "sequence": payload.sequence,
                    }
                )
            else:
                events.append(payload)
        if sample_rows:
            self.samples_persisted += self.db.insert_ct_samples(sample_rows)
        for event in events:
            self.db.insert_device_event(
                {
                    "event_id": event.event_id,
                    "device_id": event.device_id,
                    "entity_id": event.entity_id,
                    "event_type": event.event_type,
                    "previous_state": event.previous_state,
                    "new_state": event.new_state,
                    "attributes": event.attributes,
                    "occurred_at": event.occurred_at,
                    "received_at": event.received_at,
                }
            )
            self.events_persisted += 1

    def _active_topology(self) -> Topology | None:
        if self.topology_cache is None:
            self.topology_cache = self.db.load_topology()
        return self.topology_cache

    @staticmethod
    def _aligner_key(channel: Any) -> str:
        """Aligner buffer key for a topology channel (entity id without domain)."""
        entity_id = str(getattr(channel, "entity_id", "") or "")
        return entity_id.split(".", 1)[-1] if "." in entity_id else entity_id

    @staticmethod
    def _topology_energy_channel(channel: Any) -> bool:
        """True when a topology channel carries a cumulative energy counter."""
        metadata = getattr(channel, "metadata", None) or {}
        unit = str(metadata.get("unit", "")) if isinstance(metadata, dict) else ""
        if unit in ("kWh", "Wh"):
            return True
        ct_id = str(getattr(channel, "ct_id", "") or "")
        return ct_id.endswith(("_energy", "_return_energy"))

    def _power_window(self, topology: Topology) -> AlignedWindow:
        """Aligned power window keyed by topology ``ct_id``.

        The aligner buffers are keyed by the Home Assistant entity id without its
        domain prefix (``sensor.room_a_power`` → ``room_a_power``), while the topology
        refers to channels by ``ct_id``.  This method joins the two so the
        inference backend and the quality engine both see topology channel ids,
        with the originating ``entity_id`` attached to each reading.

        Energy counters are excluded: they live in :meth:`energy_window` and must
        never be scored or solved as power.
        """
        energy_keys = set(self.aligner.energy_channels())
        pairs = [
            (channel.ct_id, self._aligner_key(channel))
            for channel in topology.channels
            if not self._topology_energy_channel(channel)
        ]
        pairs = [(ct_id, key) for ct_id, key in pairs if key not in energy_keys]
        raw = self.aligner.window(
            channels=[key for _, key in pairs], max_age_s=self.config.stale_after_s
        )
        channels: dict[str, AlignedChannel] = {}
        for ct_id, key in pairs:
            channel = raw.channels.get(key)
            if channel is None:
                continue
            channels[ct_id] = replace(channel, channel=ct_id)
        return AlignedWindow(at=raw.at, max_age_s=raw.max_age_s, channels=channels)

    def _channel_health_for(self, topology: Topology) -> dict[str, ChannelHealth]:
        """Channel health keyed by topology ``ct_id`` (aligner uses entity names)."""
        health = self.aligner.channel_health()
        out: dict[str, ChannelHealth] = {}
        for channel in topology.channels:
            if self._topology_energy_channel(channel):
                continue
            key = self._aligner_key(channel)
            entry = health.get(key)
            if entry is not None:
                out[channel.ct_id] = replace(entry, channel=channel.ct_id)
        return out

    def _channel_noise_floors(self) -> dict[str, float]:
        """Measured innovation MAD per topology ``ct_id``, for CT step detection.

        The quality layer already computes this for every channel
        (``SampleAligner.channel_health().noise_mad_w``, a scaled MAD of
        consecutive-sample differences).  Recomputing it here would be a second
        definition of the same quantity and could disagree with the number the
        quality report shows, so it is read back and only re-keyed from the
        aligner's entity name to the topology's ``ct_id``.

        A channel with no health entry is simply absent: the detector then uses
        its configured scalar floor, which is the historical behaviour.
        """
        topology = self._active_topology()
        if topology is None:
            return {}
        by_key = {
            self._aligner_key(channel): channel.ct_id
            for channel in topology.channels
            if not self._topology_energy_channel(channel)
        }
        if not by_key:
            return {}
        floors: dict[str, float] = {}
        for key, health in self.aligner.channel_health().items():
            cid = by_key.get(key)
            if cid is None or health.noise_mad_w is None:
                continue
            value = float(health.noise_mad_w)
            if value >= 0.0 and value == value:  # finite, non-negative
                floors[cid] = value
        return floors

    def _energy_window_for(
        self, topology: Topology | None, max_age_s: float
    ) -> EnergyWindow:
        """Energy window keyed by topology ``ct_id`` where a channel is known.

        Energy stays in its own domain; this only fixes naming so quality output
        uses the same identifiers as the topology.
        """
        raw = self.aligner.energy_window(max_age_s=max_age_s)
        if topology is None:
            return raw
        by_key = {self._aligner_key(c): c.ct_id for c in topology.channels}
        if not by_key:
            return raw
        channels: dict[str, EnergyChannel] = {}
        for key, channel in raw.channels.items():
            ct_id = by_key.get(key, key)
            channels[ct_id] = replace(channel, channel=ct_id)
        return EnergyWindow(at=raw.at, max_age_s=raw.max_age_s, channels=channels)

    def _evaluate_quality(self) -> DataQualityReport:
        topology = self._active_topology()
        if topology is not None:
            window = self._power_window(topology)
            health = self._channel_health_for(topology)
        else:
            window = self.aligner.window(max_age_s=self.config.stale_after_s)
            health = self.aligner.channel_health()
        energy_window = self._energy_window_for(topology, self.config.stale_after_s)
        energy_health = self.aligner.energy_health()
        report = self.quality_engine.evaluate(
            window,
            health,
            topology=topology,
            reconciliation=self.latest_reconciliation,
            unavailable=self.hasource.unavailable_entities,
            energy_window=energy_window,
            energy_health=energy_health,
        )
        self.latest_quality = report
        return report

    async def _quality_loop(self) -> None:
        while not self._stopping:
            try:
                self._evaluate_quality()
                self.last_successful_processing_at = self._clock()
            except Exception:  # noqa: BLE001
                log.exception("data quality evaluation failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), max(1.0, self.config.inference_interval_s)
                )
                return
            except TimeoutError:
                continue

    async def _inference_loop(self) -> None:
        while not self._stopping:
            if self.inference_backend is not None:
                try:
                    await self.run_inference_once()
                except Exception:  # noqa: BLE001 - never kill the loop
                    log.exception("inference cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), max(1.0, self.config.inference_interval_s)
                )
                return
            except TimeoutError:
                continue

    async def run_inference_once(self) -> dict[str, Any]:
        """Run one inference cycle if a backend is attached.  Returns a summary."""
        topology = self._active_topology()
        if topology is None:
            # Startup discovery may have lost the race against the first Home
            # Assistant connection, or the database may have been empty and the
            # connection appeared later.  Keep trying so the engine cannot sit
            # permanently inert; this is a no-op once a topology exists.
            retry = await self.maybe_bootstrap_topology()
            if retry is not None and retry.get("status") == "candidate":
                topology = self._active_topology()
        if topology is None:
            # Never silent: this is the condition that made a healthy-looking
            # install emit nothing.  Log once per process, not once per 5 s.
            if not self._topology_skip_logged:
                self._topology_skip_logged = True
                log.warning(
                    "inference is not running: no topology configured "
                    "(discovery found nothing and none was imported). "
                    "Open the WattMosaic integration to configure CT channels."
                )
            return {"skipped": "no topology configured"}
        self._topology_skip_logged = False
        window = self._power_window(topology)
        # Per-cycle context the disaggregation engine reads back off the window.
        # Built in one dict so the frozen window is replaced once.
        #
        # ``device_levels`` publishes each device's last measured output level.
        # Attribution reads it to interpolate a measured dimming curve, so a
        # light at 40 % reports its watts at 40 % rather than its full-on value.
        # Devices with no reported level are absent from the mapping, never
        # defaulted to 100 %.
        #
        # ``channel_noise_floor_w`` publishes the quality layer's per-channel
        # measured MAD (innovation sigma).  A single scalar floor cannot serve a
        # 0.3 W inverter CT and a 30 W load-manager CT at once: too small turns
        # every tick into an event, too large hides a real step.  Without this
        # the detector's per-channel mapping was never reachable in production.
        metadata = dict(window.metadata)
        if self.device_levels:
            metadata["device_levels"] = dict(self.device_levels)
        noise_floors = self._channel_noise_floors()
        if noise_floors:
            metadata["channel_noise_floor_w"] = noise_floors
        if metadata != dict(window.metadata):
            window = replace(window, metadata=metadata)
        # Attach the declared-link transitions observed since the last cycle.
        # Without this the engine's ``_collect_transitions`` sees neither
        # ``state_transitions`` nor ``previous_states`` and reports no events at
        # all, so state-transition inference never runs in production.
        if self._pending_transitions:
            window = replace(
                window,
                state_transitions=tuple(self._pending_transitions),
                previous_states={
                    str(item["device_id"]): str(item["previous_state"])
                    for item in self._pending_transitions
                    if item.get("previous_state") is not None
                },
            )
            self._pending_transitions.clear()
        backend = self.inference_backend
        if backend is None:
            return {"skipped": self.inference_error or "inference backend not installed"}
        process = getattr(backend, "process", None)
        if process is None:
            raise InferenceBackendUnavailable("inference backend has no process() method")
        events = list(self._pending_backend_events)
        self._pending_backend_events.clear()
        batch = await maybe_await(
            process(
                window,
                device_states=self.device_states,
                topology=topology,
                models=getattr(backend, "models", None),
                events=events,
            )
        )
        results = list(
            getattr(batch, "results", [])
            or (batch.get("results", []) if isinstance(batch, dict) else [])
        )
        reconciliation = getattr(batch, "reconciliation", None)
        if reconciliation is None and isinstance(batch, dict):
            reconciliation = batch.get("reconciliation")
        if results:
            self.latest_results.update({r.device_id: r for r in results})
            self.db.insert_inference_results(results)
        if reconciliation is not None:
            if isinstance(reconciliation, dict):
                reconciliation = Reconciliation.model_validate(reconciliation)
            self.latest_reconciliation = reconciliation
            self.db.insert_reconciliation(reconciliation)
        # Track what the backend actually did so ``learning_active`` never
        # advertises a pipeline that is not running.
        self._sync_learning_state(backend)
        self.inference_last_run_at = self._clock()
        self.last_successful_processing_at = self.inference_last_run_at
        self.cycles += 1
        diagnostics = getattr(batch, "diagnostics", None)
        return {
            "results": len(results),
            "reconciliation": bool(reconciliation),
            "backend": diagnostics if isinstance(diagnostics, dict) else {},
        }

    def _sync_learning_state(self, backend: Any) -> None:
        active = getattr(backend, "learning", None) is not None
        self.learning_active = bool(active and getattr(backend, "learning_active", True))

    async def _maintenance_loop(self) -> None:
        while not self._stopping:
            try:
                self.aligner.prune()
                self.db.prune(self.config.retention_days_raw, self.config.retention_days_aggregates)
            except Exception:  # noqa: BLE001
                log.exception("maintenance cycle failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), MAINTENANCE_INTERVAL_S)
                return
            except TimeoutError:
                continue


def _event_time(event: Any) -> datetime | None:
    """``occurred_at`` of an HA device event, tolerant of mapping or object."""
    raw = (
        event.get("occurred_at")
        if isinstance(event, dict)
        else getattr(event, "occurred_at", None)
    )
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class ComponentStatus:
    name: str
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    last_started_at: datetime | None = None
