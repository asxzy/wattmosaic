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
   regression clearly singles it out and capacity fits; every losing CONFIRMED
   candidate drops to PROVISIONAL.

The O(readers^2 * samples) detectors run vectorised on a decimated copy of the
series and are cached per series object (:class:`StructureAnalysis`), so a
settle only pays for the assignment itself (ADR-0033: no long GIL holds).

Everything here is pure: no clock, no I/O, no database, no scenario names.
``None`` is absent and is always skipped, never treated as 0 W.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import EdgeLifecycleV3, TopologyEdgeV3

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
    "MAX_DECIMATED_SAMPLES",
    "MIN_STRUCTURE_SAMPLES",
    "STRUCTURAL_MARGIN",
    "STRUCTURAL_MIN_R",
    "STRUCTURAL_SLOPE",
    "StructureAnalysis",
    "analyse",
    "resolve_structure",
    "structure_series",
]

#: Capacity is only meaningful while the children actually draw power.
CAPACITY_ACTIVE_W = 300.0
#: Below this many aligned samples the series say nothing about structure.
MIN_STRUCTURE_SAMPLES = 50
#: Pair/duplicate detection and capacity run on every n-th sample (10 s -> 60 s).
DECIMATE = 6
#: ... and never on more than this many samples (a week of recorder history).
MAX_DECIMATED_SAMPLES = 2000
#: Structural confirmation needs a clear differenced-regression winner.
STRUCTURAL_MIN_R = 0.3
STRUCTURAL_SLOPE = (0.3, 1.3)
STRUCTURAL_MARGIN = 0.1
#: Weight of the regression r in the assignment's evidence score.
_REGRESSION_WEIGHT = 100.0

_CONTAINS = "contains"
_CONFIRMED = EdgeLifecycleV3.CONFIRMED.value
_PROVISIONAL = EdgeLifecycleV3.PROVISIONAL.value
_DORMANT = frozenset((EdgeLifecycleV3.REJECTED.value, EdgeLifecycleV3.RETIRED.value))

Series = Sequence[float | None]


def _value(item: Any) -> Any:
    return getattr(item, "value", item) if item is not None else None


def _as_array(values: Series) -> np.ndarray:
    """float64 copy; absent / non-finite samples become ``nan``."""
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
    fits: dict[tuple[str, str], RegressionFit | None] = field(default_factory=dict)
    capacity: dict[tuple[str, tuple[str, ...]], float] = field(default_factory=dict)

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


def _diff_fit(parent: np.ndarray, child: np.ndarray) -> RegressionFit | None:
    """Vectorised M4 ``diff_regression`` (same thresholds, same result)."""
    n = min(len(parent), len(child))
    if n < 2:
        return None
    dp = np.diff(parent[:n])
    dc = np.diff(child[:n])
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


def _find_pairs(names: Sequence[str], values: np.ndarray, mask: np.ndarray) -> list[tuple[str, str, float]]:
    """Vectorised M2 ``paired_legs`` on absolute values."""
    magnitude = np.abs(values)
    _n, r, _slope = _correlations(magnitude, mask)
    active = ((magnitude >= LEG_MIN_ACTIVE_W) & mask).astype(np.float64)
    both_active = active @ active.T
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


