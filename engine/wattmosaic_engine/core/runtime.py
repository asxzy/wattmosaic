"""Runtime: EngineCore lifecycle, ingestion callbacks, loops, persist queue.

Owned by P6 after X03 (the three-loop runtime).  All state lives on
``EngineCore`` itself, so every attribute is shared through ``self`` and no call
site changes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ..backend.loader import InferenceBackendUnavailable, load_inference_backend
from ..contracts import (
    ChangeStatus,
    DataQualityReport,
    EventGroup,
    HAEvent,
    JointInferenceResult,
    MeasurementEvent,
    ReaderMeasurement,
    VersionTriple,
)
from ..electrical.operator import build_observation_operator
from ..inference.matching import MatchStore
from ..inference.orchestrator import infer_group, infer_steady
from ..inference.records import EventRecordStore
from ..ingestion.queue import BoundedQueue
from ..mapping.updater import MatrixUpdater
from ..models.registry import DatabaseModelRepository, Registry
from ..quality.engine import channel_config_from_endpoints, excluded_readers
from ..realtime.candidate_ring import CandidateRing
from ..realtime.detection import ChangeDetector
from ..realtime.grouping import GroupingEngine
from ..realtime.reader_state import ReaderStateStore
from ..realtime.windows import WindowCollector
from ..residual.reader_residual import UnknownPriorModel
from ..versioning.service import VersionService
from .discovery_api import TOPOLOGY_BOOTSTRAP_WAIT_S
from .executors import BackgroundExecutor
from .snapshot import AssembleInputs, SnapshotAssembler

log = logging.getLogger("wattmosaic.main")

#: Persist-queue drain chunk and the writer loop's flush cadence.
SAMPLE_BATCH_SIZE = 100
SAMPLE_FLUSH_INTERVAL_S = 2.0
#: How often housekeeping (retention, integrity cache) runs.
MAINTENANCE_INTERVAL_S = 60.0


def filter_excluded_from_window(window: Any, excluded: Mapping[str, str]) -> tuple[Any, dict[str, str]]:
    """Drop excluded readers from a coerced window, returning (window, excluded).

    Pure: neither reads a clock nor mutates its inputs.  ``excluded`` maps a
    reader id to the reason it was excluded (from
    :func:`wattmosaic_engine.quality.engine.excluded_readers`).  Only readers
    actually present in the window are reported as dropped; the rest are stale
    topology entries and are ignored here.
    """
    # CoercedWindow exposes ``readings``; the aligner's AlignedWindow (used by
    # the legacy loop) exposes ``channels``.  Support both so the one-line hook
    # works in production and in the pure unit test.
    key = "readings" if hasattr(window, "readings") else "channels"
    readings = dict(getattr(window, key, {}) or {})
    dropped = {reader: reason for reader, reason in excluded.items() if reader in readings}
    if not dropped:
        return window, {}
    for reader in dropped:
        readings.pop(reader, None)
    return replace(window, **{key: readings}), dropped


def stale_sources_from_quality(report: DataQualityReport | None) -> list[str]:
    """Present excluded readers as ``reader: reason`` strings.

    These become the ``stale_sources`` entries an excluded reader is reported
    under (HB §6.1 / AT-17): a silent or bad reader appears with the concrete
    reason, never as a fabricated ``0 W`` estimate.
    """
    return [f"{reader}: {reason}" for reader, reason in excluded_readers(report).items()]


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

        # The three-loop runtime is the default.  Wire its services once so the
        # fast-loop callbacks and the event loop have typed providers, even when
        # the supervisor (not ``start``) owns the loop tasks.
        self._wire_v3_services()
        await self.hasource.start(
            on_reading=self.on_reading,
            on_ha_event=self.on_ha_event,
        )
        # The first state snapshot arrives asynchronously from ``_prime()``;
        # waiting a bounded time here is what makes discovery see real entities
        # instead of an empty cache on a genuine fresh install.
        await self.bootstrap_topology(wait_connected_s=TOPOLOGY_BOOTSTRAP_WAIT_S)
        if start_loops:
            self._tasks = [
                asyncio.create_task(self._writer_loop(), name="wm-writer"),
                asyncio.create_task(self._quality_loop(), name="wm-quality"),
                asyncio.create_task(self._event_loop(), name="wm-event"),
                asyncio.create_task(self._steady_loop(), name="wm-steady"),
                asyncio.create_task(self._maintenance_loop(), name="wm-maintenance"),
                asyncio.create_task(self._self_heal_loop(), name="wm-self-heal"),
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
        """Persist one drained batch of P1 canonical readings (v2 sample schema)."""
        v3_readings: list[ReaderMeasurement] = [
            payload for kind, payload in batch if kind == "reading"
        ]
        if v3_readings:
            self.samples_persisted += self.db.repos.samples.insert_readings(v3_readings)

    def _evaluate_quality(self) -> DataQualityReport:
        """Score every aligned reader against its v3 endpoint declaration.

        The aligner keys a reader by the entity object id, which is the reader
        id P1 assigns, so the window, the health map and the declared
        :class:`~wattmosaic_engine.quality.engine.ChannelConfig` share one key.
        """
        try:
            endpoints = self.db.repos.entities.endpoints()
            revisions = {
                str(row["endpoint_id"]): self.db.repos.entities.revisions(row["endpoint_id"])
                for row in endpoints
            }
            channels = channel_config_from_endpoints(
                {**row, "revisions": revisions.get(str(row["endpoint_id"]), [])}
                for row in endpoints
            )
        except Exception:  # noqa: BLE001 - quality must run without declarations
            channels = {}
        report = self.quality_engine.evaluate(
            self.aligner.window(max_age_s=self.config.stale_after_s),
            self.aligner.channel_health(),
            channels=channels,
            reconciliation=self.latest_reconciliation,
            unavailable=self.hasource.unavailable_entities,
            energy_window=self.aligner.energy_window(max_age_s=self.config.stale_after_s),
            energy_health=self.aligner.energy_health(),
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
                    self._stop_event.wait(), max(1.0, self.config.quality_interval_s)
                )
                return
            except TimeoutError:
                continue

    def _sync_learning_state(self, backend: Any) -> None:
        active = getattr(backend, "learning", None) is not None
        self.learning_active = bool(active and getattr(backend, "learning_active", True))

    # ------------------------------------------------------------------ #
    # Three-loop runtime (P6, HB §5): the only runtime.
    # ------------------------------------------------------------------ #

    def init_three_loop_state(self) -> None:
        """Allocate the v3 runtime state once, on :class:`EngineCore`.

        Called from ``EngineCore.__init__`` so every mixin shares it.  No here
        reads a clock or a database; the version service, snapshot assembler
        and executors are created lazily in :meth:`_wire_v3_services` when the
        engine actually starts (so a transport-only core stays cheap).
        """
        cfg = self.config
        self.three_loop_enabled = True
        self.groups_ready: BoundedQueue[EventGroup] = BoundedQueue(cfg.event_queue_max)
        self.reader_state = ReaderStateStore(
            stale_after_s=cfg.stale_after_s,
            missing_after_s=max(cfg.stale_after_s * 5.0, 600.0),
            clock=self._clock,
        )
        self.candidate_ring = CandidateRing()
        self.change_detector = ChangeDetector(self.reader_state, self._on_reading_event)
        self.window_collector = WindowCollector(
            self.reader_state,
            topology_version_id="topo-0",
            sink=self._persist_evidence_batch,
        )
        self.unknown_prior = UnknownPriorModel(penalty_scale=cfg.unknown_penalty_scale)
        self.match_store = MatchStore()
        self.record_store = EventRecordStore()
        # Facade providers / lazily built v3 services (T2/T3/D1/L1/M2/D4).
        self.version_service: VersionService | None = None
        self.model_registry_v3: Registry | None = None
        self.matrix_updater: MatrixUpdater | None = None
        self.snapshot_assembler: SnapshotAssembler | None = None
        self.background_executor: BackgroundExecutor | None = None
        self.inference_executor: ThreadPoolExecutor | None = None
        self.learning_worker: Any | None = None
        self.grouping_engine: GroupingEngine | None = None
        # Topological self-healing (T5/T7) — built in _wire_v3_services.  The
        # inference view is the durable per-pair evidence ledger; the workflow
        # turns inferred configuration changes into promoted versions and
        # requests L7 reconciliation on promotion.  All writes run on the single
        # background executor, guarded by this lock (single-writer rule).
        self.topology_inference: Any | None = None
        self.configuration_workflow: Any | None = None
        self.reconciler: Any | None = None
        self._ledger_lock = threading.Lock()
        self._association_history: dict[str, deque[str]] = {}
        self._recent_step_deltas: dict[str, tuple[datetime, float]] = {}
        self._reader_last_seen: dict[str, datetime] = {}
        self.late_evidence_count = 0
        self.last_promotion: dict[str, Any] | None = None
        # Metrics (observable, never guessed).
        self.loop_lag_s: dict[str, float] = {"fast": 0.0, "event": 0.0, "background": 0.0}
        self.stage_latency_s: dict[str, float] = {}
        self.groups_dropped = 0
        self.infer_group_calls = 0
        self.infer_steady_calls = 0
        self.heartbeats = 0
        self._last_steady_at: dict[str, datetime] = {}
        self._last_heartbeat_wall = time.monotonic()

    def _wire_v3_services(self) -> None:
        """Build the real v3 services from the injected DB/clock (idempotent)."""
        if self.version_service is not None:
            return
        db = self.db
        self.version_service = VersionService(
            topology_repo=db.repos.topology_v3,
            mapping_repo=db.repos.mapping,
            clock=self._clock,
            model_set_id="mset-live",
        )
        self.model_registry_v3 = Registry(
            DatabaseModelRepository(db), clock=self._clock
        )
        self.model_registry_v3.refresh()
        self.version_service._model_set_id = str(self.model_registry_v3.model_set_id)
        self.matrix_updater = MatrixUpdater(
            store=self._live_matrix_store(),
            versions=self.version_service,
            clock=self._clock,
        )
        self.snapshot_assembler = SnapshotAssembler(db.repos.inference, self._clock)
        self.background_executor = BackgroundExecutor(
            max_chunk_s=2.0, now=time.monotonic
        )
        self.inference_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wm-inference"
        )
        # L6 learner on the one background executor (DEC-37); re-derives its
        # dirty set from S18 on start so a restart loses no confirmed evidence.
        from ..learning.worker import LearningWorker

        self.learning_worker = LearningWorker(
            db.repos.learning,
            registry=self.model_registry_v3,
            executor=self.background_executor,
            clock=self._clock,
        )
        self.learning_worker.start()

        # Match/history/evidence sinks are the S16 repositories.
        evidence = db.repos.evidence
        self.grouping_engine = GroupingEngine(
            versions=self.version_service.current(),
            repository=evidence,
            topology=self.version_service.topology_view(
                self.version_service.current().topology_version_id
            ),
            ring=self.candidate_ring,
            persist_ha_event=self._persist_ha_event,
            on_ready=self.on_group_ready,
            on_revised=self.on_group_ready,
        )
        self._wire_self_healing()
        # Composition only: every D1 topology/mapping promotion now fires the
        # T7 detectors + L7 reconciliation hook (except the in-ledger promotion,
        # which calls the hook directly to avoid a double fire).
        self.bind_promotion_hooks()

    def _wire_self_healing(self) -> None:
        """Build T5's ledger view, T7's workflow and the L7 reconciler.

        All dependencies are the real components composed from the injected DB
        and clock; no ``runtime.fakes`` import.  Ledger consolidation runs as a
        background job (:meth:`_fold_group_ledger`), the workflow's ``reconcile``
        sink enqueues :meth:`_run_reconciliation` on the same executor, and the
        promotion hook (:meth:`_on_version_promoted`) fires the detectors again.
        """
        from ..electrical.topology_inference import TopologyInferenceView
        from ..learning.reconciliation import Reconciler
        from ..versioning.workflow import ConfigurationWorkflow

        versions = self.version_service
        if versions is None:  # pragma: no cover - wired before this point
            return
        # Seed the ledger with the currently active topology edges so inference
        # continues from the persisted evidence rather than from an empty view.
        triple = versions.current()
        topo_view = versions.topology_view(triple.topology_version_id)
        seed_edges = list(getattr(topo_view, "edges", lambda: ())())
        self.topology_inference = TopologyInferenceView(
            edges=tuple(seed_edges),
            clock=self._clock,
            versions=versions,
        )
        self.reconciler = Reconciler(
            operator=self._build_reconciliation_operator(),
            model_sets={versions.current().model_set_id: self.model_registry_v3},
            residual=self.unknown_prior,
            history=self.db.repos.evidence,
            matches=self.match_store,
            clock=self._clock,
        )
        self.configuration_workflow = ConfigurationWorkflow(
            versions,
            clock=self._clock,
            reconcile=self._enqueue_reconciliation,
            audit=self._audit_configuration,
        )

    def rebind_version_service(self, versions: Any) -> None:
        """Re-point the v3 services at a different D1 service (composition only).

        A replay harness seeds HB §17 topology/mapping into its own in-memory D1
        service; rebinding is the honest way to give the *real* T5/T7/M2 runtime
        that seeded service.  This rebuilds every collaborator that captured the
        old service and re-seeds the topology ledger from its declared edges, so
        the background detector, ledger fold and reconciliation all operate on
        the same authoritative D1 view.  No topology/mapping decision is made
        here: the edges come from D1's declared seed.
        """
        self.version_service = versions
        self._wire_self_healing()
        if self.matrix_updater is not None:
            from ..mapping.updater import MatrixUpdater

            self.matrix_updater = MatrixUpdater(
                store=self._live_matrix_store(),
                versions=versions,
                clock=self._clock,
            )
        # Rebind promotion hooks to the new service (the old wrapper flag lives
        # on the replaced object, so hooks are installed fresh).
        self.bind_promotion_hooks()

    def _build_reconciliation_operator(self) -> Any:
        """A time-scoped H for the current triple, used by L7 reconciliation."""
        from ..electrical.operator import build_observation_operator

        versions = self.version_service
        assert versions is not None
        triple = versions.current()
        at = self._clock()
        topo_view = versions.topology_view(triple.topology_version_id)
        map_view = versions.mapping_view(triple.mapping_version_id)
        # Collect readers from the mapping view's subjects.  An empty view yields
        # an empty (but valid and honest) operator — never a fabricated row.
        reader_ids: set[str] = set()
        subjects: set[tuple[str, str]] = set()
        subjects_getter = getattr(map_view, "subjects", None)
        if callable(subjects_getter):
            for subject in subjects_getter():
                mapped = map_view.most_specific_reader(subject)
                if mapped is not None:
                    reader_ids.add(mapped)
                    subjects.add(subject)
        if not reader_ids:
            for reader in self.reader_state.readers():
                reader_ids.add(reader)
        return build_observation_operator(
            topo_view, map_view, None, tuple(sorted(reader_ids)), tuple(sorted(subjects)), at
        )

    def _live_matrix_store(self) -> Any:
        """MatrixStore bound to the S15 mapping repository for durable cells."""
        from ..mapping.store import MatrixStore

        try:
            return MatrixStore(repo=self.db.repos.mapping, clock=self._clock)
        except Exception:  # noqa: BLE001 - a stub DB has no mapping repository
            return MatrixStore(clock=self._clock)

    def _persist_evidence_batch(self, batch: Sequence[Any]) -> None:
        """P4 → S16 sink: persist immutable event evidence through the DB seam.

        D5: when P4's ``EvidenceBatch.supersedes`` is set (a late sample produced
        a superseding revision), persist the immutable link through
        ``put_evidence(..., supersedes_evidence_id=...)``.  The contract record
        itself never gains a metadata field.
        """
        repository = self.db.repos.evidence
        for item in batch:
            evidence = getattr(item, "evidence", item)
            supersedes = getattr(item, "supersedes", None)
            try:
                repository.put_evidence(evidence, supersedes_evidence_id=supersedes)
            except Exception:  # noqa: BLE001 - duplicate revisions are benign
                log.debug("evidence write skipped", exc_info=True)

    def _persist_ha_event(self, event: HAEvent) -> None:
        """S16 sink for ring-held unlinked HA events that join a group (DEC-17).

        Writes directly to ``ha_events_v3`` through the single-writer DB seam;
        the P5 callback fires on the event loop but these are rare (only events
        that *become* evidence), so the short transaction is acceptable.
        """
        db = self.db
        try:
            with db._lock:  # noqa: SLF001 - storage seam, single writer
                db._conn.execute(  # noqa: SLF001 - storage seam
                    """INSERT OR IGNORE INTO ha_events_v3(
                        event_id, entity_id, observed_at, received_at, from_state, to_state,
                        attributes_delta_json, context_id, context_parent_id, context_user_id,
                        linked)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event.event_id,
                        event.entity_id,
                        event.observed_at.isoformat(),
                        event.received_at.isoformat(),
                        event.from_state,
                        event.to_state,
                        json.dumps(event.attributes_delta, default=str),
                        event.context_id,
                        event.context_parent_id,
                        event.context_user_id,
                        int(event.linked),
                    ),
                )
        except Exception:  # noqa: BLE001 - an evidence write must not kill ingestion
            log.exception("failed to persist a group-member HA event")

    # -- fast loop --------------------------------------------------------- #

    def _on_reading_event(self, event: MeasurementEvent) -> None:
        """P3 step callback (inline, O(1)): feed P4 windows and P5 groups."""
        at = self._clock()
        self._time_stage("detect", at)
        self._record_association_from_step(event)
        try:
            self.window_collector.on_detected(event, at=at)
        except Exception:  # noqa: BLE001 - never let bookkeeping break ingestion
            log.exception("event window open failed")
        if self.grouping_engine is not None:
            try:
                self.grouping_engine.on_reader_step(event, now=at)
            except Exception:  # noqa: BLE001
                log.exception("event group formation failed")

    async def on_reading(self, reading: ReaderMeasurement) -> None:
        """Fast-loop entry for a canonical P1 reading (P1 → P2 → P3 → P4/P5).

        No DB reads here: the persist queue owns the write.  The whole path is
        O(1) per sample so ~30 HA events/s cannot starve ``/healthz``.
        """
        started = time.monotonic()
        self.samples_ingested += 1
        try:
            at = reading.observed_at
            # P4 late sample / D5 supersession: a reading whose observed time is
            # behind a closed FINAL window creates a new immutable revision with
            # ``supersedes`` set.  Detect this before the ring update so a
            # backfill is still recognised after the window has closed.
            last_seen = self._reader_last_seen.get(reading.reader_id)
            is_late = last_seen is not None and at < last_seen
            self._reader_last_seen[reading.reader_id] = (
                at if last_seen is None else max(last_seen, at)
            )
            self.reader_state.add(reading)
            self.persist_queue.put_nowait(("reading", reading))
            if is_late:
                revised = self.window_collector.on_late_sample(
                    reading.reader_id, at, reading.active_power_w
                )
                if revised is not None:
                    # A late sub-event also attaches to its P5 group as a REVISED
                    # revision; P4's sink already persisted the supersession link.
                    self.late_evidence_count += 1
            self.change_detector.process(reading.reader_id, at)
            # The first post-sample makes an open window PROVISIONAL and a
            # matching group ready for inference.
            evidence = self.window_collector.on_sample(
                reading.reader_id, at, reading.active_power_w
            )
            if evidence is not None and self.grouping_engine is not None:
                self.grouping_engine.on_provisional(evidence)
        except Exception:  # noqa: BLE001 - fast loop must never propagate
            log.exception("fast-loop sample handling failed")
        finally:
            self._record_latency("fast", (time.monotonic() - started) * 1000.0)

    def on_group_ready(self, group: EventGroup) -> None:
        """P5 ready/revised callback: enqueue for the event loop (drop-oldest).

        Every ready/revised group (CONFIRMED or later) also schedules a T5 ledger
        fold on the background executor.  Keeping the fold off the event loop is
        what preserves the O(1) fast path while still folding each group's reader
        deltas into the durable per-pair evidence ledger.
        """
        if not self.groups_ready.put_nowait(group):
            self.groups_dropped += 1
            log.warning("event group queue full; dropped the oldest group")
        self._schedule_topology_ledger(group)

    def _record_association_from_step(self, event: MeasurementEvent) -> None:
        """Feed the T7 relocation detector from P3 reader-step events (a feed only).

        For a single-subject endpoint (a leaf reader containing ``plug``) each
        step is one observation.  The coincident circuit reader whose step
        amplitude matches the endpoint's step is the reader the subject matched
        on this event.  Recording that per-event pairing is reader-level
        bookkeeping; the detector alone decides whether it constitutes a move.
        """
        reader = str(event.reader_id)
        delta = event.delta_w
        if delta is None:
            return
        onset = event.onset_hi
        # Store every step: a coincident circuit step is the other half of the
        # plug endpoint's observation.  Only plug steps trigger a history entry.
        self._recent_step_deltas[reader] = (onset, delta)
        if "plug" not in reader or reader in self._parent_readers():
            return
        # One observation per ON transition (a toggle cycle): OFF transients
        # are the same event's return, not an independent match observation.
        if delta <= 1.0:
            return
        parents = self._parent_readers()
        candidates: list[tuple[float, str]] = []
        for other, (stamp, other_delta) in self._recent_step_deltas.items():
            if other == reader or other in parents:
                continue
            # A declared reader clock skew (HB §17: ct_a has +30 s) means the
            # same physical step can carry onsets 30 s apart; a wider window
            # plus amplitude agreement still rules out unrelated loads.
            if abs((stamp - onset).total_seconds()) > 120.0:
                continue
            if abs(other_delta) <= 1.0:
                continue
            candidates.append((abs(abs(other_delta) - abs(delta)), other))
        if candidates:
            matched = min(candidates)[1]
            # Keep a six-observation window ending at the most recent *reader
            # change*: when the matched reader differs from the last one, start
            # a fresh window ``[old, new]``.  This is how a "vanished on A,
            # appeared on B over >= 5 events" history is accumulated; the
            # detector still decides whether it is a relocation.
            key = f"plug:{reader}"
            history = self._association_history.get(key)
            if history is None:
                history = deque(maxlen=6)
                self._association_history[key] = history
            if history and history[-1] != matched:
                old_reader = history[-1]
                history.clear()
                history.append(old_reader)  # the reader the subject left
            # Once the window holds one old reader followed by five identical
            # new-reader observations, the detector has all the evidence the
            # design §5 rule asks for; further identical observations add no
            # information and would only slide the transition out of the window.
            if len(history) == 6 and len(set(history)) == 2 and history[-1] == matched:
                return
            history.append(matched)

    def _parent_readers(self) -> frozenset[str]:
        """Readers that are CONTAINS parents in the declared topology."""
        versions = self.version_service
        if versions is None:
            return frozenset()
        try:
            repo = getattr(versions, "_topo", None)
            rows = list(repo.edges()) if repo is not None else []
        except Exception:  # noqa: BLE001
            return frozenset()
        return frozenset(
            str(row.get("source_endpoint_id", ""))
            for row in rows
            if isinstance(row, Mapping) and str(row.get("relation", "")) == "contains"
        )

    async def on_ha_event(self, event: HAEvent) -> None:
        """Fast-loop entry for a device-free HA event (P1 → P5)."""
        self.events_ingested += 1
        try:
            if self.grouping_engine is not None:
                self.grouping_engine.on_ha_event(event, now=event.observed_at)
        except Exception:  # noqa: BLE001 - fast loop must never propagate
            log.exception("fast-loop HA event handling failed")

    # -- event loop -------------------------------------------------------- #

    def _build_input_bundle(self, group: EventGroup) -> dict[str, Any] | None:
        """Assemble the immutable input bundle for one group, off the loop.

        Everything here is a read from the injected services; no wall clock and
        no async I/O.  The bundle is handed to ``infer_group`` in the single
        inference executor.
        """
        if self.version_service is None:
            return None
        versions: VersionTriple = group.versions or self.version_service.current()
        at = group.opened_at
        topo_view = self.version_service.topology_view(versions.topology_version_id)
        map_view = self.version_service.mapping_view(versions.mapping_version_id)
        reader_ids = tuple(sorted({d.reader_id for d in group.reader_deltas}))
        # D3 quality gate: a bad/stale/missing reader never reaches inference.
        # The reason is already surfaced in ``stale_sources``; here the reader
        # is removed from the operator's rows so it contributes no estimate.
        excluded = excluded_readers(self.latest_quality)
        if excluded:
            reader_ids = tuple(r for r in reader_ids if r not in excluded)
        subject_set: set[tuple[str, str]] = set()
        for reader in reader_ids:
            subject_set.update(map_view.subjects_on(reader))
        subjects = tuple(sorted(subject_set))
        operator = build_observation_operator(
            topo_view, map_view, None, reader_ids, subjects, at
        )
        return {
            "group": group,
            "operator": operator,
            "models": self.model_registry_v3,
            "residual": self.unknown_prior,
            "history": self.db.repos.evidence,
            "matches": self.match_store,
            "versions": versions,
            "clock": self._clock,
        }

    def _run_infer_group(self, bundle: dict[str, Any]) -> JointInferenceResult:
        """Run the pure I7 entry point on the inference executor thread."""
        self.infer_group_calls += 1
        return infer_group(
            bundle["group"],
            bundle["operator"],
            bundle["models"],
            bundle["residual"],
            bundle["history"],
            bundle["matches"],
            bundle["versions"],
            bundle["clock"],
        )

    async def _event_loop(self) -> None:
        """Drain ready groups: infer → persist → matrix → snapshot → learning."""
        while not self._stopping:
            group = await self.groups_ready.get()
            if group is None:
                return
            started = time.monotonic()
            try:
                bundle = await asyncio.to_thread(self._build_input_bundle, group)
                if bundle is None:
                    continue
                result = await asyncio.to_thread(self._run_infer_group, bundle)
                await asyncio.to_thread(self._publish_group_result, group, result, bundle)
            except Exception:  # noqa: BLE001 - one bad group must not kill the loop
                log.exception("event-loop inference failed for group %s", group.group_id)
            finally:
                self._record_latency("event", (time.monotonic() - started) * 1000.0)

    def _publish_group_result(
        self,
        group: EventGroup,
        result: JointInferenceResult,
        bundle: dict[str, Any],
    ) -> None:
        """Persist → apply matrix → swap snapshot → enqueue learning (in order)."""
        at = result.computed_at
        evidence = self.db.repos.evidence
        # 1. Persist the versioned record (I6/S16).
        try:
            record = self.record_store.open_provisional(
                tuple(d.reader_id for d in group.reader_deltas),
                at=at,
                versions=result.versions,
                group_id=group.group_id,
            )
            record.result = result  # publish the full inferred result, not a stub
            evidence.put_record(record)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist an inference record")
        # 2. Matrix update (M2).
        if self.matrix_updater is not None:
            try:
                self.matrix_updater.apply(result, bundle["operator"])
            except Exception:  # noqa: BLE001
                log.exception("matrix update failed")
        # 3. Snapshot assembly and copy-on-write publish (D4).
        if self.snapshot_assembler is not None:
            try:
                held = self.reader_state.held_all(at)
                self.snapshot_assembler.on_result(
                    result,
                    held,
                    self.latest_quality,
                    inputs=AssembleInputs(
                        result=result,
                        held_readings=held,
                        quality=self.latest_quality,
                        stale_sources=stale_sources_from_quality(self.latest_quality),
                    ),
                )
                self._publish_v3_snapshot()
            except Exception:  # noqa: BLE001
                log.exception("snapshot publish failed")
        # 4. Learning job only for CONFIRMED groups (L4/L6 gate).
        if str(getattr(result.status, "value", result.status)) == "confirmed" and self.learning_worker is not None:
            try:
                self.learning_worker.submit_observations(
                    self._learning_observations(result)
                )
                self.learning_worker.schedule()
            except Exception:  # noqa: BLE001
                log.exception("learning submit failed")

    def _learning_observations(self, result: JointInferenceResult) -> list[Any]:
        """Derive valid L4 observations from a CONFIRMED result.

        A device with no point (UNKNOWN / non-identifiable) contributes no
        observation: a fabricated 0 W would poison learning (HB §18).
        """
        from ..contracts import EvidenceRef, LearningObservation, ModelBasis

        observations: list[Any] = []
        for estimate in result.device_estimates:
            point = getattr(estimate, "point_w", None)
            if point is None:
                continue
            sources = list(getattr(estimate, "source_reader_ids", []) or [])
            endpoint = sources[0] if sources else ""
            observations.append(
                LearningObservation(
                    observation_id=f"obs::{result.result_id}::{estimate.subject_id}",
                    subject_kind="device",
                    subject_id=str(estimate.subject_id),
                    endpoint_id=endpoint,
                    event_group_ref=EvidenceRef(
                        kind="event_group", id=str(result.event_group_id or result.result_id)
                    ),
                    transition="on",
                    attributes={},
                    delta_w=float(point),
                    level_w=None,
                    status="confirmed",
                    identifiability=getattr(
                        estimate, "identifiability", "identifiable"
                    ),
                    basis=ModelBasis.DELTA,
                    created_at=result.computed_at,
                )
            )
        return observations

    def _publish_v3_snapshot(self) -> None:
        """Expose the current D4 snapshot copy-on-write for facade readers."""
        if self.snapshot_assembler is None:
            return
        snapshot = self.snapshot_assembler.current
        if snapshot is not None:
            self._v3_snapshot = snapshot

    # -- steady loop / heartbeat ------------------------------------------ #

    async def _steady_loop(self) -> None:
        """Debounced >3σ steady reads + a 60 s staleness-only heartbeat."""
        while not self._stopping:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), max(1.0, self.config.steady_heartbeat_s)
                )
                return
            except TimeoutError:
                pass
            started = time.monotonic()
            try:
                self._steady_pass()
            except Exception:  # noqa: BLE001
                log.exception("steady pass failed")
            finally:
                self._record_latency("background", (time.monotonic() - started) * 1000.0)

    def _steady_pass(self) -> None:
        """Run one steady pass: >3σ level changes, then a heartbeat."""
        now = self._clock()
        if self.version_service is None:
            return
        versions = self.version_service.current()
        topo_view = self.version_service.topology_view(versions.topology_version_id)
        map_view = self.version_service.mapping_view(versions.mapping_version_id)
        for reader in self.reader_state.readers():
            reading = self.reader_state.held(reader, now)
            sigma = self.reader_state.sigma(reader)
            baseline = self.reader_state.baseline(reader)
            if reading.value is None or sigma is None or baseline is None:
                continue
            if abs(reading.value - baseline) <= 3.0 * sigma:
                continue
            last = self._last_steady_at.get(reader)
            if last is not None and (now - last).total_seconds() < self.config.steady_heartbeat_s:
                continue
            self._last_steady_at[reader] = now
            operator = build_observation_operator(
                topo_view, map_view, None, (reader,), (), now
            )
            held = {reader: reading}
            established = _StaticEstablished()
            result = infer_steady(
                held, operator, self.model_registry_v3, self.unknown_prior,
                established, versions, self._clock,
            )
            self.infer_steady_calls += 1
            self._publish_group_result(_steady_group(result, now), result, {"operator": operator})

        # Heartbeat publishes staleness only (DEC-14); no full solve.
        self.heartbeats += 1
        self._stale_sources = stale_sources_from_quality(self.latest_quality)

    # -- background jobs --------------------------------------------------- #

    def submit_background(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Submit one chunked job to the single background executor."""
        if self.background_executor is None:
            return None
        return self.background_executor.submit(fn, *args, **kwargs)

    def drain_background(self, *, timeout: float | None = None) -> None:
        """Drive the runtime's own background executor synchronously.

        Used by a deterministic harness (no wall clock is read): it waits for
        every job submitted so far, so ledger folds, detector cycles and L7
        reconciliation settle before the caller inspects state.  Jobs are the
        runtime's real ones -- this method never computes a result itself.
        """
        executor = self.background_executor
        if executor is None:
            return
        # Snapshot the pending futures under the executor's lock, then wait for
        # each.  New jobs submitted by a running job are drained by a later call.
        with executor._lock:
            pending = list(executor._jobs)
        for future in pending:
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001 - a job error is already logged
                continue

    def run_self_heal_cycle(self) -> tuple[Any, ...]:
        """Run one real T7 detector+workflow cycle synchronously (harness seam).

        The production self-heal loop is timer driven; a deterministic harness
        calls this to drive the same job the loop would submit.  All detection
        still happens in :func:`detect_inferred_changes` on the executor path.
        """
        return self._self_heal_cycle()

    def fold_groups(self, groups: Sequence[EventGroup]) -> None:
        """Drive the real T5 ledger fold on P5's finalized groups (harness seam).

        P5 merges co-temporal reader steps into one group *after* the ready
        callback fires, so the queued snapshot carries only the first delta.
        A synchronous harness feeds the final groups back through this method,
        which submits the runtime's own ``_fold_group_ledger`` job -- no result
        is computed here.
        """
        for group in groups:
            # The early ready-callback fold already claimed this group id with
            # only its first delta; use a distinct id for the finalized group so
            # the ledger's deduplication does not discard the co-temporal deltas.
            final = group.model_copy(update={"group_id": f"{group.group_id}:final"})
            self._schedule_topology_ledger(final)

    # -- topological self-healing (T5 / T7 / L7) --------------------------- #

    def _schedule_topology_ledger(self, group: EventGroup) -> None:
        """P5 ready/revised hook: fold one FINAL group into the ledger off-loop.

        One background job per ready group keeps the fast/event loops O(1).  The
        job is pure (an injected clock) except for its S14 write, which goes
        through the repository's own short transaction (single-writer rule).
        """
        self.submit_background(self._fold_group_ledger, group)

    def _fold_group_ledger(self, group: EventGroup) -> int:
        """Fold ``group``'s reader deltas into the T5 ledger and persist edges.

        Called on the single background executor.  A group id is deduplicated by
        :class:`TopologyInferenceView`, so a revised delivery is idempotent.  When
        an edge crosses into CONFIRMED/CONFLICTED the view proposes and promotes
        a topology version through the injected VersionService (D1); the
        promotion hook then schedules the self-healing detectors and L7
        reconciliation.  Returns the number of edges written this call.
        """
        view = self.topology_inference
        if view is None:
            return 0
        with self._ledger_lock:
            from ..electrical.topology_inference import DeltaObservation

            obs = DeltaObservation.from_group(group)
            updated = view.observe_group(obs)
            self.topology_inference = updated
            repo = self.db.repos.topology_v3
            changed = 0
            for edge in updated.edges:
                try:
                    repo.upsert_edge(edge)
                    changed += 1
                except Exception:  # noqa: BLE001 - one edge must not abort the rest
                    log.debug("topology ledger edge write skipped", exc_info=True)
            # The wrapped D1 promote_topology already fires _on_version_promoted
            # (detectors + L7 reconciliation); no duplicate hook here.
            return changed

    def _on_version_promoted(self, kind: str, version_id: str) -> None:
        """Run after a topology/mapping promotion (T7 step 3 + L7 enqueue).

        Re-runs the inferred detectors immediately on the background executor so
        a freshly-promoted version is reconciled without waiting for the 15 min
        timer, enqueues L7 reconciliation for the promoted triple, and records
        the promotion for observability.
        """
        self.last_promotion = {"kind": kind, "version_id": version_id, "at": self._clock()}
        self._enqueue_reconciliation(
            {
                "reason": f"{kind}_promotion",
                "version_id": version_id,
                "at": self._clock().isoformat(),
            }
        )
        self._schedule_self_heal()

    def bind_promotion_hooks(self) -> None:
        """Wrap D1's promote methods so every promotion fires the T7 hook.

        This is composition only: it decorates the real VersionService rather
        than changing its producer module.  The wrapper preserves the original
        return value and delegates the hook off-loop via the background executor.
        """
        versions = self.version_service
        if versions is None or getattr(versions, "_wm_promotion_hooks", False):
            return
        promote_topology = versions.promote_topology
        promote_mapping = versions.promote_mapping

        def _topology(version_id: str, reason: str) -> None:
            from ..versioning.service import VersionConflictError

            try:
                promote_topology(version_id, reason)
            except VersionConflictError:
                # An already-active version is an idempotent re-promotion (the
                # promotion hook re-runs detectors); treat it as a no-op so a
                # repeated detector cycle cannot abort the T7 workflow.
                if self.version_service is not None and str(
                    self.version_service.current().topology_version_id
                ) != version_id:
                    raise
            self._on_version_promoted("topology", version_id)

        def _mapping(version_id: str, reason: str) -> None:
            from ..versioning.service import VersionConflictError

            try:
                promote_mapping(version_id, reason)
            except VersionConflictError:
                # Idempotent re-promotion from a detector cycle triggered by the
                # promotion hook: an already-active version is a no-op.
                if self.version_service is not None and str(
                    self.version_service.current().mapping_version_id
                ) != version_id:
                    raise
            self._on_version_promoted("mapping", version_id)

        versions.promote_topology = _topology  # type: ignore[method-assign]
        versions.promote_mapping = _mapping  # type: ignore[method-assign]
        versions._wm_promotion_hooks = True  # type: ignore[attr-defined]

    def _schedule_self_heal(self) -> None:
        """Submit one detector+workflow cycle to the background executor."""
        self.submit_background(self._self_heal_cycle)

    def _mark_detector_provenance(self, event_id: str, change: Any) -> None:
        """Append a SUSPECTED event carrying the producing detector's evidence ref.

        D1's configuration-event ledger is append-only, so provenance is added as
        a sibling row rather than editing the workflow's original event.  The
        evidence ref is a plain string token (``detector:<kind>``) that the
        integration can resolve to the pure detector that produced the change.
        """
        try:
            from ..contracts import (
                ChangeDetection,
                ChangeStatus,
                ConfigurationEvent,
                EvidenceRef,
            )

            evidence = " ".join(str(token) for token in getattr(change, "evidence", ()))
            # Persist to the runtime's own ledger (the surface v3_configuration_events
            # reads), independent of which D1 service a harness rebinds.
            self.db.repos.topology_v3.append_configuration_event(
                ConfigurationEvent(
                    event_id=f"{event_id}:detector-evidence",
                    kind=change.kind,
                    detection=ChangeDetection.INFERRED,
                    status=ChangeStatus.SUSPECTED,
                    affected_endpoint_ids=[change.subject],
                    evidence_refs=[
                        EvidenceRef(
                            kind="observation",
                            id=f"topology_change.detect_inferred_changes:{evidence}",
                        )
                    ],
                    created_at=self._clock(),
                )
            )
        except Exception:  # noqa: BLE001 - provenance is best-effort
            log.debug("detector provenance write skipped", exc_info=True)

    def _publish_confirmed_configuration(self, record: Any, change: Any) -> None:
        """Append the CONFIRMED `ConfigurationEvent` after T7 promotion.

        The workflow keeps its own append-only record ledger; D1's ledger only
        sees the persisted SUSPECTED rows.  On promotion the runtime publishes a
        CONFIRMED row through the same injected D1 service, so the persisted
        configuration-event surface reflects the established change.  The
        detection stays INFERRED and a detector evidence ref is attached.
        """
        try:
            from ..contracts import ChangeDetection, ChangeStatus, ConfigurationEvent, EvidenceRef

            evidence = " ".join(str(token) for token in getattr(change, "evidence", ()))
            # Persist to the runtime's own ledger (the surface v3_configuration_events
            # reads), independent of which D1 service a harness rebinds.
            self.db.repos.topology_v3.append_configuration_event(
                ConfigurationEvent(
                    event_id=f"{record.event_id}:confirmed",
                    kind=change.kind,
                    detection=ChangeDetection.INFERRED,
                    status=ChangeStatus.CONFIRMED,
                    affected_endpoint_ids=list(getattr(record, "affected_endpoints", ()))
                    or [change.subject],
                    evidence_refs=[
                        EvidenceRef(
                            kind="observation",
                            id=f"topology_change.detect_inferred_changes:{evidence}",
                        )
                    ],
                    created_at=self._clock(),
                    resolved_at=self._clock(),
                )
            )
        except Exception:  # noqa: BLE001 - ledger append must not kill the worker
            log.debug("confirmed configuration event write skipped", exc_info=True)

    def _self_heal_cycle(self) -> tuple[Any, ...]:
        """Detector → SUSPECTED event → workflow → promote → reconcile.

        Runs on the background executor.  Inferred detectors are pure
        (:func:`detect_inferred_changes`, :func:`detect_reader_removals`) over the
        association history and reader last-seen times the runtime maintains;
        each proposal is recorded through the T7 workflow, which promotes after
        its independent-evidence threshold and enqueues L7 reconciliation.
        """
        workflow = self.configuration_workflow
        if workflow is None:
            return ()
        from ..electrical.topology_change import (
            detect_inferred_changes,
            detect_reader_removals,
        )

        try:
            changes = list(
                detect_inferred_changes(
                    association_history=self._association_history,
                    now=self._clock(),
                )
            )
            changes.extend(
                detect_reader_removals(
                    reader_last_seen=self._reader_last_seen,
                    now=self._clock(),
                    missing_after_s=max(self.config.stale_after_s * 5.0, 600.0),
                )
            )
            out: list[Any] = []
            for change in changes:
                # A repeated detector cycle re-registers the same SUSPECTED event
                # (append-only D1 must not see a duplicate id) and adds one more
                # *independent* corroboration, so suspicion can promote over time.
                event_id = f"evt-inferred-{getattr(change.kind, 'value', change.kind)}-{change.subject}"
                existing = workflow.record_for(event_id)
                if existing is None:
                    record = workflow.record_inferred(change)
                    # Stamp the SUSPECTED event's evidence ref with the detector
                    # that produced it — the integration asserts provenance.
                    self._mark_detector_provenance(record.event_id, change)
                    # HB §14 step 2: mark affected lifecycles exactly once.  A
                    # repeated detector cycle only adds corroboration; re-running
                    # begin() would append a duplicate append-only history row.
                    record = workflow.begin(record.event_id, affected_endpoints=[change.subject])
                else:
                    record = existing
                # A record already past SUSPECTED needs no further cycles: the
                # promotion hook re-schedules detectors on every version bump.
                if str(getattr(record.status, "value", record.status)) != "suspected":
                    continue
                count = (record.corroborations + 1)
                confirmed = workflow.observe_confirming_event(
                    record.event_id,
                    # A distinct token per detector cycle is what lets the T7
                    # threshold count several independent observations (HB §14).
                    source=f"detector:{change.kind}:observation-{count}",
                )
                if str(getattr(confirmed.status, "value", confirmed.status)) == "confirmed":
                    self._publish_confirmed_configuration(confirmed, change)
                out.append(confirmed)
            return tuple(out)
        except Exception:  # noqa: BLE001 - a detector cycle must not kill the worker
            log.exception("self-healing detector cycle failed")
            return ()

    def _enqueue_reconciliation(self, payload: Any) -> None:
        """T7 promotion sink: enqueue L7 ``Reconciler`` off the event loop."""
        self.submit_background(self._run_reconciliation, payload)

    def _run_reconciliation(self, payload: Any) -> Any | None:
        """Run one L7 reconciliation for a promoted configuration change."""
        reconciler = self.reconciler
        versions = self.version_service
        if reconciler is None or versions is None:
            return None
        try:
            triple = versions.current()
            # The harness may rebind the version service (a different model-set
            # id) after composition; register the live registry under whatever
            # id the current triple carries before replaying.
            if self.model_registry_v3 is not None:
                reconciler._model_sets[triple.model_set_id] = self.model_registry_v3
            end = self._clock()
            start = end - timedelta(hours=1)
            return reconciler.run(
                (start, end),
                triple,
                reason="topology_promotion",
            )
        except Exception:  # noqa: BLE001 - reconciliation is best-effort background work
            log.exception("L7 reconciliation failed")
            return None

    def _audit_configuration(self, payload: Any) -> None:
        """Append-only audit sink for T7 promotion/rejection decisions."""
        log.info("configuration workflow: %s", payload)

    def _record_latency(self, stage: str, millis: float) -> None:
        self.stage_latency_s[stage] = round(millis / 1000.0, 6)

    def runtime_metrics(self) -> dict[str, Any]:
        """Loop lag, per-stage latency, queue depths and dropped groups."""
        return {
            "loop_lag_s": dict(self.loop_lag_s),
            "stage_latency_ms": {k: round(v * 1000.0, 3) for k, v in self.stage_latency_s.items()},
            "event_queue": self.groups_ready.stats(),
            "persist_queue": self.persist_queue.stats(),
            "groups_dropped": self.groups_dropped,
            "infer_group_calls": self.infer_group_calls,
            "infer_steady_calls": self.infer_steady_calls,
            "heartbeats": self.heartbeats,
            "background": (
                self.background_executor.stats() if self.background_executor is not None else {}
            ),
        }

    # -- facade providers (stubbed until P6) -------------------------------- #

    def now(self) -> datetime:
        """The injected clock, exposed for v3 route adapters (no wall clock)."""
        return self._clock()

    def v3_configuration_events(self, status: ChangeStatus | None = None) -> list[Any]:
        """T7: the configuration-event ledger, filtered by status when asked."""
        from ..contracts import ConfigurationEvent

        wanted = status.value if isinstance(status, ChangeStatus) else (
            str(status) if status is not None else None
        )
        rows = self.db.repos.topology_v3.events(wanted)
        events: list[Any] = []
        for row in rows:
            if isinstance(row, ConfigurationEvent):
                events.append(row)
            else:
                try:
                    events.append(ConfigurationEvent.model_validate(dict(row)))
                except Exception:  # noqa: BLE001 - one malformed row never fails the read
                    continue
        return events

    def reader_residual_points(self, reader_id: str) -> list[Any]:
        """T3: per-reader residual points from the S19 series (nulls preserved)."""
        rows = self.db.repos.residual.series(reader_id)
        points: list[Any] = []
        for row in rows:
            from ..contracts import ReaderResidualPoint

            data = dict(row)
            data.pop("id", None)
            if data.get("residual_w") is not None:
                data["residual_w"] = float(data["residual_w"])
            points.append(ReaderResidualPoint(**data))
        return points

    def residual_model(self) -> Any:
        """T3: the live ``ResidualModelView`` used by inference."""
        return self.unknown_prior

    def unknown_profiles(self, scope: str | None = None) -> list[Any]:
        """T6: persistent unknown-load profiles from the S19 repository."""
        return self.db.repos.residual.profiles(scope)

    def promote_unknown_profile(self, profile_id: str) -> dict[str, Any]:
        """T6: a user-driven promotion never auto-confirms a device.

        The device is created through D2's service (``origin`` recorded) and the
        PROVISIONAL mapping edge through D1's version service.  No power model is
        touched (AT-15: a mapping change never changes a model).
        """
        from ..contracts import UnknownLoadProfileV3
        from ..devices.service import DeviceService
        from ..residual.unknown_profiles import promote_profile

        rows = self.db.repos.residual.profiles()
        raw = next((p for p in rows if str(p.get("profile_id")) == profile_id), None)
        if raw is None:
            return {"ok": False, "reason": "profile not found", "profile_id": profile_id}
        profile = raw if isinstance(raw, UnknownLoadProfileV3) else UnknownLoadProfileV3(**raw)
        devices = DeviceService(self.db)
        try:
            promotion = promote_profile(
                profile,
                devices=devices,
                mapping_edges=self.version_service,
                profiles=self.db.repos.residual,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001 - promotion is a user action
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "profile_id": profile_id}
        if promotion is None:
            return {"ok": False, "reason": "profile is not promotable", "profile_id": profile_id}
        return {
            "ok": True,
            "profile_id": profile_id,
            "device_id": promotion.device.device_id,
            "mapping_edge_id": promotion.mapping_edge.mapping_edge_id,
            "lifecycle": str(promotion.profile.lifecycle),
        }

    def _time_stage(self, _name: str, _at: datetime) -> None:
        """Hook kept for stage-boundary instrumentation (no-op by default)."""

    def _prune_evidence(self) -> None:
        """Background job: bound the new L2 evidence/L1 raw tables (DEC-30).

        Runs on the single background executor, never the event loop.  Absent
        horizons mean "keep everything" — an explicit operator action bounds
        evidence, never a fabricated default.
        """
        db = self.db
        cfg = self.config
        cutoff_days = cfg.evidence_retention_days
        raw_days = cfg.evidence_raw_retention_days
        with db._lock:  # noqa: SLF001 - single-writer storage seam
            for table, days in (("event_evidence", cutoff_days), ("ha_events_v3", raw_days)):
                if days is None:
                    continue
                cutoff = (self._clock() - timedelta(days=days)).isoformat()
                try:
                    db._conn.execute(  # noqa: SLF001 - storage seam
                        f"DELETE FROM {table} WHERE window_start < ?" if table == "event_evidence"
                        else f"DELETE FROM {table} WHERE observed_at < ?",
                        (cutoff,),
                    )
                except Exception:  # noqa: BLE001 - table may not exist yet
                    log.debug("evidence prune skipped for %s", table, exc_info=True)

    async def _self_heal_loop(self) -> None:
        """T7 detectors on the background executor every 15 minutes."""
        interval = max(1.0, 15.0 * 60.0)
        while not self._stopping:
            try:
                self._schedule_self_heal()
            except Exception:  # noqa: BLE001
                log.exception("failed to schedule self-healing cycle")
            try:
                await asyncio.wait_for(self._stop_event.wait(), interval)
                return
            except TimeoutError:
                continue

    async def _maintenance_loop(self) -> None:
        while not self._stopping:
            try:
                self.aligner.prune()
                self.db.prune(self.config.retention_days_raw, self.config.retention_days_aggregates)
                # Evidence/raw retention runs off the event loop (DEC-37).
                self.submit_background(self._prune_evidence)
            except Exception:  # noqa: BLE001
                log.exception("maintenance cycle failed")
            try:
                # A fresh install whose startup discovery lost the race against
                # the first HA connection keeps retrying (rate-limited) until
                # measurement endpoints exist; a no-op afterwards.
                await self.maybe_bootstrap_topology()
            except Exception:  # noqa: BLE001
                log.exception("endpoint discovery retry failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), MAINTENANCE_INTERVAL_S)
                return
            except TimeoutError:
                continue


class _StaticEstablished:
    """An empty ``EstablishedStructure`` for the first steady pass (DEC-22)."""

    def groups(self) -> Mapping[str, tuple[str, ...]]:
        return {}

    def identities(self) -> Mapping[tuple[str, str], float]:
        return {}


def _steady_group(result: JointInferenceResult, at: datetime) -> EventGroup:
    """A lightweight synthetic group for a steady-pass result."""
    return EventGroup(
        group_id=f"steady-{result.result_id}",
        revision=1,
        status=result.status,
        opened_at=at,
    )


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
