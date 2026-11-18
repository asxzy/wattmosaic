"""Joint delta and absolute solver (I3, HB §8.1–8.2, design §7).

One convex solve over the whole candidate set instead of a greedy per-candidate
credit (DEC-21).  The objective is

``Σ_r w_r·Huber_δ(r_r/σ̃_r) + λ_m·Σ_i ((ΔP_i − μ_i)/σ_i)²
 + λ_u·Σ_s ((ΔU_s − μ_U)/σ_U)² + λ_t·Σ_i (1 − temporal_fit_i)``

solved by Huber IRLS around :func:`wattmosaic_engine.electrical.solver.solve_constrained_nnls`
(≤ 20 iterations, tolerance 1e-6), with per-variable **signed** bounds:

* an ON transition of a consumer gives ``ΔP ≥ 0``;
* an OFF transition gives ``ΔP ≤ 0`` — the sign is preserved, never clipped;
* producers, batteries and bidirectional loads are unrestricted;
* unknown columns (ΔU) are unrestricted in delta mode.

The module is pure: no DB, no wall clock, no globals.  It consumes the K08
runtime seams (``ObservationOperator``, ``UnknownPrior``, ``JointSolution``) and
the K04 inference contracts, and never imports the test doubles in
``runtime.fakes``.

Covariance handling lives in :mod:`wattmosaic_engine.inference.covariance`:
GLS when a reader covariance is estimable, otherwise conservative ``σ·√k``
inflation from ``RowMeta.sigma_inflation``/shared support, so nested readers
never gain the fake precision of an independence assumption (AT-09).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from wattmosaic_engine.contracts import CandidateHypothesis, DeltaPrior, Observability
from wattmosaic_engine.electrical.solver import (
    HUBER_DELTA,
    IRLS_MAX_ITER,
    IRLS_TOL,
    solve_robust_constrained,
    transition_bounds,
)
from wattmosaic_engine.inference.covariance import (
    ReaderCovariance,
    conservative_inflation,
    effective_sigma,
    gls_whitener,
    posterior_covariance,
)
from wattmosaic_engine.runtime.interfaces import JointSolution, ObservationOperator, UnknownPrior

__all__: list[str] = [
    "DEFAULT_LAMBDA_MODEL",
    "DEFAULT_LAMBDA_TEMPORAL",
    "DEFAULT_LAMBDA_U",
    "DEFAULT_UNKNOWN_SIGMA_W",
    "ANCHOR_WEIGHT",
    "ANCHOR_MIN_SIGMA_W",
    "JointSolveReport",
    "solve_joint",
    "solve_joint_detailed",
    "unknown_share",
]

#: Weight of the model-prior penalty term λ_m (design §7).  ``1.0`` means a
#: prior is a normal observation at its own σ; it is a *soft* penalty, never a
#: bound (DEC-19).
DEFAULT_LAMBDA_MODEL = 1.0

#: Weight of the temporal-fit penalty λ_t (design §7).
DEFAULT_LAMBDA_TEMPORAL = 0.5

#: Fallback unknown-load penalty λ_u when the residual engine supplies none.
#: DEC-23 starts low so "unknown" is cheap; a scope with frequent unexplained
#: activity lowers it further, a quiet scope raises it.
DEFAULT_LAMBDA_U = 0.25

#: Fallback σ_U when an ``UnknownPrior`` is absent.  It is a penalty scale, not
#: a measurement: the unknown column stays available, it is never forced to 0.
DEFAULT_UNKNOWN_SIGMA_W = 200.0

#: A single-subject endpoint (a plug) is an anchor: weight ×10 (design §7).
ANCHOR_WEIGHT = 10.0

#: …and its σ floor is 0.5 W, so it stays a measurement instead of hardening
#: into an unsatisfiable constraint.
ANCHOR_MIN_SIGMA_W = 0.5


@dataclass(frozen=True)
class JointSolveReport:
    """A :class:`JointSolution` plus the diagnostics the callers need.

    ``unknown_share_by_reader`` is the DEC-23 diagnostic: the fraction of a
    reader's observed |ΔY| explained by its ΔU column.  It is ``None`` for a
    reader with no observation (absent is not zero).
    """

    solution: JointSolution
    subject_ids: tuple[str, ...]
    unknown_scopes: tuple[str, ...]
    lambda_u_by_scope: Mapping[str, float]
    unknown_share_by_reader: Mapping[str, float | None]
    sigma: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    iterations: int
    warnings: tuple[str, ...] = ()

    @property
    def converged(self) -> bool:
        return self.solution.converged

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.solution.mode,
            "converged": self.solution.converged,
            "objective": self.solution.objective,
            "iterations": self.iterations,
            "subject_ids": list(self.subject_ids),
            "x": {sid: float(v) for sid, v in zip(self.subject_ids, self.solution.x, strict=False)},
            "unknown_scopes": list(self.unknown_scopes),
            "u": {
                scope: float(v)
                for scope, v in zip(self.unknown_scopes, self.solution.u, strict=False)
            },
            "unknown_share_by_reader": dict(self.unknown_share_by_reader),
            "lambda_u_by_scope": dict(self.lambda_u_by_scope),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Input normalisation.
# --------------------------------------------------------------------------- #


def _row_values(
    values: Mapping[str, float | None] | Sequence[float | None] | np.ndarray | None,
    operator: ObservationOperator,
) -> np.ndarray:
    """Per-row measured values; a missing reader becomes ``nan`` (not 0)."""
    rows = operator.rows
    out = np.full(len(rows), np.nan, dtype=float)
    if values is None:
        return out
    if isinstance(values, Mapping):
        for i, meta in enumerate(rows):
            value = values.get(meta.reader_id)
            if value is not None and np.isfinite(float(value)):
                out[i] = float(value)
        return out
    array = np.asarray(values, dtype=float).reshape(-1)
    if array.size != len(rows):
        raise ValueError("values must have one entry per operator row")
    out[:] = array
    return out


def _row_sigma(
    sigma: Mapping[str, float | None] | Sequence[float | None] | np.ndarray | None,
    operator: ObservationOperator,
) -> dict[str, float | None]:
    """Normalise the caller's sigma into a reader-keyed mapping."""
    if sigma is None:
        return {}
    if isinstance(sigma, Mapping):
        return {reader: (None if value is None else float(value)) for reader, value in sigma.items()}
    array = np.asarray(sigma, dtype=float).reshape(-1)
    if array.size != len(operator.rows):
        raise ValueError("sigma must have one entry per operator row")
    out: dict[str, float | None] = {}
    for value, meta in zip(array, operator.rows, strict=False):
        out[meta.reader_id] = None if not np.isfinite(value) else float(value)
    return out


