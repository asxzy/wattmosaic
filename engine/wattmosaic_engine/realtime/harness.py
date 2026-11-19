"""Deterministic HB §17 harness: replay driver bound to a real ``EngineCore``.

This is the P6 seam used by ``tests/acceptance/conftest.py``'s ``engine_harness``
fixture.  It constructs an ``EngineCore`` over an in-memory database with an
injected clock, then drives the X07 replay driver through the observation
stream (HA messages + reader samples).  No wall clock is read: every timestamp
comes from the house's ``start_time`` plus the record's simulated offset, so two
runs of the same seed are byte-identical (AT-19, V1).

The harness deliberately does not import ``runtime.fakes`` (production code
never does).  It uses a small recording adapter to bridge the replay driver's
synchronous callback surface to ``EngineCore``'s async callbacks.  It makes **no**
inference, topology or mapping decision itself: it only feeds messages, advances
the injected clock and drives the runtime's own executors/loops synchronously.
The T5 ledger, T7 detectors and the configuration workflow are all the real ones.

All heavy imports are **local** to methods: this module is re-exported from
``realtime/__init__.py`` and ``realtime`` is imported by ``core.runtime``, so a
top-level import of ``main`` would create a cycle.  Keeping them local breaks it
without weakening the surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["EngineHarness"]


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _default_revision(reader_id: str, at: datetime) -> Any:
    """A neutral, declared-by-migration revision for a replay reader.

    Polarity is ``normal`` and the multiplier is 1.0: the simulator's watt
    values are already canonical, so replay applies no hidden transformation.
    """
    from ..contracts import EndpointConfigRevision

    return EndpointConfigRevision(
        endpoint_id=reader_id,
        revision=1,
        valid_from=at - timedelta(days=1),
        phase="l1",
        leg=None,
        voltage_reference_v=None,
        polarity="normal",
        multiplier=1.0,
        calibration_version="replay@1",
        measurement_type="instantaneous",
        measurement_window_ms=1000,
        expected_interval_s=None,
        resolution_w=None,
        declared_by="migration",
    )


class _CoreConsumer:
    """Bridge: replay's sync callbacks → EngineCore's async callbacks."""

    def __init__(self, core: Any, revisions: Mapping[str, Any]) -> None:
        self.core = core
        self.revisions = revisions
        self._seq = 0

    def on_state_changed(self, msg: dict[str, Any]) -> None:
        import asyncio

        from ..contracts import HAEvent

        observed = _as_utc(msg.get("observed_at") or msg.get("last_updated") or msg["state_timestamp"])
        received = _as_utc(msg.get("received_at") or observed)
        context = msg.get("context") if isinstance(msg.get("context"), dict) else {}
        attributes = msg.get("attributes") if isinstance(msg.get("attributes"), dict) else {}
        event = HAEvent.make(
            entity_id=str(msg.get("entity_id", "")),
            observed_at=observed,
            received_at=received,
            from_state=msg.get("from_state"),
            to_state=str(msg.get("state")) if msg.get("state") is not None else None,
            attributes_delta=dict(attributes),
            context_id=context.get("id"),
            context_parent_id=context.get("parent_id"),
            context_user_id=context.get("user_id"),
            linked=False,
        )
        asyncio.run(self.core.on_ha_event(event))

    def on_sample(self, payload: dict[str, Any]) -> None:
        import asyncio

        from ..contracts import MeasurementType, QualityFlag, ReaderMeasurement

        reader_id = str(payload.get("reader_id", ""))
        observed = _as_utc(payload.get("state_timestamp") or payload["received_at"])
        received = _as_utc(payload.get("received_at") or observed)
        value = payload.get("value_w", payload.get("value"))
        if value is None:
            return
        self._seq += 1
        flags = [QualityFlag(f) for f in (payload.get("quality_flags") or [])]
        reading = ReaderMeasurement(
            reader_id=reader_id,
            observed_at=observed,
            received_at=received,
            active_power_w=float(value),
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
        asyncio.run(self.core.on_reading(reading))


class EngineHarness:
    """A running ``EngineCore`` driven by a deterministic replay.

    Use :meth:`from_hb17` with the fixture dict the acceptance conftest yields.
    ``core`` is the real engine; ``result`` is the latest published
    :class:`~wattmosaic_engine.contracts.JointInferenceResult` (or ``None`` when
    the observation stream carried no inferable group).
    """

    def __init__(self, core: Any, db: Any, clock: list[datetime]) -> None:
        self.core = core
        self.db = db
        self._clock = clock
        self.result: Any = None
        self.replay_result: Any = None

    @classmethod
    def from_hb17(cls, hb17: Any, *, model_set_id: str = "mset-hb17") -> EngineHarness:
        """Build and drive a harness from the HB §17 acceptance fixture."""
        from tools.simulator.replay import replay

        from ..config.settings import EngineConfig
        from ..main import EngineCore
        from ..realtime.grouping import GroupingEngine
        from ..storage.database import Database
        from ..versioning.service import VersionService

        house = hb17.get("house") if isinstance(hb17, Mapping) else hb17
        observation = hb17.get("observation", []) if isinstance(hb17, Mapping) else []
        start = getattr(house, "start_time", None) or _as_utc(
            next(iter(observation or []), {}).get("timestamp", datetime.now(UTC))
        )

        # A single-element clock cell: replay advances it to each record's t_s.
        now = [_as_utc(start)]
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
        # Seed topology/mapping from the observation's declared parents and
        # entity ids (never from truth).  from_hb17 owns its own in-memory DB,
        # which stays alive on the service for the harness's lifetime.  Rebind
        # through the runtime's composition seam so the real T5/T7/M2 services,
        # promotion hooks and ledger view all point at the same authoritative
        # D1 instance -- no detection or mapping decision happens here.
        core.rebind_version_service(VersionService.from_hb17(observation))
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

        revisions: dict[str, Any] = {}
        for record in observation or []:
            for reader in (record.get("reader_samples") or {}):
                revisions.setdefault(reader, _default_revision(reader, now[0]))
        consumer = _CoreConsumer(core, revisions)

        # Drive the X07 driver with an injected clock.  The clock's float value
        # is computed from the simulated instant; the real datetime used by the
        # engine is the captured ``now[0]`` cell advanced by the driver.
        records = [
            {
                "t_s": r.get("t_s", 0.0),
                "ha_messages": r.get("ha_messages") or [],
                "reader_samples": dict(r.get("reader_samples") or {}),
            }
            for r in (observation or [])
        ]
        origin = _as_utc(start)

        # Advance the engine clock as the driver steps through records.  We hook
        # this by wrapping the consumer callbacks: each record advances before
        # its messages are delivered, so derive it from the message timestamp.
        def _advance_clock_from_msg(msg: dict[str, Any]) -> None:
            stamp = msg.get("observed_at") or msg.get("last_updated")
            if stamp:
                now[0] = _as_utc(stamp)

        original_state = consumer.on_state_changed

        def _state(msg: dict[str, Any]) -> None:
            _advance_clock_from_msg(msg)
            original_state(msg)

        consumer.on_state_changed = _state  # type: ignore[method-assign]

        result = replay(
            records,
            consumer,
            clock=lambda: (now[0] - origin).total_seconds(),
            on_sample=consumer.on_sample,
            check_seam=True,
        )

        # P5 merges co-temporal reader steps into a group after the ready
        # callback fires, so feed the final groups back through the runtime's
        # own T5 ledger fold (no result is computed in the harness), then drain
        # the executor.  This runs the real folds and detector cycles that
        # on_group_ready / the promotion hook submitted.
        if core.grouping_engine is not None:
            core.fold_groups(core.grouping_engine.groups())
        core.drain_background()

        # Drain every ready group through the real v3 infer path with the
        # injected clock.  This is the event loop's work, run synchronously for
        # a deterministic harness (no wall clock, no async task scheduling).
        latest: Any = None
        while True:
            group = core.groups_ready.get_nowait()
            if group is None:
                break
            bundle = core._build_input_bundle(group)
            if bundle is None:
                continue
            latest = core._run_infer_group(bundle)
            core._publish_group_result(group, latest, bundle)
            # Promotions from T7 submit more detector/reconciliation jobs; drain
            # them too so the ledger and configuration events settle.
            core.drain_background()

        # Drive the real T7 job with the injected clock.  The production
        # self-heal loop is timer-driven, so a synchronous harness must drive
        # that job explicitly once the observation stream is folded.  The T7
        # detector's association history was accumulated inline by the P3 step
        # feed (:meth:`_record_association_from_step`) during the replay.
        # Several cycles supply the independent corroborations the workflow
        # needs to promote a SUSPECTED change (HB §14 step 3); each cycle uses a
        # distinct evidence source token.  CONFIRMED_EVENTS is the threshold.
        from ..versioning.workflow import CONFIRMED_EVENTS

        for _ in range(CONFIRMED_EVENTS):
            core.run_self_heal_cycle()
            core.drain_background()

        harness = cls(core, db, now)
        harness.replay_result = result
        harness.result = latest if latest is not None else getattr(core, "_v3_snapshot", None)
        return harness

    def close(self) -> None:
        """Release the in-memory database and worker threads."""
        core = self.core
        if getattr(core, "background_executor", None) is not None:
            core.background_executor.shutdown(wait=False)
        if getattr(core, "inference_executor", None) is not None:
            core.inference_executor.shutdown(wait=False)
        self.db.close()
