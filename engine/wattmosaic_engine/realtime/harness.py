"""Deterministic replay harness: a replay driver bound to a real ``EngineCore``.

This is the P6 seam used by ``tests/acceptance/conftest.py``'s ``engine_harness``
fixture, the V1 end-to-end walkthrough and the v3 accuracy harness.  It
constructs an ``EngineCore`` over an in-memory database with an injected clock,
then drives the X07 replay driver through the observation stream (HA messages +
reader samples) **one record at a time**, running the runtime's three loops
synchronously after each record:

* **fast loop** — every HA message and reader sample goes through
  ``EngineCore.on_ha_event`` / ``EngineCore.on_reading`` (P1 → P2 → P3 → P4/P5);
* **event loop** — :meth:`EngineHarness.pump` drains ``groups_ready`` through the
  runtime's own bundle → ``infer_group`` → publish path, exactly the work
  ``_event_loop`` does, then drains the background executor;
* **steady loop** — :meth:`EngineHarness.heartbeat` runs ``_steady_pass`` on the
  injected clock's ``steady_heartbeat_s`` cadence (group lifecycle ticks,
  steady reads, staleness heartbeat).

No wall clock is read: every timestamp comes from the house's ``start_time``
plus the record's simulated offset, so two runs of the same seed are
byte-identical (AT-19, V1).

The harness imports no test fakes and no scenario data: the declared topology
and mapping arrive through an injected ``seed`` callable (the HB §17 seed lives
in ``tests/support/hb17_seed.py``).  It makes **no** inference, topology or
mapping decision itself:
it only feeds messages, advances the injected clock and drives the runtime's own
executors/loops.  The T5 ledger, T7 detectors and the configuration workflow are
all the real ones.

All heavy imports are **local** to methods: this module is re-exported from
``realtime/__init__.py`` and ``realtime`` is imported by ``core.runtime``, so a
top-level import of ``main`` would create a cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["EngineHarness"]

#: HA attributes that describe an entity rather than one state change.  HA's
#: ``state_changed`` always carries ``old_state``; the replay stream does not, so
#: for an entity's first message the consumer assumes the integration's primed
#: state already carried these (as a real ``get_states`` prime does).
_STATIC_ATTRIBUTES = frozenset({"friendly_name", "device_class", "unit_of_measurement"})


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


class _CoreConsumer:
    """Bridge: replay's sync callbacks → EngineCore's async fast-loop callbacks.

    HA messages are normalised the way P1's ``HASource._emit_ha_event`` does:
    ``attributes_delta`` is the diff against the entity's previous state (not
    the whole attribute dict), ``linked`` comes from ``ha_entity_links`` at
    ``observed_at`` and an unlinked event is also held in the candidate ring.
    """

    def __init__(self, core: Any) -> None:
        self.core = core
        self._seq = 0
        self._last_attributes: dict[str, dict[str, Any]] = {}
        self._last_state: dict[str, str | None] = {}

    def on_state_changed(self, msg: dict[str, Any]) -> None:

        from ..contracts import HAEvent

        entity_id = str(msg.get("entity_id", ""))
        observed = _as_utc(
            msg.get("observed_at") or msg.get("last_updated") or msg["state_timestamp"]
        )
        received = _as_utc(msg.get("received_at") or observed)
        context = msg.get("context") if isinstance(msg.get("context"), dict) else {}
        attributes = msg.get("attributes") if isinstance(msg.get("attributes"), dict) else {}
        before = self._last_attributes.get(entity_id)
        if before is None:
            before = {k: v for k, v in attributes.items() if k in _STATIC_ATTRIBUTES}
        delta = {k: v for k, v in attributes.items() if before.get(k) != v}
        self._last_attributes[entity_id] = dict(attributes)
        to_state = str(msg.get("state")) if msg.get("state") is not None else None
        from_state = msg.get("from_state", self._last_state.get(entity_id))
        self._last_state[entity_id] = to_state
        linked = False
        entities = getattr(getattr(self.core.db, "repos", None), "entities", None)
        if entities is not None:
            try:
                linked = bool(entities.links_for_entity(entity_id, observed))
            except Exception:  # noqa: BLE001 - a missing link table means "unlinked"
                linked = False
        event = HAEvent.make(
            entity_id=entity_id,
            observed_at=observed,
            received_at=received,
            from_state=from_state,
            to_state=to_state,
            attributes_delta=delta,
            context_id=context.get("id"),
            context_parent_id=context.get("parent_id"),
            context_user_id=context.get("user_id"),
            linked=linked,
        )
        ring = getattr(self.core, "candidate_ring", None)
        if not linked and ring is not None:
            ring.add(event, now=received)
        self._run_async(self.core.on_ha_event(event))

    def _run_async(self, coro: Any) -> Any:
        """Run one coroutine on a loop reused across records (asyncio.run per
        reading created and tore down an event loop ~0.4 ms each)."""
        import asyncio

        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            loop = self._loop = asyncio.new_event_loop()
        return loop.run_until_complete(coro)

    def on_sample(self, payload: dict[str, Any]) -> None:

        from ..contracts import MeasurementType, QualityFlag, ReaderMeasurement

        reader_id = str(payload.get("reader_id", ""))
        observed = _as_utc(payload.get("state_timestamp") or payload["received_at"])
        received = _as_utc(payload.get("received_at") or observed)
        value = payload.get("value_w", payload.get("value"))
        if value is None:
            # Absent stays absent (HB §18): nothing is delivered, never a 0 W.
            return
        self._seq += 1
        flags = [QualityFlag(f) for f in (payload.get("quality_flags") or [])]
        reading = ReaderMeasurement(
            reader_id=reader_id,
            observed_at=observed,
            received_at=received,
            active_power_w=float(value),
            current_rms_a=payload.get("current_rms_a"),
            voltage_rms_v=payload.get("voltage_rms_v"),
            power_factor=payload.get("power_factor"),
            phase="l1",
            leg=None,
            voltage_reference_v=None,
            measurement_type=MeasurementType.INSTANTANEOUS,
            measurement_window_ms=int(payload.get("measurement_window_ms") or 0),
            calibration_version="replay@1",
            measurement_basis="instantaneous",
            quality_flags=flags,
            sequence=self._seq,
        )
        self._run_async(self.core.on_reading(reading))


class EngineHarness:
    """A running ``EngineCore`` driven by a deterministic replay.

    :meth:`start` builds it without replaying; the caller then calls
    :meth:`feed` per record (or :meth:`run`, then :meth:`settle`) and inspects
    :attr:`snapshot` between records.

    ``result`` is the latest :class:`~wattmosaic_engine.contracts.JointInferenceResult`
    the event loop published (``None`` when the stream carried no inferable
    group).  ``results`` keeps every published result (event and steady) in
    publication order.
    """

    def __init__(self, core: Any, db: Any, clock: list[datetime], origin: datetime) -> None:
        self.core = core
        self.db = db
        self._clock = clock
        self._origin = origin
        self._consumer = _CoreConsumer(core)
        self._last_heartbeat: datetime | None = None
        self.result: Any = None
        self.results: list[Any] = []
        self.replay_result: Any = None
        self.n_records = 0

    # -- construction ------------------------------------------------------- #
    @classmethod
    def start(
        cls,
        observation: Iterable[Mapping[str, Any]],
        *,
        start_time: datetime | None = None,
        seed: Callable[[Any], Any] | None = None,
        configure: Callable[[Any], None] | None = None,
    ) -> EngineHarness:
        """Build ``EngineCore`` over an in-memory DB for ``observation``.

        ``seed(core)`` returns the D1 version service holding the user's
        declared topology/mapping (built over ``core.db``); the runtime is
        rebound to it.  Without a seed the core keeps its own (empty) D1
        service.  ``configure(core)`` runs after seeding and before any message,
        for declarations a user would make up front (entity links, models).
        """
        from ..config.settings import EngineConfig
        from ..main import EngineCore
        from ..realtime.grouping import GroupingEngine
        from ..storage.database import Database

        observation = list(observation)
        start = start_time or _as_utc(
            next(iter(observation), {}).get("timestamp", datetime(2026, 1, 1, tzinfo=UTC))
        )
        origin = _as_utc(start)
        now = [origin]
        clock = lambda: now[0]  # noqa: E731 - deliberately captured, injected

        db = Database(":memory:")
        config = EngineConfig(
            database_path=":memory:",
            ha_token="replay",
            api_token="replay",
            learning_enabled=False,
        )
        core = EngineCore(config, db, clock=clock)
        core._wire_v3_services()
        if seed is not None:
            # Rebind through the runtime's composition seam so the real T5/T7/M2
            # services, promotion hooks and ledger view all point at the same
            # authoritative D1 instance -- no detection or mapping decision here.
            core.rebind_version_service(seed(core))
        triple = core.version_service.current()
        core.grouping_engine = GroupingEngine(
            versions=triple,
            repository=db.repos.evidence,
            topology=core.version_service.topology_view(triple.topology_version_id),
            ring=core.candidate_ring,
            persist_ha_event=core._persist_ha_event,
            on_ready=core.on_group_ready,
            on_revised=core.on_group_ready,
        )
        if configure is not None:
            configure(core)
        return cls(core, db, now, origin)

    # -- clock -------------------------------------------------------------- #
    @property
    def now(self) -> datetime:
        return self._clock[0]

    def _advance(self, at: datetime) -> None:
        """Advance the injected clock monotonically (never backwards)."""
        if at > self._clock[0]:
            self._clock[0] = at

    def _record_time(self, record: Mapping[str, Any]) -> datetime:
        stamp = record.get("timestamp")
        if stamp:
            return _as_utc(stamp)
        return self._origin + timedelta(seconds=float(record.get("t_s") or 0.0))

    # -- the three loops ---------------------------------------------------- #
    def feed(self, record: Mapping[str, Any]) -> list[Any]:
        """Deliver one observation record, then run the event + steady loops.

        Returns the results the event loop published for this record.
        """
        from tools.simulator.replay import replay

        payload = {
            "t_s": record.get("t_s", 0.0),
            "ha_messages": list(record.get("ha_messages") or []),
            "reader_samples": dict(record.get("reader_samples") or {}),
        }
        consumer = self._consumer
        record_end = self._record_time(record)

        class _Clocked:
            # Each HA message advances the injected clock to its own instant
            # before delivery (the fast loop stamps group activity with it),
            # capped at the record's end: a message the simulator scripts into
            # this record for a later instant must not move time forward early.
            def on_state_changed(inner, msg: dict[str, Any]) -> None:  # noqa: N805
                stamp = msg.get("observed_at") or msg.get("last_updated")
                if stamp:
                    self._advance(min(_as_utc(stamp), record_end))
                consumer.on_state_changed(msg)

        result = replay(
            [payload],
            _Clocked(),
            clock=lambda: (self.now - self._origin).total_seconds(),
            on_sample=consumer.on_sample,
            check_seam=True,
        )
        self._accumulate(result)
        # The record's end instant: the samples above were delivered by then.
        self._advance(record_end)
        self.n_records += 1
        published = self.pump()
        heartbeat_s = float(getattr(self.core.config, "steady_heartbeat_s", 60.0))
        if self._last_heartbeat is None:
            self._last_heartbeat = self.now
        elif (self.now - self._last_heartbeat).total_seconds() >= heartbeat_s:
            published.extend(self.heartbeat())
        return published

    def run(
        self,
        records: Iterable[Mapping[str, Any]],
        on_record: Callable[[Mapping[str, Any], list[Any]], None] | None = None,
    ) -> None:
        """Feed every record in order; ``on_record(record, published)`` after each."""
        for record in records:
            published = self.feed(record)
            if on_record is not None:
                on_record(record, published)

    def pump(self) -> list[Any]:
        """Run the event loop's work synchronously until ``groups_ready`` is empty.

        This is ``_event_loop``'s body (bundle → ``infer_group`` → publish), with
        the injected clock and no async scheduling.  Background jobs submitted by
        a publish (ledger folds, detector cycles, reconciliation) are drained.
        """
        core = self.core
        published: list[Any] = []
        while True:
            group = core.groups_ready.get_nowait()
            if group is None:
                break
            bundle = core._build_input_bundle(group)
            if bundle is None:
                continue
            result = core._run_infer_group(bundle)
            core._publish_group_result(bundle["group"], result, bundle)
            published.append(result)
            core.drain_background()
        core.drain_background()
        if published:
            self.result = published[-1]
            self.results.extend(published)
        return published

    def heartbeat(self) -> list[Any]:
        """Run one steady-loop pass on the injected clock, then pump."""
        before = len(self.results)
        captured: list[Any] = []
        core = self.core
        original = core._publish_group_result

        def _capture(group: Any, result: Any, bundle: Any) -> None:
            original(group, result, bundle)
            captured.append(result)

        core._publish_group_result = _capture  # type: ignore[method-assign]
        try:
            core._steady_pass()
        finally:
            del core._publish_group_result
        self._last_heartbeat = self.now
        # Steady results are kept in ``results``; ``result`` stays the latest
        # event-loop (``infer_group``) result.
        self.results.extend(captured)
        self.pump()
        return self.results[before:]

    def settle(self) -> None:
        """Close the stream: fold final groups, drain, run T7 cycles.

        P5 merges co-temporal reader steps into a group after its first ready
        callback, so the final groups are folded through the runtime's own T5
        ledger job.  The production self-heal loop is timer-driven; several
        cycles supply the independent corroborations the workflow needs to
        promote a SUSPECTED change (HB §14 step 3).
        """
        from ..versioning.workflow import CONFIRMED_EVENTS

        core = self.core
        if core.grouping_engine is not None:
            core.fold_groups(core.grouping_engine.groups())
        core.drain_background()
        self.pump()
        for _ in range(CONFIRMED_EVENTS):
            core.run_self_heal_cycle()
            core.drain_background()

    def _accumulate(self, result: Any) -> None:
        if self.replay_result is None:
            self.replay_result = result
            return
        total = self.replay_result
        total.n_ticks += result.n_ticks
        total.n_messages += result.n_messages
        total.n_samples += result.n_samples
        total.delivered_entities.extend(result.delivered_entities)
        total.final_t_s = result.final_t_s

    # -- reads -------------------------------------------------------------- #
    @property
    def snapshot(self) -> Any:
        """The currently published v3 ``InferenceSnapshot`` (or ``None``)."""
        return self.core._v3_snapshot()

    def close(self) -> None:
        """Release the in-memory database and worker threads."""
        core = self.core
        if getattr(core, "background_executor", None) is not None:
            core.background_executor.shutdown(wait=False)
        if getattr(core, "inference_executor", None) is not None:
            core.inference_executor.shutdown(wait=False)
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        self.db.close()