def _candidate_index(
    candidates: Sequence[CandidateHypothesis] | None,
    operator: ObservationOperator,
) -> tuple[list[int], list[CandidateHypothesis | None]]:
    """Columns of the solve: candidate subjects in operator order.

    A candidate may name a subject by ``subject_id`` (the common case) or by
    the ``(subject_kind, subject_id)`` pair.  Candidates that do not appear in
    the operator have no column and are simply not solved for.
    """
    by_pair: dict[tuple[str, str], CandidateHypothesis] = {}
    by_id: dict[str, CandidateHypothesis] = {}
    for candidate in candidates or ():
        pair = (candidate.subject_kind, candidate.subject_id)
        by_pair[pair] = candidate
        by_id.setdefault(candidate.subject_id, candidate)

    columns: list[int] = []
    chosen: list[CandidateHypothesis | None] = []
    if not candidates:
        columns = list(range(len(operator.subjects)))
        chosen = [None] * len(columns)
        return columns, chosen
    for j, pair in enumerate(operator.subjects):
        candidate = by_pair.get(pair) or by_id.get(pair[1])
        if candidate is None:
            continue
        columns.append(j)
        chosen.append(candidate)
    return columns, chosen


def _prior_for(
    priors: Mapping[str, DeltaPrior | None] | None,
    subject_id: str,
) -> DeltaPrior | None:
    """The subject's ΔP prior, or ``None`` when the model set has none."""
    if not priors:
        return None
    return priors.get(subject_id)


