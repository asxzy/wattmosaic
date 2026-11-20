"""Absolute-value capacity and paired (240 V) leg helpers (T5, wave 1).

Two physical facts make absolute readings useful to topology inference, even
though steps are the primary evidence:

* **KCL capacity.** A parent CT observes the sum of everything below it, so at
  every instant ``sum(|child|) <= |parent|`` up to CT accuracy and sampling
  skew (:func:`capacity_violation`).  A set of children that needs more power
  than its parent can carry is not inside that parent, however well the steps
  happened to line up.
* **Paired legs.** A 240 V load is clamped by two CTs, one per hot line.  Their
  absolute values are highly correlated (the same element carries both), and
  each leg sits under a *different* parent leg.  Assigning both legs to one
  parent sends the whole appliance to one hot line
  (:func:`paired_legs`, :func:`assign_parents`).

All functions here are pure: no clock, no I/O, no database.  A ``None`` sample
means "absent" and is skipped, never treated as 0 W.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from itertools import product
from math import inf

__all__: list[str] = [
    "CAPACITY_MAX_VIOLATION",
    "CAPACITY_RELATIVE",
    "CAPACITY_TOLERANCE_W",
    "LEG_MIN_ACTIVE_SAMPLES",
    "LEG_MIN_ACTIVE_W",
    "LEG_MIN_CORRELATION",
    "Series",
    "assign_parents",
    "capacity_violation",
    "fits",
    "paired_legs",
]

#: Watts of slack so CT resolution and sampling skew never break containment.
CAPACITY_TOLERANCE_W = 30.0

#: Relative slack: CT error is a share of the reading, not a fixed wattage.
CAPACITY_RELATIVE = 0.05

#: Share of comparable samples that may violate capacity before a parent no
#: longer fits its children (a few samples are ramp and sampling skew).
CAPACITY_MAX_VIOLATION = 0.02

#: Pearson correlation of absolute values that makes two CTs one 240 V load.
LEG_MIN_CORRELATION = 0.95

#: A leg only says anything about 240 V while the element actually draws.
LEG_MIN_ACTIVE_W = 200.0

#: ... and it must do so often enough for the correlation to mean something.
LEG_MIN_ACTIVE_SAMPLES = 10

#: Above this many candidate combinations the assignment search degrades to a
#: deterministic greedy walk (still pure, still one parent per child).
_ASSIGNMENT_ENUMERATION_LIMIT = 4096

#: ``index i`` is the same instant in every series passed together.
Series = Sequence[float | None]


def _magnitude(value: float | None) -> float | None:
    """``abs`` of a present sample, or ``None`` when it is absent.

    Absent is ``None``, never 0 W: the caller skips the instant rather than
    counting an unknown reading as a zero-load one.
    """
    if value is None or not math.isfinite(value):
        return None
    return abs(value)


def capacity_violation(
    parent: Series,
    children: Sequence[Series],
    *,
    tol_w: float = CAPACITY_TOLERANCE_W,
    rel: float = CAPACITY_RELATIVE,
) -> tuple[float, int]:
    """``(violation_fraction, comparable_samples)`` of ``sum(|child|) <= |parent|``.

    A sample is comparable when the parent and every child are present.  It
    violates when ``sum(abs(c)) > abs(p) + tol_w + rel * abs(p)``.  With no
    comparable sample the result is ``(0.0, 0)``; series of unequal length are
    compared over their common prefix only.
    """
    lengths = [len(parent), *(len(child) for child in children)]
    length = min(lengths) if lengths else 0
    comparable = 0
    violations = 0
    for index in range(length):
        parent_magnitude = _magnitude(parent[index])
        if parent_magnitude is None:
            continue
        total = 0.0
        complete = True
        for child in children:
            magnitude = _magnitude(child[index])
            if magnitude is None:
                complete = False
                break
            total += magnitude
        if not complete:
            continue
        comparable += 1
        limit = parent_magnitude + tol_w + rel * parent_magnitude
        if total > limit:
            violations += 1
    if comparable == 0:
        return (0.0, 0)
    return (violations / comparable, comparable)


def fits(parent: Series, children: Sequence[Series], **kw: float) -> bool:
    """Whether the children can all sit inside ``parent``.

    True when at most :data:`CAPACITY_MAX_VIOLATION` of the comparable samples
    violate capacity -- and true when nothing is comparable at all (no evidence
    is not evidence against).
    """
    violation, _comparable = capacity_violation(parent, children, **kw)
    return violation <= CAPACITY_MAX_VIOLATION


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or ``None`` when either series is constant."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    r = sxy / math.sqrt(sxx * syy)
    if not math.isfinite(r):
        return None
    return max(-1.0, min(1.0, r))


def paired_legs(
    series: Mapping[str, Series],
    *,
    min_corr: float = LEG_MIN_CORRELATION,
    min_active_w: float = LEG_MIN_ACTIVE_W,
    min_active_samples: int = LEG_MIN_ACTIVE_SAMPLES,
) -> list[tuple[str, str, float]]:
    """Readers that look like the two legs of one 240 V load.

    Pairs ``(a, b, r)`` with ``a < b`` whose absolute values correlate with
    Pearson ``r >= min_corr`` over the samples where both are present, provided
    both draw at least ``min_active_w`` together in ``min_active_samples``
    samples and neither absolute series is constant.  A reader appears in at
    most one pair: the highest ``r`` wins first, then the smallest ids.  The
    result is sorted by ``(a, b)``.
    """
    readers = sorted(series)
    candidates: list[tuple[float, str, str]] = []
    for i, a in enumerate(readers):
        sa = series[a]
        for b in readers[i + 1 :]:
            sb = series[b]
            length = min(len(sa), len(sb))
            xs: list[float] = []
            ys: list[float] = []
            active = 0
            for index in range(length):
                x = _magnitude(sa[index])
                y = _magnitude(sb[index])
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
                if x >= min_active_w and y >= min_active_w:
                    active += 1
            if active < min_active_samples:
                continue
            r = _pearson(xs, ys)
            if r is None or r < min_corr:
                continue
            candidates.append((r, a, b))
    # Highest correlation first; the ids make ties deterministic.
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used: set[str] = set()
    chosen: list[tuple[str, str, float]] = []
    for r, a, b in candidates:
        if a in used or b in used:
            continue
        used.update((a, b))
        chosen.append((a, b, r))
    return sorted(chosen)


class _Cost:
    """Lexicographic cost of one candidate assignment (lower is better)."""

    def __init__(
        self,
        series: Mapping[str, Series],
        levels: Mapping[str, float],
        pairs: Sequence[tuple[str, str]],
        fixed: Mapping[str, str],
        score: Mapping[tuple[str, str], float],
        options: Mapping[str, tuple[str, ...]],
        candidate_sets: Mapping[str, frozenset[str]],
    ) -> None:
        self._series = series
        self._levels = levels
        self._pairs = pairs
        self._fixed = fixed
        self._score = score
        self._options = options
        self._candidate_sets = candidate_sets
        self._capacity: dict[tuple[str, tuple[str, ...]], float] = {}

    def _candidate_set(self, child: str) -> frozenset[str]:
        known = self._candidate_sets.get(child)
        if known is not None:
            return known
        if child in self._fixed:
            return frozenset((self._fixed[child],))
        return frozenset()

    def _children_by_parent(self, assigned: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, set[str]] = {}
        for child, parent in assigned.items():
            grouped.setdefault(parent, set()).add(child)
        for child, parent in self._fixed.items():
            if assigned.get(child) != parent:
                grouped.setdefault(parent, set()).add(child)
        return {parent: tuple(sorted(children)) for parent, children in grouped.items()}

    def _capacity_violation(self, parent: str, children: tuple[str, ...]) -> float:
        key = (parent, children)
        if key not in self._capacity:
            parent_series = self._series.get(parent)
            if parent_series is None:
                self._capacity[key] = 0.0
            else:
                present = [self._series[c] for c in children if c in self._series]
                self._capacity[key] = capacity_violation(parent_series, present)[0]
        return self._capacity[key]

    def of(
        self, assigned: Mapping[str, str]
    ) -> tuple[float, float, float, float, tuple[str, ...]]:
        splits = 0
        for left, right in self._pairs:
            parent_left = assigned.get(left)
            parent_right = assigned.get(right)
            if parent_left is None or parent_right is None:
                continue
            if len(self._candidate_set(left) & self._candidate_set(right)) < 2:
                continue
            if parent_left == parent_right:
                splits += 1
        grouped = self._children_by_parent(assigned)
        capacity = sum(
            self._capacity_violation(parent, children) for parent, children in grouped.items()
        )
        evidence = sum(
            self._score.get((parent, child), 0.0) for child, parent in assigned.items()
        )
        depth = sum(self._levels.get(parent, inf) for parent in grouped)
        tie = tuple(assigned[child] for child in sorted(assigned))
        return (float(splits), capacity, -evidence, depth, tie)


def _reduce_nested(
    parents: tuple[str, ...], nested: Callable[[str, str], bool]
) -> tuple[str, ...]:
    """Drop candidates that are a strict ancestor of another candidate."""
    kept = tuple(
        parent
        for parent in parents
        if not any(other != parent and nested(parent, other) for other in parents)
    )
    # Mutually nested candidates (a cycle the caller should never produce):
    # keep the full list rather than losing the child entirely.
    return kept or parents


def assign_parents(
    candidates: Mapping[str, Sequence[str]],
    series: Mapping[str, Series],
    *,
    levels: Mapping[str, float],
    nested: Callable[[str, str], bool],
    pairs: Sequence[tuple[str, str]] = (),
    fixed: Mapping[str, str] | None = None,
    score: Mapping[tuple[str, str], float] | None = None,
) -> dict[str, str]:
    """Choose one parent per child, honouring capacity and paired legs.

    ``candidates[child]`` are the parents that currently qualify.
    ``nested(a, b)`` says ``a`` is an ancestor of ``b``; an ancestor candidate
    is dropped in favour of the more direct one.  ``fixed`` assignments (user
    pins) are included in every capacity check but never changed.  ``score``
    ranks evidence quality per ``(parent, child)`` pair, higher is better.

    The cost of a complete assignment is, in order: paired legs forced onto the
    same parent, summed capacity violation, negative evidence score, summed
    parent levels, then the chosen parent ids as a deterministic tie break.
    """
    fixed_map = dict(fixed or {})
    score_map = dict(score or {})

    options: dict[str, tuple[str, ...]] = {}
    declared: dict[str, frozenset[str]] = {}
    for child in sorted(candidates):
        parents = tuple(sorted({parent for parent in candidates[child]}))
        if parents:
            declared[child] = frozenset(parents)
            options[child] = _reduce_nested(parents, nested)

    assigned: dict[str, str] = {}
    ambiguous: list[str] = []
    for child, parents in options.items():
        if child in fixed_map:
            assigned[child] = fixed_map[child]
        elif len(parents) == 1:
            assigned[child] = parents[0]
        else:
            ambiguous.append(child)

    if not ambiguous:
        return assigned

    cost = _Cost(series, levels, tuple(pairs), fixed_map, score_map, options, declared)
    combinations = math.prod(len(options[child]) for child in ambiguous)
    if combinations <= _ASSIGNMENT_ENUMERATION_LIMIT:
        best_key: tuple[float, float, float, float, tuple[str, ...]] | None = None
        best: dict[str, str] = {}
        for choice in product(*(options[child] for child in ambiguous)):
            trial = dict(assigned)
            trial.update(zip(ambiguous, choice, strict=True))
            key = cost.of(trial)
            if best_key is None or key < best_key:
                best_key = key
                best = trial
        return best

    trial = dict(assigned)
    for child in ambiguous:

        def _rank(parent: str, *, chosen: dict[str, str] = trial, target: str = child):
            return cost.of({**chosen, target: parent})

        trial[child] = min(options[child], key=_rank)
    return trial
