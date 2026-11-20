"""Explain-away and multi-child step matching for the T5 topology ledger.

The ledger in :mod:`wattmosaic_engine.electrical.topology_inference` compares one
parent step with one child step.  That is wrong in two common situations:

* a parent step is explained by *several* children stepping together (the parent
  moved 100 W, one child 30 W and another 70 W), and
* part of a parent step is already explained by an edge the ledger trusts, so
  only the remainder is available to the unknown children.

This module is the pure counterpart of that rule.  It holds no state, reads no
clock, touches no database and never imports ``runtime.fakes``.  The caller
supplies signed watts and noise sigmas; every function returns verdicts for the
children it was given:

``"support"``
    the parent step is explained by this child (alone or as part of a subset).
``"conflict"``
    the parent did not move (beyond its noise, after explain-away) while this
    child clearly did.
``None``
    neutral -- the parent moved by some other amount, or the child's step sits
    inside the noise.  A neutral step is never evidence either way.

Absence is never zero (HB §18): a reader without a usable delta simply does not
appear in the input, and a non-finite value is skipped rather than counted.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .topology_inference import _SIGMA_FACTOR, MATCH_RELATIVE_TOLERANCE

__all__ = [
    "MAX_SUBSET_CANDIDATES",
    "MAX_SUBSET_SIZE",
    "ChildStep",
    "best_subset",
    "classify_with_explanation",
    "match_tolerance",
    "residual_after_known",
    "verify_known",
]

#: The largest combination of child steps a single parent step may be split into.
#: Three is enough for the observed patterns (a parent, a sub-panel and a plug
#: on a 240 V appliance) and keeps the enumeration cheap.
MAX_SUBSET_SIZE = 3
#: Only the largest steps are worth combining; a parent move is never explained
#: by a crowd of tiny steps, and the enumeration is bounded by this.
MAX_SUBSET_CANDIDATES = 8
#: Two matching subsets whose normalised errors are this close are ambiguous:
#: two equal legs of a 240 V load stepping together must not both claim one
#: parent (design §5 T5).
_AMBIGUITY_SCORE_MARGIN = 0.05


@dataclass(frozen=True, slots=True)
class ChildStep:
    """One child's step inside a parent's window.

    ``delta`` is signed (export power stays negative).  ``sigma`` is the reader's
    noise sigma in watts and must be positive; it is combined in quadrature, so
    an unusable sigma would only widen the tolerance.
    """

    reader_id: str
    delta: float
    sigma: float


def _usable(step: ChildStep) -> bool:
    """A step can only explain a parent move when both numbers are finite."""
    return (
        math.isfinite(step.delta)
        and math.isfinite(step.sigma)
        and step.delta != 0.0
    )


def _score(err: float, tolerance: float) -> float:
    """Normalised error ``err / tol``; a zero-width tolerance is exact-or-nothing."""
    if tolerance > 0.0:
        return err / tolerance
    return 0.0 if err == 0.0 else math.inf


def residual_after_known(
    parent_move: float,
    sigma_p: float,
    known: Sequence[ChildStep],
    *,
    beta: float = 1.0,
) -> tuple[float, float]:
    """Explain away the known children.

    Returns ``(residual, residual_sigma)``: the parent move minus
    ``beta * sum(known deltas)``, and
    ``sqrt(sigma_p**2 + sum((beta * k.sigma)**2))`` -- the noise of that
    remainder, which is what the unknown children are matched against.
    """
    known_sum = 0.0
    variance = float(sigma_p) ** 2
    for step in known:
        known_sum += step.delta
        variance += (beta * step.sigma) ** 2
    return float(parent_move) - beta * known_sum, math.sqrt(max(variance, 0.0))


def match_tolerance(
    total: float,
    sigmas: Sequence[float],
    residual_sigma: float,
    *,
    beta: float = 1.0,
) -> float:
    """How far ``residual`` may sit from this subset's total and still match.

    ``_SIGMA_FACTOR`` times the combined noise (the unexplained parent noise plus
    every member's own noise) plus :data:`MATCH_RELATIVE_TOLERANCE` of the scaled
    total.  The relative term is what lets a large clean step (a 2 kW element read
    a few percent apart on two CTs) match at all.
    """
    variance = float(residual_sigma) ** 2
    for sigma in sigmas:
        variance += (beta * float(sigma)) ** 2
    return _SIGMA_FACTOR * math.sqrt(max(variance, 0.0)) + (
        MATCH_RELATIVE_TOLERANCE * abs(beta * float(total))
    )


def best_subset(
    residual: float,
    residual_sigma: float,
    candidates: Sequence[ChildStep],
    *,
    beta: float = 1.0,
    max_size: int = MAX_SUBSET_SIZE,
) -> tuple[str, ...] | None:
    """The child steps that explain ``residual``, or ``None``.

    Only ``MAX_SUBSET_CANDIDATES`` candidates are considered (largest
    ``abs(delta)`` first, ties by ``reader_id``), then every non-empty subset of
    size ``1..max_size`` is scored as ``(err / tol, len, members)`` and the lowest
    score wins.  Two different subsets of the same size with practically the same
    score are *ambiguous*: the evidence cannot tell them apart (two equal legs of
    one 240 V load stepping together), so nothing is claimed.
    """
    if abs(residual) <= _SIGMA_FACTOR * abs(float(residual_sigma)):
        # The parent did not move beyond its own noise: there is nothing to
        # explain, and a subset would only be fitting that noise.
        return None
    pool = [step for step in candidates if _usable(step)]
    if not pool:
        return None
    pool.sort(key=lambda step: (-abs(step.delta), step.reader_id))
    pool = pool[:MAX_SUBSET_CANDIDATES]
    pool.sort(key=lambda step: step.reader_id)

    best_key: tuple[float, int, tuple[str, ...]] | None = None
    matches: list[tuple[float, int, tuple[str, ...]]] = []
    limit = min(max_size, len(pool))
    for size in range(1, limit + 1):
        for combo in itertools.combinations(pool, size):
            total = sum(step.delta for step in combo)
            tolerance = match_tolerance(
                total, [step.sigma for step in combo], residual_sigma, beta=beta
            )
            err = abs(float(residual) - beta * total)
            if err > tolerance:
                continue
            key = (_score(err, tolerance), size, tuple(step.reader_id for step in combo))
            matches.append(key)
            if best_key is None or key < best_key:
                best_key = key
    if best_key is None:
        return None

    best_score, best_size, best_members = best_key
    for score, size, members in matches:
        if size != best_size or members == best_members:
            continue
        if abs(score - best_score) <= _AMBIGUITY_SCORE_MARGIN:
            return None
    return best_members


def classify_with_explanation(
    parent_move: float,
    sigma_p: float,
    *,
    known: Sequence[ChildStep],
    unknown: Sequence[ChildStep],
    beta: float = 1.0,
) -> dict[str, str | None]:
    """Verdict (``"support"`` / ``"conflict"`` / ``None``) for every unknown child.

    The known children are explained away first.  If the remainder is flat (the
    trusted edges already account for the parent's move), an unknown child that
    clearly stepped without a matching remainder is a conflict for its candidate
    edge; otherwise the unknown children are matched as a subset and its members
    get support.  Everything else stays neutral.
    """
    residual, residual_sigma = residual_after_known(
        parent_move, sigma_p, known, beta=beta
    )
    verdicts: dict[str, str | None] = {step.reader_id: None for step in unknown}
    flat_band = _SIGMA_FACTOR * residual_sigma
    if abs(residual) <= flat_band:
        for step in unknown:
            scaled = beta * step.delta
            if abs(scaled) <= flat_band:
                continue
            tolerance = match_tolerance(
                step.delta, [step.sigma], residual_sigma, beta=beta
            )
            if abs(residual - scaled) > tolerance:
                verdicts[step.reader_id] = "conflict"
        return verdicts

    members = best_subset(residual, residual_sigma, unknown, beta=beta)
    if members is None:
        return verdicts
    for reader_id in members:
        verdicts[reader_id] = "support"
    return verdicts


def _explainable(
    parent_move: float, others: Sequence[ChildStep], *, beta: float = 1.0
) -> list[ChildStep]:
    """The known children this parent move can actually contain.

    Only children stepping the same way as the parent are part of its move, and
    children can never carry more than their parent (KCL).  The others are taken
    largest first while their sum still fits inside the parent's move: an edge
    that would need more than the parent moved cannot be explained away, and
    letting it try would rob every honest sibling of its support.
    """
    if parent_move == 0.0:
        return []
    same_way = [
        step
        for step in others
        if math.isfinite(step.delta)
        and step.delta != 0.0
        and (step.delta > 0.0) == (parent_move > 0.0)
    ]
    same_way.sort(key=lambda step: (-abs(step.delta), step.reader_id))
    allowed = abs(parent_move)
    explained: list[ChildStep] = []
    for step in same_way:
        if abs(beta * step.delta) > allowed:
            break
        allowed -= abs(beta * step.delta)
        explained.append(step)
    return explained


def verify_known(
    parent_move: float,
    sigma_p: float,
    *,
    known: Sequence[ChildStep],
    unknown: Sequence[ChildStep],
    beta: float = 1.0,
) -> dict[str, str | None]:
    """Edge health: does the parent still follow each already-known child?

    For every known child ``k`` the other known children are explained away, and
    ``k`` is then re-examined as if it were still an unknown candidate against
    that remainder.  A confirmed edge whose parent stops following its child
    collects conflict here, which is what later degrades or breaks the edge.
    """
    known_list = list(known)
    verdicts: dict[str, str | None] = {}
    for position, step in enumerate(known_list):
        others = [other for index, other in enumerate(known_list) if index != position]
        residual, residual_sigma = residual_after_known(
            parent_move, sigma_p, _explainable(parent_move, others, beta=beta), beta=beta
        )
        checked = classify_with_explanation(
            residual,
            residual_sigma,
            known=[],
            unknown=[step, *unknown],
            beta=beta,
        )
        verdicts[step.reader_id] = checked.get(step.reader_id)
    return verdicts
