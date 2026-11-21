"""E6 §3: current-based containment, set-KCL and reader rating classes.

Every quantity here uses **raw current** from the E5 companion ring: factor-free
and polarity-free, on 1-minute means.  All series analysis runs at aligned
rebuilds only (never in the per-group fold), is pure, and uses no scenario names.

The rules (spec ``E6-placement-current-sync.md`` §3):

1. Current containment veto: count minutes where ``|I_c| > 1.03·|I_p| + 0.3 A``;
   at >=10 per 1000 over >=120 minutes the edge is capped (same as the power
   containment veto).  It catches a reversed CT the power veto misses.
2. Set-current KCL: for a parent and its assigned children,
   ``Σ|I_c| <= 1.03·|I_p| + 0.5 A`` on >=99% of minutes.  A set that breaks it
   rejects its smallest-score child.
3. Rating classes per reader: peak current and max of 5-minute means.  A branch
   limit of 5-min > 12 A or peak > 16 A tags ``not_branch_circuit``.  A child
   whose 5-min max exceeds its parent's ×1.05 + 0.5 A is vetoed.  Highest-class
   un-contained readers are roots; a high-class reader with median >=1 A is a
   ``feeder``, one with median < 0.5 A an ``appliance_240v``.  The hint only
   breaks ties when choosing a parent.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "CURRENT_CONTAIN_REL",
    "CURRENT_CONTAIN_ABS_A",
    "CURRENT_CONTAIN_MIN_MINUTES",
    "CURRENT_CONTAIN_MAX_PER_1000",
    "CURRENT_KCL_REL",
    "CURRENT_KCL_ABS_A",
    "SET_KCL_MIN_FRACTION",
    "SET_KCL_MIN_MINUTES",
    "BRANCH_LIMIT_5MIN_A",
    "BRANCH_LIMIT_PEAK_A",
    "CHILD_ABOVE_PARENT_REL",
    "CHILD_ABOVE_PARENT_ABS_A",
    "ROOT_CLASS_A",
    "FEEDER_MEDIAN_A",
    "APPLIANCE_MEDIAN_A",
    "CurrentReport",
    "analyse_current",
    "current_containment_veto",
    "set_kcl_ok",
]

# 1-minute current containment.
CURRENT_CONTAIN_REL = 1.03
CURRENT_CONTAIN_ABS_A = 0.3
CURRENT_CONTAIN_MIN_MINUTES = 120
CURRENT_CONTAIN_MAX_PER_1000 = 10.0

# Set-current KCL.
CURRENT_KCL_REL = 1.03
CURRENT_KCL_ABS_A = 0.5
SET_KCL_MIN_FRACTION = 0.99
SET_KCL_MIN_MINUTES = 1440

# Rating classes (replaces E5's 12 A-only rule).
BRANCH_LIMIT_5MIN_A = 12.0
BRANCH_LIMIT_PEAK_A = 16.0
CHILD_ABOVE_PARENT_REL = 1.05
CHILD_ABOVE_PARENT_ABS_A = 0.5
ROOT_CLASS_A = 60.0
FEEDER_MEDIAN_A = 1.0
APPLIANCE_MEDIAN_A = 0.5

_BUCKETS_PER_MINUTE = 2  # 30 s companion grid
_BUCKETS_PER_5MIN = 10


@dataclass(slots=True)
class CurrentReport:
    """Per-reader rating classes and the current veto/KCL lookups."""

    peak_a: dict[str, float] = field(default_factory=dict)
    max_5min_a: dict[str, float] = field(default_factory=dict)
    median_a: dict[str, float] = field(default_factory=dict)
    rating_class: dict[str, str] = field(default_factory=dict)
    role_hint: dict[str, str] = field(default_factory=dict)
    branch_tag: dict[str, bool] = field(default_factory=dict)
    # (parent, child) -> (violations, minutes)
    containment: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    #: Private: 1-minute |I| arrays for set-Current-KCL (not an API field).
    _minute: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    def as_reader_dict(self, reader: str) -> dict[str, Any]:
        return {
            "peak_a": round(self.peak_a.get(reader, 0.0), 4),
            "max_5min_a": round(self.max_5min_a.get(reader, 0.0), 4),
            "rating_class": self.rating_class.get(reader, "unknown"),
            "role_hint": self.role_hint.get(reader, "unknown"),
            "not_branch_circuit": bool(self.branch_tag.get(reader, False)),
        }


def _means(values: Sequence[float], bucket: int) -> np.ndarray:
    """Non-overlapping means of ``bucket`` samples (absent stays NaN)."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size < bucket:
        return np.array([float("nan")])
    trimmed = arr[: arr.size // bucket * bucket]
    if not np.isfinite(trimmed).any():
        return np.full(trimmed.size // bucket, float("nan"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Mean of empty slice")
        return np.nanmean(trimmed.reshape(-1, bucket), axis=1)


def current_containment_veto(report: CurrentReport, parent: str, child: str) -> bool:
    """E6 §3.1: the child exceeds the parent's current on >=10 per 1000 minutes."""
    violations, minutes = report.containment.get((parent, child), (0, 0))
    if minutes < CURRENT_CONTAIN_MIN_MINUTES:
        return False
    return violations * 1000.0 / minutes >= CURRENT_CONTAIN_MAX_PER_1000


def child_above_parent(report: CurrentReport, parent: str, child: str) -> bool:
    """E6 §3: veto a child whose 5-min max exceeds its parent's 1.05× + 0.5 A."""
    c = report.max_5min_a.get(child)
    p = report.max_5min_a.get(parent)
    if c is None or p is None:
        return False
    return c > CHILD_ABOVE_PARENT_REL * p + CHILD_ABOVE_PARENT_ABS_A


def set_kcl_ok(report: CurrentReport, parent: str, children: Sequence[str]) -> bool:
    """E6 §3.2: a parent must cover its assigned children's current on >=99%.

    ``report`` carries the 1-minute |I| arrays in its private ``_minute`` map
    (populated by :func:`analyse_current`).  Absent minutes are skipped, never
    counted as zero.  With too few comparable minutes the rule is not evaluated
    and passes.
    """
    minute = getattr(report, "_minute", None) or {}
    p = minute.get(parent)
    if p is None:
        return True
    total = np.where(np.isfinite(p), np.abs(p), 0.0).copy()
    mask = np.isfinite(p)
    for child in children:
        c = minute.get(child)
        if c is None or len(c) != len(p):
            continue
        finite = np.isfinite(c)
        mask &= finite
        total -= np.where(finite, np.abs(c), 0.0)
    count = int(np.count_nonzero(mask))
    if count < SET_KCL_MIN_MINUTES:
        return True
    # Rule: sum(|I_children|) <= 1.03*|I_parent| + 0.5 A.  In residual form
    # p - sum >= p - (1.03*p + 0.5) = -0.03*p - 0.5, on each comparable minute.
    slack = (CURRENT_KCL_REL - 1.0) * np.abs(p[mask]) + CURRENT_KCL_ABS_A
    ok = int(np.count_nonzero(total[mask] >= -slack))
    return ok / count >= SET_KCL_MIN_FRACTION


def analyse_current(
    current: Mapping[str, Sequence[float]],
    *,
    parents: Mapping[str, Sequence[str]] | None = None,
) -> CurrentReport:
    """Build the rating report from aligned raw-current series (pure).

    ``current`` holds factor-free, polarity-free raw current on the 30 s grid.
    ``parents`` optionally maps each child to its candidate parents so only
    those pairs get a containment check (never all n² pairs).
    """
    report = CurrentReport()
    minute: dict[str, np.ndarray] = {}
    for reader, values in current.items():
        arr = np.asarray(values, dtype=np.float64)
        abs_arr = np.abs(np.where(np.isfinite(arr), arr, np.nan))
        finite = abs_arr[np.isfinite(abs_arr)]
        report.peak_a[reader] = float(finite.max()) if finite.size else 0.0
        m1 = _means(abs_arr.tolist(), _BUCKETS_PER_MINUTE)
        m5 = _means(abs_arr.tolist(), _BUCKETS_PER_5MIN)
        fm1, fm5 = m1[np.isfinite(m1)], m5[np.isfinite(m5)]
        report.max_5min_a[reader] = float(fm5.max()) if fm5.size else 0.0
        report.median_a[reader] = float(np.median(fm1)) if fm1.size else 0.0
        report.branch_tag[reader] = (
            bool(fm5.size and fm5.max() > BRANCH_LIMIT_5MIN_A)
            or report.peak_a[reader] > BRANCH_LIMIT_PEAK_A
        )
        minute[reader] = m1

    # A child whose 5-min max exceeds its parent's is never placed under it.
    for child, plist in (parents or {}).items():
        for parent in plist:
            if parent not in minute or child not in minute:
                continue
            p, c = minute[parent], minute[child]
            if len(p) != len(c):
                continue
            mask = np.isfinite(p) & np.isfinite(c)
            if not mask.any():
                continue
            violations = int(
                np.count_nonzero(np.abs(c[mask]) > CURRENT_CONTAIN_REL * np.abs(p[mask]) + CURRENT_CONTAIN_ABS_A)
            )
            report.containment[(parent, child)] = (violations, int(mask.sum()))

    # Rating class and role hint: the highest observed class (above ~60 A
    # sustained, i.e. a 5-min max above the root threshold) is a root class; a
    # high-class un-contained reader is a root.  The hint only breaks ties.
    for reader in report.max_5min_a:
        maximum = report.max_5min_a[reader]
        if maximum >= ROOT_CLASS_A:
            report.rating_class[reader] = "highest"
        elif report.branch_tag[reader] or maximum >= BRANCH_LIMIT_5MIN_A:
            report.rating_class[reader] = "high"
        else:
            report.rating_class[reader] = "branch"
        median = report.median_a.get(reader, 0.0)
        if report.rating_class[reader] in ("highest", "high"):
            if median >= FEEDER_MEDIAN_A:
                report.role_hint[reader] = "feeder"
            elif median < APPLIANCE_MEDIAN_A:
                report.role_hint[reader] = "appliance_240v"
            else:
                report.role_hint[reader] = "unknown"
        else:
            report.role_hint[reader] = "branch"

    # Keep the minute arrays for set_kcl_ok (private, not part of the API dict).
    report._minute = minute
    return report
