"""Pure nesting-comparison helpers kept after the Z2 cutover.

The legacy ``nesting.py`` engine (and its pointer-tree ``NestingCandidate``)
was deleted; the v3 topology inference engine keeps only these pure numeric
helpers.  No pointer-tree concept survives here.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

CONTAINMENT_MIN = 0.995

#: Watts of slack, so CT resolution and sampling skew cannot break containment.
CONTAINMENT_TOL_W = 30.0

#: Below this the parent is not carrying enough power for the comparison to
#: mean anything.  Instants are excluded, not counted as failures.
CONTAINMENT_MIN_PARENT_W = 200.0

#: The child must reach this fraction of the parent at least once, otherwise it
#: is an idle circuit that is contained by everything.
EXCITATION_MIN_RATIO = 0.05

#: A channel must carry at least this much for its reading to say anything about
#: *which* wire it is on.  Two channels at 8 W and 9 W agree about nothing.
ACTIVITY_MIN_W = 200.0

#: Fraction of the pair's combined active time during which *both* are active.
#: Two CTs on one wire are active together essentially always.  Two unrelated
#: circuits that are each idle most of the time are rarely active together, and
#: any agreement between them is the agreement of two near-zero numbers.
DUPLICATE_ACTIVITY_RATIO_MIN = 0.5

#: Median disagreement between two measurements of one wire, relative to the
#: larger reading.  Measured on the reference installation: two monitors on the
#: same service conductors disagree by 5.7 % and 9.0 % (median), while the
#: closest pair of genuinely different circuits disagrees by 16.6 %.
DUPLICATE_REL_DIFF_MAX = 0.15

#: Median magnitude ratio bound.  A x2-declared leg reports half its partner and
#: must stay a separate channel for the user to scale, not be collapsed.
DUPLICATE_RATIO_MAX = 1.25

#: Closure: the parent must never be explained away beyond this slack.
CLOSURE_TOL_W = 100.0

#: Fraction of comparable instants where the parent covers its own loads plus
#: the child.  Below this the parent is *over-explained* by the child, so the
#: child cannot be inside it.
CLOSURE_MIN = 0.95

#: A split-phase panel feed is published as two CTs whose ids differ only in the
#: leg suffix.  This is the same ``name_derived`` grouping discovery already uses
#: for double-pole loads, and it is reported as an assumption, never as fact.
_LEG_SUFFIX = re.compile(r"^(?P<stem>.+)_l[12]$")

#: A channel type that may be proposed as a nesting *child*.  Only a panel feed
#: has a level that measurement can establish; see the module docstring.
_NESTABLE_CHILD_TYPES = frozenset({"main"})


@dataclass(frozen=True, slots=True)
class MeasurementNode:
    """One physical meter point, observed by one or more CT channels.

    ``members`` is the set of CTs that measure the *same* conductors.  A node is
    the unit every comparison in this module is made on: comparing legs of one
    panel against legs of another compares four different conductors and produces
    numbers that mean nothing.
    """

    node_id: str
    members: tuple[str, ...]
    ct_type: str
    #: ``"single"``, ``"declared_pair"`` or ``"name_stem"``.  The last is an
    #: assumption derived from the channel ids and is always reported as such.
    grouping: str
    phase: str

    @property
    def is_assumed(self) -> bool:
        return self.grouping == "name_stem"

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "members": list(self.members),
            "ct_type": self.ct_type,
            "grouping": self.grouping,
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class DuplicateEvidence:
    """How strongly two series look like one wire measured twice."""

    joint_instants: int
    activity_ratio: float
    rel_diff: float
    ratio: float
    median_a_w: float
    median_b_w: float


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _as_float_array(values: Sequence[float]) -> np.ndarray:
    """``float64`` copy of ``values`` with exactly ``_finite``'s acceptance rule.

    Every kernel below used to be a Python loop guarded by ``_finite``, which
    accepts ``int``/``float`` and *nothing else*: ``None``, strings, ``Decimal``
    and even ``numpy.float32`` scalars are dropped.  ``np.asarray`` is more
    permissive than that (it happily parses ``"12.5"`` and widens ``float32``),
    so the fast path is only taken for dtypes that can only have come from an
    accepted Python scalar -- ``float64``, integer and ``bool``.  Anything else
    falls back to the elementwise strict conversion, so a value the loop
    rejected is still rejected.

    Rejected elements become ``nan``, which every kernel already filters out, so
    the distinction between "absent" and "not a number" is preserved -- neither
    is ever treated as zero.
    """
    if isinstance(values, np.ndarray) and values.dtype == np.float64:
        return values
    array = np.asarray(values)
    if array.dtype == np.float64:
        return array
    if array.dtype.kind in "iub":
        return array.astype(np.float64)
    out = np.full(len(values), math.nan, dtype=np.float64)
    for i, value in enumerate(values):
        if isinstance(value, (int, float)):
            out[i] = float(value)
    return out


def _finite_max(values: np.ndarray) -> float:
    """``max([v for v in values if finite])``, or ``0.0`` when nothing is finite."""
    finite = values[np.isfinite(values)]
    return float(finite.max()) if finite.size else 0.0


def _finite_abs_max(values: np.ndarray) -> float:
    """``max([abs(v) for v in values if finite])``, or ``0.0`` when nothing is finite."""
    finite = values[np.isfinite(values)]
    return float(np.abs(finite).max()) if finite.size else 0.0


def _partition_median(values: np.ndarray) -> float:
    """``_median`` on an array that is already known to be finite and non-empty.

    Same value as :func:`_median` (including the mean of the two middle
    elements for an even count) but ``O(n)`` instead of ``O(n log n)``, which
    matters because this runs once per candidate pair over ~77 000 instants.
    """
    n = values.size
    mid = n // 2
    if n % 2:
        return float(np.partition(values, mid)[mid])
    part = np.partition(values, (mid - 1, mid))
    return float((part[mid - 1] + part[mid]) / 2.0)


def containment(
    outer: Sequence[float],
    inner: Sequence[float],
    *,
    tol_w: float = CONTAINMENT_TOL_W,
    min_outer_w: float = CONTAINMENT_MIN_PARENT_W,
) -> tuple[float, int]:
    """Fraction of comparable instants where ``inner`` stays inside ``outer``.

    Returns ``(fraction, comparable_instants)``.  Instants where the outer
    channel is below ``min_outer_w`` are skipped: at that level the comparison is
    noise, and counting them either way would make the fraction meaningless.
    An empty comparison returns ``(0.0, 0)`` -- no evidence, never "contained".
    """
    if len(outer) != len(inner):
        raise ValueError("series must have the same length")
    outer_array = _as_float_array(outer)
    inner_array = _as_float_array(inner)
    comparable = np.isfinite(outer_array) & np.isfinite(inner_array) & (outer_array >= min_outer_w)
    count = int(np.count_nonzero(comparable))
    if count == 0:
        return 0.0, 0
    # ``i <= o + tol_w`` is exactly the loop's test; both sides are already
    # ``float`` and non-finite instants were removed by ``comparable``.
    ok = int(np.count_nonzero(inner_array[comparable] <= outer_array[comparable] + tol_w))
    return ok / count, count


def containment_tolerant(
    outer: Sequence[float],
    inner: Sequence[float],
    *,
    lag: int = 1,
    tol_w: float = CONTAINMENT_TOL_W,
    min_outer_w: float = CONTAINMENT_MIN_PARENT_W,
) -> tuple[float, int]:
    """Skew-tolerant :func:`containment`: ``inner`` may lead ``outer`` by ``lag``.

    Instant ``i`` passes when ``inner[i] <= max(outer[i-lag : i+lag+1]) + tol_w``,
    with the maximum taken over the finite values of that window only.  One grid
    step of cross-device skew (30 s on the structural memory's grid) otherwise
    makes a true child look above its parent exactly at its own step.

    ``comparable`` keeps :func:`containment`'s rule (both values finite and the
    outer carrying at least ``min_outer_w``); a window always includes ``i``
    itself, so it always has at least one finite value at a comparable instant.
    Returns ``(0.0, 0)`` for an empty comparison -- no evidence, never "contained".
    """
    if len(outer) != len(inner):
        raise ValueError("series must have the same length")
    if lag < 0:
        raise ValueError("lag must be non-negative")
    outer_array = _as_float_array(outer)
    inner_array = _as_float_array(inner)
    comparable = np.isfinite(outer_array) & np.isfinite(inner_array) & (outer_array >= min_outer_w)
    count = int(np.count_nonzero(comparable))
    if count == 0:
        return 0.0, 0
    # Pad with -inf (never a candidate for a maximum) so a sliding window at the
    # ends needs no special case.  Non-finite samples are mapped to -inf too:
    # the window's maximum is taken over finite values only.
    padded = np.where(np.isfinite(outer_array), outer_array, -np.inf)
    if lag:
        padded = np.concatenate(
            (np.full(lag, -np.inf, dtype=np.float64), padded, np.full(lag, -np.inf, dtype=np.float64))
        )
    width = 2 * lag + 1
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    maxima = windows.max(axis=-1)
    ok = int(np.count_nonzero(inner_array[comparable] <= maxima[comparable] + tol_w))
    return ok / count, count


def duplicate_evidence(
    a: Sequence[float],
    b: Sequence[float],
    *,
    activity_min_w: float = ACTIVITY_MIN_W,
) -> DuplicateEvidence | None:
    """Measure whether ``a`` and ``b`` are one wire measured twice.

    Only instants where **both** series carry at least ``activity_min_w`` are
    evidence.  This is the guard that mutual containment lacks: at near-idle
    levels two unrelated channels agree to the watt on everything, so every idle
    pair looked like a duplicate.

    Returns ``None`` when there is no joint activity at all -- which is not a
    negative result but the absence of one.
    """
    if len(a) != len(b):
        raise ValueError("series must have the same length")
    array_a = _as_float_array(a)
    array_b = _as_float_array(b)
    finite = np.isfinite(array_a) & np.isfinite(array_b)
    active_a = np.abs(array_a) >= activity_min_w
    active_b = np.abs(array_b) >= activity_min_w
    either = int(np.count_nonzero(finite & (active_a | active_b)))
    joint = finite & active_a & active_b
    joint_instants = int(np.count_nonzero(joint))
    if joint_instants == 0 or either == 0:
        return None

    x = array_a[joint]
    y = array_b[joint]
    # |x - y| / max(|x|, |y|, 1.0), elementwise, exactly as the loop computed it.
    diffs = np.abs(x - y) / np.maximum(np.maximum(np.abs(x), np.abs(y)), 1.0)
    med_a = _partition_median(x)
    med_b = _partition_median(y)
    return DuplicateEvidence(
        joint_instants=joint_instants,
        activity_ratio=joint_instants / either,
        rel_diff=_partition_median(diffs),
        ratio=max(abs(med_a), abs(med_b)) / max(min(abs(med_a), abs(med_b)), 1e-9),
        median_a_w=med_a,
        median_b_w=med_b,
    )


def closure_fraction(
    parent: Sequence[float],
    accounted: Sequence[float],
    *,
    tol_w: float = CLOSURE_TOL_W,
    min_parent_w: float = CONTAINMENT_MIN_PARENT_W,
) -> tuple[float, int, float]:
    """Fraction of instants where ``parent - accounted`` is not negative.

    ``accounted`` is the parent's own directly measured loads plus the proposed
    child feed.  Returns ``(fraction, comparable_instants, median_remainder_w)``.

    An instant is only comparable when *both* the parent and the accounted sum
    are finite.  A parent container's own-load series legitimately contains
    ``None`` where none of its loads had a sample yet, and adding that as zero
    would invent a load -- so the instant is skipped, never substituted.
    """
    if len(parent) != len(accounted):
        raise ValueError("series must have the same length")
    parent_array = _as_float_array(parent)
    accounted_array = _as_float_array(accounted)
    comparable_mask = (
        np.isfinite(parent_array) & np.isfinite(accounted_array) & (parent_array >= min_parent_w)
    )
    comparable = int(np.count_nonzero(comparable_mask))
    if comparable == 0:
        return 0.0, 0, 0.0
    remainders = parent_array[comparable_mask] - accounted_array[comparable_mask]
    ok = int(np.count_nonzero(remainders >= -tol_w))
    # ``_median`` of the remainders: same value as the loop's
    # ``sorted(...)[len // 2]`` (odd count -> middle element, even count ->
    # *upper* middle element, which is what indexing ``len // 2`` does).
    return ok / comparable, comparable, float(np.partition(remainders, comparable // 2)[comparable // 2])


# --------------------------------------------------------------------------- #
# Measurement nodes
# --------------------------------------------------------------------------- #
def build_nodes(channels: Mapping[str, Mapping[str, Any]]) -> dict[str, MeasurementNode]:
    """Group CTs that observe the same conductors into measurement nodes.

    Two groupings are recognised:

    ``declared_pair``
        Reciprocal ``paired_ct_id`` links.  The user has stated that these two
        CTs are the legs of one double-pole circuit, so they are one meter point
        by declaration.

    ``name_stem``
        Two ``main`` CTs whose ids differ only in a trailing ``_l1`` / ``_l2``.
        Discovery publishes a split-phase panel feed as exactly this shape, and
        the same ``name_derived`` grouping is already used for double-pole loads.
        It is still an *assumption*, so the node records it and every candidate
        that depends on it says so.

    Everything else is a node of one member.
    """
    member_to_node: dict[str, str] = {}
    nodes: dict[str, MeasurementNode] = {}

    def add(members: Sequence[str], grouping: str) -> None:
        ordered = tuple(sorted(members))
        node_id = "+".join(ordered)
        first = channels[ordered[0]]
        nodes[node_id] = MeasurementNode(
            node_id=node_id,
            members=ordered,
            ct_type=str(first.get("ct_type") or "unknown"),
            grouping=grouping,
            phase=str(first.get("phase") or "unknown"),
        )
        for member in ordered:
            member_to_node[member] = node_id

    # Declared pairs first: they are facts, and they win over name grouping.
    for ct_id in sorted(channels):
        if ct_id in member_to_node:
            continue
        paired = str(channels[ct_id].get("paired_ct_id") or "")
        if paired and paired in channels and str(channels[paired].get("paired_ct_id") or "") == ct_id:
            add([ct_id, paired], "declared_pair")

    # Split-phase panel legs, by id stem.  Only when exactly two legs exist.
    stems: dict[str, list[str]] = {}
    for ct_id in sorted(channels):
        if ct_id in member_to_node:
            continue
        if str(channels[ct_id].get("ct_type") or "") != "main":
            continue
        match = _LEG_SUFFIX.match(ct_id)
        if match:
            stems.setdefault(match.group("stem"), []).append(ct_id)
    for stem in sorted(stems):
        legs = stems[stem]
        if len(legs) == 2:
            add(legs, "name_stem")

    for ct_id in sorted(channels):
        if ct_id not in member_to_node:
            add([ct_id], "single")

    return nodes


