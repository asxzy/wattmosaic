"""E4: chance-corrected, pure numpy pair analysis on the aligned series.

Everything here runs **once per aligned-series rebuild** on the 30 s grid and is
cached on the series object by the runtime; no fold ever recomputes it.  The
module is pure: no clock, no I/O, no database and no scenario names.

The quantities (see ``docs/specs/topology-evidence-fixes/E4-principles.md``):

``lift`` / ``wlift`` / ``match``
    P2, chance-corrected against 16 time-shifted controls.  A busy feed steps
    near almost any child step by chance, so a raw match rate is not specific.
    ``wlift`` weights each child step by the rarity of its chance match.
``mirror``
    P6, the same lift against ``p - sum(structural siblings of p except c)``;
    the best tie-break between candidate parents.
``contain`` / ``zero_floor``
    P1 vetoes on informative buckets: a level violation caps an edge, a leaf
    parent that stays at the floor while the child draws is likewise capped.
``kcl``
    P5 set-level KCL on 1-minute means with factor-scaled children.

Stats are computed **lazily per ordered pair** and cached in the analysis, so a
caller only pays for pairs that have a ledger edge in either direction, never
for all ``n²`` reader pairs.  A caller may :meth:`PairAnalysis.warm` a set of
candidate pairs once at rebuild time so every later lookup is a dict read.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__: list[str] = [
    "lift_gate_passes",
    "CONTAIN_DECISIVE_INFORMATIVE",
    "CONTAIN_MIN_INFORMATIVE",
    "KCL_MIN",
    "KCL_MIN_SAMPLES",
    "MIRROR_MIN",
    "STEP_LIFT_MIN_STEPS",
    "STEP_MATCH_MIN",
    "STEP_WLIFT_MIN",
    "ZERO_FLOOR_CHILD_W",
    "ZERO_FLOOR_MIN_BUCKETS",
    "ZERO_FLOOR_PARENT_W",
    "PairAnalysis",
    "PairStats",
    "analyse_pairs",
]

# Promotion gate (E4 §2).  A pair needs this many child steps before lift is
# evaluated; below it the gate is not applied (the historical verdict rule
# still decides).
STEP_LIFT_MIN_STEPS = 8
STEP_WLIFT_MIN = 0.13
#: Raw match floor on the engine's 30 s grid.  The 0.5 measured offline at 5 s
#: resolution is unreachable here: true pairs match 0.40-0.48 on busy mains.
#: On the 10-day reference replay 0.3 keeps every false pair out except
#: balanced 240 V legs (left to sibling ambiguity / KCL) and lifts direct-pair
#: recall from 0.43 to 0.70; ``wlift`` stays the discriminating score.
STEP_MATCH_MIN = 0.3


def lift_gate_passes(stats: Any | None) -> bool:
    """E4 §2 promotion gate for an inferred CONTAINS pair.

    Not evaluated (passes) without stats or with fewer than
    :data:`STEP_LIFT_MIN_STEPS` child steps; otherwise the pair must clear the
    rarity-weighted lift and the raw match floor.
    """
    if stats is None or int(getattr(stats, "n", 0)) < STEP_LIFT_MIN_STEPS:
        return True
    return float(stats.wlift) >= STEP_WLIFT_MIN and float(stats.match) >= STEP_MATCH_MIN

#: A mirror at/above this value is positive asymmetric evidence for a parent;
#: below half of it (or absent) is no evidence (E4 §2).
MIRROR_MIN = 0.30

#: Set-KCL: a parent must cover its factor-scaled children on at least 99 % of
#: one-minute samples, over at least a day of them (E4 §2).
KCL_MIN = 0.99
KCL_MIN_SAMPLES = 1440

#: Containment veto (E4 §2): violations are scored per 1000 informative buckets.
CONTAIN_MIN_INFORMATIVE = 120
CONTAIN_DECISIVE_INFORMATIVE = 2880

#: Leaf zero-floor veto (E4 §2): an absolute floor only, no relative one.
ZERO_FLOOR_PARENT_W = 5.0
ZERO_FLOOR_CHILD_W = 30.0
ZERO_FLOOR_MIN_BUCKETS = 3

#: 16 prime bucket offsets (3.5–33 min on a 30 s grid).  Primes avoid aliasing
#: periodic loads; the window rule is the same as the true alignment's.
_SHIFTS: tuple[int, ...] = (
    -7, 7, -11, 11, -17, 17, -23, 23, -31, 31, -41, 41, -53, 53, -67, 67,
)
_MATCH_REL = 0.10
_MATCH_ABS_W = 15.0
_STEP_FLOOR_W = 20.0
_SIGMA_FACTOR = 3.0
_MAD_TO_SIGMA = 1.4826
_CONTAIN_ABS_W = 15.0
_CONTAIN_REL = 0.02
_CONTAIN_TOL_W = 30.0
_KCL_ABS_W = 30.0
_KCL_REL = 0.03


def _as_array(values: Any) -> np.ndarray:
    """float64 copy with NaN for absent / non-finite samples (never 0 W)."""
    if isinstance(values, np.ndarray) and values.dtype == np.float64:
        out = values.astype(np.float64, copy=True)
    else:
        try:
            out = np.array(
                [np.nan if v is None or (isinstance(v, float) and math.isnan(v)) else v for v in values],
                dtype=np.float64,
            )
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


def _deltas(values: np.ndarray) -> np.ndarray:
    """``d[i] = x[i] - x[i-1]``; ``d[0]`` is absent (NaN)."""
    d = np.full(values.shape, np.nan, dtype=np.float64)
    if values.size > 1:
        d[1:] = values[1:] - values[:-1]
    return d


def _robust_sigma(deltas: np.ndarray) -> float:
    """MAD-based sigma of a reader's delta series."""
    finite = deltas[np.isfinite(deltas)]
    if finite.size == 0:
        return 0.0
    med = float(np.median(finite))
    mad = float(np.median(np.abs(finite - med)))
    return _MAD_TO_SIGMA * mad