def _sign_class(
    candidate: CandidateHypothesis | None,
    prior: DeltaPrior | None,
    mode: Literal["delta", "absolute"],
) -> str:
    """Resolve the sign class of one variable (design §7).

    In **delta** mode a declared transition fixes the direction of the change:
    ON ⇒ ``pos``, OFF ⇒ ``neg``.  In **absolute** mode the variable is a level,
    so a transition direction says nothing about its sign; the model prior's
    sign class does, and a consumer defaults to ``pos``.
    """
    if mode == "absolute":
        return prior.sign if prior is not None else "pos"
    if candidate is not None and candidate.transition:
        transition = candidate.transition.lower()
        if transition in {"on", "up", "enabled", "activate"}:
            return "pos"
        if transition in {"off", "down", "disabled", "deactivate"}:
            return "neg"
        if transition in {"any", "bidirectional", "unknown"}:
            return "any"
    if prior is not None:
        return prior.sign
    # No declared transition and no prior: a delta can be either direction
    # without evidence, so it stays free.
    return "any"


def _unknown_columns(
    operator: ObservationOperator,
) -> tuple[list[str], np.ndarray]:
    """One ΔU column per unknown scope listed on the operator."""
    scopes: list[str] = []
    columns: list[np.ndarray] = []
    for scope, rows in operator.unknown_scopes:
        column = np.zeros(len(operator.rows), dtype=float)
        for row in rows:
            if 0 <= int(row) < column.size:
                column[int(row)] = 1.0
        scopes.append(scope)
        columns.append(column)
    if not columns:
        return scopes, np.zeros((len(operator.rows), 0), dtype=float)
    return scopes, np.column_stack(columns)


def _unknown_prior_for(
    unknown_priors: Mapping[str, UnknownPrior] | Sequence[UnknownPrior] | None,
    scope: str,
) -> UnknownPrior | None:
    if unknown_priors is None:
        return None
    if isinstance(unknown_priors, Mapping):
        return unknown_priors.get(scope)
    for prior in unknown_priors:
        if prior.scope_reader_id == scope:
            return prior
    return None


def _anchor_rows(operator: ObservationOperator, columns: Sequence[int]) -> np.ndarray:
    """Rows of a single-subject endpoint (a smart plug), in the solve's columns.

    A row qualifies when its support has exactly one candidate variable and
    that entry is a DIRECT observation: a reader whose scope is exactly one
    device.  Parents and residualised branch rows are never anchors.
    """
    H = np.asarray(operator.H, dtype=float)
    anchors = np.zeros(len(operator.rows), dtype=bool)
    for row in range(H.shape[0]):
        support = np.flatnonzero(np.abs(H[row, list(columns)]) > 0.0) if columns else np.zeros(0, int)
        if support.size != 1:
            continue
        original_column = columns[int(support[0])]
        meta = operator.entries.get((row, original_column))
        if meta is None or meta.observability is not Observability.DIRECT:
            continue
        anchors[row] = True
    return anchors


# --------------------------------------------------------------------------- #
# The solve.
# --------------------------------------------------------------------------- #


