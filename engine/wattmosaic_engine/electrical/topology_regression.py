"""Differenced-series regression, duplicate meters and hold resampling (M4).

Busy, noisy mains and unsynchronised sampling make per-event step matching fail
for sub-panel feeds: the parent can look flat during a large child step.  Over
many samples a lag-0 regression of the parent's sample-to-sample *differences*
on the child's differences separates the true parent from an unrelated leg.
A second meter on the same mains (high raw correlation, small constant offset)
is a duplicate, never a child.  HA's recorder writes a row only when a value
changes, so a flat reader has long gaps that must be held, not dropped.

The module is pure: no I/O, no clock, no scenario knowledge.  ``None`` is absent
and is always skipped, never treated as 0 W.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__: list[str] = [
    "DUP_MAX_OFFSET_W",
    "DUP_MIN_ACTIVE_SAMPLES",
    "DUP_MIN_CORRELATION",
    "GRID_STEP_S",
    "REG_MIN_CHILD_DIFF_W",
    "REG_MIN_SAMPLES",
    "RegressionFit",
    "align",
    "diff_regression",
    "duplicate_meters",
    "rank_parents",
    "resample_hold",
]

Series = Sequence[float | None]  # aligned: index i is the same instant for every series

GRID_STEP_S = 10.0
REG_MIN_CHILD_DIFF_W = 50.0
REG_MIN_SAMPLES = 20
DUP_MIN_CORRELATION = 0.98
DUP_MAX_OFFSET_W = 150.0
DUP_MIN_ACTIVE_SAMPLES = 50


@dataclass(frozen=True, slots=True)
class RegressionFit:
    """Least-squares slope through the origin, Pearson r and sample count."""

    slope: float
    r: float
    n: int


def resample_hold(
    times: Sequence[datetime],
    values: Sequence[float | None],
    *,
    start: datetime,
    end: datetime,
    step_s: float = GRID_STEP_S,
    max_hold_s: float | None = None,
) -> list[float | None]:
    """Value at every grid instant ``start, start + step, ... < end``.

    ``times`` are sorted ascending.  The value at an instant is the last sample
    at or before it.  The result is ``None`` before the first sample, when the
    last sample is itself ``None``, or when ``max_hold_s`` is set and the last
    sample is older than that.  ``max_hold_s=None`` holds forever, which is what
    a recorder that writes only on change needs.
    """
    if step_s <= 0:
        raise ValueError("step_s must be positive")
    span = (end - start).total_seconds()
    count = max(0, math.ceil(span / step_s))
    if count == 0 or not times:
        return [None] * count

    delta = timedelta(seconds=step_s)
    out: list[float | None] = []
    for i in range(count):
        instant = start + i * delta
        idx = bisect_right(times, instant) - 1
        if idx < 0:
            out.append(None)
            continue
        value = values[idx]
        if value is None:
            out.append(None)
            continue
        if max_hold_s is not None and (instant - times[idx]).total_seconds() > max_hold_s:
            out.append(None)
            continue
        out.append(value)
    return out


def align(
    history: Mapping[str, Sequence[tuple[datetime, float | None]]],
    *,
    start: datetime,
    end: datetime,
    step_s: float = GRID_STEP_S,
    max_hold_s: float | None = None,
) -> dict[str, list[float | None]]:
    """Hold-resample every reader onto one grid; every list has the same length."""
    out: dict[str, list[float | None]] = {}
    for name, rows in history.items():
        ordered = sorted(rows, key=lambda row: row[0])
        out[name] = resample_hold(
            [row[0] for row in ordered],
            [row[1] for row in ordered],
            start=start,
            end=end,
            step_s=step_s,
            max_hold_s=max_hold_s,
        )
    return out


def _fit_stats(dps: Sequence[float], dcs: Sequence[float]) -> tuple[float, float] | None:
    """Return ``(slope through origin, Pearson r)`` or ``None`` if undefined."""
    n = len(dps)
    sum_dcdc = sum(d * d for d in dcs)
    if n < 2 or sum_dcdc <= 0:
        return None
    slope = sum(dp * dc for dp, dc in zip(dps, dcs, strict=True)) / sum_dcdc

    mean_p = sum(dps) / n
    mean_c = sum(dcs) / n
    spread_p = sum((v - mean_p) ** 2 for v in dps)
    spread_c = sum((v - mean_c) ** 2 for v in dcs)
    if spread_p <= 0 or spread_c <= 0:
        return None
    cross = sum((p - mean_p) * (c - mean_c) for p, c in zip(dps, dcs, strict=True))
    r = cross / math.sqrt(spread_p * spread_c)
    return slope, r


def diff_regression(
    parent: Series,
    child: Series,
    *,
    min_child_diff_w: float = REG_MIN_CHILD_DIFF_W,
    min_samples: int = REG_MIN_SAMPLES,
) -> RegressionFit | None:
    """Regress the parent's differences on the child's differences at lag 0.

    For each ``i >= 1`` where all four values are present, ``dp`` and ``dc`` are
    the sample-to-sample differences.  Only steps with
    ``abs(dc) >= min_child_diff_w`` are used.  Fewer than ``min_samples`` used
    samples, or zero variance in either series, yields ``None``.
    """
    n = min(len(parent), len(child))
    if n < 2:
        return None
    dps: list[float] = []
    dcs: list[float] = []
    for i in range(1, n):
        p0, p1 = parent[i - 1], parent[i]
        c0, c1 = child[i - 1], child[i]
        if p0 is None or p1 is None or c0 is None or c1 is None:
            continue
        dc = c1 - c0
        if abs(dc) < min_child_diff_w:
            continue
        dps.append(p1 - p0)
        dcs.append(dc)
    if len(dps) < min_samples:
        return None
    stats = _fit_stats(dps, dcs)
    if stats is None:
        return None
    slope, r = stats
    return RegressionFit(slope=slope, r=r, n=len(dps))


def rank_parents(
    child: str,
    candidates: Iterable[str],
    series: Mapping[str, Series],
    **kw: float | int,
) -> list[tuple[str, RegressionFit]]:
    """Rank candidate parents by their differenced regression with ``child``.

    Candidates equal to the child, absent from ``series``, or without a fit are
    dropped.  Results are sorted by ``r`` descending, then by parent id.
    """
    if child not in series:
        return []
    child_series = series[child]
    out: list[tuple[str, RegressionFit]] = []
    seen: set[str] = set()
    for parent_id in candidates:
        if parent_id == child or parent_id in seen or parent_id not in series:
            continue
        seen.add(parent_id)
        fit = diff_regression(series[parent_id], child_series, **kw)
        if fit is not None:
            out.append((parent_id, fit))
    out.sort(key=lambda item: (-item[1].r, item[0]))
    return out


def duplicate_meters(
    series: Mapping[str, Series],
    *,
    min_corr: float = DUP_MIN_CORRELATION,
    max_offset_w: float = DUP_MAX_OFFSET_W,
    min_active_samples: int = DUP_MIN_ACTIVE_SAMPLES,
) -> list[tuple[str, str, float, float]]:
    """Pairs ``(a, b, r, offset)`` that look like one wire measured twice.

    Over samples where both series are present (at least
    ``min_active_samples`` of them, neither constant) the raw Pearson ``r`` must
    reach ``min_corr``, the intercept-bearing slope of ``b`` on ``a`` must be in
    ``[0.9, 1.1]``, and ``abs(median(b - a))`` must not exceed ``max_offset_w``.
    ``offset`` is that median.  Results are sorted by ``(a, b)``.
    """
    names = sorted(series)
    out: list[tuple[str, str, float, float]] = []
    for i, a in enumerate(names):
        values_a = series[a]
        for b in names[i + 1 :]:
            values_b = series[b]
            common = min(len(values_a), len(values_b))
            xs: list[float] = []
            ys: list[float] = []
            for j in range(common):
                x, y = values_a[j], values_b[j]
                if x is None or y is None:
                    continue
                xs.append(x)
                ys.append(y)
            n = len(xs)
            if n < min_active_samples:
                continue

            mean_a = sum(xs) / n
            mean_b = sum(ys) / n
            spread_a = sum((v - mean_a) ** 2 for v in xs)
            spread_b = sum((v - mean_b) ** 2 for v in ys)
            if spread_a <= 0 or spread_b <= 0:
                continue
            cross = sum((x - mean_a) * (y - mean_b) for x, y in zip(xs, ys, strict=True))
            r = cross / math.sqrt(spread_a * spread_b)
            if r < min_corr:
                continue
            slope = cross / spread_a
            if not 0.9 <= slope <= 1.1:
                continue
            offset = statistics.median([y - x for x, y in zip(xs, ys, strict=True)])
            if abs(offset) > max_offset_w:
                continue
            out.append((a, b, r, offset))
    return out
