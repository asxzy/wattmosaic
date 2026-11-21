"""Voltage-sag leg classification and the branch-current tag (E5 §4).

All analysis runs on the aligned 30 s companion series, at aligned rebuilds only
(never in the per-group fold).  A 120 V load sags one mains leg under its step;
a balanced 240 V load sags both equally.  Voltage references are grouped by
correlation, the common mode is removed, and references cluster into two legs by
the sign of their residual correlation.  Per-reader step sags are then reduced to
a leg index L/R/both/unknown.  A sustained current over 12 A tags a reader as
``not_branch_circuit`` -- a behavioural prior used only as a tie-break in T5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "BRANCH_CURRENT_A",
    "BRANCH_WINDOW_S",
    "LEG_SAG_THRESHOLD",
    "MIN_LEG_STEPS",
    "MIN_VETO_STEPS",
    "LegReport",
    "analyse_legs",
]

#: Per-reader step classification thresholds (E5 §4.2).
LEG_SAG_THRESHOLD = 0.15  # volts per kW
COMMON_MODE_SAG = -0.2
MIN_LEG_STEPS = 5
MIN_VETO_STEPS = 10
#: A 5-min mean |I| above this at least once tags not_branch_circuit.
BRANCH_CURRENT_A = 12.0
BRANCH_WINDOW_S = 300.0
REFERENCE_CORR_MIN = 0.999
BUCKET_S = 30.0


@dataclass(slots=True)
class LegReport:
    """Per-reader leg indices, sample counts and branch-current tags."""

    leg: dict[str, str] = field(default_factory=dict)
    leg_n: dict[str, int] = field(default_factory=dict)
    branch_tag: dict[str, bool] = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    common_mode_removed: bool = False

    def as_reader_dict(self, reader: str) -> dict[str, Any]:
        return {
            "leg": self.leg.get(reader, "unknown"),
            "leg_n": int(self.leg_n.get(reader, 0)),
            "not_branch_circuit": bool(self.branch_tag.get(reader, False)),
        }


def _means(values: Sequence[float], bucket: int) -> list[float]:
    """Non-overlapping means of ``bucket`` samples (NaN kept as NaN)."""
    arr = np.asarray(values, dtype=np.float64)
    out: list[float] = []
    for start in range(0, len(arr), bucket):
        window = arr[start : start + bucket]
        finite = window[np.isfinite(window)]
        out.append(float(np.mean(finite)) if finite.size else float("nan"))
    return out


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    finite = np.isfinite(a) & np.isfinite(b)
    if finite.sum() < 3:
        return 0.0
    x, y = a[finite], b[finite]
    if np.std(x) == 0 or np.std(y) == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _reference_groups(
    voltage: Mapping[str, Sequence[float]],
) -> tuple[list[list[str]], dict[str, str]]:
    """Group readers whose 1-min voltage means correlate at >= 0.999."""
    if not voltage:
        return [], {}
    series = {r: np.asarray(_means(list(v), 2), dtype=np.float64) for r, v in voltage.items()}
    readers = sorted(series)
    groups: list[list[str]] = []
    membership: dict[str, str] = {}
    for reader in readers:
        placed = False
        for group in groups:
            if np.mean([_correlation(series[reader], series[r]) for r in group]) >= REFERENCE_CORR_MIN:
                group.append(reader)
                membership[reader] = "".join(group)
                placed = True
                break
        if not placed:
            groups.append([reader])
            membership[reader] = reader
    return groups, membership


def _cluster_legs(
    groups: list[list[str]], voltage: Mapping[str, Sequence[float]]
) -> tuple[dict[str, str], bool]:
    """Cluster voltage references into two legs by residual-correlation sign."""
    series = {r: np.asarray(_means(list(v), 2), dtype=np.float64) for r, v in voltage.items()}
    representatives: list[str] = []
    for group in groups:
        representatives.append(sorted(group)[0])
    if len(representatives) < 2:
        return {r: "L" for r in representatives}, False

    # Remove common mode (the mean of all references) and correlate residuals.
    common = np.nanmean(np.vstack([series[r] for r in representatives]), axis=0)
    residual = {r: series[r] - common for r in representatives}
    # Anchor L to the reference that sorts first by endpoint id.
    anchor = sorted(representatives)[0]
    legs = {anchor: "L"}
    for r in representatives:
        if r == anchor:
            continue
        corr = _correlation(residual[anchor], residual[r])
        legs[r] = "L" if corr >= 0 else "R"
    mapping: dict[str, str] = {}
    for group in groups:
        label = legs[sorted(group)[0]]
        for reader in group:
            mapping[reader] = label
    return mapping, True


def _steps(power: Sequence[float]) -> list[int]:
    """Indices where |ΔP| >= 50 W on the aligned series."""
    arr = np.asarray(power, dtype=np.float64)
    out: list[int] = []
    for i in range(1, len(arr)):
        if not np.isfinite(arr[i]) or not np.isfinite(arr[i - 1]):
            continue
        if abs(arr[i] - arr[i - 1]) >= 50.0:
            out.append(i)
    return out


def _leg_index(
    power: Sequence[float],
    leg_voltage: Mapping[str, Sequence[float]],
    leg_labels: Mapping[str, str],
) -> tuple[str, int]:
    """Median per-step sag difference in V/kW -> leg index and step count."""
    l_refs = [r for r in leg_voltage if leg_labels.get(r) == "L"]
    r_refs = [r for r in leg_voltage if leg_labels.get(r) == "R"]
    if not l_refs or not r_refs:
        return "unknown", 0
    sags: list[float] = []
    common_drops: list[float] = []
    for i in _steps(power):
        dp_kw = (float(power[i]) - float(power[i - 1])) / 1000.0
        if dp_kw == 0:
            continue
        dv_l = np.mean([float(leg_voltage[r][i]) - float(leg_voltage[r][i - 1]) for r in l_refs])
        dv_r = np.mean([float(leg_voltage[r][i]) - float(leg_voltage[r][i - 1]) for r in r_refs])
        sign = 1.0 if dp_kw > 0 else -1.0
        sags.append(sign * (dv_l - dv_r) / abs(dp_kw))
        common_drops.append(sign * (dv_l + dv_r) / 2.0 / abs(dp_kw))
    n = len(sags)
    if n < MIN_LEG_STEPS:
        return "unknown", n
    median = float(np.median(sags))
    if median < -LEG_SAG_THRESHOLD:
        return "L", n
    if median > LEG_SAG_THRESHOLD:
        return "R", n
    if float(np.median(common_drops)) < COMMON_MODE_SAG:
        return "both", n
    return "unknown", n


def _branch_tags(current: Mapping[str, Sequence[float]]) -> dict[str, bool]:
    """True when a 5-min mean |I| exceeds 12 A in any window."""
    tags: dict[str, bool] = {}
    buckets = int(BRANCH_WINDOW_S // BUCKET_S)
    for reader, values in current.items():
        means = _means([abs(float(v)) if np.isfinite(v) else float("nan") for v in values], buckets)
        tags[reader] = any(np.isfinite(m) and m > BRANCH_CURRENT_A for m in means)
    return tags


def analyse_legs(
    *,
    power: Mapping[str, Sequence[float]],
    voltage: Mapping[str, Sequence[float]],
    current: Mapping[str, Sequence[float]] | None = None,
) -> LegReport:
    """Run the whole leg analysis on already-aligned series.

    ``voltage`` holds the readers' voltage companion series; readers whose
    voltage series correlate at >= 0.999 form one physical reference.  ``power``
    holds the aligned power levels used to locate steps.  ``current`` is only
    needed for the branch-current tag.  Everything here is pure and runs once
    per aligned rebuild (the caller caches the report on the series object).
    """
    groups, _ = _reference_groups(voltage)
    labels, removed = _cluster_legs(groups, voltage)
    report = LegReport(references=labels, common_mode_removed=removed)
    reference_readers = set(labels)
    for reader in sorted(power):
        if reader in reference_readers:
            report.leg[reader] = labels.get(reader, "unknown")
            report.leg_n[reader] = 0
        else:
            leg, n = _leg_index(power[reader], voltage, labels)
            report.leg[reader] = leg
            report.leg_n[reader] = n
    if current:
        report.branch_tag = _branch_tags(current)
    return report


# --------------------------------------------------------------------------- #
# T5 uses (pure helpers)
# --------------------------------------------------------------------------- #


def cross_leg_veto(
    source_leg: str,
    target_leg: str,
    source_n: int,
    target_n: int,
) -> bool:
    """True when a CONTAINS edge must be capped at CANDIDATE (one veto only).

    Two readers classified on different legs (L vs R, neither ``both``) veto a
    structural crossing once both calls have at least ``MIN_VETO_STEPS`` steps.
    """
    pair = {source_leg, target_leg}
    return (
        "L" in pair
        and "R" in pair
        and "both" not in pair
        and source_n >= MIN_VETO_STEPS
        and target_n >= MIN_VETO_STEPS
    )


def same_leg_parent(
    child_leg: str,
    candidate_parents: Mapping[str, str],
) -> str | None:
    """The unique candidate parent on the child's leg (E5 §4.3 tie-break).

    ``candidate_parents`` maps parent reader id to its leg index.  A child
    classified L or R breaks E3 ambiguity in favour of the parent on the same
    leg, but only when exactly one candidate qualifies.
    """
    if child_leg not in ("L", "R"):
        return None
    matches = [parent for parent, leg in candidate_parents.items() if leg == child_leg]
    return matches[0] if len(matches) == 1 else None


def prefer_branch_tag(
    candidates: Sequence[str],
    branch_tag: Mapping[str, bool],
) -> list[str]:
    """Order equal-standing candidate parents, preferring not_branch_circuit.

    The branch tag's only behavioural use is this tie-break: when two candidate
    parents are otherwise equal, one tagged ``not_branch_circuit`` wins.  The
    stable input order is otherwise preserved.
    """
    return sorted(candidates, key=lambda reader: (not branch_tag.get(reader, False), candidates.index(reader)))