def solve_joint_detailed(
    operator: ObservationOperator,
    delta_y: Mapping[str, float | None] | Sequence[float | None] | np.ndarray,
    sigma: Mapping[str, float | None] | Sequence[float | None] | np.ndarray | None = None,
    candidates: Sequence[CandidateHypothesis] | None = None,
    priors: Mapping[str, DeltaPrior | None] | None = None,
    unknown_priors: Mapping[str, UnknownPrior] | Sequence[UnknownPrior] | None = None,
    mode: Literal["delta", "absolute"] = "delta",
    *,
    covariance: ReaderCovariance | None = None,
    lambda_model: float = DEFAULT_LAMBDA_MODEL,
    lambda_temporal: float = DEFAULT_LAMBDA_TEMPORAL,
    huber_delta: float = HUBER_DELTA,
    max_iter: int = IRLS_MAX_ITER,
    tol: float = IRLS_TOL,
    prior_ceiling: Mapping[str, float] | None = None,
) -> JointSolveReport:
    """Solve the joint objective and return the solution with diagnostics.

    ``delta_y`` is ΔY in delta mode and the absolute level in absolute mode.
    ``prior_ceiling`` adds a *physical* declared ceiling per subject (a model
    can never supply a floor; DEC-19).  Every absent value is ``None``/``nan``,
    never 0.
    """
    if mode not in ("delta", "absolute"):
        raise ValueError("mode must be 'delta' or 'absolute'")

    H = np.asarray(operator.H, dtype=float)
    if H.ndim != 2:
        raise ValueError("operator.H must be 2-D")
    m = H.shape[0]

    columns, chosen = _candidate_index(candidates, operator)
    y_all = _row_values(delta_y, operator)
    sigma_map = _row_sigma(sigma, operator)

    subject_ids = [operator.subjects[j][1] for j in columns]
    A_subjects = H[:, columns] if columns else np.zeros((m, 0), dtype=float)
    scopes, U = _unknown_columns(operator)
    n_unknown = U.shape[1]
    n_total = A_subjects.shape[1] + n_unknown

    # Active rows: an observed value *and* a usable sigma.  A row without a
    # sigma is excluded and reported; it is never weighted as if certain.
    active = np.zeros(m, dtype=bool)
    for row in range(m):
        if not np.isfinite(y_all[row]):
            continue
        value = sigma_map.get(operator.rows[row].reader_id)
        if value is None:
            value = operator.rows[row].sigma_w
        if value is None or not np.isfinite(value) or value <= 0.0:
            continue
        active[row] = True
    active_rows = tuple(int(i) for i in np.flatnonzero(active))

    warnings: list[str] = []
    if active_rows and len(active_rows) < m:
        warnings.append("inactive_rows_excluded")

    # Conservative sigma (σ·√k from shared support / RowMeta.sigma_inflation).
    sigma_eff = effective_sigma(
        operator,
        sigma_map,
        active_rows,
        inflation=conservative_inflation(operator, active_rows),
    )
    anchors = _anchor_rows(operator, columns)
    row_scale = np.ones(m, dtype=float)
    for row in active_rows:
        value = sigma_eff[row]
        if not np.isfinite(value):
            continue
        if anchors[row]:
            # Weight x10 on the least-squares row (precision x100), with the
            # 0.5 W sigma floor so the anchor stays a measurement.
            row_scale[row] = ANCHOR_WEIGHT
            sigma_eff[row] = max(float(value), ANCHOR_MIN_SIGMA_W)

    # Optional GLS: whiten the measurement rows by Σ^-1/2 when a covariance is
    # estimable.  The whitened design is what the robust loop sees.
    whitened = None
    if covariance is not None and active_rows:
        whitened = gls_whitener(covariance, [operator.rows[row].reader_id for row in active_rows])
        if whitened is None:
            warnings.append("gls_unavailable_fallback_conservative")

    design_rows: list[np.ndarray] = []
    target: list[float] = []
    row_weight_sigma: list[float] = []
    row_weight_scale: list[float] = []
    robust_flags: list[bool] = []
    # The penalty (model/unknown) rows are appended after the measurement
    # block; ``n_measurement_rows`` records where that block ends.
    for index, row in enumerate(active_rows):
        design_row = np.concatenate([A_subjects[row], U[row]])
        if whitened is not None:
            # GLS: whiten with the full row set (the whitener is built from the
            # active rows, in order); σ becomes 1, the row scale keeps anchor
            # and base weights.
            design_row = whitened[index] @ np.vstack(
                [np.concatenate([A_subjects[r], U[r]]) for r in active_rows]
            )
            design_rows.append(design_row)
            target.append(float(whitened[index] @ y_all[list(active_rows)]))
            row_weight_sigma.append(1.0)
            row_weight_scale.append(float(row_scale[row]))
        else:
            design_rows.append(design_row)
            target.append(float(y_all[row]))
            row_weight_sigma.append(float(sigma_eff[row]))
            row_weight_scale.append(float(row_scale[row]))
        # An anchor row is a near-hard constraint: its "outlier" is a real
        # disagreement to be surfaced, never down-weighted away.
        robust_flags.append(not bool(anchors[row]))
    n_measurement_rows = len(design_rows)

    # Model and unknown penalties as extra least-squares rows:
    # λ·((x − μ)/σ)²  ⇔  one row  sqrt(λ)/σ · x = sqrt(λ)·μ/σ.
    subject_prior_precision = np.zeros(A_subjects.shape[1], dtype=float)
    for j, subject_id in enumerate(subject_ids):
        prior = _prior_for(priors, subject_id)
        if prior is None:
            continue
        prior_sigma = float(prior.sigma_w)
        if not np.isfinite(prior_sigma) or prior_sigma <= 0.0:
            warnings.append(f"prior_sigma_missing:{subject_id}")
            continue
        if lambda_model <= 0.0:
            continue
        scale = float(np.sqrt(lambda_model)) / prior_sigma
        row = np.zeros(n_total, dtype=float)
        row[j] = scale
        design_rows.append(row)
        target.append(float(np.sqrt(lambda_model)) * float(prior.mean_w) / prior_sigma)
        row_weight_sigma.append(1.0)
        row_weight_scale.append(1.0)
        robust_flags.append(False)
        subject_prior_precision[j] = float(lambda_model) / prior_sigma**2

    unknown_prior_precision = np.zeros(n_unknown, dtype=float)
    lambda_u_by_scope: dict[str, float] = {}
    for s, scope in enumerate(scopes):
        unknown_prior = _unknown_prior_for(unknown_priors, scope)
        if unknown_prior is None:
            lambda_u = DEFAULT_LAMBDA_U
            mu_u = 0.0
            sigma_u = DEFAULT_UNKNOWN_SIGMA_W
        else:
            lambda_u = float(unknown_prior.lambda_u)
            mu_u = float(unknown_prior.mu_w)
            sigma_u = float(unknown_prior.sigma_w)
            if not np.isfinite(sigma_u) or sigma_u <= 0.0:
                sigma_u = DEFAULT_UNKNOWN_SIGMA_W
                warnings.append(f"unknown_sigma_missing:{scope}")
        lambda_u_by_scope[scope] = lambda_u
        if lambda_u <= 0.0:
            continue
        scale = float(np.sqrt(lambda_u)) / sigma_u
        row = np.zeros(n_total, dtype=float)
        row[A_subjects.shape[1] + s] = scale
        design_rows.append(row)
        target.append(float(np.sqrt(lambda_u)) * mu_u / sigma_u)
        row_weight_sigma.append(1.0)
        row_weight_scale.append(1.0)
        robust_flags.append(False)
        unknown_prior_precision[s] = lambda_u / sigma_u**2

    # Bounds: signed per transition; unknown columns unrestricted in delta mode
    # and non-negative consumption in absolute mode.
    lower = np.empty(n_total, dtype=float)
    upper = np.empty(n_total, dtype=float)
    for j, subject_id in enumerate(subject_ids):
        kind = _sign_class(chosen[j], _prior_for(priors, subject_id), mode)
        ceiling = None
        if prior_ceiling is not None and subject_id in prior_ceiling:
            ceiling = prior_ceiling[subject_id]
        if mode == "absolute":
            if kind == "any":
                lo, hi = transition_bounds("any")
            elif kind == "neg":
                lo, hi = transition_bounds("neg", magnitude=ceiling)
                # A consumer cannot have negative absolute power; a producer
                # declared "neg" keeps its signed range.
            else:
                lo, hi = transition_bounds("pos", magnitude=ceiling)
        else:
            lo, hi = transition_bounds(kind, magnitude=ceiling)
        lower[j] = lo
        upper[j] = hi
    if n_unknown:
        unknown_lower = 0.0 if mode == "absolute" else -np.inf
        lower[A_subjects.shape[1] :] = unknown_lower
        upper[A_subjects.shape[1] :] = np.inf

    if n_total == 0 or not design_rows:
        empty = np.zeros(0, dtype=float) if n_total == 0 else np.clip(np.zeros(n_total), lower, upper)
        x = empty[: A_subjects.shape[1]]
        u = empty[A_subjects.shape[1] :]
        residual = np.full(m, np.nan, dtype=float)
        solution = JointSolution(
            x=np.asarray(x, dtype=float),
            cov=None,
            u=np.asarray(u, dtype=float),
            active_rows=active_rows,
            row_residual_w=residual,
            objective=0.0,
            converged=False,
            mode=mode,
        )
        warnings.append("no_usable_rows")
        return JointSolveReport(
            solution=solution,
            subject_ids=tuple(subject_ids),
            unknown_scopes=tuple(scopes),
            lambda_u_by_scope=lambda_u_by_scope,
            unknown_share_by_reader={meta.reader_id: None for meta in operator.rows},
            sigma=sigma_eff,
            lower=lower,
            upper=upper,
            iterations=0,
            warnings=tuple(warnings),
        )

    A_full = np.vstack(design_rows)
    y_full = np.asarray(target, dtype=float)
    sigma_full = np.asarray(row_weight_sigma, dtype=float)
    weight_full = np.asarray(row_weight_scale, dtype=float)
    robust_mask = np.asarray(robust_flags, dtype=bool)

    result = solve_robust_constrained(
        A_full,
        y_full,
        sigma=sigma_full,
        weights=weight_full,
        lower=lower,
        upper=upper,
        huber_delta=huber_delta,
        max_iter=max_iter,
        tol=tol,
        analyse=False,
        robust_mask=robust_mask,
    )
    warnings.extend(w for w in result.warnings if w not in warnings)

    # Final LS weights (search direction), used to form the posterior precision.
    base_weight = np.where(sigma_full > 0, weight_full / sigma_full, 0.0)
    final_weights = base_weight * np.sqrt(result.robust_weights)
    prior_precision = np.concatenate([subject_prior_precision, unknown_prior_precision])
    # The posterior precision is ``A_measᵀ W A_meas + Λ`` over *measurement*
    # rows only; the penalty rows are exactly Λ and must not be counted twice.
    # Unknown columns stay in the normal matrix, so a ΔU column that can absorb
    # a subject's row correctly makes that subject non-identifiable instead of
    # silently sharpening its variance.  ``JointSolution.cov`` is N x N over
    # ``x`` (the subject variables) and is ``None`` when the system is singular
    # — a model prior never counts as evidence of identifiability (DEC-19).
    n_subjects = A_subjects.shape[1]
    full_cov = posterior_covariance(
        A_full[:n_measurement_rows],
        final_weights[:n_measurement_rows],
        prior_precision,
    )
    cov = None if full_cov is None else full_cov[:n_subjects, :n_subjects]

    x_full = np.asarray(result.x, dtype=float)
    x = x_full[: A_subjects.shape[1]]
    u = x_full[A_subjects.shape[1] :]

    residual = np.full(m, np.nan, dtype=float)
    for row in active_rows:
        residual[row] = float(y_all[row] - (A_subjects[row] @ x) - (U[row] @ u))

    # λ_t term: a candidate's temporal fit, when I2 supplies one, is a constant
    # w.r.t. x, so it shapes the reported objective without moving the solution.
    temporal_penalty = 0.0
    for candidate in chosen:
        if candidate is None:
            continue
        fit = candidate.evidence_terms.get("temporal_fit")
        if fit is None:
            continue
        temporal_penalty += float(lambda_temporal) * (1.0 - min(max(float(fit), 0.0), 1.0))

    solution = JointSolution(
        x=x,
        cov=cov,
        u=u,
        active_rows=active_rows,
        row_residual_w=residual,
        objective=float(result.objective + temporal_penalty),
        converged=bool(result.converged),
        mode=mode,
    )
    return JointSolveReport(
        solution=solution,
        subject_ids=tuple(subject_ids),
        unknown_scopes=tuple(scopes),
        lambda_u_by_scope=lambda_u_by_scope,
        # Rows not listed as an unknown scope report ``None`` (absent, not 0).
        unknown_share_by_reader=unknown_share(operator, solution, delta_y),
        sigma=sigma_eff,
        lower=lower,
        upper=upper,
        iterations=result.iterations,
        warnings=tuple(warnings),
    )


