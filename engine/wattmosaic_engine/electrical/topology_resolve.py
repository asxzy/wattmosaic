"""Structural resolution of the T5 ledger from aligned absolute series (design §5 T5).

The evidence ledger (:mod:`.topology_inference`) decides each edge's lifecycle
from step counts alone.  This module adds what the absolute values say about
the *structure*, once per settle:

1. **Duplicate meters** (M4 :func:`~.topology_regression.duplicate_meters`) are
   never parent or child of their twin, and the twin with the larger id is
   ignored as a candidate parent of anything (one meter suffices).
2. **Paired 240 V legs** (M2 :func:`~.topology_capacity.paired_legs`) are split
   across sibling parents, never summed into one.
3. **One parent per child** (M2 :func:`~.topology_capacity.assign_parents`) with
   capacity (KCL) evaluated only while the children draw
   :data:`CAPACITY_ACTIVE_W`, the differenced regression (M4) as extra score.
4. **Structural confirmation** of a PROVISIONAL chosen parent when the
   regression clearly singles it out and capacity fits; every losing STRUCTURAL
   candidate drops to PROVISIONAL.
5. **Sticky decisions.**  The chosen direct parent of each child is returned
   (:class:`StructureResolution` ``decided``) and the caller keeps it across
   settles.  A decided STRUCTURAL parent stays the direct parent until the
   structure *positively* disagrees on adequate data (capacity violation, a
   rival with a higher regression r, or its own r below
   :data:`STRUCTURAL_UNCONFIRM_R`): confirm at r >= 0.3 with a 0.1 margin,
   un-confirm only at margin < 0 or r < 0.2 (hysteresis, no churn).

Duplicate meters and 240 V leg pairs look alike on raw values (a balanced
240 V load's two legs carry the same current).
:meth:`StructureAnalysis.classify` decides ONE class per reader pair from the
ledger: a reader with children is never a 240 V leg, two leaf legs whose
shared candidate parents include two non-nested siblings are a pair, and a
duplicate must also be symmetric in the differenced regression (a parent
dominated by one child has extra independent steps, so the reverse fit
differs and it is not a duplicate).

The O(readers^2 * samples) detectors run vectorised on a decimated copy of the
series and are cached per series object (:class:`StructureAnalysis`), so a
settle only pays for the assignment itself (ADR-0033: no long GIL holds).

Everything here is pure: no clock, no I/O, no database, no scenario names.
``None`` is absent and is always skipped, never treated as 0 W.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import TopologyEdgeState, TopologyEdgeV3

from .topology_capacity import (
    CAPACITY_MAX_VIOLATION,
    LEG_MIN_ACTIVE_SAMPLES,
    LEG_MIN_ACTIVE_W,
    LEG_MIN_CORRELATION,
    assign_parents,
    capacity_violation,
)
from .topology_regression import (
    DUP_MAX_OFFSET_W,
    DUP_MIN_ACTIVE_SAMPLES,
    DUP_MIN_CORRELATION,
    REG_MIN_CHILD_DIFF_W,
    REG_MIN_SAMPLES,
    RegressionFit,
)

__all__ = [
    "CAPACITY_ACTIVE_W",
    "DECIMATE",
    "DECIMATE_S",
    "MAX_DECIMATED_SAMPLES",
    "MIN_STRUCTURE_SAMPLES",
    "STRUCTURAL_MARGIN",
    "STRUCTURAL_MIN_R",
    "STRUCTURAL_SLOPE",
    "STRUCTURAL_UNCONFIRM_R",
    "STRUCTURE_DIFF_SPAN_S",
    "AlignedSeries",
    "Classified",
    "StructureAnalysis",
    "StructureResolution",
    "analyse",
    "resolve",
    "resolve_structure",
    "structure_series",
]

#: Capacity is only meaningful while the children actually draw power.
CAPACITY_ACTIVE_W = 300.0
#: ... for ONE child against one parent a lower bar suffices: a constant 230 W
#: load never reaches 300 W, yet outdraws a 130-280 W sub-panel leg 19 % of the
#: time (live), which no real parent allows (KCL).
CAPACITY_ALONE_ACTIVE_W = 50.0
#: Below this many aligned samples the series say nothing about structure.
MIN_STRUCTURE_SAMPLES = 50
#: Pair/duplicate detection and capacity run on one sample a minute (every
#: n-th sample of the default 10 s grid) ...
DECIMATE_S = 60.0
DECIMATE = 6
#: ... and never on more than this many samples (a week of recorder history).
MAX_DECIMATED_SAMPLES = 2000
#: Grid step assumed for a plain mapping (the live and replay default).
DEFAULT_STEP_S = 10.0
#: Structural confirmation needs a clear differenced-regression winner.
STRUCTURAL_MIN_R = 0.3
STRUCTURAL_SLOPE = (0.3, 1.3)
STRUCTURAL_MARGIN = 0.1
#: Hysteresis: a decided (sticky) STRUCTURAL parent is only un-confirmed when its
#: r drops below this, or a rival's r exceeds its own (margin < 0).
STRUCTURAL_UNCONFIRM_R = 0.2
STRUCTURAL_UNCONFIRM_MARGIN = 0.0
#: Pair completion needs the leg's edge below the ledger's PROVISIONAL conflict
#: bound (``CONFLICT_RATIO_PROVISIONAL`` of the T5 ledger).
PAIR_MAX_CONFLICT_RATIO = 0.10
#: Duplicate meters must be symmetric in the differenced regression.  Absolute
#: r is low live (unsynchronised sampling: a true parent fits at r ~0.55), so
#: symmetry is what is tested: r both ways above a floor and close together,
#: slopes alike, and both readers stepping about equally often.
DUP_SYM_MIN_R = 0.3
DUP_SYM_MAX_R_GAP = 0.15
DUP_SYM_SLOPE_RATIO = (0.8, 1.25)
DUP_SYM_MIN_COUNT_RATIO = 0.75
#: Sparse twins (:meth:`StructureAnalysis.co_moving`): differences over 5 min,
#: both ways at least this r, symmetric like :meth:`~StructureAnalysis.symmetric_twins`.
TWIN_DIFF_SPAN_S = 300.0
TWIN_MIN_R = 0.6
#: Two edges between the same readers (one each way, same lifecycle) whose
#: evidence scores differ by less than this fraction give no direction: the
#: stronger one does not make its source a parent (240 V legs step together).
DIRECTION_TIE_RATIO = 0.25
#: Weight of the regression r in the assignment's evidence score.
_REGRESSION_WEIGHT = 100.0
#: Structural fits difference over this many grid steps (30 s on the 10 s
#: grid).  Readers are sampled unsynchronised (live: 16 s), so a step lands in
#: different 10 s buckets on parent and child; lag-1 differences then split it
#: into two unrelated halves (live: slope ~0.6 for a true feed, and a child
#: fitting a random reader better than its own sub-panel).  A span longer than
#: the sampling interval sees the whole step on both.  On another grid the span
#: is :data:`STRUCTURE_DIFF_SPAN_S` in that grid's steps (30 s grid: 1).
STRUCTURE_DIFF_SPAN = 3
STRUCTURE_DIFF_SPAN_S = 30.0

_CONTAINS = "contains"
_STRUCTURAL = TopologyEdgeState.STRUCTURAL.value
_PROVISIONAL = TopologyEdgeState.PROVISIONAL.value
_DORMANT = frozenset((TopologyEdgeState.REJECTED.value, TopologyEdgeState.RETIRED.value))

Series = Sequence[float | None]


def _value(item: Any) -> Any:
    return getattr(item, "value", item) if item is not None else None


class AlignedSeries(dict):  # type: ignore[type-arg]
    """Aligned series (reader -> values on one grid) that know their grid.

    ``step_s`` is the grid step and ``start_ts`` the POSIX time of index 0 (or
    ``None``).  A plain mapping is taken to be on the 10 s grid.
    """

    def __init__(self, data: Any = (), *, step_s: float = 10.0, start_ts: float | None = None) -> None:
        super().__init__(data)
        self.step_s = float(step_s)
        self.start_ts = start_ts


def grid_step(series: Mapping[str, Series]) -> float:
    """The grid step of ``series`` (:class:`AlignedSeries`), else the 10 s default."""
    step = getattr(series, "step_s", None)
    return float(step) if isinstance(step, (int, float)) and step > 0 else DEFAULT_STEP_S


def _as_array(values: Series) -> np.ndarray:
    """float64 copy; absent / non-finite samples become ``nan``."""
    if isinstance(values, np.ndarray):
        out = values.astype(np.float64, copy=True)
        out[~np.isfinite(out)] = np.nan
        return out
    try:
        out = np.array([np.nan if v is None else v for v in values], dtype=np.float64)
    except (TypeError, ValueError):
        out = np.array(
            [
                float(v)
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                else np.nan
                for v in values
            ],
            dtype=np.float64,
        )
    out[~np.isfinite(out)] = np.nan
    return out


def structure_series(series: Mapping[str, Series] | None) -> Mapping[str, Series] | None:
    """``series`` when it is aligned and long enough to resolve structure, else None."""
    if not series or len(series) < 2:
        return None
    lengths = {len(values) for values in series.values()}
    if len(lengths) != 1 or next(iter(lengths)) < MIN_STRUCTURE_SAMPLES:
        return None
    return series


# --------------------------------------------------------------------------- #
# Cached, vectorised analysis of one aligned series set.
# --------------------------------------------------------------------------- #


@dataclass
class StructureAnalysis:
    """Pairs, duplicates and lazily cached fits for one aligned series set.

    ``source`` is the exact mapping it was computed from; callers reuse an
    analysis only while they still hold that very object.
    """

    source: Mapping[str, Series]
    readers: tuple[str, ...]
    full: Mapping[str, np.ndarray]
    decimated: Mapping[str, list[float | None]]
    pairs: tuple[tuple[str, str, float], ...]
    duplicates: tuple[tuple[str, str, float, float], ...]
    #: Decimated samples where both readers draw >= LEG_MIN_ACTIVE_W, per
    #: detected pair / duplicate (``(a, b)`` with ``a < b``).
    active: Mapping[tuple[str, str], int] = field(default_factory=dict)
    fits: dict[tuple[str, str], RegressionFit | None] = field(default_factory=dict)
    #: Span-differenced fits (:data:`STRUCTURE_DIFF_SPAN`) the decisions use.
    sfits: dict[tuple[str, str], RegressionFit | None] = field(default_factory=dict)
    #: Capacity violations keyed ``(parent, sorted children)``; shared with
    #: :func:`~.topology_capacity.assign_parents` so folds never recompute them.
    capacity: dict[tuple[str, tuple[str, ...]], float] = field(default_factory=dict)
    #: One-child violations at :data:`CAPACITY_ALONE_ACTIVE_W`, keyed ``(parent, child)``.
    alone: dict[tuple[str, str], float] = field(default_factory=dict)
    #: Grid steps of the structural difference (:data:`STRUCTURE_DIFF_SPAN_S`).
    span: int = STRUCTURE_DIFF_SPAN
    #: Grid step of ``full`` in seconds.
    grid_s: float = DEFAULT_STEP_S

    def partner(self, reader: str) -> str | None:
        """The other leg of ``reader``'s 240 V pair, if any."""
        for a, b, _r in self.pairs:
            if a == reader:
                return b
            if b == reader:
                return a
        return None

    def twins(self, reader: str) -> frozenset[str]:
        """Readers that measure the same wire as ``reader``."""
        out: set[str] = set()
        for a, b, _r, _offset in self.duplicates:
            if a == reader:
                out.add(b)
            elif b == reader:
                out.add(a)
        return frozenset(out)

    def fit(self, parent: str, child: str) -> RegressionFit | None:
        key = (parent, child)
        if key not in self.fits:
            p = self.full.get(parent)
            c = self.full.get(child)
            self.fits[key] = None if p is None or c is None else _diff_fit(p, c)
        return self.fits[key]

    def sfit(self, parent: str, child: str) -> RegressionFit | None:
        """The structural fit: differences over :data:`STRUCTURE_DIFF_SPAN` steps.

        :meth:`fit` stays the exact M4 lag-1 ``diff_regression``; every
        structural decision (score, confirmation, hysteresis, duplicates) uses
        this skew-tolerant fit instead.
        """
        key = (parent, child)
        if key not in self.sfits:
            p = self.full.get(parent)
            c = self.full.get(child)
            self.sfits[key] = (
                None if p is None or c is None else _diff_fit(p, c, span=self.span)
            )
        return self.sfits[key]

    def capacity_violation(self, parent: str, children: Sequence[str]) -> float:
        """Violation fraction of ``children`` inside ``parent`` while they draw power."""
        key = (parent, tuple(sorted(children)))
        if key not in self.capacity:
            parent_series = self.decimated.get(parent)
            present = [self.decimated[c] for c in key[1] if c in self.decimated]
            if parent_series is None or not present:
                self.capacity[key] = 0.0
            else:
                self.capacity[key] = capacity_violation(
                    parent_series, present, min_children_w=CAPACITY_ACTIVE_W
                )[0]
        return self.capacity[key]

    def fits_capacity(self, parent: str, children: Sequence[str]) -> bool:
        return self.capacity_violation(parent, children) <= CAPACITY_MAX_VIOLATION

    def fits_alone(self, parent: str, child: str) -> bool:
        """Whether ``child`` alone fits ``parent`` while it draws :data:`CAPACITY_ALONE_ACTIVE_W`."""
        key = (parent, child)
        if key not in self.alone:
            parent_series = self.decimated.get(parent)
            child_series = self.decimated.get(child)
            if parent_series is None or child_series is None:
                self.alone[key] = 0.0
            else:
                self.alone[key] = capacity_violation(
                    parent_series, [child_series], min_children_w=CAPACITY_ALONE_ACTIVE_W
                )[0]
        return self.alone[key] <= CAPACITY_MAX_VIOLATION and self.fits_capacity(parent, [child])

    def symmetric_twins(self, a: str, b: str) -> bool:
        """Whether ``a`` and ``b`` step together both ways (one wire, two meters)."""
        forward = self.sfit(a, b)
        reverse = self.sfit(b, a)
        if forward is None or reverse is None:
            return False
        if min(forward.r, reverse.r) < DUP_SYM_MIN_R or abs(forward.r - reverse.r) > DUP_SYM_MAX_R_GAP:
            return False
        if forward.slope <= 0.0 or reverse.slope <= 0.0:
            return False
        low, high = DUP_SYM_SLOPE_RATIO
        if not low <= forward.slope / reverse.slope <= high:
            return False
        return min(forward.n, reverse.n) >= DUP_SYM_MIN_COUNT_RATIO * max(forward.n, reverse.n)

    def co_moving(self, a: str, b: str) -> bool:
        """Symmetric differenced fits over :data:`TWIN_DIFF_SPAN_S`.

        The twin test for a meter sampled much more sparsely than its twin
        (live: one row every ~2 min against 16 s): over 30 s the sparse meter
        has mostly not reported yet, so the structural fits are weak and
        lopsided (r 0.40 / 0.25) although both meter one wire; over 5 min they
        agree (r 0.85 / 0.85, slopes 0.88 / 0.79).  Only used where the ledger
        gives the two readers no containment direction at all (a parent with one
        dominant child has a STRUCTURAL edge to it).
        """
        p, c = self.full.get(a), self.full.get(b)
        if p is None or c is None:
            return False
        span = max(self.span, round(TWIN_DIFF_SPAN_S / self.grid_s))
        forward = _diff_fit(p, c, span=span)
        reverse = _diff_fit(c, p, span=span)
        if forward is None or reverse is None or forward.slope <= 0.0 or reverse.slope <= 0.0:
            return False
        low, high = DUP_SYM_SLOPE_RATIO
        return (
            min(forward.r, reverse.r) >= TWIN_MIN_R
            and abs(forward.r - reverse.r) <= DUP_SYM_MAX_R_GAP
            and low <= forward.slope / reverse.slope <= high
        )

    def classify(
        self,
        edges: Sequence[TopologyEdgeV3],
        nested: Callable[[str, str], bool] | None = None,
    ) -> Classified:
        """ONE class per reader pair: duplicate meter, 240 V leg pair, or neither.

        * a reader with children (an active CONTAINS edge to a third reader) is
          never a 240 V leg; with a symmetric duplicate it is a duplicate;
        * two leaf legs whose shared candidate parents include two non-nested
          siblings are a 240 V pair (balanced legs look like one wire);
        * a symmetric duplicate whose readers share the same best parent is a
          duplicate; otherwise the raw detection decides (pair first).

        A duplicate must have :data:`DUP_MIN_ACTIVE_SAMPLES` samples where both
        draw power and be symmetric in the differenced regression.
        """
        is_nested = nested or _ancestry(edges)
        children: dict[str, set[str]] = {}
        parents: dict[str, dict[str, tuple[int, float]]] = {}
        for edge in edges:
            if not _is_contains(edge):
                continue
            lifecycle = str(_value(edge.lifecycle))
            if lifecycle in _DORMANT:
                continue
            if lifecycle not in (_STRUCTURAL, _PROVISIONAL):
                # A CANDIDATE is one equal-delta step; every leg collects a
                # reversed one toward its main.  Not evidence of children.
                continue
            parent, child = edge.source_endpoint_id, edge.target_endpoint_id
            children.setdefault(parent, set()).add(child)
            parents.setdefault(child, {})[parent] = (
                1 if lifecycle == _STRUCTURAL else 0,
                float(edge.evidence.support) - 2.0 * float(edge.evidence.conflict),
            )

        def leaf(reader: str, twin: str) -> bool:
            """No child, except ones whose reverse edge is about as strong.

            Equal deltas give an edge both ways (two legs, two meters): that is
            no direction, so it is no child either.  "About as strong" is the
            same lifecycle and a score within :data:`DIRECTION_TIE_RATIO`: the
            two legs of a 240 V load step together, so neither edge collects
            conflict and their counts differ only by a few unpaired steps
            (live: 391/1 vs 384/0 must not make one leg the other's parent).
            ``twin`` counts like any other reader: a parent with one dominant
            child (a STRUCTURAL edge to it, or a reverse edge contradicted by
            the parent's own steps) is not a leaf.
            """
            del twin
            own = parents.get(reader, {})
            for child in children.get(reader, set()):
                reverse = own.get(child)
                forward = parents.get(child, {}).get(reader)
                if reverse is None or forward is None:
                    return False
                if forward[0] != reverse[0]:
                    if forward[0] > reverse[0]:
                        return False
                    continue
                if forward[1] > reverse[1] + DIRECTION_TIE_RATIO * max(abs(reverse[1]), 1.0):
                    return False
            return True

        def candidates(reader: str, twin: str) -> dict[str, tuple[int, float]]:
            return {p: rank for p, rank in parents.get(reader, {}).items() if p != twin}

        def best(reader: str, twin: str) -> str | None:
            ranked = candidates(reader, twin)
            if not ranked:
                return None
            return max(ranked.items(), key=lambda item: (item[1], item[0]))[0]

        def split(a: str, b: str) -> bool:
            shared = sorted(set(candidates(a, b)) & set(candidates(b, a)))
            return any(
                not is_nested(p, q) and not is_nested(q, p)
                for i, p in enumerate(shared)
                for q in shared[i + 1:]
            )

        def undirected(a: str, b: str) -> bool:
            """No STRUCTURAL/PROVISIONAL containment between ``a`` and ``b`` either way."""
            return b not in parents.get(a, {}) and a not in parents.get(b, {})

        raw_pairs = {(a, b) for a, b, _r in self.pairs}
        strict = {
            (a, b)
            for a, b, _r, _offset in self.duplicates
            if self.active.get((a, b), 0) >= DUP_MIN_ACTIVE_SAMPLES
            and (
                self.symmetric_twins(a, b)
                or (undirected(a, b) and self.co_moving(a, b))
            )
        }
        pairs: list[tuple[str, str]] = []
        duplicates: list[tuple[str, str]] = []
        used: set[str] = set()
        for a, b in sorted(raw_pairs | strict):
            is_dup = (a, b) in strict
            kind: str | None
            if not (leaf(a, b) and leaf(b, a)):
                kind = "dup" if is_dup else None
            elif split(a, b):
                kind = "pair"
            elif is_dup and best(a, b) is not None and best(a, b) == best(b, a):
                kind = "dup"
            elif (a, b) in raw_pairs:
                kind = "pair"
            else:
                kind = "dup" if is_dup else None
            if kind == "dup":
                duplicates.append((a, b))
            elif kind == "pair" and a not in used and b not in used:
                used.update((a, b))
                pairs.append((a, b))
        return Classified(pairs=tuple(pairs), duplicates=tuple(duplicates))


