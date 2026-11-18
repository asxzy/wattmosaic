"""Reader-noise covariance for nested readers (I3, HB §8.3, design §7).

Nested CTs observe the same physical load.  A load on ``Circuit A`` appears in
the ``Circuit A``, ``Subpanel`` and ``Main`` rows, so those rows are **not**
independent measurements of the same subject.  Treating them as independent
divides the posterior variance by the number of readers, which is the
double-counting gain AT-09 forbids.

Two regimes, per design §7:

* **GLS** when a covariance is estimable: at least
  :data:`MIN_SHARED_EVENTS` historical events were observed by every reader in
  the scope, so the sample covariance of their residuals is used to whiten the
  measurement rows.
* **Conservative weighting** otherwise: ``σ̃_r = σ_r · √k_r`` where ``k_r`` is
  the number of readers that observe at least one of the same subjects.  With
  three nested readers on one load this restores the single-reader variance
  instead of a third of it.

Everything here is pure numpy: no DB, no clock, no globals.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from wattmosaic_engine.runtime.interfaces import ObservationOperator

__all__: list[str] = [
    "MIN_SHARED_EVENTS",
    "COVARIANCE_FLOOR_REL",
    "POSTERIOR_SINGULAR_RTOL",
    "ReaderCovariance",
    "estimate_reader_covariance",
    "conservative_inflation",
    "effective_sigma",
    "gls_whitener",
    "posterior_covariance",
]

#: Fewer shared events than this and the sample covariance is not trusted
#: (design §7: "GLS when Σ is estimable (≥ 30 shared events ...)").
MIN_SHARED_EVENTS = 30

#: Relative eigenvalue floor used when projecting a sample covariance onto the
#: positive-semidefinite cone.  A sample covariance from real data is only
#: approximately PSD; the floor keeps the Cholesky factor well defined without
#: changing any usable direction.
COVARIANCE_FLOOR_REL = 1e-9

#: A normal matrix whose smallest eigenvalue is below this fraction of its
#: largest is treated as singular, so ``JointSolution.cov`` stays ``None``
#: rather than publishing an unidentifiable posterior as if it were finite.
POSTERIOR_SINGULAR_RTOL = 1e-9

#: Smallest sigma accepted anywhere in the pipeline; below this the value is
#: numerically indistinguishable from a zero-variance (hard) constraint.
MIN_SIGMA_W = 1e-6


@dataclass(frozen=True)
class ReaderCovariance:
    """The estimated covariance of reader residuals, in reader order.

    ``matrix`` is ``(len(reader_ids), len(reader_ids))`` in W².  It is only
    built from events observed by *every* listed reader, which is what makes
    the off-diagonal terms comparable.
    """

    reader_ids: tuple[str, ...]
    matrix: np.ndarray
    n_events: int
    method: str = "sample_covariance"

    def as_dict(self) -> dict[str, object]:
        return {
            "reader_ids": list(self.reader_ids),
            "n_events": self.n_events,
            "method": self.method,
            "sigma_w": {
                reader: float(np.sqrt(max(self.matrix[i, i], 0.0)))
                for i, reader in enumerate(self.reader_ids)
            },
        }


def _as_matrix(residuals: np.ndarray | Sequence[Sequence[float]]) -> np.ndarray:
    matrix = np.asarray(residuals, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("residuals must be a 2-D (events x readers) array")
    return matrix


def _project_psd(matrix: np.ndarray, *, floor_rel: float = COVARIANCE_FLOOR_REL) -> np.ndarray:
    """Nearest symmetric PSD matrix by eigenvalue flooring."""
    sym = 0.5 * (matrix + matrix.T)
    eigenvalues, eigenvectors = np.linalg.eigh(sym)
    largest = float(np.max(np.abs(eigenvalues))) if eigenvalues.size else 0.0
    floor = max(largest * floor_rel, MIN_SIGMA_W**2)
    floored = np.maximum(eigenvalues, floor)
    return (eigenvectors * floored) @ eigenvectors.T


def estimate_reader_covariance(
    residuals: np.ndarray | Sequence[Sequence[float]],
    reader_ids: Sequence[str],
    *,
    min_shared_events: int = MIN_SHARED_EVENTS,
    ddof: int = 1,
) -> ReaderCovariance | None:
    """Sample covariance of historical per-reader residuals.

    ``residuals`` is ``(events x readers)``; a ``nan`` entry means that reader
    did not observe that event.  Only rows with every reader present are used
    ("shared events").  Fewer than ``min_shared_events`` shared events yields
    ``None`` — the caller then falls back to conservative weighting, and never
    to an independence assumption.
    """
    matrix = _as_matrix(residuals)
    ids = tuple(reader_ids)
    if matrix.shape[1] != len(ids):
        raise ValueError("residuals has a different column count than reader_ids")
    if not ids:
        return None
    complete = matrix[np.all(np.isfinite(matrix), axis=1)]
    n_events = int(complete.shape[0])
    if n_events < max(int(min_shared_events), 2):
        return None
    if n_events <= ddof:
        return None
    covariance = np.cov(complete, rowvar=False, ddof=ddof)
    covariance = np.atleast_2d(np.asarray(covariance, dtype=float))
    if covariance.shape != (len(ids), len(ids)):
        return None
    projected = _project_psd(covariance)
    return ReaderCovariance(
        reader_ids=ids,
        matrix=projected,
        n_events=n_events,
        method="sample_covariance",
    )


def conservative_inflation(
    operator: ObservationOperator,
    active_rows: Sequence[int] | None = None,
) -> np.ndarray:
    """Per-row ``σ̃/σ`` multiplier: ``√k`` for shared supports.

    ``k_r`` is one plus the number of other active rows that observe at least
    one of the same subjects.  A row that observes nobody (an all-zero H row)
    keeps ``k = 1``: it carries no information anyway.

    ``RowMeta.sigma_inflation`` is honoured when it is larger than the
    geometric value; both are conservative, so the larger one wins.
    """
    H = np.asarray(operator.H, dtype=float)
    support = np.abs(H) > 0.0
    rows = list(range(H.shape[0])) if active_rows is None else list(active_rows)
    inflation = np.ones(H.shape[0], dtype=float)
    for row in rows:
        k = 1
        if support[row].any():
            for other in rows:
                if other != row and np.any(support[row] & support[other]):
                    k += 1
        declared = 1.0
        if row < len(operator.rows):
            value = operator.rows[row].sigma_inflation
            if value is not None and np.isfinite(value) and float(value) > 1.0:
                declared = float(value)
        inflation[row] = max(declared, float(np.sqrt(k)), 1.0)
    return inflation


def effective_sigma(
    operator: ObservationOperator,
    sigma: Mapping[str, float | None],
    active_rows: Sequence[int],
    *,
    inflation: np.ndarray | None = None,
) -> np.ndarray:
    """The conservative per-row sigma used for weighting.

    The caller-supplied ``sigma`` (keyed by reader) wins; ``RowMeta.sigma_w`` is
    the fallback.  A row with no usable sigma gets ``nan`` — absent is not zero,
    and an unweighted row would silently dominate the fit.
    """
    H = np.asarray(operator.H, dtype=float)
    multiplier = conservative_inflation(operator, active_rows) if inflation is None else inflation
    out = np.full(H.shape[0], np.nan, dtype=float)
    for row in active_rows:
        value = sigma.get(operator.rows[row].reader_id)
        if value is None:
            meta_value = operator.rows[row].sigma_w
            value = meta_value
        if value is None:
            continue
        value = float(value)
        if not np.isfinite(value) or value <= 0.0:
            continue
        out[row] = max(value * float(multiplier[row]), MIN_SIGMA_W)
    return out


def gls_whitener(
    covariance: ReaderCovariance | None,
    row_reader_ids: Sequence[str],
) -> np.ndarray | None:
    """``Σ^-1/2`` for the given rows, or ``None`` when it cannot be built.

    Returns ``None`` when the covariance is absent, when a row's reader is not
    part of the estimated covariance, or when the sub-matrix is not positive
    definite.  The caller then keeps the conservative diagonal weighting; it
    never assumes independence.
    """
    if covariance is None:
        return None
    index = {reader: i for i, reader in enumerate(covariance.reader_ids)}
    try:
        rows = [index[reader] for reader in row_reader_ids]
    except KeyError:
        return None
    sub = np.asarray(covariance.matrix, dtype=float)[np.ix_(rows, rows)]
    if sub.shape != (len(rows), len(rows)) or sub.size == 0:
        return None
    try:
        factor = np.linalg.cholesky(_project_psd(sub))
    except np.linalg.LinAlgError:  # pragma: no cover - projection keeps it PD
        return None
    return np.linalg.inv(factor)


def posterior_covariance(
    A: np.ndarray,
    weights: np.ndarray | Sequence[float],
    prior_precision: np.ndarray | Sequence[float] | None = None,
    *,
    singular_rtol: float = POSTERIOR_SINGULAR_RTOL,
) -> np.ndarray | None:
    """``cov = (Aᵀ W A + Λ)^-1`` or ``None`` when the system is singular.

    ``weights`` are the least-squares row weights (``1/σ̃`` times the robust
    factor), so their square is the precision.  Singularity is judged on the
    **data** normal matrix ``AᵀWA`` alone: a model prior ``Λ`` makes the
    posterior finite but does not identify the subject, and model priors are
    never evidence of identifiability (DEC-19).  Returning ``None`` is therefore
    the honest answer for an unidentifiable system; I4 decides what to publish.
    """
    design = np.asarray(A, dtype=float)
    if design.ndim != 2:
        raise ValueError("A must be 2-D")
    n = design.shape[1]
    if n == 0:
        return np.zeros((0, 0), dtype=float)
    w = np.asarray(weights, dtype=float).reshape(-1)
    if w.size != design.shape[0]:
        raise ValueError("weights must have one value per row")
    w = np.where(np.isfinite(w), w, 0.0)
    weighted = design * w[:, None]
    data_normal = weighted.T @ weighted
    if not np.all(np.isfinite(data_normal)):
        return None
    eigenvalues = np.linalg.eigvalsh(data_normal)
    largest = float(np.max(np.abs(eigenvalues))) if eigenvalues.size else 0.0
    if largest <= 0.0:
        return None
    if float(np.min(eigenvalues)) <= max(largest * singular_rtol, 0.0):
        return None
    normal = data_normal
    if prior_precision is not None:
        precision = np.asarray(prior_precision, dtype=float).reshape(-1)
        if precision.size != n:
            raise ValueError("prior_precision must have one value per variable")
        normal = normal + np.diag(np.maximum(precision, 0.0))
    try:
        factor = np.linalg.cholesky(normal)
    except np.linalg.LinAlgError:
        return None
    inverse = np.linalg.inv(factor)
    covariance = inverse.T @ inverse
    return 0.5 * (covariance + covariance.T)