def solve_joint(
    operator: ObservationOperator,
    delta_y: Mapping[str, float | None] | Sequence[float | None] | np.ndarray,
    sigma: Mapping[str, float | None] | Sequence[float | None] | np.ndarray | None = None,
    candidates: Sequence[CandidateHypothesis] | None = None,
    priors: Mapping[str, DeltaPrior | None] | None = None,
    unknown_priors: Mapping[str, UnknownPrior] | Sequence[UnknownPrior] | None = None,
    mode: Literal["delta", "absolute"] = "delta",
    **kwargs: object,
) -> JointSolution:
    """The I3 handoff used by I4/I5/AT-06/08/09 (interfaces.md §6).

    Variables are the candidate subjects plus one ΔU column per unknown scope.
    Bounds are signed per transition (design §7), the loss is Huber/IRLS with at
    most 20 iterations, and the posterior covariance is
    ``(AᵀWA + Λ)^-1`` or ``None`` when the system is singular.

    ``converged=False`` in the returned :class:`JointSolution` is a hard flag:
    a solve that did not converge is never presented as a measurement.
    """
    report = solve_joint_detailed(
        operator,
        delta_y,
        sigma,
        candidates,
        priors,
        unknown_priors,
        mode,
        **kwargs,  # type: ignore[arg-type]
    )
    return report.solution


def unknown_share(
    operator: ObservationOperator,
    solution: JointSolution,
    delta_y: Mapping[str, float | None] | Sequence[float | None] | np.ndarray,
) -> dict[str, float | None]:
    """The DEC-23 diagnostic for an already-solved problem.

    The share of each scope's observed |ΔY| that its ΔU column explains.  A
    reader with no unknown column, no observation, or a zero total observation
    reports ``None`` — absent, never zero.
    """
    observed = _row_values(delta_y, operator)
    shares: dict[str, float | None] = {meta.reader_id: None for meta in operator.rows}
    if solution.u.size == 0:
        return shares
    for s, (scope, rows) in enumerate(operator.unknown_scopes):
        if s >= solution.u.size:
            break
        contribution = 0.0
        total = 0.0
        any_row = False
        for row in rows:
            if not 0 <= int(row) < observed.size or not np.isfinite(observed[int(row)]):
                continue
            any_row = True
            contribution += float(solution.u[s])
            total += abs(float(observed[int(row)]))
        shares[scope] = (abs(contribution) / total) if any_row and total > 0.0 else None
    return shares