@dataclass(frozen=True)
class Classified:
    """Reader pairs classified against the ledger: never both a pair and a twin."""

    pairs: tuple[tuple[str, str], ...] = ()
    duplicates: tuple[tuple[str, str], ...] = ()

    def partner(self, reader: str) -> str | None:
        """The other leg of ``reader``'s 240 V pair, if any."""
        for a, b in self.pairs:
            if a == reader:
                return b
            if b == reader:
                return a
        return None

    def twins(self, reader: str) -> frozenset[str]:
        """Readers that measure the same wire as ``reader``."""
        out = {b for a, b in self.duplicates if a == reader}
        out |= {a for a, b in self.duplicates if b == reader}
        return frozenset(out)


def _diff_fit(parent: np.ndarray, child: np.ndarray, *, span: int = 1) -> RegressionFit | None:
    """Vectorised M4 ``diff_regression`` (same thresholds; ``span=1`` is the same result).

    ``span`` differences over that many samples (``x[i + span] - x[i]``).
    """
    n = min(len(parent), len(child))
    span = max(1, int(span))
    if n <= span:
        return None
    dp = parent[span:n] - parent[: n - span]
    dc = child[span:n] - child[: n - span]
    use = np.isfinite(dp) & np.isfinite(dc) & (np.abs(dc) >= REG_MIN_CHILD_DIFF_W)
    count = int(np.count_nonzero(use))
    if count < REG_MIN_SAMPLES:
        return None
    dps = dp[use]
    dcs = dc[use]
    sum_dcdc = float(np.dot(dcs, dcs))
    if sum_dcdc <= 0.0:
        return None
    slope = float(np.dot(dps, dcs)) / sum_dcdc
    cp = dps - dps.mean()
    cc = dcs - dcs.mean()
    spread_p = float(np.dot(cp, cp))
    spread_c = float(np.dot(cc, cc))
    if spread_p <= 0.0 or spread_c <= 0.0:
        return None
    r = float(np.dot(cp, cc)) / math.sqrt(spread_p * spread_c)
    return RegressionFit(slope=slope, r=r, n=count)


