"""Mathematically justified member bounds (I4, HB §8.4, DEC-19).

A member of a non-identifiable group is never given a point value.  It gets an
interval that is *implied* by evidence only:

* the identified sums (each widened by ``n_sigma`` posterior standard
  deviations, or used exactly when no posterior is available);
* the subject's physical sign class (``consumer >= 0`` by default);
* a *declared* ceiling, when the user declared one.

A model prior is never a constraint, a floor or a cap (DEC-19).  This module has
no model parameter at all, so a power model cannot leak into a bound, and there
is deliberately no ``sigma = 0.0`` default anywhere: an absent posterior sigma
is ``None``.

The interval comes from two small linear programs per member (min and max of one
coordinate subject to the constraints above).  Under non-negativity alone a
member of a group with identified sum ``G`` therefore gets ``[0, G]``; a
zero-width interval is only produced for a subject the caller has already
identified (:meth:`Bounds._point`).  For any other subject an exact point would
be a fabricated number, so :class:`ZeroWidthBoundError` is raised instead of
being published.

Null policy (HB §18): an absent bound is ``None``, never ``0``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.optimize import linprog

from wattmosaic_engine.contracts import Bounds, IdentifiabilityStatus

__all__: list[str] = [
    "DEFAULT_N_SIGMA",
    "SignClass",
    "SumConstraint",
    "ZeroWidthBoundError",
    "compute_bounds",
    "group_sum_constraint",
]

#: Posterior standard deviations covered by an identified-sum constraint.
DEFAULT_N_SIGMA = 3.0

#: Which half-line a subject's power may occupy.  ``any`` is for a producer or
#: a bidirectional subject; it is never the default for a consumer.
SignClass = Literal["non_negative", "non_positive", "any"]

#: Absolute tolerance used to decide which constraint produced an endpoint.
_ENDPOINT_TOL_W = 1e-6

_Basis = Literal[
    "non_negativity", "group_sum", "reader_ceiling", "posterior", "declared_ceiling"
]


class ZeroWidthBoundError(ValueError):
    """A non-identified subject cannot have a zero-width interval."""


@dataclass(frozen=True, slots=True)
class SumConstraint:
    """An identified linear combination ``sum(x[member_ids])`` and its interval.

    ``lower_w``/``upper_w`` are already-justified absolute watts (for example
    the solved sum widened by ``3 sigma``).  ``None`` on one side means that
    side is not constrained by this row.
    """

    member_ids: tuple[str, ...]
    lower_w: float | None = None
    upper_w: float | None = None

    def __post_init__(self) -> None:
        if not self.member_ids:
            raise ValueError("a sum constraint needs at least one member id")
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("a sum constraint must not repeat a member id")
        if (
            self.lower_w is not None
            and self.upper_w is not None
            and self.lower_w > self.upper_w
        ):
            raise ValueError("lower_w must not exceed upper_w")


def group_sum_constraint(
    member_ids: Sequence[str],
    sum_w: float,
    sigma_w: float | None,
    *,
    n_sigma: float = DEFAULT_N_SIGMA,
) -> SumConstraint:
    """Widen an identified sum into a constraint interval.

    With no posterior ``sigma_w`` the sum is used exactly: an identified linear
    functional is a point, and inventing a width for it would be as dishonest
    as inventing a value.  Nothing is filled in with ``0`` for a missing sigma.
    """
    ids = tuple(member_ids)
    if sigma_w is None:
        return SumConstraint(ids, float(sum_w), float(sum_w))
    margin = float(n_sigma) * float(sigma_w)
    return SumConstraint(ids, float(sum_w) - margin, float(sum_w) + margin)


def compute_bounds(
    subject_ids: Sequence[str],
    *,
    identified: Mapping[str, float] | None = None,
    identifiability: Mapping[str, IdentifiabilityStatus | str] | None = None,
    sigma: Mapping[str, float | None] | None = None,
    sums: Sequence[SumConstraint] = (),
    signs: Mapping[str, SignClass] | None = None,
    ceilings: Mapping[str, float | None] | None = None,
    n_sigma: float = DEFAULT_N_SIGMA,
) -> dict[str, Bounds]:
    """Justified bounds for every subject in ``subject_ids``.

    ``identified`` holds subjects whose own value is known, and only those get
    a zero-width :class:`Bounds`.  When ``identifiability`` is supplied every
    identified subject must be ``IDENTIFIABLE``; anything else raises
    :class:`ZeroWidthBoundError`, because a point for a non-identified subject
    is exactly the fabrication DEC-19 forbids.

    ``sigma`` holds the posterior standard deviation used to widen a known
    value for the other subjects' LPs.  ``sums`` are identified linear
    combinations.  ``signs`` defaults to ``non_negative`` (household
    consumers); pass an explicit class for a producer or a bidirectional
    subject.  ``ceilings`` are *declared* user ceilings, never model priors.
    """
    ids = list(subject_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("subject_ids must be unique")
    index = {subject_id: position for position, subject_id in enumerate(ids)}
    points = {k: float(v) for k, v in (identified or {}).items() if k in index}
    statuses = dict(identifiability or {})
    for subject_id in points:
        status = statuses.get(subject_id)
        if status is not None and IdentifiabilityStatus(status) is not IdentifiabilityStatus.IDENTIFIABLE:
            raise ZeroWidthBoundError(
                f"{subject_id!r} is {IdentifiabilityStatus(status).value}; a "
                "zero-width bound is only allowed for an IDENTIFIABLE subject"
            )
    sigmas = {k: v for k, v in (sigma or {}).items() if k in index}
    sign_map = dict(signs or {})
    ceiling_map = {
        k: float(v) for k, v in (ceilings or {}).items() if k in index and v is not None
    }

    constraints = list(sums)
    for subject_id, value in sorted(points.items()):
        constraints.append(
            group_sum_constraint(
                (subject_id,), value, sigmas.get(subject_id), n_sigma=n_sigma
            )
        )

    rows, rhs = _inequality_system(ids, constraints, index)
    boxes = _boxes(ids, sign_map, ceiling_map)

    result: dict[str, Bounds] = {}
    for subject_id in ids:
        if subject_id in points:
            result[subject_id] = Bounds._point(points[subject_id], "posterior")
            continue
        column = index[subject_id]
        lower = _extreme(rows, rhs, boxes, column, maximize=False)
        upper = _extreme(rows, rhs, boxes, column, maximize=True)
        if lower is not None and upper is not None:
            if lower > upper:
                lower, upper = upper, lower
            if abs(upper - lower) <= _ENDPOINT_TOL_W:
                raise ZeroWidthBoundError(
                    f"{subject_id!r} is not identified, so its bounds cannot be a "
                    "point interval"
                )
        result[subject_id] = Bounds(
            lower_w=None if lower is None else round(lower, 6),
            upper_w=None if upper is None else round(upper, 6),
            basis=_basis(subject_id, lower, upper, ceiling_map, sign_map),
        )
    return result


def _inequality_system(
    ids: Sequence[str],
    constraints: Sequence[SumConstraint],
    index: Mapping[str, int],
) -> tuple[np.ndarray, np.ndarray]:
    """``A_ub x <= b_ub`` rows for the sum constraints."""
    rows: list[np.ndarray] = []
    rhs: list[float] = []
    for constraint in constraints:
        row = np.zeros(len(ids))
        for member_id in constraint.member_ids:
            if member_id not in index:
                continue
            row[index[member_id]] = 1.0
        if not np.any(row):
            continue
        if constraint.upper_w is not None:
            rows.append(row)
            rhs.append(float(constraint.upper_w))
        if constraint.lower_w is not None:
            rows.append(-row)
            rhs.append(-float(constraint.lower_w))
    if not rows:
        return np.zeros((0, len(ids))), np.zeros(0)
    return np.vstack(rows), np.asarray(rhs, dtype=float)


def _boxes(
    ids: Sequence[str],
    signs: Mapping[str, SignClass],
    ceilings: Mapping[str, float],
) -> list[tuple[float | None, float | None]]:
    boxes: list[tuple[float | None, float | None]] = []
    for subject_id in ids:
        sign = signs.get(subject_id, "non_negative")
        ceiling = ceilings.get(subject_id)
        if sign == "non_positive":
            boxes.append((None, 0.0 if ceiling is None else min(0.0, ceiling)))
        elif sign == "any":
            boxes.append((None, ceiling))
        else:
            boxes.append((0.0, ceiling))
    return boxes


def _extreme(
    rows: np.ndarray,
    rhs: np.ndarray,
    boxes: Sequence[tuple[float | None, float | None]],
    column: int,
    *,
    maximize: bool,
) -> float | None:
    """One LP extremum of ``x[column]``; ``None`` when it is unbounded or the
    system is infeasible (a contradictory sum pair yields no justified bound)."""
    cost = np.zeros(len(boxes))
    cost[column] = -1.0 if maximize else 1.0
    solution = linprog(
        cost,
        A_ub=rows if rows.size else None,
        b_ub=rhs if rows.size else None,
        bounds=boxes,
        method="highs",
    )
    if not solution.success:
        # 3 = unbounded: the honest answer is "no bound on this side".
        return None
    value = float(solution.fun)
    return -value if maximize else value


def _basis(
    subject_id: str,
    lower: float | None,
    upper: float | None,
    ceilings: Mapping[str, float],
    signs: Mapping[str, SignClass],
) -> _Basis:
    """Which constraint justifies the interval (the strongest one that binds).

    ``non_negativity`` is the canonical ``[0, G]`` interval of a group member:
    the sign class supplies the floor and the identified sum the ceiling.
    ``declared_ceiling`` is reported when a declared ceiling caps the subject;
    ``group_sum`` when a sum bound moves an endpoint *inside* the sign class.
    """
    ceiling = ceilings.get(subject_id)
    if ceiling is not None and upper is not None and abs(upper - ceiling) <= _ENDPOINT_TOL_W:
        return "declared_ceiling"
    sign = signs.get(subject_id, "non_negative")
    box_lower = None if sign in {"non_positive", "any"} else 0.0
    if lower is not None and (box_lower is None or lower > box_lower + _ENDPOINT_TOL_W):
        return "group_sum"
    return "non_negativity"