def analyse(series: Mapping[str, Series], *, decimate: int = DECIMATE) -> StructureAnalysis:
    """Detect paired legs and duplicate meters once for an aligned series set."""
    names = tuple(sorted(series))
    full = {name: _as_array(series[name]) for name in names}
    length = max((len(values) for values in series.values()), default=0)
    step = max(1, int(decimate), math.ceil(length / MAX_DECIMATED_SAMPLES))
    small = {name: full[name][::step] for name in names}
    decimated = {
        name: [None if math.isnan(v) else float(v) for v in small[name].tolist()] for name in names
    }
    if names:
        matrix = np.vstack([small[name] for name in names])
        mask = np.isfinite(matrix)
        values = np.where(mask, matrix, 0.0)
        pairs = _find_pairs(names, values, mask)
        duplicates = _find_duplicates(names, values, mask)
    else:
        pairs, duplicates = [], []
    return StructureAnalysis(
        source=series,
        readers=names,
        full=full,
        decimated=decimated,
        pairs=tuple(pairs),
        duplicates=tuple(duplicates),
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
        if _is_contains(edge) and str(_value(edge.lifecycle)) == _CONFIRMED:
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


def _transition(edge: TopologyEdgeV3, new: str, why: str) -> str:
    return f"{edge.edge_id} ({why}): {_value(edge.lifecycle)} -> {new}"


def resolve_structure(
    edges: Sequence[TopologyEdgeV3],
    series: Mapping[str, Series],
    *,
    levels: Mapping[str, float] | None = None,
    scores: Mapping[tuple[str, str], float] | None = None,
    analysis: StructureAnalysis | None = None,
) -> tuple[list[TopologyEdgeV3], list[str]]:
    """One direct parent per child from evidence, capacity, legs and regression.

    Returns the new edge list (same order, lifecycles updated) and one
    transition string per changed edge.  User-declared and pinned edges are
    never changed; they count as ``fixed`` in every capacity check.  ``scores``
    adds to the evidence score per ``(parent, child)``.
    """
    out = list(edges)
    info = analysis if analysis is not None and analysis.source is series else analyse(series)
    level_map = dict(levels or {})
    extra = dict(scores or {})
    nested = _ancestry(out)

    duplicates = info.duplicates
    ignored_parents = {b for _a, b, _r, _o in duplicates}
    twin_pairs = {frozenset((a, b)) for a, b, _r, _o in duplicates}

    transitions: list[str] = []

    def set_lifecycle(index: int, new: str, why: str) -> None:
        edge = out[index]
        if str(_value(edge.lifecycle)) == new:
            return
        transitions.append(_transition(edge, new, why))
        out[index] = edge.model_copy(update={"lifecycle": EdgeLifecycleV3(new)})

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
            if lifecycle == _CONFIRMED:
                fixed.setdefault(child, parent)
            continue
        if frozenset((parent, child)) in twin_pairs:
            # One wire measured twice is never parent and child.
            if lifecycle == _CONFIRMED:
                set_lifecycle(index, _PROVISIONAL, "duplicate meter")
            continue
        if lifecycle in (_CONFIRMED, _PROVISIONAL):
            inferred.setdefault(child, []).append(index)

    pair_of = {a: b for a, b, _r in info.pairs} | {b: a for a, b, _r in info.pairs}

    def score_of(index: int) -> float:
        edge = out[index]
        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        evidence = edge.evidence
        value = float(evidence.support) - 2.0 * float(evidence.conflict)
        fit = info.fit(parent, child)
        if fit is not None:
            value += _REGRESSION_WEIGHT * fit.r
        return value + float(extra.get((parent, child), 0.0))

    candidates: dict[str, list[str]] = {}
    index_of: dict[tuple[str, str], int] = {}
    for child, indices in sorted(inferred.items()):
        if child in fixed:
            # A user-declared parent is a fact; inference leaves the child alone.
            continue
        usable = [i for i in indices if out[i].source_endpoint_id not in ignored_parents]
        if not usable:
            continue
        confirmed = [i for i in usable if str(_value(out[i].lifecycle)) == _CONFIRMED]
        provisional = [i for i in usable if str(_value(out[i].lifecycle)) == _PROVISIONAL]
        if child in pair_of:
            chosen = confirmed + provisional
        elif confirmed:
            chosen = list(confirmed)
            if not any(
                info.fits_capacity(out[i].source_endpoint_id, [child]) for i in confirmed
            ):
                chosen += provisional
        else:
            chosen = provisional
        for i in chosen:
            index_of[(out[i].source_endpoint_id, child)] = i
        candidates[child] = [out[i].source_endpoint_id for i in chosen]

    if not candidates:
        return out, transitions

    score = {key: score_of(i) for key, i in index_of.items()}
    pairs = [(a, b) for a, b, _r in info.pairs]
    assigned = assign_parents(
        candidates,
        info.decimated,
        levels=level_map,
        nested=nested,
        pairs=pairs,
        fixed=fixed,
        score=score,
        min_children_w=CAPACITY_ACTIVE_W,
    )

    children_of: dict[str, list[str]] = {}
    for child, parent in {**fixed, **assigned}.items():
        children_of.setdefault(parent, []).append(child)

    for child in sorted(assigned):
        parent = assigned[child]
        chosen_index = index_of.get((parent, child))
        if chosen_index is None:
            continue
        chosen_edge = out[chosen_index]
        if str(_value(chosen_edge.lifecycle)) == _PROVISIONAL and _structural_ok(
            info, parent, child, candidates[child], children_of.get(parent, [child])
        ):
            set_lifecycle(chosen_index, _CONFIRMED, "structural")
        for other in candidates[child]:
            if other == parent:
                continue
            other_index = index_of[(other, child)]
            if str(_value(out[other_index].lifecycle)) == _CONFIRMED:
                set_lifecycle(other_index, _PROVISIONAL, f"structure: direct parent is {parent}")
        # Confirmed edges from an ignored duplicate twin lose to the kept twin.
        for i in inferred.get(child, ()):
            edge = out[i]
            if (
                edge.source_endpoint_id in ignored_parents
                and edge.source_endpoint_id != parent
                and str(_value(edge.lifecycle)) == _CONFIRMED
            ):
                set_lifecycle(i, _PROVISIONAL, "duplicate meter")
    return out, transitions


def _structural_ok(
    info: StructureAnalysis,
    parent: str,
    child: str,
    candidates: Sequence[str],
    siblings: Sequence[str],
) -> bool:
    """A PROVISIONAL parent singled out by regression and capacity."""
    fit = info.fit(parent, child)
    if fit is None or fit.r < STRUCTURAL_MIN_R:
        return False
    low, high = STRUCTURAL_SLOPE
    if not low <= fit.slope <= high:
        return False
    for other in candidates:
        if other == parent:
            continue
        other_fit = info.fit(other, child)
        if other_fit is not None and fit.r - other_fit.r < STRUCTURAL_MARGIN:
            return False
    # The reverse direction must not explain the pair as well (no cycles).
    reverse = info.fit(child, parent)
    if reverse is not None and fit.r - reverse.r < STRUCTURAL_MARGIN:
        return False
    return info.fits_capacity(parent, siblings) and info.fits_capacity(parent, [child])