def _pairwise(values: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, ...]:
    """Pairwise-complete moments: n, sums, spreads and cross products (row = a)."""
    x = np.where(mask, values, 0.0)
    m = mask.astype(np.float64)
    n = m @ m.T
    sum_a = x @ m.T
    sum_b = sum_a.T
    sq = x * x
    sum_aa = sq @ m.T
    sum_bb = sum_aa.T
    sum_ab = x @ x.T
    with np.errstate(divide="ignore", invalid="ignore"):
        spread_a = sum_aa - sum_a * sum_a / n
        spread_b = sum_bb - sum_b * sum_b / n
        cross = sum_ab - sum_a * sum_b / n
    # Relative floor: a constant series leaves only rounding noise.
    floor_a = 1e-9 * (sum_aa + 1.0)
    floor_b = 1e-9 * (sum_bb + 1.0)
    return n, spread_a, spread_b, cross, floor_a, floor_b


def _correlations(values: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n, spread_a, spread_b, cross, floor_a, floor_b = _pairwise(values, mask)
    valid = (n >= 2) & (spread_a > floor_a) & (spread_b > floor_b)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(valid, cross / np.sqrt(np.abs(spread_a * spread_b)), np.nan)
        slope = np.where(valid, cross / spread_a, np.nan)
    return n, np.clip(r, -1.0, 1.0), slope


def _both_active(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Samples where both readers draw at least :data:`LEG_MIN_ACTIVE_W`."""
    active = ((np.abs(values) >= LEG_MIN_ACTIVE_W) & mask).astype(np.float64)
    return active @ active.T


def _find_pairs(
    names: Sequence[str], values: np.ndarray, mask: np.ndarray, both_active: np.ndarray | None = None
) -> list[tuple[str, str, float]]:
    """Vectorised M2 ``paired_legs`` on absolute values."""
    magnitude = np.abs(values)
    _n, r, _slope = _correlations(magnitude, mask)
    if both_active is None:
        both_active = _both_active(values, mask)
    candidates: list[tuple[float, str, str]] = []
    count = len(names)
    for i in range(count):
        for j in range(i + 1, count):
            if both_active[i, j] < LEG_MIN_ACTIVE_SAMPLES:
                continue
            rij = r[i, j]
            if not math.isfinite(rij) or rij < LEG_MIN_CORRELATION:
                continue
            candidates.append((float(rij), names[i], names[j]))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used: set[str] = set()
    chosen: list[tuple[str, str, float]] = []
    for rij, a, b in candidates:
        if a in used or b in used:
            continue
        used.update((a, b))
        chosen.append((a, b, rij))
    return sorted(chosen)


def _find_duplicates(
    names: Sequence[str], values: np.ndarray, mask: np.ndarray
) -> list[tuple[str, str, float, float]]:
    """Vectorised M4 ``duplicate_meters`` (raw values, slope and median offset)."""
    n, r, slope = _correlations(values, mask)
    out: list[tuple[str, str, float, float]] = []
    count = len(names)
    for i in range(count):
        for j in range(i + 1, count):
            if n[i, j] < DUP_MIN_ACTIVE_SAMPLES:
                continue
            rij = r[i, j]
            if not math.isfinite(rij) or rij < DUP_MIN_CORRELATION:
                continue
            if not 0.9 <= slope[i, j] <= 1.1:
                continue
            both = mask[i] & mask[j]
            offset = float(np.median(values[j][both] - values[i][both]))
            if abs(offset) > DUP_MAX_OFFSET_W:
                continue
            out.append((names[i], names[j], float(rij), offset))
    return out


def analyse(series: Mapping[str, Series], *, decimate: int | None = None) -> StructureAnalysis:
    """Detect paired legs and duplicate meters once for an aligned series set.

    The grid step comes from the series (:func:`grid_step`): the structural
    difference spans :data:`STRUCTURE_DIFF_SPAN_S` and the detectors run on one
    sample per :data:`DECIMATE_S` whatever the grid (a 30 s week-long memory and
    a 10 s replay give the same fits).
    """
    names = tuple(sorted(series))
    full = {name: _as_array(series[name]) for name in names}
    length = max((len(values) for values in series.values()), default=0)
    grid = grid_step(series)
    span = max(1, round(STRUCTURE_DIFF_SPAN_S / grid))
    if decimate is None:
        decimate = max(1, round(DECIMATE_S / grid))
    step = max(1, int(decimate), math.ceil(length / MAX_DECIMATED_SAMPLES))
    small = {name: full[name][::step] for name in names}
    decimated = {
        name: [None if math.isnan(v) else float(v) for v in small[name].tolist()] for name in names
    }
    if names:
        matrix = np.vstack([small[name] for name in names])
        mask = np.isfinite(matrix)
        values = np.where(mask, matrix, 0.0)
        both_active = _both_active(values, mask)
        pairs = _find_pairs(names, values, mask, both_active)
        duplicates = _find_duplicates(names, values, mask)
        index = {name: i for i, name in enumerate(names)}
        detected = {(a, b) for a, b, _r in pairs} | {(a, b) for a, b, _r, _o in duplicates}
        active = {(a, b): int(both_active[index[a], index[b]]) for a, b in detected}
    else:
        pairs, duplicates, active = [], [], {}
    return StructureAnalysis(
        source=series,
        readers=names,
        full=full,
        decimated=decimated,
        pairs=tuple(pairs),
        duplicates=tuple(duplicates),
        active=active,
        span=span,
        grid_s=grid,
    )


# --------------------------------------------------------------------------- #
# Resolution.
# --------------------------------------------------------------------------- #


def _is_contains(edge: TopologyEdgeV3) -> bool:
    return str(_value(edge.relation)) == _CONTAINS


def _ancestry(edges: Sequence[TopologyEdgeV3]) -> Callable[[str, str], bool]:
    """``nested(a, b)``: ``a`` is a (transitive) confirmed ancestor of ``b``."""
    parents: dict[str, set[str]] = {}
    for edge in edges:
        if _is_contains(edge) and str(_value(edge.lifecycle)) == _STRUCTURAL:
            parents.setdefault(edge.target_endpoint_id, set()).add(edge.source_endpoint_id)
    memo: dict[str, frozenset[str]] = {}

    def ancestors(node: str) -> frozenset[str]:
        if node in memo:
            return memo[node]
        memo[node] = frozenset()  # cycle guard
        seen: set[str] = set()
        stack = list(parents.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(parents.get(current, ()))
        memo[node] = frozenset(seen)
        return memo[node]

    return lambda a, b: a != b and a in ancestors(b)


def _ambiguous_parent(
    info: StructureAnalysis,
    child: str,
    parent: str,
    candidates: Sequence[str],
    nested: Callable[[str, str], bool],
    siblings: Sequence[str],
) -> str | None:
    """A sibling candidate fitting ``child`` indistinguishably from ``parent``.

    A balanced 240 V load has two measured legs (or one x2 leg plus an
    unmonitored one) and steps both mains by the same amount.  Fits to the two
    sibling parents are then within :data:`STRUCTURAL_MARGIN`: which crossing
    is real is not identifiable from steps, so neither can be STRUCTURAL.

    Asymmetric evidence still allows STRUCTURAL: a rival that cannot carry the
    child's siblings (a capacity violation) or a fit clearly worse than the
    chosen parent's discriminates the crossing.  Returns the rival's id when
    ambiguous, else None (spec P5).
    """
    fit = info.sfit(parent, child)
    if fit is None:
        return None
    for rival in candidates:
        if rival == parent or nested(parent, rival) or nested(rival, parent):
            continue
        other = info.sfit(rival, child)
        if other is None or abs(fit.r - other.r) >= STRUCTURAL_MARGIN:
            continue
        # Capacity is asymmetric evidence: if either crossing cannot carry the
        # child's siblings, the structure (not the balanced steps) picks a side.
        if not info.fits_capacity(parent, siblings) or not info.fits_capacity(rival, siblings):
            continue
        return rival
    return None


def _transition(edge: TopologyEdgeV3, new: str, why: str) -> str:
    return f"{edge.edge_id} ({why}): {_value(edge.lifecycle)} -> {new}"


def _balanced_leg(
    info: StructureAnalysis,
    leg: str,
    parent: str,
    rival: str,
) -> bool:
    """A leg fits ``parent`` and a sibling ``rival`` indistinguishably (spec P5).

    A balanced 240 V load steps both mains by the same amount: steps alone say
    nothing about which leg sits under which main.  When the two structural fits
    are within :data:`STRUCTURAL_MARGIN` there is no asymmetric evidence, so
    neither crossing may be made STRUCTURAL.  An asymmetric step (only one main
    moving) makes one fit clearly better and this returns False.
    """
    fit = info.sfit(parent, leg)
    other = info.sfit(rival, leg)
    if fit is None or other is None:
        # No fit means no evidence either way: keep the soft, honest verdict.
        return True
    if abs(fit.r - other.r) >= STRUCTURAL_MARGIN:
        # One main follows the leg clearly better: asymmetric evidence.
        return False
    # Capacity is asymmetric evidence too; a main that cannot carry the leg
    # cannot be its parent however alike the balanced steps look.
    if not info.fits_capacity(parent, [leg]) or not info.fits_capacity(rival, [leg]):
        return False
    # For a measured pair, if one main cannot carry BOTH legs while they run,
    # the split is decided by capacity (not by balanced steps): the crossing is
    # identifiable and the existing 240 V pair rule may confirm it.
    partner = None
    for a, b in _pair_members(info, leg):
        partner = b if a == leg else a
    if partner is not None:
        group = [leg, partner]
        if not info.fits_capacity(parent, group) or not info.fits_capacity(rival, group):
            return False
    return True


def _pair_members(info: StructureAnalysis, reader: str) -> tuple[tuple[str, str], ...]:
    """Raw detected 240 V pairs involving ``reader`` (capacity asymmetry info)."""
    return tuple((a, b) for a, b, _r in info.pairs if reader in (a, b))


@dataclass(frozen=True)
class StructureResolution:
    """Result of :func:`resolve`: new edges, transition strings and decisions.

    ``decided`` maps each child the structure resolved to its direct parent;
    the caller keeps it across settles (sticky structural decisions).
    ``leg_unidentified`` holds children whose parent crossing was kept soft
    because balanced steps fit two sibling parents alike (spec P5).
    """

    edges: list[TopologyEdgeV3]
    transitions: list[str]
    decided: dict[str, str]
    leg_unidentified: frozenset[str] = frozenset()


def resolve_structure(
    edges: Sequence[TopologyEdgeV3],
    series: Mapping[str, Series],
    *,
    levels: Mapping[str, float] | None = None,
    scores: Mapping[tuple[str, str], float] | None = None,
    analysis: StructureAnalysis | None = None,
) -> tuple[list[TopologyEdgeV3], list[str]]:
    """One direct parent per child (stateless form of :func:`resolve`)."""
    result = resolve(edges, series, levels=levels, scores=scores, analysis=analysis)
    return result.edges, result.transitions


def resolve(
    edges: Sequence[TopologyEdgeV3],
    series: Mapping[str, Series],
    *,
    levels: Mapping[str, float] | None = None,
    scores: Mapping[tuple[str, str], float] | None = None,
    analysis: StructureAnalysis | None = None,
    decided: Mapping[str, str] | None = None,
    sticky: Collection[tuple[str, str]] = (),
    previous: Mapping[str, str] | None = None,
    excluded: Collection[tuple[str, str]] = (),
    allow_confirm: Callable[[TopologyEdgeV3, Sequence[TopologyEdgeV3]], bool] | None = None,
    declared_pairs: frozenset[frozenset[str]] = frozenset(),
    leg_unidentified: frozenset[str] = frozenset(),
    pairs_analysis: Any | None = None,
) -> StructureResolution:
    """One direct parent per child from evidence, capacity, legs and regression.

    Returns the new edge list (same order, lifecycles updated), one transition
    string per changed edge and the decided parent per child.  User-declared
    and pinned edges are never changed; they count as ``fixed`` in every
    capacity check.  ``scores`` adds to the evidence score per
    ``(parent, child)``.

    ``decided`` are the previous settles' decisions: a decided parent whose
    edge is STRUCTURAL stays the direct parent unless the structure positively
    disagrees (:func:`_disagreement`).  ``sticky`` are the decided edges that
    are STRUCTURAL *only* because of that decision; on disagreement they drop
    to PROVISIONAL.  ``previous`` (edge id -> lifecycle before this settle)
    limits structural confirmation to edges that were already PROVISIONAL:
    never CANDIDATE -> STRUCTURAL in one settle.  ``excluded`` pairs (BROKEN
    health) are no candidates at all.  ``allow_confirm`` is an extra gate for
    structural confirmation (the ledger's direction cap).  ``declared_pairs``
    are user-declared PAIRED_LEG/SAME_CONDUCTOR reader sets (spec P4): a
    CONTAINS edge between them is never a structural candidate and the pair
    rule applies from the first settle.  ``leg_unidentified`` are children with
    indistinguishable candidate parents (E3 §2): their crossings may never be
    made STRUCTURAL by this pass.
    """
    out = list(edges)
    info = analysis if analysis is not None and analysis.source is series else analyse(series)
    level_map = dict(levels or {})
    extra = dict(scores or {})
    nested = _ancestry(out)
    decided_in = dict(decided or {})
    sticky_set = set(sticky)
    excluded_set = set(excluded)

    classified = info.classify(out, nested)
    ignored_parents = {b for _a, b in classified.duplicates}
    twin_pairs = {frozenset(pair) for pair in classified.duplicates}
    pair_of = {a: b for a, b in classified.pairs} | {b: a for a, b in classified.pairs}
    # Declared pairs (P4) are authoritative from the first settle, before the
    # data-driven classifier has enough evidence to see the pair.
    for pair in declared_pairs:
        members = tuple(sorted(pair))
        if len(members) == 2:
            a, b = members
            pair_of.setdefault(a, b)
            pair_of.setdefault(b, a)
    if declared_pairs:
        pairs = sorted({tuple(sorted(p)) for p in (*classified.pairs, *declared_pairs)
                        if len(p) == 2})
    else:
        pairs = list(classified.pairs)

    transitions: list[str] = []

    def lifecycle_of(index: int) -> str:
        return str(_value(out[index].lifecycle))

    def set_lifecycle(index: int, new: str, why: str) -> None:
        edge = out[index]
        if str(_value(edge.lifecycle)) == new:
            return
        transitions.append(_transition(edge, new, why))
        out[index] = edge.model_copy(update={"lifecycle": TopologyEdgeState(new)})

    fixed: dict[str, str] = {}
    inferred: dict[str, list[int]] = {}
    for index, edge in enumerate(out):
        if not _is_contains(edge):
            continue
        lifecycle = str(_value(edge.lifecycle))
        if lifecycle in _DORMANT:
            continue
        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        if edge.pinned or edge.declared_by != "inference":
            if lifecycle == _STRUCTURAL:
                fixed.setdefault(child, parent)
            continue
        if frozenset((parent, child)) in twin_pairs:
            # One wire measured twice is never parent and child.
            if lifecycle == _STRUCTURAL:
                set_lifecycle(index, _PROVISIONAL, "duplicate meter")
            continue
        if pair_of.get(parent) == child:
            # The two legs of one 240 V load are never parent and child.
            if lifecycle == _STRUCTURAL:
                set_lifecycle(index, _PROVISIONAL, "240 V leg pair")
            continue
        if (parent, child) in excluded_set:
            # A BROKEN edge never competes: it must not win and demote its rival.
            continue
        if lifecycle in (_STRUCTURAL, _PROVISIONAL):
            inferred.setdefault(child, []).append(index)

    def score_of(index: int) -> float:
        edge = out[index]
        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        evidence = edge.evidence
        value = float(evidence.support) - 2.0 * float(evidence.conflict)
        fit = info.sfit(parent, child)
        if fit is not None:
            value += _REGRESSION_WEIGHT * fit.r
        return value + float(extra.get((parent, child), 0.0))

    def edge_index(parent: str, child: str) -> int | None:
        return next(
            (i for i in inferred.get(child, ()) if out[i].source_endpoint_id == parent), None
        )

    # A 240 V pair whose legs were both decided under ONE parent is split when
    # that parent cannot carry both legs while they run (capacity while on):
    # the leg with the weaker evidence there is released and re-assigned; the
    # other leg's decision stands.  Each leg alone fits its parent, so the
    # per-child check below never sees this.
    released: set[str] = set()
    for a, b in classified.pairs:
        p = decided_in.get(a)
        if p is None or p != decided_in.get(b) or a in fixed or b in fixed:
            continue
        a_index, b_index = edge_index(p, a), edge_index(p, b)
        if a_index is None or b_index is None:
            continue
        if lifecycle_of(a_index) != _STRUCTURAL or lifecycle_of(b_index) != _STRUCTURAL:
            continue
        if info.fits_capacity(p, [a, b]):
            continue
        weaker, weaker_index = min(
            ((a, a_index), (b, b_index)), key=lambda item: (score_of(item[1]), item[0])
        )
        released.add(weaker)
        if (p, weaker) in sticky_set:
            set_lifecycle(weaker_index, _PROVISIONAL, "structure disagrees: 240 V pair capacity")

    # Sticky decisions: kept unless the structure positively disagrees.
    held: dict[str, str] = {}
    for child, indices in sorted(inferred.items()):
        kept = decided_in.get(child)
        if child in fixed or kept is None or kept in ignored_parents or child in released:
            continue
        kept_index = next((i for i in indices if out[i].source_endpoint_id == kept), None)
        if kept_index is None or lifecycle_of(kept_index) != _STRUCTURAL:
            continue
        rivals = [
            out[i].source_endpoint_id
            for i in indices
            if i != kept_index and out[i].source_endpoint_id not in ignored_parents
        ]
        below = next(
            (
                out[i].source_endpoint_id
                for i in indices
                if i != kept_index
                and lifecycle_of(i) == _STRUCTURAL
                and nested(kept, out[i].source_endpoint_id)
            ),
            None,
        )
        # A decided parent that is now a confirmed ancestor of another confirmed
        # candidate is a grandparent: the lower one is direct (transitive feed).
        reason = (
            f"{below} is below it"
            if below is not None
            else _disagreement(info, kept, child, rivals, paired=child in pair_of)
        )
        if reason is None:
            held[child] = kept
        elif (kept, child) in sticky_set:
            set_lifecycle(kept_index, _PROVISIONAL, f"structure disagrees: {reason}")

    def acyclic(parent: str, child: str) -> bool:
        """No structural confirmation that would close a containment cycle."""
        if nested(child, parent):
            return False
        return not any(
            _is_contains(edge)
            and edge.source_endpoint_id == child
            and edge.target_endpoint_id == parent
            and str(_value(edge.lifecycle)) not in _DORMANT
            and (
                str(_value(edge.lifecycle)) == _STRUCTURAL
                or edge.pinned
                or edge.declared_by != "inference"
            )
            for edge in out
        )

    active_links = {
        (edge.source_endpoint_id, edge.target_endpoint_id)
        for edge in out
        if _is_contains(edge) and str(_value(edge.lifecycle)) in (_STRUCTURAL, _PROVISIONAL)
    }

    def confirmable(index: int, parents: Sequence[str]) -> bool:
        """A PROVISIONAL edge structural confirmation may promote in this settle."""
        edge = out[index]
        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        before = (previous or {}).get(edge.edge_id, str(_value(edge.lifecycle)))
        return (
            lifecycle_of(index) == _PROVISIONAL
            and str(_value(before)) == _PROVISIONAL
            and child not in leg_unidentified
            and acyclic(parent, child)
            and _structural_ok(info, parent, child, parents, [child])
            and (allow_confirm is None or allow_confirm(edge, out))
        )

    def lower_level(index: int, confirmed: Sequence[int], parents: Sequence[str]) -> bool:
        """A PROVISIONAL parent below a STRUCTURAL one that the structure singles out.

        A grandparent feed sees every step of its grandchildren, so its edge
        reaches STRUCTURAL on step counts while the direct parent's edge (the
        busier sub-panel) may stay PROVISIONAL.  Left out of the assignment,
        the child would sit under the grandparent and be counted twice in its
        capacity (once itself, once inside the sub-panel), which then refuses
        the sub-panel's own confirmation (live: a crossed sub-panel never
        confirmed).  The provisional parent competes when a confirmed candidate
        has an active edge to it and the regression singles it out
        (:func:`_structural_ok` against every candidate).
        """
        parent = out[index].source_endpoint_id
        return any(
            (out[j].source_endpoint_id, parent) in active_links for j in confirmed
        ) and confirmable(index, parents)

    candidates: dict[str, list[str]] = {}
    index_of: dict[tuple[str, str], int] = {}
    soft_lower: set[tuple[str, str]] = set()
    for child, indices in sorted(inferred.items()):
        if child in fixed:
            # A user-declared parent is a fact; inference leaves the child alone.
            continue
        usable = [i for i in indices if out[i].source_endpoint_id not in ignored_parents]
        # KCL: a parent the child alone outdraws while it runs is no parent at
        # all.  Kept as a candidate it is still assigned when it is the child's
        # only one, and its capacity then refuses every real child of that
        # parent (live: a constant 300 W load on a 280 W sub-panel leg blocked
        # the leg's own circuits).
        over = [i for i in usable if not info.fits_alone(out[i].source_endpoint_id, child)]
        for i in over:
            if lifecycle_of(i) == _STRUCTURAL:
                set_lifecycle(i, _PROVISIONAL, "structure disagrees: capacity")
        usable = [i for i in usable if i not in over]
        if not usable:
            continue
        confirmed = [i for i in usable if lifecycle_of(i) == _STRUCTURAL]
        provisional = [i for i in usable if lifecycle_of(i) == _PROVISIONAL]
        if child in pair_of:
            chosen = confirmed + provisional
        elif confirmed:
            parents = [out[j].source_endpoint_id for j in usable]
            lower = [i for i in provisional if lower_level(i, confirmed, parents)]
            soft_lower.update((out[i].source_endpoint_id, child) for i in lower)
            chosen = confirmed + lower
        else:
            chosen = provisional
        for i in chosen:
            index_of[(out[i].source_endpoint_id, child)] = i
        candidates[child] = [out[i].source_endpoint_id for i in chosen]

    if not candidates:
        return StructureResolution(out, transitions, {})

    score = {key: score_of(i) for key, i in index_of.items()}
    leg_unidentified: set[str] = set()
    # ``pairs`` already includes declared pairs (set above).
    # A held leg whose partner is open stays in the assignment (fixed to its
    # parent) so the pair rule sees both legs and splits the open one off.
    open_candidates = {
        child: parents
        for child, parents in candidates.items()
        if child not in held or (pair_of.get(child) in candidates and pair_of.get(child) not in held)
    }
    assigned: dict[str, str] = {}
    if open_candidates:
        assigned = assign_parents(
            open_candidates,
            info.decimated,
            levels=level_map,
            nested=nested,
            pairs=pairs,
            fixed={**fixed, **held},
            score=score,
            min_children_w=CAPACITY_ACTIVE_W,
            capacity_cache=info.capacity,
            kcl_ok=(pairs_analysis.fits_kcl if pairs_analysis is not None else None),
        )
    assigned = {child: parent for child, parent in assigned.items() if child not in fixed}
    assigned.update({child: parent for child, parent in held.items() if child in candidates})

    children_of: dict[str, list[str]] = {}
    for child, parent in {**fixed, **assigned}.items():
        children_of.setdefault(parent, []).append(child)

    decided_out: dict[str, str] = {}
    for child in sorted(assigned):
        parent = assigned[child]
        chosen_index = index_of.get((parent, child))
        if chosen_index is None:
            continue
        decided_out[child] = parent
        chosen_edge = out[chosen_index]
        before = (previous or {}).get(chosen_edge.edge_id, str(_value(chosen_edge.lifecycle)))
        # P5: if a sibling parent fits this child just as well, the crossing is
        # not identifiable from balanced steps; keep it PROVISIONAL (soft).
        ambiguous = _ambiguous_parent(
            info, child, parent, candidates[child], nested, children_of.get(parent, [child])
        )
        if ambiguous is not None and child in pair_of:
            # Capacity is the asymmetric evidence for a 240 V pair: if one
            # sibling parent cannot carry BOTH legs, the structure splits them
            # (existing pair rule) and the crossing is identifiable.
            partner = pair_of[child]
            for rival in candidates[child]:
                if rival == parent:
                    continue
                group = [c for c in (child, partner) if c in candidates]
                if group and (
                    not info.fits_capacity(parent, group) or not info.fits_capacity(rival, group)
                ):
                    ambiguous = None
                    break
        if ambiguous is not None:
            transitions.append(
                _transition(
                    chosen_edge,
                    _PROVISIONAL,
                    f"leg_unidentified: true; {ambiguous} fits {child} equally well",
                )
            )
            leg_unidentified.add(child)
            if lifecycle_of(chosen_index) != _PROVISIONAL:
                out[chosen_index] = chosen_edge.model_copy(
                    update={"lifecycle": TopologyEdgeState(_PROVISIONAL)}
                )
        elif (
            lifecycle_of(chosen_index) == _PROVISIONAL
            and str(_value(before)) == _PROVISIONAL
            and child not in leg_unidentified
            and acyclic(parent, child)
            and _structural_ok(
                info, parent, child, candidates[child], children_of.get(parent, [child])
            )
            and (allow_confirm is None or allow_confirm(chosen_edge, out))
        ):
            set_lifecycle(chosen_index, _STRUCTURAL, "structural")
        if (parent, child) in soft_lower and lifecycle_of(chosen_index) != _STRUCTURAL:
            # A lower-level parent that did not confirm after all (its
            # siblings' capacity) never orphans the child: the confirmed
            # grandparent stays until a later settle decides.
            del decided_out[child]
            continue
        for other in candidates[child]:
            if other == parent:
                continue
            other_index = index_of[(other, child)]
            if lifecycle_of(other_index) == _STRUCTURAL:
                set_lifecycle(other_index, _PROVISIONAL, f"structure: direct parent is {parent}")
        # Confirmed edges from an ignored duplicate twin lose to the kept twin.
        for i in inferred.get(child, ()):
            edge = out[i]
            if (
                edge.source_endpoint_id in ignored_parents
                and edge.source_endpoint_id != parent
                and str(_value(edge.lifecycle)) == _STRUCTURAL
            ):
                set_lifecycle(i, _PROVISIONAL, "duplicate meter")

    # 240 V pair completion: one leg STRUCTURAL under P, the other leg's direct
    # parent Q a non-nested sibling of P.  Regression cannot separate the legs
    # (both mains follow both legs equally), so no r margin is required: the
    # partner's placement, low conflict and capacity while on decide.
    for a, b in classified.pairs:
        for leg, other_leg in ((a, b), (b, a)):
            p, q = assigned.get(leg), assigned.get(other_leg)
            if p is None or q is None or p == q or nested(p, q) or nested(q, p):
                continue
            leg_index = index_of.get((p, leg))
            q_index = index_of.get((q, other_leg))
            if leg_index is None or q_index is None or lifecycle_of(leg_index) != _STRUCTURAL:
                continue
            q_edge = out[q_index]
            before = (previous or {}).get(q_edge.edge_id, str(_value(q_edge.lifecycle)))
            # P5: a balanced load moves both mains by the same amount.  If the
            # other leg also fits the two siblings equally, no asymmetric
            # evidence separates the crossings; keep both PROVISIONAL (soft) and
            # record the ambiguity in the transition text.
            if _balanced_leg(info, other_leg, q, p):
                # Keep the crossing soft and record the ambiguity explicitly.
                transitions.append(
                    _transition(
                        q_edge,
                        _PROVISIONAL,
                        "leg_unidentified: true; balanced legs fit both mains alike",
                    )
                )
                leg_unidentified.add(other_leg)
                if lifecycle_of(q_index) != _PROVISIONAL:
                    out[q_index] = q_edge.model_copy(
                        update={"lifecycle": TopologyEdgeState(_PROVISIONAL)}
                    )
                decided_out[other_leg] = q
                continue
            if (
                lifecycle_of(q_index) == _PROVISIONAL
                and str(_value(before)) == _PROVISIONAL
                and other_leg not in leg_unidentified
                and q_edge.evidence.conflict_ratio < PAIR_MAX_CONFLICT_RATIO
                and acyclic(q, other_leg)
                and info.fits_capacity(q, [other_leg])
                and (allow_confirm is None or allow_confirm(q_edge, out))
            ):
                set_lifecycle(q_index, _STRUCTURAL, f"structural: 240 V pair of {leg} under {p}")
    return StructureResolution(out, transitions, decided_out, frozenset(leg_unidentified))


def _disagreement(
    info: StructureAnalysis,
    parent: str,
    child: str,
    rivals: Sequence[str],
    *,
    paired: bool = False,
) -> str | None:
    """Why the structure positively disagrees with a decided parent, else None.

    Only adequate data disagrees: no regression fit (too few child steps in the
    window) or no comparable capacity sample is no evidence either way.
    Hysteresis: confirmation needs r >= :data:`STRUCTURAL_MIN_R` and a margin of
    :data:`STRUCTURAL_MARGIN`; un-confirming needs r <
    :data:`STRUCTURAL_UNCONFIRM_R` or a rival with a higher r.
    """
    if not info.fits_capacity(parent, [child]):
        return "capacity"
    fit = info.sfit(parent, child)
    if fit is None:
        return None
    if fit.r < STRUCTURAL_UNCONFIRM_R:
        return f"r {fit.r:.2f}"
    if paired:
        # Both parents of a 240 V load follow both legs alike: rivals' r says
        # nothing about which leg is on which line.
        return None
    for other in rivals:
        other_fit = info.sfit(other, child)
        if other_fit is not None and fit.r - other_fit.r < STRUCTURAL_UNCONFIRM_MARGIN:
            return f"{other} fits better"
    return None


def _structural_ok(
    info: StructureAnalysis,
    parent: str,
    child: str,
    candidates: Sequence[str],
    siblings: Sequence[str],
) -> bool:
    """A PROVISIONAL parent singled out by regression and capacity."""
    fit = info.sfit(parent, child)
    if fit is None or fit.r < STRUCTURAL_MIN_R:
        return False
    low, high = STRUCTURAL_SLOPE
    if not low <= fit.slope <= high:
        return False
    for other in candidates:
        if other == parent:
            continue
        other_fit = info.sfit(other, child)
        if other_fit is not None and fit.r - other_fit.r < STRUCTURAL_MARGIN:
            return False
    # The reverse direction must not explain the pair as well (no cycles).
    reverse = info.sfit(child, parent)
    if reverse is not None and fit.r - reverse.r < STRUCTURAL_MARGIN:
        return False
    return info.fits_capacity(parent, siblings) and info.fits_capacity(parent, [child])
