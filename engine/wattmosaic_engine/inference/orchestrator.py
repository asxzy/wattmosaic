"""Pure orchestration of the v3 inference pipeline (I7, design §7, DEC-22).

This module is the single composition root for the two entry points P6 calls
and L7 replays:

* :func:`infer_group` runs I1 → I2 → I3 → I4 → I5 → I6 over one event group;
* :func:`infer_steady` runs the same objective in absolute mode, constrained by
  the groups and identities established by earlier CONFIRMED event inferences
  (DEC-22).

Both are **pure**: no database, no wall clock (the only time source is the
injected ``clock``), no globals that change between calls.  Every cross-area
value is either a K04 contract or a K08 runtime ``Protocol``; production code
here never imports ``runtime.fakes`` and never imports ``realtime``, ``mapping``
or ``storage`` (architecture §4).

Null policy (HB §18): an absent reading, delta, sigma or residual stays
``None``.  Nothing here coerces a missing measurement into ``0.0``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

import numpy as np
from pydantic import Field

from wattmosaic_engine.contracts import (
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    MatchState,
    UnknownEstimate,
    VersionTriple,
)
from wattmosaic_engine.runtime.interfaces import (
    Clock,
    EstablishedStructure,
    HeldReading,
    IdentifiabilityHistory,
    MatchStore,
    ModelSetView,
    ObservationOperator,
    PredictionContext,
    ResidualModelView,
)

from .candidates import generate_candidates
from .estimates import DirectReading, EstimateInputs, assemble_status_estimates
from .evidence import EvidenceInputs, score_and_record
from .identifiability import (
    GroupIdentifiability,
    IdentifiabilityAnalyzer,
    IdentifiabilityReport,
)
from .joint import solve_joint_detailed
from .matching import classify_match

__all__: list[str] = [
    "CONFLICT_N_SIGMA",
    "IDENTITY_CONSTRAINT_SIGMA_W",
    "SteadyInferenceResult",
    "infer_group",
    "infer_steady",
]


#: DEC-22: a disagreement beyond three standard deviations is a conflict.
CONFLICT_N_SIGMA = 3.0

#: Sigma of the *soft* point constraint an established identity contributes to
#: the absolute solve.  It is a penalty scale, never a measurement, and it is
#: deliberately finite so that a genuinely contradictory current reading can
#: move the solution enough to be detected as a conflict.
IDENTITY_CONSTRAINT_SIGMA_W = 2.0

#: An established group contributes one sum-constraint row with this sigma.
GROUP_CONSTRAINT_SIGMA_W = 5.0

_NONZERO_TOL = 1e-9
_SIGN_POS = "pos"


# --------------------------------------------------------------------------- #
# Small adapters derived from the operator (no new public seam).
# --------------------------------------------------------------------------- #


class _OperatorMapping:
    """A read-only ``MappingView`` reconstructed from an operator's DIRECT entries.

    ``infer_group``'s normative signature has no ``MappingView`` argument: T2
    has already folded topology and mapping into ``operator``.  The DIRECT
    entries are the most-specific reader for a subject, which is exactly what
    I1's candidate generation needs.  No ``wattmosaic_engine.mapping`` import.
    """

    def __init__(self, operator: ObservationOperator) -> None:
        pairs: dict[tuple[str, str], str] = {}
        for (row, column), entry in operator.entries.items():
            if column >= len(operator.subjects) or row >= len(operator.rows):
                continue
            if _name(entry.observability) != "direct" or abs(entry.coefficient) <= _NONZERO_TOL:
                continue
            pairs.setdefault(operator.subjects[column], operator.rows[row].reader_id)
        # Fall back to the strongest non-zero row for a subject with no DIRECT
        # entry (e.g. a subject seen only through an EXPECTED ancestor).
        H = np.asarray(operator.H, dtype=float)
        for column, subject in enumerate(operator.subjects):
            if subject in pairs or column >= H.shape[1]:
                continue
            support = np.flatnonzero(np.abs(H[:, column]) > _NONZERO_TOL)
            if support.size and int(support[0]) < len(operator.rows):
                pairs[subject] = operator.rows[int(support[0])].reader_id
        self._pairs = MappingProxyType(pairs)

    def most_specific_reader(self, subject: tuple[str, str]) -> str | None:
        return self._pairs.get((str(subject[0]), str(subject[1])))

    def subjects_on(self, reader_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(s for s, reader in self._pairs.items() if reader == reader_id))


class _NoHistory:
    """An empty ``IdentifiabilityHistory`` for the steady-state pass.

    ``infer_steady`` has no history argument in the normative signature; its
    identifiability comes from ``EstablishedStructure`` (CONFIRMED event
    inferences) rather than from a fresh history query.
    """

    def events_touching(self, subject_ids: Any, since: Any) -> list[Any]:
        return []


def _name(value: Any) -> str:
    return str(getattr(value, "value", value))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _op_column(operator: ObservationOperator, subject_id: str) -> int | None:
    for index, (_kind, sid) in enumerate(operator.subjects):
        if sid == subject_id:
            return index
    return None


def _row_index(operator: ObservationOperator, reader_id: str) -> int | None:
    for index, meta in enumerate(operator.rows):
        if meta.reader_id == reader_id:
            return index
    return None


def _direct_subjects(operator: ObservationOperator) -> dict[str, tuple[str, int]]:
    """Single-subject DIRECT endpoints: ``subject_id -> (reader_id, row)``."""
    result: dict[str, tuple[str, int]] = {}
    H = np.asarray(operator.H, dtype=float)
    for (row, column), entry in operator.entries.items():
        if _name(entry.observability) != "direct":
            continue
        if column >= len(operator.subjects) or row >= len(operator.rows):
            continue
        support = np.flatnonzero(np.abs(H[row]) > _NONZERO_TOL) if H.ndim == 2 else np.zeros(0, int)
        if support.size != 1:
            continue
        result.setdefault(operator.subjects[column][1], (operator.rows[row].reader_id, row))
    return result


def _unknown_priors(
    residual: ResidualModelView, scopes: Sequence[str], at: Any
) -> dict[str, Any]:
    priors: dict[str, Any] = {}
    for scope in dict.fromkeys(scopes):
        prior = residual.unknown_prior(scope, at)
        if prior is not None:
            priors[scope] = prior
    return priors


def _model_priors_delta(
    models: ModelSetView, candidates: Sequence[Any]
) -> dict[str, DeltaPrior]:
    priors: dict[str, DeltaPrior] = {}
    for candidate in candidates:
        if _name(candidate.subject_kind) not in ("device", "component"):
            continue
        prior = models.transition_prior(
            candidate.subject_id, candidate.transition or "on", {}
        )
        if prior is not None:
            priors[candidate.subject_id] = prior
    return priors


def _model_priors_level(models: ModelSetView, operator: ObservationOperator, at: Any) -> dict[str, DeltaPrior]:
    priors: dict[str, DeltaPrior] = {}
    for _kind, subject_id in operator.subjects:
        prediction = models.predict(subject_id, PredictionContext(at=at))
        mean = _finite(prediction.mean_w)
        sigma = _finite(prediction.sigma_w)
        if mean is None or sigma is None or sigma <= 0.0:
            continue
        priors[subject_id] = DeltaPrior(
            mean_w=mean,
            sigma_w=sigma,
            sign=_SIGN_POS,
            model_version_id=prediction.model_version_id,
        )
    return priors


def _align_solution(
    operator: ObservationOperator, subject_ids: Sequence[str], solution: Any
) -> tuple[np.ndarray, np.ndarray | None]:
    """Scatter a partial solution onto ``operator.subjects`` columns.

    I4 indexes ``JointSolution.x`` by operator column, so a subset solve must
    be expanded before it is handed over.  Missing entries stay absent (NaN in
    the numeric array, never a fabricated measurement value).
    """
    n = len(operator.subjects)
    x_full = np.full(n, np.nan, dtype=float)
    for index, sid in enumerate(subject_ids):
        column = _op_column(operator, sid)
        if column is not None and index < np.asarray(solution.x).size:
            x_full[column] = float(solution.x[index])
    cov_full: np.ndarray | None = None
    cov = solution.cov
    if cov is not None:
        cov_full = np.full((n, n), np.nan, dtype=float)
        op_columns = [
            _op_column(operator, sid)
            for sid in subject_ids
        ]
        for i, row in enumerate(op_columns):
            if row is None:
                continue
            for j, col in enumerate(op_columns):
                if col is None:
                    continue
                cov_full[row, col] = float(np.asarray(cov)[i, j])
    return x_full, cov_full


def _partial_solution(solution: Any, x_full: np.ndarray, cov_full: np.ndarray | None) -> Any:
    """A ``JointSolution`` with aligned x/cov (same dataclass, new values)."""
    from dataclasses import replace

    return replace(solution, x=x_full, cov=cov_full)


def _plain_match_key(key: str) -> str:
    """Normalise a composite ``match_key`` to a plain candidate id."""
    return key.rsplit("\x00", 1)[-1]


def _result_matches(
    matches: MatchStore, delta: Mapping[str, MatchState]
) -> dict[str, MatchState]:
    """Merge a match delta into the injected immutable snapshot (pure)."""
    updated = matches.apply(dict(delta))
    states: dict[str, MatchState] = {}
    for key, state in updated.snapshot().items():
        states[_plain_match_key(str(key))] = MatchState(state)
    for candidate_id, state in delta.items():
        states[_plain_match_key(str(candidate_id))] = MatchState(state)
    return dict(sorted(states.items()))


# --------------------------------------------------------------------------- #
# infer_group — event-delta path.
# --------------------------------------------------------------------------- #


def infer_group(
    group: EventGroup,
    operator: ObservationOperator,
    models: ModelSetView,
    residual: ResidualModelView,
    history: IdentifiabilityHistory,
    matches: MatchStore,
    versions: VersionTriple,
    clock: Clock,
) -> JointInferenceResult:
    """Compose I1→I2→I3→I4→I5→I6 for one event group (pure).

    ``versions`` and ``clock`` are injected: the same inputs (including the
    same clock value) always produce a byte-identical result and ``result_id``.
    """
    at = clock()

    # -- I1: candidate generation (mapping reconstructed from the operator) -- #
    candidate_set = generate_candidates(
        group, operator, models, _OperatorMapping(operator)
    )
    candidates = [
        candidate
        for candidate in candidate_set.candidates
        if not bool(getattr(candidate, "filtered", False))
    ]

    # -- I2: evidence terms A-H + temporal fit as a solve penalty ------------ #
    evidence_inputs = EvidenceInputs(group=group, operator=operator, models=models)
    scored, evidence = score_and_record(list(candidates), evidence_inputs)
    scored = [
        candidate.model_copy(
            update={
                "evidence_terms": {
                    **candidate.evidence_terms,
                    "temporal_fit": float(evidence[candidate.candidate_id].temporal_fit),
                }
            }
        )
        if candidate.candidate_id in evidence
        else candidate
        for candidate in scored
        if not evidence[candidate.candidate_id].rejected
    ]

    # -- I3: one joint delta solve ------------------------------------------- #
    # Missing deltas stay ``None`` (HB §18) and a hard-rejected candidate is
    # excluded from the solve: a confirmed consumer with a clear ON/sign clash
    # must never be attributed a number (I2 step 4).
    rejected_ids = {result.candidate_id for result in evidence.values() if result.rejected}
    scored = [candidate for candidate in scored if candidate.candidate_id not in rejected_ids]
    delta_y = {rd.reader_id: rd.delta_w for rd in group.reader_deltas}
    sigma = {rd.reader_id: rd.sigma_w for rd in group.reader_deltas}
    scopes = [scope for scope, _rows in operator.unknown_scopes]
    scopes.extend(
        candidate.subject_id
        for candidate in scored
        if _name(candidate.subject_kind) == "unknown_scope"
    )
    solve_report = solve_joint_detailed(
        operator,
        delta_y,
        sigma,
        scored,
        _model_priors_delta(models, scored),
        _unknown_priors(residual, scopes, at),
        mode="delta",
    )
    solution = solve_report.solution

    solved_ids = list(solve_report.subject_ids)
    x_full, cov_full = _align_solution(operator, solved_ids, solution)
    aligned = _partial_solution(solution, x_full, cov_full)

    # -- I4: identifiability from H + injected history ----------------------- #
    # I1 may contribute operator-visible subjects that are not hypotheses for
    # this particular event (e.g. a fridge that did not step).  Feeding those
    # to I4 as if they were candidates would let unrelated columns dilute the
    # experiment matrix and hide a genuine co-toggle group (DEF-04).  Restrict
    # the analysis to the actual (unfiltered) hypotheses that entered the solve.
    solved_subjects = {
        sid
        for sid in solved_ids
        if any(
            candidate.subject_id == sid
            and _name(candidate.subject_kind) != "unknown_scope"
            for candidate in scored
        )
    }
    analysis_ids = [
        sid for _kind, sid in operator.subjects if sid in solved_subjects
    ]
    if len(analysis_ids) >= 2:
        report = IdentifiabilityAnalyzer().analyze(operator, aligned, analysis_ids, history)
    elif analysis_ids:
        # A single candidate has no co-toggle structure to analyse: it is
        # identifiable when the solve produced a finite value (and a plug
        # anchor is MEASURED in I5).  Do not feed it to an analyzer that needs
        # at least one candidate with usable rows beyond itself.
        report = _single_subject_report(operator, analysis_ids[0], aligned)
    else:
        report = _empty_report(operator)

    # -- I5: status table, bounds, provenance -------------------------------- #
    points: dict[str, float | None] = {}
    for sid in report.subject_ids:
        column = _op_column(operator, sid)
        if column is not None and column < x_full.size:
            value = _finite(x_full[column])
            points[sid] = value
    model_points = {
        sid: value
        for sid, value in (
            (sid, _finite(getattr(models.predict(sid, PredictionContext(at=at)), "mean_w", None)))
            for sid in report.subject_ids
        )
        if value is not None
    }
    direct = _group_direct_readings(group, operator)
    devices, group_estimates = assemble_status_estimates(
        EstimateInputs(
            report=report,
            operator=operator,
            versions=versions,
            at=at,
            points=points,
            models=model_points,
            method=InferenceMethod.JOINT_DELTA,
            direct=direct,
            reader_quality={
                meta.reader_id: ({} if meta.sigma_w is None else {"quality": 1.0})
                for meta in operator.rows
            },
            events=[
                sub.ref for sub in group.sub_events if _name(sub.kind) == "ha_state"
            ],
        )
    )

    # -- I6: match lifecycle; result.matches is the new snapshot ------------- #
    match_delta: dict[str, MatchState] = {}
    total = sum(abs(value) for value in points.values() if value is not None)
    for candidate in scored:
        sid = candidate.subject_id
        point = points.get(sid)
        share = (abs(point) / total) if total > 0.0 and point is not None else None
        identifiable = report.subjects.get(sid) is IdentifiabilityStatus.IDENTIFIABLE
        match_delta[candidate.candidate_id] = classify_match(
            share=share, identifiable=identifiable
        )

    return JointInferenceResult(
        event_group_id=group.group_id,
        event_group_revision=group.revision,
        status=group.status,
        device_estimates=devices,
        group_estimates=group_estimates,
        unknown_estimates=_unknown_estimates(
            operator, scored, solve_report.unknown_scopes, solution.u
        ),
        candidates=scored,
        matches=_result_matches(matches, match_delta),
        residual_w_by_reader=_residual_map(operator, group, solution),
        versions=versions,
        computed_at=at,
    )


def _group_direct_readings(
    group: EventGroup, operator: ObservationOperator
) -> dict[str, DirectReading]:
    """Fresh single-subject endpoint values from a group's reader deltas.

    A confirmed DIRECT endpoint is its subject's measurement; when the event
    is a level report (e.g. HB §17 step 8's 3 W plug standby) the delta carries
    that level and the device is MEASURED rather than inferred.
    """
    direct: dict[str, DirectReading] = {}
    for sid, (reader_id, _row) in _direct_subjects(operator).items():
        delta = next((rd for rd in group.reader_deltas if rd.reader_id == reader_id), None)
        if delta is None or delta.delta_w is None:
            continue
        direct[sid] = DirectReading(
            subject_id=sid,
            reader_id=reader_id,
            power_w=float(delta.delta_w),
            observed_at=group.opened_at,
        )
    return direct


def _unknown_estimates(
    operator: ObservationOperator,
    candidates: Sequence[Any],
    solved_scopes: Sequence[str],
    u: np.ndarray,
) -> list[UnknownEstimate]:
    scopes: list[str] = list(dict.fromkeys(solved_scopes))
    for candidate in candidates:
        if _name(candidate.subject_kind) == "unknown_scope":
            scopes.append(candidate.subject_id)
    for scope, _rows in operator.unknown_scopes:
        scopes.append(scope)
    result: list[UnknownEstimate] = []
    for scope in dict.fromkeys(scopes):
        index = list(solved_scopes).index(scope) if scope in solved_scopes else -1
        delta = _finite(u[index]) if 0 <= index < np.asarray(u).size else None
        result.append(UnknownEstimate(scope_reader_id=scope, delta_w=delta))
    return result


def _residual_map(
    operator: ObservationOperator,
    group: EventGroup,
    solution: Any,
) -> dict[str, float | None]:
    residuals: dict[str, float | None] = {}
    for index, meta in enumerate(operator.rows):
        residuals[meta.reader_id] = _finite(solution.row_residual_w[index])
    for rd in group.reader_deltas:
        residuals.setdefault(rd.reader_id, None)
    return dict(sorted(residuals.items()))


def _single_subject_report(
    operator: ObservationOperator, subject_id: str, solution: Any
) -> IdentifiabilityReport:
    """I4 report for a single candidate (no co-toggle structure to analyse).

    A finite joint-solve value for the only candidate is a point estimate; a
    missing value stays unknown.  This avoids manufacturing a co-toggle group
    from a one-column experiment matrix.
    """
    column = _op_column(operator, subject_id)
    x = np.asarray(solution.x, dtype=float)
    point = _finite(x[column]) if column is not None and column < x.size else None
    status = (
        IdentifiabilityStatus.IDENTIFIABLE
        if point is not None
        else IdentifiabilityStatus.INSUFFICIENT_EVIDENCE
    )
    return IdentifiabilityReport(
        subject_ids=(subject_id,),
        subjects={subject_id: status},
        groups=(),
        rank=1 if point is not None else 0,
        n_experiments=len(solution.active_rows),
        n_history_events=0,
        condition_number=None,
        singular_values=(),
        null_space_share={subject_id: 0.0 if point is not None else 1.0},
        column_correlation={},
        usable_rows=tuple(solution.active_rows),
        experiment_matrix=np.asarray(operator.H, dtype=float),
    )


def _empty_report(operator: ObservationOperator) -> IdentifiabilityReport:
    return IdentifiabilityReport(
        subject_ids=tuple(sid for _kind, sid in operator.subjects),
        subjects={},
        groups=(),
        rank=0,
        n_experiments=0,
        n_history_events=0,
        condition_number=None,
        singular_values=(),
        null_space_share={},
        column_correlation={},
        usable_rows=tuple(range(len(operator.rows))),
        experiment_matrix=np.asarray(operator.H, dtype=float),
    )


# --------------------------------------------------------------------------- #
# infer_steady — absolute pass constrained by established structure (DEC-22).
# --------------------------------------------------------------------------- #


class SteadyInferenceResult(JointInferenceResult):
    """A steady result carrying DEC-22's conflict surface.

    The normative :class:`JointInferenceResult` has no ``unresolved_events``
    field (that lives on ``InferenceSnapshot``), so this subtype adds it — plus
    the ids marked CONFLICTED — while remaining a ``JointInferenceResult`` for
    every downstream consumer (M2/L4/D4/P6).  Its extra fields participate in
    construction and ``result_id`` derivation, so replay stays deterministic.
    """

    unresolved_events: list[str] = Field(default_factory=list)
    conflicted_subject_ids: list[str] = Field(default_factory=list)


def infer_steady(
    readings: Mapping[str, HeldReading],
    operator: ObservationOperator,
    models: ModelSetView,
    residual: ResidualModelView,
    established: EstablishedStructure,
    versions: VersionTriple,
    clock: Clock,
) -> SteadyInferenceResult:
    """Steady-state absolute solve constrained by established structure.

    Identities contribute soft point constraints and groups contribute one sum
    constraint each; no new attribution is invented.  A current reading that
    disagrees with an established identity by more than
    :data:`CONFLICT_N_SIGMA` standard deviations is flagged CONFLICTED and its
    id is added to ``unresolved_events`` (DEC-22).
    """
    at = clock()
    levels: dict[str, float | None] = {}
    sigmas: dict[str, float | None] = {}
    for meta in operator.rows:
        held = readings.get(meta.reader_id)
        value = held.value if held is not None else None
        usable = held is not None and held.state == "held" and value is not None
        levels[meta.reader_id] = _finite(value) if usable else None
        # Prefer the reader's declared noise sigma.  When absent, a reading of
        # N watts is *not* its own sigma: deriving a positive sigma from the
        # level would fabricate a measurement precision (HB §18).  The solver
        # excludes rows without a usable sigma rather than treating them as
        # certain; the 3σ conflict check below likewise has no sigma to use.
        sigmas[meta.reader_id] = meta.sigma_w if meta.sigma_w is not None else None

    # Established identities/groups -> soft constraint rows on the same columns.
    op_constrained, observed, sigma_map, constraint_rows = _with_established_constraints(
        operator, established, levels, sigmas
    )
    scopes = [scope for scope, _rows in operator.unknown_scopes]
    solve_report = solve_joint_detailed(
        op_constrained,
        observed,
        sigma_map,
        None,
        _model_priors_level(models, operator, at),
        _unknown_priors(residual, scopes, at),
        mode="absolute",
    )
    solution = solve_report.solution
    solved_ids = list(solve_report.subject_ids)
    x_full, cov_full = _align_solution(op_constrained, solved_ids, solution)
    aligned = _partial_solution(solution, x_full, cov_full)

    report = _steady_report(operator, aligned, established)
    points: dict[str, float | None] = {
        sid: _finite(x_full[_op_column(operator, sid)])
        for sid in report.subject_ids
        if _op_column(operator, sid) is not None
    }
    # An established identity pins its subject's published point; it is the
    # CONFIRMED group/identity fact, not the arbitrary steady-state split.  A
    # group member's published point stays None (DEC-19).
    for pair, level in established.identities().items():
        sid = str(pair[1])
        if sid in points:
            points[sid] = _finite(level)

    conflicted = _conflicts(operator, readings, levels, sigmas, established)
    direct = _steady_direct_readings(operator, readings)

    devices, groups = assemble_status_estimates(
        EstimateInputs(
            report=report,
            operator=operator,
            versions=versions,
            at=at,
            points=points,
            models={},
            method=InferenceMethod.JOINT_ABSOLUTE,
            direct=direct,
            reader_quality={
                meta.reader_id: ({} if meta.sigma_w is None else {"quality": 1.0})
                for meta in operator.rows
            },
            ages_s={
                meta.reader_id: max(
                    0.0,
                    (at - readings[meta.reader_id].last_confirmed_at).total_seconds(),
                )
                for meta in operator.rows
                if meta.reader_id in readings
            },
        )
    )

    matches: dict[str, MatchState] = {
        sid: MatchState.CONFLICTED for sid in conflicted
    }
    # ``unresolved_events`` is event-group ids on InferenceSnapshot; the steady
    # pass has no event group, so it reports the conflicting subject ids there
    # (its DEC-22 consumer surface), also mirrored as CONFLICTED in ``matches``.
    # The subtype remains a JointInferenceResult for every downstream consumer.
    return SteadyInferenceResult(
        event_group_id=None,
        event_group_revision=None,
        status=(
            EventGroupStatus.UNRESOLVED if conflicted else EventGroupStatus.CONFIRMED
        ),
        device_estimates=devices,
        group_estimates=groups,
        unknown_estimates=_unknown_estimates(
            operator, (), solve_report.unknown_scopes, solution.u
        ),
        candidates=[],
        matches=dict(sorted(matches.items())),
        residual_w_by_reader=_steady_residual_map(operator, solution, constraint_rows),
        versions=versions,
        computed_at=at,
        unresolved_events=list(conflicted),
        conflicted_subject_ids=list(conflicted),
    )


def _with_established_constraints(
    operator: ObservationOperator,
    established: EstablishedStructure,
    levels: Mapping[str, float | None],
    sigmas: Mapping[str, float | None],
) -> tuple[ObservationOperator, dict[str, float | None], dict[str, float | None], frozenset[int]]:
    """Append soft point/sum rows to the operator, leaving columns unchanged."""
    from wattmosaic_engine.runtime.interfaces import RowMeta

    n_cols = len(operator.subjects)
    extra_H: list[np.ndarray] = []
    extra_values: dict[str, float] = {}
    extra_sigmas: dict[str, float] = {}
    identities = {
        (str(pair[0]), str(pair[1])): value
        for pair, value in established.identities().items()
    }
    for (kind, sid), level in sorted(identities.items(), key=lambda item: item[0][1]):
        column = _op_column(operator, sid) if kind in ("device", "component") else None
        if column is None or level is None:
            continue
        row = np.zeros(n_cols, dtype=float)
        row[column] = 1.0
        reader_id = f"identity::{sid}"
        extra_H.append(row)
        extra_values[reader_id] = float(level)
        extra_sigmas[reader_id] = IDENTITY_CONSTRAINT_SIGMA_W

    memberships = established.groups()
    group_sums = established.group_sums() if hasattr(established, "group_sums") else {}
    for group_id in sorted(memberships):
        member_columns = [
            _op_column(operator, sid) for sid in memberships[group_id]
        ]
        member_columns = [column for column in member_columns if column is not None]
        if len(member_columns) < 2:
            continue
        known = [
            _finite(identities.get(("device", sid)))
            for sid in memberships[group_id]
        ]
        # A group's sum is established only when all its members have identities
        # or the structure supplies the group sum explicitly.  A partial sum
        # would pin an unidentifiable member to a fabricated point (DEC-19).
        group_sum = _finite(group_sums.get(group_id)) if group_id in group_sums else None
        if group_sum is None:
            if any(value is None for value in known):
                continue
            group_sum = float(sum(value for value in known if value is not None))
        row = np.zeros(n_cols, dtype=float)
        for column in member_columns:
            row[column] = 1.0
        reader_id = f"group::{group_id}"
        extra_H.append(row)
        extra_values[reader_id] = float(group_sum)
        extra_sigmas[reader_id] = GROUP_CONSTRAINT_SIGMA_W

    if not extra_H:
        return operator, dict(levels), dict(sigmas), frozenset()

    H = np.vstack([np.asarray(operator.H, dtype=float), *extra_H])
    rows = list(operator.rows) + [
        RowMeta(
            reader_id=reader_id,
            kind="residualised_parent",
            sigma_w=extra_sigmas[reader_id],
            sigma_inflation=1.0,
            excluded_reason=None,
        )
        for reader_id in extra_values
    ]
    entries = dict(operator.entries)
    expanded = ObservationOperator(
        H=H,
        rows=tuple(rows),
        subjects=operator.subjects,
        entries=MappingProxyType(entries),
        unknown_scopes=operator.unknown_scopes,
        soft_alternatives=operator.soft_alternatives,
        topology_version_id=operator.topology_version_id,
        mapping_version_id=operator.mapping_version_id,
        at=operator.at,
    )
    observed = dict(levels)
    observed.update(extra_values)
    sigma_map = dict(sigmas)
    sigma_map.update(extra_sigmas)
    constraint_rows = frozenset(range(len(operator.rows), len(rows)))
    return expanded, observed, sigma_map, constraint_rows


def _steady_report(
    operator: ObservationOperator,
    solution: Any,
    established: EstablishedStructure,
) -> IdentifiabilityReport:
    """Report whose evidence is the established (CONFIRMED) structure."""
    identities = {
        str(pair[1]): _finite(value)
        for pair, value in established.identities().items()
    }
    membership: dict[str, str] = {}
    groups: list[GroupIdentifiability] = []
    subjects: dict[str, IdentifiabilityStatus] = {}
    shares: dict[str, float] = {}
    for _column, (_kind, sid) in enumerate(operator.subjects):
        members = tuple(
            sorted(
                sid2
                for group_id, ids in established.groups().items()
                if sid in (str(i) for i in ids)
                for sid2 in (str(i) for i in ids)
            )
        )
        shares[sid] = 0.0 if sid in identities or sid in membership else 1.0
        if sid in identities:
            subjects[sid] = IdentifiabilityStatus.IDENTIFIABLE
        elif members:
            membership[sid] = _group_id_for(established.groups(), sid)
            subjects[sid] = IdentifiabilityStatus.NON_IDENTIFIABLE
        elif sid in _direct_subjects(operator):
            subjects[sid] = IdentifiabilityStatus.IDENTIFIABLE
        else:
            subjects[sid] = IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE

    for group_id, ids in sorted(established.groups().items()):
        members = tuple(sorted(str(member) for member in ids))
        member_columns = [column for column in (_op_column(operator, m) for m in members) if column is not None]
        if len(member_columns) < 2:
            continue
        # The established sum is the group's *identity*, not the sum of the
        # current solve's arbitrary split: a non-identifiable member's solve
        # value is meaningless, so reusing it would fabricate a group point.
        group_sum = established.group_sums().get(group_id) if hasattr(established, "group_sums") else None
        known_members = [_finite(identities.get(member)) for member in members]
        if group_sum is None and not all(value is not None for value in known_members):
            # A partial sum (e.g. one known member of a two-member group) is not
            # the group's established sum.  Publishing it would pin the other
            # member to a fabricated point (DEC-19).
            continue
        sum_w = _finite(group_sum) if group_sum is not None else None
        if sum_w is None:
            sum_w = float(sum(value for value in known_members if value is not None))
        groups.append(
            GroupIdentifiability(
                group_id=group_id,
                member_ids=members,
                identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
                sum_w=sum_w,
                sigma_w=None,
                source_reader_ids=tuple(
                    meta.reader_id
                    for index, meta in enumerate(operator.rows)
                    if any(
                        index < np.asarray(operator.H).shape[0]
                        and abs(float(np.asarray(operator.H)[index, column])) > _NONZERO_TOL
                        for column in member_columns
                    )
                ),
                evidence_refs=(),
                covers_all_candidates=False,
            )
        )

    return IdentifiabilityReport(
        subject_ids=tuple(sid for _kind, sid in operator.subjects),
        subjects=subjects,
        groups=tuple(groups),
        rank=int(np.linalg.matrix_rank(np.asarray(operator.H, dtype=float))),
        n_experiments=int(np.asarray(operator.H).shape[0]),
        n_history_events=0,
        condition_number=None,
        singular_values=(),
        null_space_share=shares,
        column_correlation={},
        usable_rows=tuple(range(len(operator.rows))),
        experiment_matrix=np.asarray(operator.H, dtype=float),
    )


def _group_id_for(groups: Mapping[str, Any], sid: str) -> str:
    for group_id, members in groups.items():
        if sid in (str(member) for member in members):
            return str(group_id)
    return ""


def _conflicts(
    operator: ObservationOperator,
    readings: Mapping[str, HeldReading],
    levels: Mapping[str, float | None],
    sigmas: Mapping[str, float | None],
    established: EstablishedStructure,
) -> list[str]:
    """Subjects whose current reading disagrees with an identity by > 3σ."""
    direct = _direct_subjects(operator)
    conflicts: list[str] = []
    for pair, level in established.identities().items():
        sid = str(pair[1])
        expected = _finite(level)
        endpoint = direct.get(sid)
        if endpoint is None or expected is None:
            continue
        reader_id, row = endpoint
        observed = levels.get(reader_id)
        sigma = sigmas.get(reader_id)
        if sigma is None and row < len(operator.rows):
            sigma = operator.rows[row].sigma_w
        sigma = _finite(sigma)
        if observed is None or sigma is None or sigma <= 0.0:
            continue
        if abs(observed - expected) > CONFLICT_N_SIGMA * sigma:
            conflicts.append(sid)
    return sorted(set(conflicts))


def _steady_direct_readings(
    operator: ObservationOperator, readings: Mapping[str, HeldReading]
) -> dict[str, DirectReading]:
    direct: dict[str, DirectReading] = {}
    for sid, (reader_id, _row) in _direct_subjects(operator).items():
        held = readings.get(reader_id)
        if held is None or held.value is None or held.state != "held":
            continue
        direct[sid] = DirectReading(
            subject_id=sid,
            reader_id=reader_id,
            power_w=float(held.value),
            observed_at=held.last_confirmed_at,
        )
    return direct


def _steady_residual_map(
    operator: ObservationOperator, solution: Any, constraint_rows: frozenset[int]
) -> dict[str, float | None]:
    residuals: dict[str, float | None] = {}
    for index, meta in enumerate(operator.rows):
        if index in constraint_rows:
            continue
        residuals[meta.reader_id] = _finite(solution.row_residual_w[index])
    return dict(sorted(residuals.items()))
