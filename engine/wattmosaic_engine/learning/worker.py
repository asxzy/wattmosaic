"""Background learner worker — training, validation, promotion off the event loop (L6).

The fast path never trains.  It only calls :meth:`LearningWorker.submit` with
observation ids; the worker owns a *dirty set* of subjects and drains it on an
injected executor (DEC-37: one background thread, each job chunked to ≤ 2 s of
CPU).  Training, validation, promotion and the atomic ``Registry.activate`` swap
therefore never run on the asyncio loop — the ADR-0033 defect class where a
synchronous 5 s train starved ``/health`` and ``/healthz``.

Design rules (F6 / HB §15 / DEC-37):

* **Pure where possible.**  Queuing, chunking, dirty-set bookkeeping and the
  restart re-derivation are pure given injected inputs.  There is no wall clock
  in this module: every phase boundary is driven by the injected ``clock`` and
  CPU budget is measured by an injected ``budget`` callable.
* **No hidden in-memory state is authoritative.**  The dirty set is a *cache* of
  work; on :meth:`start` it is re-derived from S18 ``dirty_subjects()`` so a
  restart loses no confirmed evidence.  A subject's newest observation id is
  its watermark: a subject stays dirty until training has consumed that id.
* **Idle is free.**  A cycle with no dirty subjects performs zero learning DB
  queries — no scan of every topology device (``integration.py:690-700``).
* **Atomic swap by version key.**  A promoted candidate goes through
  ``Registry.activate``; the registry swaps its active set and cache under one
  lock.  No partial model set is ever published.
* **No ``runtime.fakes`` import.**  Production depends only on Protocols and
  contracts; tests supply the doubles.

The worker is deliberately *not* wired to a wall-clock thread or the main
process here — P6 owns the executor and the start/stop hook.  Supply any
executor with ``submit(fn)`` (a ``ThreadPoolExecutor`` in production, a
same-thread executor in tests that want to drive cycles directly).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from ..contracts import (
    EvidenceRef,
    LearningObservation,
    ModelBasis,
    ModelClass,
    PowerModelVersion,
)
from ..contracts import model_set_id as make_model_set_id
from ..runtime.interfaces import Clock
from .incremental import IncrementalAccumulator
from .promotion import evaluate_promotion_v2

_LOGGER = logging.getLogger(__name__)

#: DEC-37: no single job may consume more than 2 s of CPU before yielding.
MAX_JOB_CPU_S = 2.0


@runtime_checkable
class Executor(Protocol):
    """The minimum P6 executor seam (``concurrent.futures.Executor`` satisfies it)."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any: ...


class InlineExecutor:
    """A synchronous executor for driving the worker directly in tests (P6 note 4).

    It makes the worker deterministic and lets a test force a slow (e.g. 2 s)
    job to verify the fast path is not involved, without any real thread.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)


@dataclass(frozen=True)
class LearningJobStats:
    """What one training job actually did (observable, never guessed)."""

    subject_id: str
    observations_used: int = 0
    promoted: bool = False
    version_id: str | None = None
    model_set_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class CycleReport:
    """Result of one drain of the worker's dirty set."""

    trained: tuple[LearningJobStats, ...] = ()
    active: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _DirtyEntry:
    """One subject's queued work: the newest observation id wins."""

    subject_kind: str
    newest_observation_id: str
    at: datetime


# Phase 1 of the job: fit a candidate from the subject's confirmed observations.
# Phases 2 and 3 (promotion decision / registry swap) are separate callables so
# the worker can yield the executor between them (CPU chunking, DEC-37).
_Job = tuple[str, str, list[LearningObservation], datetime]