def _step_indices(deltas: np.ndarray, sigma: float) -> np.ndarray:
    """Buckets whose |Δ| exceeds ``max(3 sigma, 20 W)``."""
    threshold = max(_SIGMA_FACTOR * sigma, _STEP_FLOOR_W)
    return np.flatnonzero(np.isfinite(deltas) & (np.abs(deltas) > threshold))


def _window_max(values: np.ndarray, width: int) -> np.ndarray:
    """Rolling max over ``width`` buckets, NaN treated as absent (-inf)."""
    padded = np.where(np.isfinite(values), values, -np.inf)
    half = width // 2
    if half:
        padded = np.concatenate(
            (np.full(half, -np.inf, dtype=np.float64), padded, np.full(half, -np.inf, dtype=np.float64))
        )
    return np.lib.stride_tricks.sliding_window_view(padded, width).max(axis=-1)


def _minute_means(values: np.ndarray) -> np.ndarray:
    """Mean of consecutive bucket pairs (1-minute means); odd tail dropped."""
    even = len(values) // 2 * 2
    if not even:
        return np.empty(0, dtype=np.float64)
    return (values[:even:2] + values[1:even:2]) / 2.0


def _lift(
    child_deltas: np.ndarray,
    child_steps: np.ndarray,
    child_scale: float,
    parent_deltas: np.ndarray,
) -> tuple[float, float, float, int]:
    """``(lift, wlift, match, n)`` of one ordered pair.

    The parent's Δ over ``[i-1, i, i+1]`` (best of the three, the tight E4
    skew window) is compared with the child's scaled step; the 16 shifted
    positions give both the chance correction and each step's rarity weight.
    """
    n = int(child_steps.size)
    if n == 0 or parent_deltas.size < 3:
        return 0.0, 0.0, 0.0, 0
    target = child_deltas[child_steps] * child_scale
    target_sign = np.sign(target)
    tolerance = _MATCH_REL * np.abs(target) + _MATCH_ABS_W
    length = parent_deltas.size

    def hits_at(shift: int) -> np.ndarray:
        index = child_steps + shift
        hit = np.zeros(n, dtype=bool)
        for offset in (-1, 0, 1):
            j = index + offset
            ok = (j >= 0) & (j < length)
            if not ok.any():
                continue
            pd = np.full(n, np.nan, dtype=np.float64)
            pd[ok] = parent_deltas[j[ok]]
            hit |= (
                ok
                & np.isfinite(pd)
                & (np.sign(pd) == target_sign)
                & (np.abs(pd - target) <= tolerance)
            )
        return hit

    true_hit = hits_at(0)
    match = float(true_hit.mean())
    shifted = [hits_at(shift) for shift in _SHIFTS]
    chance = float(np.mean([float(h.mean()) for h in shifted]))
    lift = match - chance
    # Per-step chance probability from its 16 shifted positions (Laplace
    # smoothing), then rarity weight.  A step the parent could match nearly
    # anywhere carries almost no information.
    counts = np.zeros(n, dtype=np.float64)
    for h in shifted:
        counts += h
    chance_by_step = (counts + 0.5) / (len(_SHIFTS) + 1.0)
    weights = -np.log(chance_by_step)
    total = float(weights.sum())
    wlift = (
        float(np.sum(weights * (true_hit.astype(np.float64) - chance_by_step)) / total)
        if total > 0
        else 0.0
    )
    return lift, wlift, match, n


