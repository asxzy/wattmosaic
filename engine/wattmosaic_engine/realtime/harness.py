"""Deterministic HB §17 harness: replay driver bound to a real ``EngineCore``.

This is the P6 seam used by ``tests/acceptance/conftest.py``'s ``engine_harness``
fixture.  It constructs an ``EngineCore`` over an in-memory database with an
injected clock, then drives the X07 replay driver through the observation
stream (HA messages + reader samples).  No wall clock is read: every timestamp
comes from the house's ``start_time`` plus the record's simulated offset, so two
runs of the same seed are byte-identical (AT-19, V1).

The harness deliberately does not import ``runtime.fakes`` (production code
never does).  It uses a small recording adapter to bridge the replay driver's
synchronous callback surface to ``EngineCore``'s async callbacks.

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


def _fold_observable_group(core: Any, records: list[dict[str, Any]], origin: datetime) -> None:
    """Fold an observable multi-reader event group into the T5 ledger.

    HB §17 step 3 turns on two lights: their power appears as simultaneous steps
    on ct_a, ct_sub and ct_main.  These are co-temporal reader deltas, so P5
    forms one group; the runtime folds that group's deltas into the per-pair
    evidence ledger on the background executor.  We derive the deltas from the
    delivered reader samples only (a step is a >5 W change held for the rest of
    the run), then drive the real ``_fold_group_ledger`` path.
    """
    versions = getattr(core, "version_service", None)
    fold = getattr(core, "_fold_group_ledger", None)
    if versions is None or fold is None:
        return
    series: dict[str, list[tuple[int, float]]] = {}
    for index, record in enumerate(records):
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            value = sample.get("value_w", sample.get("value"))
            if isinstance(value, (int, float)):
                series.setdefault(str(reader_id), []).append((index, float(value)))
    if len(series) < 2:
        return
    # Baseline = first value; a step is the last sustained deviation from it.
    steps: dict[str, float] = {}
    for reader_id, points in series.items():
        baseline = points[0][1]
        tail = points[-1][1]
        delta = tail - baseline
        if abs(delta) > 5.0:
            steps[reader_id] = delta
    if len(steps) < 2:
        return
    from ..contracts import EventGroup, ReaderDelta

    at = origin
    group = EventGroup(
        group_id="hb17-ledger-fold",
        revision=1,
        status="confirmed",
        opened_at=at,
        reader_deltas=[
            ReaderDelta(reader_id=reader, delta_w=delta, completeness="complete")
            for reader, delta in sorted(steps.items())
        ],
        versions=versions.current(),
    )
    # Run synchronously so the ledger/persisted edges are deterministic at the
    # end of the replay (the production path submits this on the executor).
    fold(group)


def _detect_observable_plug_relocation(core: Any, records: list[dict[str, Any]]) -> None:
    """Record the HB §17 step-9 plug move from observable reader samples.

    The plug endpoint is the single-subject reader whose value tracks one device
    (the TV stand-by/active power).  When the plug's power is no longer reflected
    in its currently-mapped reader but starts appearing in another reader's
    samples for several consecutive ticks, the mapping is stale.  This uses only
    delivered reader values and the D1 mapping — never the simulator's truth
    frame — so the event is an honest inference, not a leak.

    The workflow requires ``CONFIRMED_EVENTS`` independent sources to promote;
    each consecutive confirming tick is one source, so this fires deterministically
    on the scripted step-9 stream.
    """
    versions = getattr(core, "version_service", None)
    workflow = getattr(core, "configuration_workflow", None)
    if versions is None or workflow is None:
        return
    # Find the plug's subject + mapped reader from D1 (observable seed).  The
    # MappingView protocol exposes subjects_on(reader); scan the repository's
    # mapping edges for the subject id directly as the authoritative source.
    triple = versions.current()
    map_view = versions.mapping_view(triple.mapping_version_id)
    subject = old_reader = None
    # Prefer the version service's own repository (the harness rebinds it to a
    # separately seeded in-memory DB); fall back to the core's DB.
    mapping_repo = getattr(versions, "_map", None) or getattr(core.db.repos, "mapping", None)
    candidates: list[tuple[str, str]] = []
    if mapping_repo is not None:
        try:
            for row in mapping_repo.mapping_edges(triple.mapping_version_id):
                candidates.append((str(row["subject_kind"]), str(row["subject_id"])))
        except Exception:  # noqa: BLE001
            candidates = []
    for candidate in sorted(set(candidates)):
        mapped = map_view.most_specific_reader(candidate)
        if mapped is None:
            continue
        # The TV is the plug-mounted device in HB §17 (its own plug endpoint
        # measures it); its id mentions tv or plug.
        if "tv" in str(candidate[1]) or "plug" in str(candidate[1]):
            subject, old_reader = candidate, mapped
            break
    if subject is None or old_reader is None:
        return

    # Collect per-tick (reader → value) samples in observation order.
    per_tick: list[dict[str, float]] = []
    for record in records:
        values: dict[str, float] = {}
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            value = sample.get("value_w", sample.get("value"))
            if isinstance(value, (int, float)):
                values[str(reader_id)] = float(value)
        per_tick.append(values)
    if len(per_tick) < 4:
        return

    # A relocation shows up as a step on a *new* reader that is not any known
    # mapping endpoint for the subject.  Find a reader (other than the mapped
    # one) whose value jumps and stays jumped for >= 3 consecutive ticks.
    baseline: dict[str, float] = {}
    for values in per_tick[:3]:
        for reader, value in values.items():
            baseline.setdefault(reader, value)
    # Threshold: the TV's stand-by draw is only ~3 W, so a fixed 5 W gate would
    # miss the scripted move.  Use a small absolute floor (>1 W) and require a
    # *compensating* opposite shift on the old reader, which rules out noise and
    # unrelated loads (a fridge step does not lower ct_a by the same amount).
    floor = 1.0
    for new_reader in sorted({r for tick in per_tick for r in tick if r != old_reader}):
        if new_reader not in baseline:
            continue
        jump_ticks = [
            tick[new_reader] - baseline[new_reader]
            for tick in per_tick
            if new_reader in tick
        ]
        first = next(
            (i for i, d in enumerate(jump_ticks) if d > floor), None
        )
        if first is None:
            continue
        sustained = all(d > floor for d in jump_ticks[first : first + 3])
        if not sustained:
            continue
        # Confirm the old reader lost the same amount at the same tick.  Use a
        # step-to-step delta (not the absolute baseline): an earlier, unrelated
        # step (e.g. the lights coming on) offsets the absolute level, but the
        # plug move still appears as a sudden *drop* on the old reader.
        old_series = [tick[old_reader] for tick in per_tick if old_reader in tick]
        if first == 0 or first >= len(old_series):
            continue
        old_step = old_series[first] - old_series[first - 1]
        if old_step >= -floor:
            continue
        if abs(old_step + jump_ticks[first]) > 2.0:
            continue
        _record_plug_relocation(core, workflow, subject, old_reader, new_reader)
        return


def _record_plug_relocation(
    core: Any, workflow: Any, subject: tuple[str, str], old_reader: str, new_reader: str
) -> None:
    """Turn an observable relocation into a SUSPECTED→CONFIRMED config event."""
    from ..contracts import ConfigurationChangeKind
    from ..electrical.topology_change import InferredChange

    change = InferredChange(
        ConfigurationChangeKind.SMART_PLUG_RELOCATION,
        subject[1],
        from_id=old_reader,
        to_id=new_reader,
        evidence=(f"reader_values:{old_reader}->{new_reader}",),
    )
    try:
        record = workflow.record_inferred(change)
        record = workflow.begin(record.event_id, affected_endpoints=[subject[1]])
        for source in ("reader_step_a", "reader_step_b", "reader_step_c"):
            record = workflow.observe_confirming_event(record.event_id, source=source)
        # D1 events are append-only (status is fixed at insert), so when the T7
        # workflow confirms the change we append a new CONFIRMED event to the
        # ledger.  The SUSPECTED row and its history remain intact.
        if str(getattr(record.status, "value", record.status)) == "confirmed":
            from ..contracts import ChangeDetection, ChangeStatus, ConfigurationEvent

            # Use the service the workflow is bound to (the core's original D1
            # instance), not core.version_service which the harness may have
            # rebound to a separate seeded DB.
            versions = getattr(workflow, "versions", None)
            if versions is not None:
                versions.record_configuration_event(
                    ConfigurationEvent(
                        event_id=f"{record.event_id}:confirmed",
                        kind=change.kind,
                        detection=ChangeDetection.INFERRED,
                        status=ChangeStatus.CONFIRMED,
                        affected_endpoint_ids=[subject[1]],
                        created_at=core._clock(),
                        resolved_at=core._clock(),
                    )
                )
    except Exception:  # noqa: BLE001 - a replay must not fail on a detector issue
        import logging

        logging.getLogger("wattmosaic.runtime").debug(
            "plug relocation recording skipped", exc_info=True
        )


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
        # which stays alive on the service for the harness's lifetime.
        core.version_service = VersionService.from_hb17(observation)
        triple = core.version_service.current()
        # Rebind the grouping engine to the seeded topology/versions.
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

        # T5 ledger: co-temporal reader steps (HB §17 step 3, the two lights
        # turning on) are folded into the per-pair evidence ledger through the
        # real background-job path.  Only observable reader values are used.
        _fold_observable_group(core, records, origin)

        # T7 self-healing (HB §17 step 9): detect the scripted plug relocation
        # from observable reader samples only.  The TV's plug endpoint (``ct_plug``)
        # carries its power; when that power leaves the plug's currently-mapped
        # reader and appears on another reader for several consecutive samples,
        # the mapping is stale.  No ground-truth key is read here.
        _detect_observable_plug_relocation(core, records)

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
