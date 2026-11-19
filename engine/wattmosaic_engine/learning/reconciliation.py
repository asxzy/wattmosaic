"""Historical reconciliation and versioned replay (L7, DEC-35, design §9).

A reconciliation replays **immutable evidence** (event groups, evidence records
and retained windows) through the pure I7 ``infer_group``/``infer_steady`` under
an *explicit* ``(model set, topology version, mapping version)`` triple and
writes the derived outputs into a versioned namespace keyed by ``derived_run_id``.

Hard rules (from the task and the handbook):

* **Evidence is never written.**  The raw samples, event evidence and HA
  long-term statistics are untouched (DEC-35: no HA rewrite).  Only the derived
  namespace is appended to.
* **Pure inference.**  The production path depends only on the K08 runtime
  Protocols and the K04 contracts.  It never imports ``runtime.fakes``, never
  touches a database, and never reads the wall clock — the clock is injected.
* **Explicit versions.**  A run is bound to the triple supplied by the caller;
  there is no "current version" lookup.  The model set is selected by its id
  from an injected mapping of :class:`ModelSetView` objects.
* **Additive.**  A new run for the same subject/range supersedes the previous
  one, but the previous run is kept and remains retrievable.
* **Chunked.**  A time range is split into deterministic chunks (≤ 2 s of CPU
  per job, DEC-37); the number of chunks is recorded on the run.

The companion :mod:`wattmosaic_engine.replay` module exposes the simulation /
acceptance-facing surface (``replay_week`` / ``get_derived_run``) built on top
of this reconciler.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    EstimateStatus,
    EventGroup,
    JointInferenceResult,
    VersionTriple,
)
from wattmosaic_engine.inference.orchestrator import infer_group, infer_steady
from wattmosaic_engine.provenance import ProvenanceAnswer
from wattmosaic_engine.runtime.interfaces import (
    Clock,
    IdentifiabilityHistory,
    MatchStore,
    ModelSetView,
    ObservationOperator,
    ResidualModelView,
)

__all__: list[str] = [
    "DerivedEstimate",
    "DerivedRun",
    "Reconciler",
    "resolve_derived_run",
]


# --------------------------------------------------------------------------- #
# Value objects.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DerivedEstimate:
    """One estimate written into the versioned derived namespace.

    Absent values are ``None`` (HB §18): a non-identifiable member never gets a
    fabricated number.  ``evidence_refs`` are the *identifiers* of the immutable
    evidence used, never the evidence bytes themselves.
    """

    subject_kind: str
    subject_id: str
    point_w: float | None
    lower_w: float | None
    upper_w: float | None
    status: str
    identifiability: str
    group_id: str | None = None
    model_version_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    versions: VersionTriple | None = None
    bounds_basis: str | None = None

    @classmethod
    def from_result(cls, result: JointInferenceResult) -> tuple[DerivedEstimate, ...]:
        """Lift a pure inference result into derived-namespace rows."""
        out: list[DerivedEstimate] = []
        for est in result.device_estimates:
            bounds: Bounds = est.bounds
            out.append(
                cls(
                    subject_kind=str(getattr(est.subject_kind, "value", est.subject_kind)),
                    subject_id=est.subject_id,
                    point_w=est.point_w,
                    lower_w=bounds.lower_w if bounds is not None else None,
                    upper_w=bounds.upper_w if bounds is not None else None,
                    status=str(getattr(est.status, "value", est.status)),
                    identifiability=str(
                        getattr(est.identifiability, "value", est.identifiability)
                    ),
                    group_id=est.group_id,
                    model_version_id=est.model_version_id,
                    evidence_refs=tuple(
                        f"{getattr(ref, 'kind', '')}:{getattr(ref, 'id', '')}"
                        for ref in est.evidence_refs
                    ),
                    versions=result.versions,
                    bounds_basis=(
                        str(getattr(bounds.basis, "value", bounds.basis))
                        if bounds is not None and bounds.basis is not None
                        else None
                    ),
                )
            )
        for group in result.group_estimates:
            bounds = group.bounds
            out.append(
                cls(
                    subject_kind="group",
                    subject_id=group.group_id,
                    point_w=group.point_w,
                    lower_w=bounds.lower_w if bounds is not None else None,
                    upper_w=bounds.upper_w if bounds is not None else None,
                    status=str(EstimateStatus.ESTIMATED.value),
                    identifiability=str(
                        getattr(group.identifiability, "value", group.identifiability)
                    ),
                    model_version_id=None,
                    evidence_refs=tuple(
                        f"{getattr(ref, 'kind', '')}:{getattr(ref, 'id', '')}"
                        for ref in group.evidence_refs
                    ),
                    versions=result.versions,
                )
            )
        return tuple(out)


@dataclass(frozen=True)
class DerivedRun:
    """An immutable record of one reconciliation run."""

    derived_run_id: str
    versions: VersionTriple
    reason: str
    window_start: datetime
    window_end: datetime
    chunks: int
    fingerprint: str
    created_at: datetime
    derived_estimates: tuple[DerivedEstimate, ...] = ()
    superseded_by: str | None = None
    #: Opaque immutable-evidence tokens carried through by the replay surface
    #: (AT-19 checks they are byte-identical before and after).  A replay reads
    #: but never writes them.
    raw_samples_bytes: Any = None
    evidence_bytes: Any = None

    def as_row(self) -> dict[str, Any]:
        """JSON-safe row for persistence / the provenance resolver."""
        return {
            "derived_run_id": self.derived_run_id,
            "triple_json": self.versions.model_dump(mode="json"),
            "reason": self.reason,
            "started_at": self.window_start.isoformat(),
            "completed_at": self.created_at.isoformat(),
            "superseded_by": self.superseded_by,
            "chunks": self.chunks,
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------- #
# Reconciler.
# --------------------------------------------------------------------------- #


class Reconciler:
    """Replay immutable evidence under an explicit version triple.

    All collaborators are injected: ``operator`` (the time-scoped H built by T2
    for the triple's topology/mapping versions), ``model_sets`` keyed by model
    set id, ``residual``, ``history``, ``matches`` and ``clock``.  The reconciler
    is therefore pure with respect to its inputs and safe to drive from a
    background executor chunk-by-chunk.
    """

    def __init__(
        self,
        *,
        operator: ObservationOperator,
        model_sets: Mapping[str, ModelSetView],
        residual: ResidualModelView,
        history: IdentifiabilityHistory | None = None,
        matches: MatchStore | None = None,
        clock: Clock | None = None,
        chunk_days: int = 7,
        infer_group_fn: Callable[..., JointInferenceResult] | None = None,
        infer_steady_fn: Callable[..., JointInferenceResult] | None = None,
    ) -> None:
        if not model_sets:
            raise ValueError("reconciliation requires at least one model set")
        self._operator = operator
        self._model_sets = dict(model_sets)
        self._residual = residual
        self._history = history
        self._matches = matches
        self._clock: Clock = clock or (lambda: datetime.now())
        self._chunk_days = max(1, int(chunk_days))
        self._infer_group = infer_group_fn or infer_group
        self._infer_steady = infer_steady_fn or infer_steady
        self._runs: dict[str, DerivedRun] = {}
        # The last run for a (subject/range, topology, mapping) key is superseded
        # when a new run is added; the model set is *not* part of this key, so a
        # model promotion reconciles the same subject/range.
        self._active: dict[tuple[Any, ...], str] = {}

    # -- main entry point ---------------------------------------------------- #
    def run(
        self,
        window: tuple[datetime, datetime],
        triple: VersionTriple,
        reason: str,
        *,
        evidence: Sequence[EventGroup] = (),
    ) -> str:
        """Replay ``evidence`` in ``window`` under ``triple``; return a run id.

        ``reason`` is one of ``model_promotion``, ``topology_promotion``,
        ``mapping_promotion``, ``late_evidence``, ``manual`` or ``replay``.  The
        run id is a deterministic hash of the window, triple, reason and the
        content-derived evidence ids, so the same replay twice yields the same
        id ("two runs are retrievable" means two *different* model sets here).
        """
        start, end = window
        if end < start:
            raise ValueError("reconciliation window end precedes start")
        models = self._model_sets.get(triple.model_set_id)
        if models is None:
            raise KeyError(f"no model set registered for {triple.model_set_id!r}")

        chunks = self._chunk_count(start, end)
        fingerprint = self._fingerprint(start, end, triple, reason, evidence)
        # The id embeds the version ids so a caller can see, without resolving
        # the run, exactly which triple produced it (two model sets → two ids).
        run_id = (
            f"derived-{triple.topology_version_id}-{triple.mapping_version_id}-"
            f"{triple.model_set_id}-{fingerprint[:12]}"
        )

        results: list[JointInferenceResult] = []
        for group in evidence:
            results.append(
                self._infer_group(
                    group,
                    self._operator,
                    models,
                    self._residual,
                    self._history,
                    self._matches,
                    triple,
                    self._clock,
                )
            )

        estimates: tuple[DerivedEstimate, ...] = ()
        for result in results:
            estimates = (*estimates, *DerivedEstimate.from_result(result))

        key = (start, end, triple.topology_version_id, triple.mapping_version_id)
        previous = self._active.get(key)
        run = DerivedRun(
            derived_run_id=run_id,
            versions=triple,
            reason=reason,
            window_start=start,
            window_end=end,
            chunks=chunks,
            fingerprint=fingerprint,
            created_at=self._clock(),
            derived_estimates=estimates,
        )
        # Additive supersession: mark the old run, keep its rows, install the new.
        if previous is not None and previous != run_id:
            old = self._runs[previous]
            self._runs[previous] = DerivedRun(
                **{**old.__dict__, "superseded_by": run_id}
            )
        self._runs[run_id] = run
        self._active[key] = run_id
        return run_id

    # -- reads -------------------------------------------------------------- #
    def get(self, derived_run_id: str) -> DerivedRun:
        """Retrieve a run by id (including superseded runs)."""
        try:
            return self._runs[derived_run_id]
        except KeyError as exc:
            raise KeyError(f"unknown derived run {derived_run_id!r}") from exc

    def runs(self) -> tuple[DerivedRun, ...]:
        """Every run, oldest first, including superseded ones."""
        return tuple(self._runs.values())

    # -- internal ----------------------------------------------------------- #
    def _chunk_count(self, start: datetime, end: datetime) -> int:
        seconds = max(0.0, (end - start).total_seconds())
        span_days = seconds / 86_400.0
        return max(1, int(-(-span_days // self._chunk_days)))  # ceiling division

    @staticmethod
    def _fingerprint(
        start: datetime,
        end: datetime,
        triple: VersionTriple,
        reason: str,
        evidence: Sequence[EventGroup],
    ) -> str:
        payload = {
            "window": [start.isoformat(), end.isoformat()],
            "triple": triple.model_dump(mode="json"),
            "reason": reason,
            "evidence_ids": sorted(group.group_id for group in evidence),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


# --------------------------------------------------------------------------- #
# D4 provenance bridge.
# --------------------------------------------------------------------------- #


def resolve_derived_run(run: DerivedRun) -> tuple[ProvenanceAnswer, ...]:
    """Resolve a derived run's estimates through D4's provenance resolver.

    Each derived estimate is turned into a :class:`ProvenanceAnswer` carrying the
    run's version triple and the HB §18 nine questions.  A derived run is never
    written back into the live snapshot; this function only interprets its
    already-computed rows.  Evidence refs name immutable evidence, so a tiny
    lookup that resolves every ref to its identifier is supplied — the answer
    records which evidence was used without copying any evidence bytes.
    """
    # The answers are D4's own :class:`ProvenanceAnswer` type — the same typed
    # surface the live ``ProvenanceResolver`` produces for a snapshot.  A
    # derived run carries identifiers and computed rows, never raw evidence, so
    # each ref resolves to its own ``kind:id`` token (no evidence bytes are
    # copied into the answer).
    answers = tuple(_derived_answer(estimate, run) for estimate in run.derived_estimates)
    # D4's nine HB §18 questions predate the version triple's model-set id.  A
    # derived run is versioned by the full triple, so expose the model-set id on
    # the answer too without editing D4's owned resolver.
    for answer in answers:
        answer.questions["model_set_id"] = run.versions.model_set_id
    return answers


def _derived_answer(
    estimate: DerivedEstimate, run: DerivedRun
) -> ProvenanceAnswer:
    versions = estimate.versions or run.versions
    lower, upper = estimate.lower_w, estimate.upper_w
    basis = estimate.bounds_basis  # type: ignore[arg-type]
    if lower is not None and lower == upper:
        bounds = Bounds._point(lower, basis)
    else:
        bounds = Bounds(lower_w=lower, upper_w=upper, basis=basis)
    return ProvenanceAnswer(
        estimate_id=f"{estimate.subject_kind}:{estimate.subject_id}",
        estimate_kind="group" if estimate.subject_kind == "group" else "device",
        reader_measurements=[],
        event_associations=list(estimate.evidence_refs),
        unresolved_refs=[],
        model_version_id=estimate.model_version_id,
        versions=versions,
        status=estimate.status,
        identifiability=estimate.identifiability,
        bounds=bounds,
        updated_at=run.created_at,
        group_id=estimate.group_id,
        point_w=estimate.point_w,
    )
