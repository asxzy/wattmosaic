"""Deterministic constrained least-squares solver and identifiability analysis.

Problem
-------
``min_x  || W (A x - y) ||_2^2  + lambda ||x - x_prior||_2^2``
subject to ``lower <= x <= upper``.

* ``lower`` defaults to ``0`` because a consuming device cannot produce
  negative average power in our sign convention.  Export is a property of the
  *main* CT, not of a device, and is preserved in the reconciliation target.
* ``upper`` is optional and comes from learned device models (a washing machine
  model says 2 500 W max).  It is a soft engineering bound, applied only when a
  validated model provides it.
* ``NNLS`` (Lawson–Hanson, via ``scipy.optimize.nnls``) is used when there are
  no upper bounds and no ridge term; otherwise ``scipy.optimize.lsq_linear``
  (bounded trust-region-reflective).  Both are deterministic.

Both routines are used *without* random initialisation, so repeated calls on
the same data return bit-identical results.

Identifiability
---------------
Take the weighted matrix ``Aw = W A`` and its SVD ``Aw = U S V^T``.  The null
space is spanned by the last ``n - rank`` columns of ``V``.  A linear
functional ``c^T x`` is identifiable from the measurements iff ``c`` is
orthogonal to the null space.  In particular device ``i`` is individually
identifiable iff ``||N[i, :]||_2 ≈ 0``; the squared norm is the *null-space
share* of that device.  Devices that co-vary in a null direction cannot be
separated no matter which solver is used — this is a property of the topology,
not of the algorithm, and is reported instead of being hidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import lsq_linear, nnls

#: Relative singular-value cutoff.  Singular values below
#: ``max(m, n) * eps * s_max`` are numerically zero; ``1e-9`` is added because
#: real CT matrices are small and well scaled in watts.
RANK_RTOL = 1e-9

#: A device is "uniquely identifiable" when its null-space share is below this.
NULL_SHARE_TOL = 1e-8


@dataclass(slots=True)
class Identifiability:
    rank: int
    n_devices: int
    n_rows: int
    condition_number: float
    null_space_dimension: int
    null_space_basis: np.ndarray
    null_share: np.ndarray
    uniquely_identifiable: list[str]
    unidentifiable: list[str]
    ambiguous_groups: list[list[str]]
    singular_values: np.ndarray
    device_ids: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "n_devices": self.n_devices,
            "n_rows": self.n_rows,
            "condition_number": None
            if not np.isfinite(self.condition_number)
            else float(self.condition_number),
            "null_space_dimension": self.null_space_dimension,
            "uniquely_identifiable": list(self.uniquely_identifiable),
            "unidentifiable": list(self.unidentifiable),
            "ambiguous_groups": [list(g) for g in self.ambiguous_groups],
            "null_share": {
                d: float(v) for d, v in zip(self.device_ids, self.null_share, strict=False)
            },
        }


@dataclass(slots=True)
class SolveResult:
    x: np.ndarray
    device_ids: list[str]
    residual: np.ndarray
    residual_norm: float
    weighted_residual_norm: float
    cost: float
    status: int
    method: str
    at_lower_bound: list[str] = field(default_factory=list)
    at_upper_bound: list[str] = field(default_factory=list)
    identifiability: Identifiability | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float | str | list[str]]:
        return {
            "residual_norm": self.residual_norm,
            "weighted_residual_norm": self.weighted_residual_norm,
            "cost": self.cost,
            "method": self.method,
            "at_lower_bound": list(self.at_lower_bound),
            "at_upper_bound": list(self.at_upper_bound),
            "warnings": list(self.warnings),
        }

    def value(self, device_id: str) -> float | None:
        try:
            return float(self.x[self.device_ids.index(device_id)])
        except ValueError:
            return None


def analyse_identifiability(
    A: np.ndarray,
    device_ids: Sequence[str],
    *,
    weights: np.ndarray | Sequence[float] | None = None,
    rank_rtol: float = RANK_RTOL,
    null_share_tol: float = NULL_SHARE_TOL,
    coupling_tol: float = 1e-6,
    n_device_columns: int | None = None,
) -> Identifiability:
    """SVD-based rank / null-space / identifiability analysis.

    ``n_device_columns`` supports *augmented* systems (device columns followed
    by nuisance columns such as the unknown-load slack).  Identifiability is a
    property of the device columns only: a device is identifiable when every
    null-space direction has a zero component on it, i.e. its variation cannot
    be absorbed by the nuisance columns.
    """
    A = np.asarray(A, dtype=float)
    n = A.shape[1]
    ids = list(device_ids)
    if n == 0:
        return Identifiability(
            rank=0,
            n_devices=0,
            n_rows=int(A.shape[0]),
            condition_number=float("inf"),
            null_space_dimension=0,
            null_space_basis=np.zeros((0, 0)),
            null_share=np.zeros(0),
            uniquely_identifiable=[],
            unidentifiable=[],
            ambiguous_groups=[],
            singular_values=np.zeros(0),
            device_ids=[],
        )
    Aw = A
    if weights is not None:
        w = np.asarray(weights, dtype=float).reshape(-1)
        if w.size == A.shape[0]:
            Aw = A * w[:, None]
    # Drop all-zero rows: they carry no information and would break conditioning.
    if Aw.shape[0]:
        keep = np.linalg.norm(Aw, axis=1) > 0
        Aw = Aw[keep]
    if Aw.shape[0] == 0:
        basis = np.eye(n)
        return Identifiability(
            rank=0,
            n_devices=n,
            n_rows=0,
            condition_number=float("inf"),
            null_space_dimension=n,
            null_space_basis=basis,
            null_share=np.ones(n),
            uniquely_identifiable=[],
            unidentifiable=list(ids),
            ambiguous_groups=[list(ids)] if n > 1 else [],
            singular_values=np.zeros(0),
            device_ids=ids,
        )
    U, S, Vt = np.linalg.svd(Aw, full_matrices=True)
    s_max = float(S[0]) if S.size else 0.0
    tol = max(Aw.shape) * np.finfo(float).eps * s_max + rank_rtol * s_max
    rank = int(np.sum(tol < S))
    V = Vt.T
    # ``full_matrices=True`` guarantees V has n columns.
    null_basis = V[:, rank:] if rank < n else np.zeros((n, 0))
    k_dev = n if n_device_columns is None else max(0, min(int(n_device_columns), n))
    if null_basis.shape[1]:
        full_share = np.sum(null_basis**2, axis=1)
        null_share = full_share[:k_dev].copy()
    else:
        null_share = np.zeros(k_dev)
    ids = ids[:k_dev]
    n_dev = k_dev

    uniquely = [d for d, share in zip(ids, null_share, strict=False) if share <= null_share_tol]
    unident = [d for d, share in zip(ids, null_share, strict=False) if share > null_share_tol]

    # Coupling groups: union-find over null-space support.
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for k in range(null_basis.shape[1]):
        support = np.flatnonzero(np.abs(null_basis[:n_dev, k]) > coupling_tol)
        for a, b in zip(support, support[1:], strict=False):
            union(int(a), int(b))
    groups: dict[int, list[str]] = {}
    for i in range(n_dev):
        root = find(i)
        groups.setdefault(root, []).append(ids[i])
    ambiguous = [g for g in groups.values() if len(g) > 1]

    nonzero = S[tol < S]
    cond = float(s_max / nonzero[-1]) if nonzero.size else float("inf")
    return Identifiability(
        rank=rank,
        n_devices=n_dev,
        n_rows=int(Aw.shape[0]),
        condition_number=cond,
        null_space_dimension=int(n - rank),
        null_space_basis=null_basis,
        null_share=null_share,
        uniquely_identifiable=uniquely,
        unidentifiable=unident,
        ambiguous_groups=sorted(ambiguous, key=lambda g: (-len(g), g)),
        singular_values=S,
        device_ids=ids,
    )


def augment_with_unknown_slack(
    A: np.ndarray,
    row_channel_ids: Sequence[str],
    row_kind: Mapping[str, str],
    *,
    shared_rows: Sequence[str] = (),
) -> tuple[np.ndarray, list[str], list[int]]:
    """Append one non-negative "unknown load" column per unmodelled-load row.

    A main CT measures the whole panel total, which includes loads that are not
    modelled as devices.  Without an explicit slack the least-squares fit would
    have to push unknown power onto known devices to satisfy the main row —
    exactly the fabricated attribution the specification forbids.

    ``shared_rows`` extends the same treatment to branch rows the user has
    declared shared: one modelled device on a ``lights`` circuit does not own the
    whole circuit, and a slack keeps the unmodelled remainder out of its value.
    Without this, a single lamp on a 300 W lights circuit would be reported at
    300 W — the most damaging possible error, because it looks measurable.

    Returns ``(A_aug, slack_names, slack_rows)``.
    """
    A = np.asarray(A, dtype=float)
    ordered = list(dict.fromkeys([*shared_rows]))
    slack_rows = [
        i
        for i, cid in enumerate(row_channel_ids)
        if row_kind.get(cid) == "main" or cid in ordered
    ]
    if not slack_rows:
        return A, [], []
    n_dev = A.shape[1]
    aug = np.zeros((A.shape[0], n_dev + len(slack_rows)))
    aug[:, :n_dev] = A
    names: list[str] = []
    for j, row in enumerate(slack_rows):
        aug[row, n_dev + j] = 1.0
        names.append(f"__unknown__:{row_channel_ids[row]}")
    return aug, names, slack_rows


def solve_constrained_nnls(
    A: np.ndarray,
    y: np.ndarray,
    *,
    device_ids: Sequence[str] | None = None,
    weights: np.ndarray | Sequence[float] | None = None,
    lower: float | Sequence[float] = 0.0,
    upper: float | Sequence[float] | None = None,
    prior: Sequence[float] | None = None,
    ridge: float = 0.0,
    analyse: bool = True,
    method: str | None = None,
) -> SolveResult:
    """Solve the bounded, optionally ridge-regularised NNLS problem."""
    A = np.asarray(A, dtype=float)
    y = np.asarray(y, dtype=float)
    if A.ndim != 2:
        raise ValueError("A must be 2-D")
    m, n = A.shape
    if y.shape[0] != m:
        raise ValueError(f"y has {y.shape[0]} rows but A has {m}")
    ids = list(device_ids) if device_ids is not None else [f"x{i}" for i in range(n)]

    w = np.ones(m) if weights is None else np.asarray(weights, dtype=float).reshape(-1)
    if w.size != m:
        raise ValueError("weights must have one value per row")
    # Zero/negative weights mean "ignore this row".
    w = np.where(np.isfinite(w) & (w > 0), w, 0.0)

    lo = np.full(n, float(lower)) if np.isscalar(lower) else np.asarray(lower, dtype=float)
    if upper is None:
        hi = np.full(n, np.inf)
    elif np.isscalar(upper):
        hi = np.full(n, float(upper))
    else:
        hi = np.asarray(upper, dtype=float)
    if lo.size != n or hi.size != n:
        raise ValueError("bounds must have one value per device")
    hi = np.where(np.isfinite(hi) & (hi < lo), lo, hi)

    Aw = A * w[:, None]
    yw = y * w
    warnings: list[str] = []

    ridge = float(max(ridge, 0.0))
    if ridge > 0:
        p = np.zeros(n) if prior is None else np.asarray(prior, dtype=float)
        Aw = np.vstack([Aw, np.sqrt(ridge) * np.eye(n)])
        yw = np.concatenate([yw, np.sqrt(ridge) * p])

    if n == 0:
        return SolveResult(
            x=np.zeros(0),
            device_ids=[],
            residual=y.copy(),
            residual_norm=float(np.linalg.norm(y)),
            weighted_residual_norm=float(np.linalg.norm(yw)),
            cost=0.0,
            status=0,
            method="trivial",
            warnings=["no devices to solve"],
        )
    if m == 0 or not np.any(w > 0) and ridge == 0:
        # Nothing to fit: the best supported answer is the lower bound (0).
        x = np.clip(np.zeros(n), lo, hi)
        res = y - A @ x
        warnings.append("no usable measurement rows")
        ident = (
            analyse_identifiability(A, ids, weights=w) if analyse else None
        )
        return SolveResult(
            x=x,
            device_ids=ids,
            residual=res,
            residual_norm=float(np.linalg.norm(res)),
            weighted_residual_norm=float(np.linalg.norm(w * res)),
            cost=0.0,
            status=0,
            method="unconstrained_no_data",
            identifiability=ident,
            warnings=warnings,
        )

    use_nnls = method == "nnls" or (
        method is None and np.all(lo == 0.0) and np.all(~np.isfinite(hi)) and ridge == 0.0
    )
    if use_nnls:
        # scipy.optimize.nnls requires non-negative m x n and 1-D b.
        x, rnorm = nnls(Aw, yw)
        status = 0
        method_used = "scipy.optimize.nnls"
        cost = float(rnorm**2)
    else:
        res = lsq_linear(
            Aw,
            yw,
            bounds=(lo, hi),
            method="trf",
            tol=1e-12,
            lsq_solver=None,
            verbose=0,
        )
        x = np.asarray(res.x, dtype=float)
        status = int(res.status)
        method_used = "scipy.optimize.lsq_linear(trf)"
        cost = float(res.cost)
        if res.status == 1:
            warnings.append("bounded solver hit the iteration limit")
        elif res.status == 2:
            warnings.append("bounded solver reported an infeasible problem")

    x = np.minimum(np.maximum(x, lo), np.where(np.isfinite(hi), hi, np.inf))
    # Recompute against the *unregularised* original system for reporting.
    residual = y - A @ x
    weighted_res = w * residual

    at_lower = [
        d
        for d, xi, li in zip(ids, x, lo, strict=False)
        if abs(xi - li) <= max(1e-9, 1e-9 * max(1.0, abs(li)))
    ]
    at_upper = [
        d
        for d, xi, ui in zip(ids, x, hi, strict=False)
        if np.isfinite(ui) and abs(xi - ui) <= max(1e-9, 1e-9 * max(1.0, abs(ui)))
    ]
    ident = analyse_identifiability(A, ids, weights=w) if analyse else None
    return SolveResult(
        x=x,
        device_ids=ids,
        residual=residual,
        residual_norm=float(np.linalg.norm(residual)),
        weighted_residual_norm=float(np.linalg.norm(weighted_res)),
        cost=cost,
        status=status,
        method=method_used,
        at_lower_bound=at_lower,
        at_upper_bound=at_upper,
        identifiability=ident,
        warnings=warnings,
    )


def residual_summary(
    A: np.ndarray,
    x: Sequence[float],
    y: Sequence[float],
    row_channel_ids: Sequence[str],
    *,
    weights: np.ndarray | Sequence[float] | None = None,
    noise_sigma: Sequence[float] | None = None,
) -> dict[str, object]:
    """Per-row residual with a documented z-score when sigma is known."""
    pred = np.asarray(A, dtype=float) @ np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    res = y_arr - pred
    out: dict[str, object] = {}
    for i, cid in enumerate(row_channel_ids):
        entry: dict[str, float | bool | None] = {
            "measured_w": float(y_arr[i]),
            "predicted_w": float(pred[i]),
            "residual_w": float(res[i]),
        }
        if noise_sigma is not None and i < len(noise_sigma) and noise_sigma[i] > 0:
            z = float(res[i] / noise_sigma[i])
            entry["z_score"] = z
            entry["outlier"] = bool(abs(z) > 4.0)
        out[cid] = entry
    return out