@dataclass(frozen=True, slots=True)
class PairStats:
    """Every E4 quantity for one ordered ``(parent, child)`` pair."""

    lift: float = 0.0
    wlift: float = 0.0
    match: float = 0.0
    n: int = 0
    mirror: float | None = None
    violations: int = 0
    informative_n: int = 0
    zero_floor: bool = False
    #: E3 containment fraction / comparable instants, computed at the same
    #: rebuild so the live gate never runs a containment kernel in a fold.
    contain_fraction: float = 1.0
    contain_comparable: int = 0

    def as_detail(self) -> dict[str, Any]:
        """Diagnostic payload for ``evidence_detail`` (API payload, no contract)."""
        return {
            "lift": round(self.lift, 4),
            "wlift": round(self.wlift, 4),
            "match": round(self.match, 4),
            "n": self.n,
            "mirror": None if self.mirror is None else round(self.mirror, 4),
            "containment_violations": self.violations,
            "informative_n": self.informative_n,
            "zero_floor": self.zero_floor,
            "kcl": None,
        }


@dataclass(slots=True)
class PairAnalysis:
    """Per-rebuild analysis.  ``stats`` is the lazy, dict-backed cache.

    ``source`` is the exact series mapping this was built from: it is reused by
    the runtime only while that object is still current.
    """

    source: Mapping[str, Any]
    scale: Mapping[str, float]
    structural_children: Mapping[str, frozenset[str]]
    arrays: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    deltas: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    steps: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    #: Per-parent [i-1, i, i+1] rolling max, shared by containment violations
    #: and the E3 gate fraction (both use the same 3-bucket window).
    roll_max: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    #: Per-parent 5th percentile of finite levels (informative threshold).
    p5: dict[str, float] = field(default_factory=dict, repr=False)
    stats: dict[tuple[str, str], PairStats] = field(default_factory=dict)
    kcl_cache: dict[tuple[str, tuple[str, ...]], tuple[float, int]] = field(default_factory=dict)
    #: Parents with no structural *or* provisional children (the zero-floor
    #: veto only applies to a leaf so far).
    leaf_parents: frozenset[str] = frozenset()

    # -- lookups ------------------------------------------------------------ #
    def stats_for(self, parent: str, child: str) -> PairStats | None:
        """Stats for an ordered pair, computing it once on first lookup.

        The result is memoised on this shared object, so a pair with a ledger
        edge is analysed at most once per aligned rebuild (never once per fold);
        pairs never looked up are never analysed.
        """
        key = (parent, child)
        cached = self.stats.get(key)
        if cached is not None:
            return cached
        if parent not in self.arrays or child not in self.arrays:
            return None
        result = self._compute(parent, child)
        self.stats[key] = result
        return result

    #: Explicit name for the lazy-compute lookup (same as :meth:`stats_for`).
    stats_for_or_compute = stats_for

    def warm(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Pre-compute a set of ordered pairs (optional, at rebuild time)."""
        for parent, child in pairs:
            self.stats_for(parent, child)

    def lift(self, parent: str, child: str) -> float:
        stats = self.stats_for(parent, child)
        return 0.0 if stats is None else stats.lift

    def match(self, parent: str, child: str) -> float:
        stats = self.stats_for(parent, child)
        return 0.0 if stats is None else stats.match

    def mirror(self, parent: str, child: str) -> float | None:
        stats = self.stats_for(parent, child)
        return None if stats is None else stats.mirror

    def contain(self, parent: str, child: str) -> tuple[int, int]:
        stats = self.stats_for(parent, child)
        return (0, 0) if stats is None else (stats.violations, stats.informative_n)

    def zero_floor(self, parent: str, child: str) -> bool:
        stats = self.stats_for(parent, child)
        return bool(stats is not None and stats.zero_floor)

    def containment_fraction(self, parent: str, child: str) -> tuple[float, int]:
        """E3's skew-tolerant containment fraction, computed at the rebuild."""
        stats = self.stats_for(parent, child)
        return (
            (1.0, 0)
            if stats is None
            else (stats.contain_fraction, stats.contain_comparable)
        )

    def kcl(self, parent: str, children: Sequence[str]) -> tuple[float, int]:
        """``(fraction, samples)`` of set-KCL on 1-minute means.

        Children are scaled by their monitor factor (``1/f``); absent buckets
        are skipped, never counted as zero.  The result is cached per parent
        and child set so a structural pass evaluates each set once.
        """
        key = (parent, tuple(sorted(children)))
        cached = self.kcl_cache.get(key)
        if cached is not None:
            return cached
        result = self._compute_kcl(parent, key[1])
        self.kcl_cache[key] = result
        return result

    def fits_kcl(self, parent: str, children: Sequence[str]) -> bool:
        """Whether a child set fits under ``parent`` (no data is not a veto)."""
        if parent not in self.arrays:
            return True
        fraction, samples = self.kcl(parent, children)
        if samples < KCL_MIN_SAMPLES:
            return True
        return fraction >= KCL_MIN

    def edge_details(
        self, pairs: Iterable[tuple[str, str]] | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """``(parent, child) -> evidence_detail`` for the given (or cached) pairs.

        With ``pairs`` supplied, each pair's stats are computed on first lookup
        and cached; without it, only already-analysed pairs are reported.
        """
        if pairs is not None:
            for key in pairs:
                self.stats_for(*key)
        return {key: stats.as_detail() for key, stats in self.stats.items()}

    def with_kcl_details(self, details: Mapping[tuple[str, str], dict[str, Any]]) -> None:
        """Attach each edge's set-KCL fraction to its diagnostic payload."""
        for key, detail in details.items():
            parent, child = key
            siblings = set(self.structural_children.get(parent, ())) | {child}
            fraction, samples = self.kcl(parent, sorted(siblings))
            detail["kcl"] = round(fraction, 4) if samples else None
            detail["kcl_samples"] = samples

    # -- computation -------------------------------------------------------- #
    def _compute(self, parent: str, child: str) -> PairStats:
        p = self.arrays[parent]
        c = self.arrays[child]
        scale = float(self.scale.get(child, 1.0))
        c_scaled = c * scale
        lift, wlift, match, n = _lift(
            self.deltas[child], self.steps[child], scale, self.deltas[parent]
        )
        mirror: float | None = None
        siblings = self.structural_children.get(parent, frozenset()) - {child}
        if siblings:
            # Residual mirroring (P6): subtract every known structural sibling
            # but the candidate, ignoring NaN (an absent sample adds nothing).
            residual = np.where(np.isfinite(p), p, 0.0).copy()
            for sibling in sorted(siblings):
                series = self.arrays.get(sibling)
                if series is None:
                    continue
                s = float(self.scale.get(sibling, 1.0))
                residual += np.where(np.isfinite(series), series * s, 0.0)
            residual = np.where(np.isfinite(p), residual, np.nan)
            r_delta = _deltas(residual)
            # P6: the child's steps appear in what remains of the parent after
            # the other structural children are subtracted.  Use the rarity-
            # weighted chance-corrected lift as the single mirror score.
            mirror = _lift(
                self.deltas[child], self.steps[child], scale, r_delta
            )[1]

        informative_mask = np.isfinite(c_scaled) & np.isfinite(p)
        informative_n = 0
        violations = 0
        fraction, comparable = 1.0, 0
        if informative_mask.any() and p.size:
            floor = self.p5.get(parent, 0.0)
            informative_mask &= (c_scaled > floor)
            informative_n = int(np.count_nonzero(informative_mask))
            if informative_n:
                limit = self.roll_max[parent] + _CONTAIN_ABS_W + _CONTAIN_REL * np.abs(p)
                violations = int(np.count_nonzero(informative_mask & (c_scaled > limit)))
            # E3's gate fraction, computed once per pair with the same rolling
            # max (skew-tolerant containment over a ±1 bucket window).
            comparable_mask = np.isfinite(p) & np.isfinite(c_scaled) & (p >= 200.0)
            comparable = int(np.count_nonzero(comparable_mask))
            if comparable:
                ok = np.count_nonzero(
                    c_scaled[comparable_mask]
                    <= self.roll_max[parent][comparable_mask] + _CONTAIN_TOL_W
                )
                fraction = float(ok) / comparable

        zero_floor = False
        if parent in self.leaf_parents and c_scaled.size:
            draws = np.isfinite(p) & np.isfinite(c_scaled) & (c_scaled >= ZERO_FLOOR_CHILD_W)
            draw_n = int(np.count_nonzero(draws))
            if draw_n >= CONTAIN_MIN_INFORMATIVE:
                zero_floor = (
                    int(np.count_nonzero(draws & (p <= ZERO_FLOOR_PARENT_W)))
                    >= ZERO_FLOOR_MIN_BUCKETS
                )

        return PairStats(
            lift=lift,
            wlift=wlift,
            match=match,
            n=n,
            mirror=mirror,
            violations=violations,
            informative_n=informative_n,
            zero_floor=zero_floor,
            contain_fraction=fraction,
            contain_comparable=comparable,
        )

    def _compute_kcl(self, parent: str, children: tuple[str, ...]) -> tuple[float, int]:
        p = self.arrays.get(parent)
        if p is None:
            return 0.0, 0
        p_m = _minute_means(p)
        if not p_m.size:
            return 0.0, 0
        mask = np.isfinite(p_m)
        total = np.where(mask, p_m, 0.0).copy()
        for child in children:
            series = self.arrays.get(child)
            if series is None:
                continue
            scale = float(self.scale.get(child, 1.0))
            c_m = _minute_means(series * scale)
            if c_m.size != p_m.size:
                continue
            mask &= np.isfinite(c_m)
            total -= np.where(np.isfinite(c_m), c_m, 0.0)
        count = int(np.count_nonzero(mask))
        if not count:
            return 0.0, 0
        residual = total[mask]
        parent_m = p_m[mask]
        ok = int(np.count_nonzero(residual >= -(_KCL_ABS_W + _KCL_REL * np.abs(parent_m))))
        return ok / count, count


def analyse_pairs(
    series: Mapping[str, np.ndarray],
    scale: Mapping[str, float],
    structural_children: Mapping[str, frozenset[str]],
    *,
    bucket_s: float = 30.0,
    non_leaf: frozenset[str] = frozenset(),
) -> PairAnalysis:
    """Prepare one rebuild's pair analysis (pure, no pair-pair comparisons yet).

    ``series`` holds the absolute levels on one grid, NaN for absent buckets;
    ``scale[r]`` is ``1/f`` applied to ``r`` whenever it is a child.  Per-reader
    arrays, robust sigmas and step masks are prepared once.  Per-pair lift,
    mirror, containment and the E3 fraction are computed lazily and cached, so
    only pairs with a ledger edge (warmed by the runtime) are ever evaluated.
    ``bucket_s`` is recorded for diagnostics; the window offsets are buckets.
    """
    arrays = {reader: _as_array(values) for reader, values in series.items()}
    deltas = {reader: _deltas(values) for reader, values in arrays.items()}
    steps = {
        reader: _step_indices(deltas[reader], _robust_sigma(deltas[reader]))
        for reader in arrays
    }
    # Parent-side quantities are shared by every pair with that parent: compute
    # them once here, not once per (parent, child) pair.
    roll_max = {reader: _window_max(values, 3) for reader, values in arrays.items()}
    p5: dict[str, float] = {}
    for reader, values in arrays.items():
        finite = values[np.isfinite(values)]
        p5[reader] = float(np.percentile(finite, 5)) if finite.size else 0.0
    children = {
        str(parent): frozenset(str(child) for child in kids)
        for parent, kids in structural_children.items()
    }
    # A leaf parent is one that has no children of its own (neither structural
    # nor provisional).  "non_leaf" carries the provisional-child information
    # the runtime knows; structural children come from ``structural_children``.
    parents_with_children = set(children) | set(non_leaf)
    leaf = frozenset(reader for reader in arrays if reader not in parents_with_children)
    return PairAnalysis(
        source=series,
        scale={str(reader): float(factor) for reader, factor in scale.items()},
        structural_children=children,
        arrays=arrays,
        deltas=deltas,
        steps=steps,
        roll_max=roll_max,
        p5=p5,
        leaf_parents=leaf,
    )