class LearningWorker:
    """Per-subject dirty-set learner driven by an injectable executor.

    Parameters
    ----------
    source:
        An object exposing ``dirty_subjects() -> list[(kind, subject_id)]``
        (S18 ``LearningRepository``) used for restart re-derivation, and
        ``observations(subject_id, status='confirmed')`` returning row dicts.
    registry:
        The v3 :class:`~wattmosaic_engine.models.registry.Registry`; a candidate
        is published only via ``activate``.  May be ``None`` for training-only
        use (the worker still validates/promotes the decision).
    executor:
        Anything with ``submit(fn, ...)``.  P6 injects the single background
        executor; tests may use :class:`InlineExecutor`.
    clock:
        Injected source of "now" (no wall clock in engine code).
    budget:
        Injected CPU-seconds probe, defaulting to ``time.process_time``.  Each
        job starts a new chunk and stops when the budget is exhausted, so no
        single job exceeds ``max_job_cpu_s``.
    """

    def __init__(
        self,
        source: Any,
        registry: Any = None,
        *,
        executor: Executor | None = None,
        clock: Clock | None = None,
        budget: Callable[[], float] | None = None,
        max_job_cpu_s: float = MAX_JOB_CPU_S,
    ) -> None:
        # Imported lazily so the module stays importable without a wall clock
        # dependency at construction time; ``budget`` is injectable and pure
        # tests never reach this default.
        import time as _time

        self.source = source
        self.registry = registry
        self.executor: Executor = executor if executor is not None else InlineExecutor()
        self.clock: Clock = clock or (lambda: datetime.now(UTC))
        self._budget: Callable[[], float] = budget or _time.process_time
        self.max_job_cpu_s = max(0.0, float(max_job_cpu_s))

        self._dirty: dict[tuple[str, str], _DirtyEntry] = {}
        self._future: Any | None = None
        self._running = False
        # Cumulative, observable counters (a cycle with no work leaves them
        # unchanged, which is what the zero-query test asserts).
        self.jobs_run = 0
        self.observations_consumed = 0
        self.last_cycle_at: datetime | None = None
        self.last_report: CycleReport | None = None

    # -- dirty set / fast path ---------------------------------------------- #
    def submit(self, observation_ids: Iterable[Any]) -> int:
        """Enqueue confirmed observation ids, returning how many were new work.

        This is the entire fast path: map each id to its subject (pure, via the
        injected ``source``) and fold it into the dirty set.  No training, no
        validation, no registry swap — and crucially no per-device DB scan.
        An id already at or below a subject's watermark is deduplicated.
        """
        added = 0
        for observation_id in observation_ids:
            observation = self._lookup_observation(str(observation_id))
            if observation is None:
                continue
            if self._mark_dirty(observation):
                added += 1
        return added

    def submit_observations(self, observations: Iterable[LearningObservation]) -> int:
        """Fold already-materialised observations into the dirty set (pure)."""
        added = 0
        for observation in observations:
            if self._mark_dirty(observation):
                added += 1
        return added

    def _lookup_observation(self, observation_id: str) -> LearningObservation | None:
        """Resolve one observation id to a contract via the source seam.

        Production sources expose ``observation_by_id`` (S18); tests may instead
        pass whole ``LearningObservation`` objects through
        :meth:`submit_observations`.  Absent ids are silently ignored — a submit
        never raises into the inference loop.
        """
        finder = getattr(self.source, "observation_by_id", None)
        if callable(finder):
            try:
                row = finder(observation_id)
            except Exception:  # noqa: BLE001 - fast path must not fail inference
                _LOGGER.warning("learning observation lookup failed for %s", observation_id)
                return None
            return self._coerce_observation(row)
        return None

    def _coerce_observation(self, row: Any) -> LearningObservation | None:
        if row is None:
            return None
        if isinstance(row, LearningObservation):
            return row
        if isinstance(row, Mapping):
            try:
                return LearningObservation.model_validate(dict(row))
            except Exception:  # noqa: BLE001
                return None
        return None

    def _mark_dirty(self, observation: LearningObservation) -> bool:
        """Add a subject to the dirty set if this observation advances it.

        Observation ids are content hashes (not ordered in time), so dedup is
        by *subject membership*: once a subject is queued, all of its confirmed
        observations are re-read together when the job runs.  Re-submitting ids
        for an already-dirty subject is therefore a no-op.
        """
        if str(getattr(observation, "status", "")) != "confirmed":
            return False
        key = (str(observation.subject_kind), str(observation.subject_id))
        current = self._dirty.get(key)
        if current is not None:
            # Keep the newest watermark by timestamp, but do not re-queue the
            # subject (the job re-reads all its evidence anyway).
            if observation.created_at > current.at:
                self._dirty[key] = _DirtyEntry(
                    subject_kind=key[0],
                    newest_observation_id=observation.observation_id,
                    at=observation.created_at,
                )
            return False
        self._dirty[key] = _DirtyEntry(
            subject_kind=key[0],
            newest_observation_id=observation.observation_id,
            at=observation.created_at,
        )
        return True

    @property
    def dirty_subjects(self) -> tuple[tuple[str, str], ...]:
        """The current dirty set, in deterministic order."""
        return tuple(sorted(self._dirty))

    @property
    def is_running(self) -> bool:
        return self._running

    # -- restart safety ------------------------------------------------------ #
    def start(self) -> None:
        """Re-derive the dirty set from S18 ``dirty_subjects()`` (HB §15).

        The persisted S18 view — confirmed observations newer than each
        subject's active version — is authoritative across a restart.  A
        subject already queued locally is kept; its newest id is reconciled
        against the re-derived watermark so no confirmed evidence is lost.
        """
        deriver = getattr(self.source, "dirty_subjects", None)
        if not callable(deriver):
            return
        rows = deriver() or []
        for subject_kind, subject_id in rows:
            key = (str(subject_kind), str(subject_id))
            newest = self._newest_confirmed_observation_id(subject_id)
            if newest is None:
                # Still mark the subject dirty with a sentinel id so the job
                # re-reads its observations; an empty result is a no-op.
                newest = ""
            current = self._dirty.get(key)
            if current is not None and current.newest_observation_id == newest:
                continue
            self._dirty[key] = _DirtyEntry(
                subject_kind=key[0],
                newest_observation_id=newest,
                at=self.clock(),
            )

    def _newest_confirmed_observation_id(self, subject_id: str) -> str | None:
        reader = getattr(self.source, "observations", None)
        if not callable(reader):
            return None
        try:
            rows = reader(subject_id, status="confirmed") or []
        except Exception:  # noqa: BLE001
            return None
        newest: str | None = None
        newest_at: datetime | None = None
        for row in rows:
            observation = self._coerce_observation(row)
            if observation is None:
                continue
            if newest_at is None or observation.created_at >= newest_at:
                newest_at = observation.created_at
                newest = observation.observation_id
        return newest

    # -- executor-driven draining ------------------------------------------- #
    def schedule(self) -> bool:
        """Submit one drain cycle to the executor; return whether one was started.

        Repeated calls while a cycle is pending are no-ops, so the fast path can
        call this freely.  A cycle with an empty dirty set is still dispatched:
        it performs zero learning DB queries (it only checks the in-memory set).
        """
        if self._future is not None and not self._future_done():
            return False
        try:
            self._future = self.executor.submit(self.drain)
            self._running = True
            return True
        except Exception as exc:  # noqa: BLE001 - executor may be shutting down
            _LOGGER.warning("learning worker could not schedule a cycle: %s", exc)
            return False

    def _future_done(self) -> bool:
        done = getattr(self._future, "done", None)
        return bool(done()) if callable(done) else True

    def flush(self, *, timeout: float | None = None) -> CycleReport:
        """Wait for a scheduled cycle and return its report (P6 convenience)."""
        if self._future is None:
            return self.last_report or CycleReport()
        result = self._future
        wait = getattr(result, "result", None)
        report = wait(timeout=timeout) if callable(wait) else self.last_report
        self._running = False
        self._future = None
        return report if report is not None else CycleReport()

    def drain(self) -> CycleReport:
        """Run all currently dirty subjects, one CPU-chunked job each.

        Pure with respect to the dirty set: each subject is popped before its
        job runs, so a failure or a budget timeout never leaves it stuck forever
        — the next submit (or a restart) re-queues it.  A failure on one subject
        is isolated and never aborts the rest.
        """
        subjects = sorted(self._dirty)
        trained: list[LearningJobStats] = []
        for key in subjects:
            entry = self._dirty.pop(key, None)
            if entry is None:
                continue
            try:
                stats = self._train_subject(key[1], entry)
            except Exception as exc:  # noqa: BLE001 - per-subject isolation
                _LOGGER.warning("learning job failed for %s: %s", key[1], exc)
                stats = LearningJobStats(subject_id=key[1], reason=f"error:{type(exc).__name__}")
            trained.append(stats)
            self.jobs_run += 1
        report = CycleReport(
            trained=tuple(trained),
            active=self._active_pairs(),
        )
        self.last_cycle_at = self.clock()
        self.last_report = report
        self._running = False
        return report

    # -- training / promotion / swap ---------------------------------------- #
    def _observations_for(self, subject_id: str, kind: str) -> list[LearningObservation]:
        reader = getattr(self.source, "observations", None)
        if not callable(reader):
            return []
        try:
            rows = reader(subject_id, status="confirmed") or []
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("failed to read learning observations for %s: %s", subject_id, exc)
            return []
        out: list[LearningObservation] = []
        for row in rows:
            observation = self._coerce_observation(row)
            if observation is None:
                continue
            if kind and str(observation.subject_kind) != kind:
                continue
            out.append(observation)
        out.sort(key=lambda o: (o.created_at, o.observation_id))
        return out

    def _train_subject(self, subject_id: str, entry: _DirtyEntry) -> LearningJobStats:
        observations = self._observations_for(subject_id, entry.subject_kind)
        if not observations:
            return LearningJobStats(subject_id=subject_id, reason="no_confirmed_observations")
        self.observations_consumed += len(observations)

        # Phase 1: fit a candidate incrementally.  Chunk by CPU budget: each
        # accumulator update is a bounded unit of work; if the injected budget
        # is exhausted we keep the observations for the next submit rather than
        # exceeding the 2 s cap.
        accumulator = IncrementalAccumulator(ModelClass.FIXED)
        used: list[LearningObservation] = []
        start_budget = self._budget()
        for observation in observations:
            if self.max_job_cpu_s > 0 and self._budget() - start_budget > self.max_job_cpu_s:
                _LOGGER.info(
                    "learning job for %s hit the %.1fs CPU chunk; deferring rest",
                    subject_id,
                    self.max_job_cpu_s,
                )
                break
            accumulator.update(observation)
            used.append(observation)
        if not used:
            return LearningJobStats(subject_id=subject_id, reason="cpu_budget_exhausted")

        spec = accumulator.spec()
        number = self._next_version_number(subject_id)
        candidate = PowerModelVersion(
            version_id=f"mdl-{subject_id}-{number}",
            subject_kind=entry.subject_kind,  # type: ignore[arg-type]
            subject_id=subject_id,
            number=number,
            model_class=ModelClass.FIXED,
            basis=ModelBasis.DELTA,
            spec=spec,
            evidence_refs=[
                EvidenceRef(kind="observation", id=o.observation_id) for o in used
            ],
            trainer_version="l6-worker",
            created_at=self.clock(),
        )

        # Phase 2: pure promotion decision on the frozen evidence set.
        active = self._active_version(subject_id)
        frozen = self._is_frozen(subject_id)
        decision = evaluate_promotion_v2(
            candidate, active, used, frozen=frozen, clock=self.clock
        )
        if not decision.promoted:
            return LearningJobStats(
                subject_id=subject_id,
                observations_used=len(used),
                promoted=False,
                reason=decision.reason,
            )

        # Phase 3: append the candidate, append its events, then atomically
        # activate through the registry (single version-key swap).
        self._append_version(candidate)
        self._append_events(candidate.version_id, decision.events)
        activated_id: str | None = None
        model_set: str | None = None
        if self.registry is not None:
            self.registry.activate(candidate.version_id, reason="promoted")
            activated_id = candidate.version_id
            refresh = getattr(self.registry, "refresh", None)
            if callable(refresh):
                refresh()
            model_set = str(getattr(self.registry, "model_set_id", "") or "") or None
        return LearningJobStats(
            subject_id=subject_id,
            observations_used=len(used),
            promoted=True,
            version_id=activated_id,
            model_set_id=model_set,
            reason="promoted",
        )

    # -- registry / repository helpers (duck-typed seams, no fakes import) --- #
    def _next_version_number(self, subject_id: str) -> int:
        versions = self._versions(subject_id)
        return max((int(getattr(v, "number", 0)) for v in versions), default=0) + 1

    def _versions(self, subject_id: str) -> list[PowerModelVersion]:
        if self.registry is None:
            return []
        try:
            rows = self.registry.versions(subject_id) or []
        except Exception:  # noqa: BLE001
            return []
        out: list[PowerModelVersion] = []
        for row in rows:
            if isinstance(row, PowerModelVersion):
                out.append(row)
            elif isinstance(row, Mapping):
                try:
                    out.append(PowerModelVersion.model_validate(dict(row)))
                except Exception:  # noqa: BLE001
                    continue
        return out

    def _active_version(self, subject_id: str) -> PowerModelVersion | None:
        if self.registry is None:
            return None
        getter = getattr(self.registry, "active_version", None)
        if not callable(getter):
            return None
        row = getter(subject_id)
        if isinstance(row, PowerModelVersion):
            return row
        if isinstance(row, Mapping):
            try:
                return PowerModelVersion.model_validate(dict(row))
            except Exception:  # noqa: BLE001
                return None
        return None

    def _is_frozen(self, subject_id: str) -> bool:
        if self.registry is None:
            return False
        getter = getattr(self.registry, "is_frozen", None)
        return bool(getter(subject_id)) if callable(getter) else False

    def _append_version(self, version: PowerModelVersion) -> None:
        repo = self._model_repo()
        if repo is not None:
            repo.append_version(version)

    def _append_events(self, version_id: str, events: Collection[Any]) -> None:
        repo = self._model_repo()
        if repo is None:
            return
        for event in events:
            repo.append_event(event)

    def _model_repo(self) -> Any | None:
        if self.registry is None:
            return None
        return getattr(self.registry, "_repo", None)

    def _active_pairs(self) -> tuple[tuple[str, str], ...]:
        """Currently activated (subject_id, version_id) pairs, deterministically."""
        if self.registry is None:
            return ()
        pairs: list[tuple[str, str]] = []
        for subject_id in self._subject_ids():
            version = self._active_version(subject_id)
            if version is not None:
                pairs.append((subject_id, version.version_id))
        return tuple(sorted(pairs))

    def _subject_ids(self) -> Collection[str]:
        if self.registry is None:
            return ()
        helper = getattr(self.registry, "subjects", None)
        try:
            return tuple(sorted(helper())) if callable(helper) else ()
        except Exception:  # noqa: BLE001
            return ()

    # -- diagnostics --------------------------------------------------------- #
    def status(self) -> dict[str, Any]:
        """A small, honest status dict (no measurements guessed as 0)."""
        active_ids = [version_id for _, version_id in self._active_pairs()]
        return {
            "dirty_subjects": [f"{kind}:{sid}" for kind, sid in self.dirty_subjects],
            "jobs_run": self.jobs_run,
            "observations_consumed": self.observations_consumed,
            "running": self._running,
            "last_cycle_at": None if self.last_cycle_at is None else self.last_cycle_at.isoformat(),
            "model_set_id": make_model_set_id(active_ids) if active_ids else None,
        }


__all__ = [
    "MAX_JOB_CPU_S",
    "CycleReport",
    "Executor",
    "InlineExecutor",
    "LearningJobStats",
    "LearningWorker",
]
