"""Topology inference engine: asymmetric delta + absolute evidence ledger (T5).

This module replaces the two legacy absolute-only engines (``nesting.py`` for
the live wizard and ``hierarchy.py`` for the audit pass) with one evidence
accumulator.  It is **pure**: every clock, input series and collaborator is
injected.  It never reads a database or the wall clock, and it never imports
``runtime.fakes``.

The evidence model
------------------
For an ordered reader pair ``(parent, child)`` the ledger keeps four counters:

``support``
    A child step whose parent step agrees within
    ``3 * sqrt(sigma_p**2 + beta**2 * sigma_c**2)``.
``conflict``
    A child step the parent does not explain (including "the parent did not
    move" -- a child moving on its own counts *against* the edge).
``parent_only``
    A parent step with no child step.  This is residual evidence and is **never**
    counted against the edge (HB §4.3, AT-02).
``independent_causes``
    Distinct explaining subjects / unknown profiles across FINAL event groups.
    One event, however strong, can never promote an edge past CANDIDATE.

Ported guards (design §5, analysis C traps T8/T9/T10/T13)
---------------------------------------------------------
* absolute containment ``>= 0.995`` stays a hard gate (never lowered);
* scaled copies and unknown measurement bases refuse to promote an edge;
* legs are grouped by ``_l1``/``_l2`` suffix, but the phase gate uses measured
  delta agreement, never the declared phase label (T9);
* the ledger is updated incrementally per FINAL group, never recomputed from a
  window (T13);
* CORRELATED edges are co-response without containment and are never structural;
  SAME_CONDUCTOR edges come from the duplicate detector.

Topology versions are proposed (and promoted) through the injected
:class:`~wattmosaic_engine.runtime.interfaces.VersionService` Protocol when an
edge crosses into STRUCTURAL or CONFLICTED.  User-pinned and REJECTED pairs are
never modified by inference.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EventGroup,
    MeasurementBasis,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)

from .current_rules import set_kcl_ok
from .nesting_math import (
    ACTIVITY_MIN_W,
    CLOSURE_MIN,
    CONTAINMENT_MIN,
    CONTAINMENT_MIN_PARENT_W,
    CONTAINMENT_TOL_W,
    DUPLICATE_ACTIVITY_RATIO_MIN,
    DUPLICATE_RATIO_MAX,
    DUPLICATE_REL_DIFF_MAX,
    EXCITATION_MIN_RATIO,
    DuplicateEvidence,
    MeasurementNode,
    _as_float_array,
    _finite_abs_max,
    _finite_max,
    build_nodes,
    closure_fraction,
    containment,
    containment_tolerant,
    duplicate_evidence,
)
from .sync_evidence import sync_verdict
from .topology_health import HealthBook, demotions
from .topology_pairs import (
    CONTAIN_DECISIVE_INFORMATIVE,
    CONTAIN_MIN_INFORMATIVE,
    MIRROR_MIN,
    STEP_MATCH_MIN,
    PairAnalysis,
    lift_gate_passes,
)
from .topology_placement import PlacementReport, compute_placements
from .topology_resolve import StructureAnalysis, analyse, resolve, structure_series

__all__: list[str] = [
    "CONTAINMENT_MIN",
    "CONTAINMENT_MIN_INSTANTS",
    "CONTAINMENT_DECISIVE_INSTANTS",
    "NestingCandidate",
    "infer_nesting",
    "MIN_INDEPENDENT_CAUSES",
    "MIN_SUPPORT_STRUCTURAL",
    "MIN_SUPPORT_PROVISIONAL",
    "WLIFT_MIN_PROVISIONAL",
    "WLIFT_MIN_STRUCTURAL",
    "LIFT_SUBSTITUTION_MIN_STEPS",
    "EVIDENCE_DECAY",
    "EVIDENCE_WINDOW",
    "TopologyEvidence",
    "DeltaObservation",
    "TopologyInferenceView",
    "detect_same_conductor",
    "detect_inferred_duplicates",
    "infer_topology",
]

# --------------------------------------------------------------------------- #
# Thresholds (design §5 / DEC-10).  Configurable at the call site where a knob
# exists; the defaults here are the normative ones.
# --------------------------------------------------------------------------- #

MIN_SUPPORT_CANDIDATE = 1
# E6 §2 / ADR-0045: calibrated from the 10-day reference replay.  One event
# still never passes CANDIDATE: PROVISIONAL needs two verdicts (or the >=8-step
# lift substitution below), STRUCTURAL needs three plus the lift gate.
MIN_SUPPORT_PROVISIONAL = 2
MIN_SUPPORT_STRUCTURAL = 3
MIN_INDEPENDENT_CAUSES = 2
CONFLICT_RATIO_PROVISIONAL = 0.34
CONFLICT_RATIO_STRUCTURAL = 0.10
CONFLICT_RATIO_CONFLICTED = 0.25
# E6 §2 / ADR-0045: the rarity-weighted lift a pair must reach before its
# support weight alone may promote it (evaluated only when n >= 8).
WLIFT_MIN_PROVISIONAL = 0.10
WLIFT_MIN_STRUCTURAL = 0.13
#: E6 §2 lift substitution: with many clean steps an edge may reach PROVISIONAL
#: even below the support_w floor.  At least 8 steps are required, so the
#: one-event invariant is preserved.
LIFT_SUBSTITUTION_MIN_STEPS = 8
LIFT_SUBSTITUTION_MAX_VIOLATIONS_PER_1000 = 2.0
#: The reverse-edge contradiction bar for the direction cap.  This stays at the
#: old, stricter 0.10: a reverse edge with a quarter of its verdicts conflicting
#: is genuinely contradicted even after E6 raises the PROVISIONAL promotion bar
#: to 0.34.  Decoupling it keeps direction identification honest (HB §4.3).
DIRECTION_CAP_CONFLICT_MAX = 0.10
#: A level-containment gate needs one hour of 30 s buckets before it is trusted;
#: below that it is "not evaluated" and does not gate promotion (E3 §1).
CONTAINMENT_MIN_INSTANTS = 120
#: With a full day of comparable instants a failing gate is decisive: the child
#: spends more than 0.5 % of a day above its candidate parent's level, which no
#: containment allows, so the edge becomes CONFLICTED regardless of step support
#: (E3 §1).
CONTAINMENT_DECISIVE_INSTANTS = 2880
MIN_EVENTS_CONFLICTED = 6
MIN_CORRELATED_EVENTS = 5
#: Decaying evidence (ADR-0043).  Each decisive verdict folds as
#: ``weight = EVIDENCE_DECAY * weight + (1 if that verdict else 0)``, so recent
#: evidence dominates and an edge follows the wiring in both directions.  Decay
#: is by verdict count, never wall clock: quiet periods do not erode a true edge
#: and replay needs no clock.  The pair sums to ``EVIDENCE_WINDOW`` at steady state.
EVIDENCE_DECAY = 0.95
EVIDENCE_WINDOW = 20  # round(1 / (1 - EVIDENCE_DECAY))
#: Hysteresis band: a STRUCTURAL edge keeps that state while its recent support
#: stays above KEEP_SUPPORT_STRUCTURAL and its conflict ratio below
#: KEEP_CONFLICT_RATIO_STRUCTURAL, even though it no longer meets the promotion
#: bar.  A plug move still demotes it once the recent conflicts accumulate.
KEEP_SUPPORT_STRUCTURAL = 2
KEEP_CONFLICT_RATIO_STRUCTURAL = 0.15
#: Topology versions are debounced: at most one inferred version per window
#: unless the change *removes* an edge from STRUCTURAL (which must publish now).
TOPOLOGY_VERSION_DEBOUNCE_S = 900.0
#: Leaving the good states judges the conflict *rate*, not the point ratio of a
#: handful of events: a one-sided Wilson bound at z = 1.0 (84 %).  CONFLICTED
#: needs the lower bound above CONFLICT_RATIO_CONFLICTED (2 conflicts in 6
#: events is no longer enough -- true pairs with few events ended CONFLICTED on
#: real data -- while 3 of 6, or any cross-leg pair at > 90 % conflict, still
#: is), and an already STRUCTURAL edge stays while the lower bound is below
#: CONFLICT_RATIO_PROVISIONAL.  Promotion keeps the point-ratio rule (DEC-10).
CONFLICT_BOUND_Z = 1.0
#: Structural passes per settle (each may build on the previous one's
#: confirmations; stops early once a pass changes nothing).
STRUCTURE_ROUNDS = 3

#: hierarchy.py:104 -- a constant child/parent ratio is one conductor with a
#: multiplier, not two nested feeds (scaled-copy refusal).
SCALED_COPY_RATIO_SPREAD_MAX = 0.02
#: hierarchy.py:84 -- below this a ratio is noise.
MIN_PAIR_ACTIVE_W = 200.0

_SIGMA_FACTOR = 3.0
#: Two CTs reading one step differ by a few percent (CT accuracy, sampling
#: instant, ramp).  A match tolerance of 3 sigma alone made every large clean
#: step (a 2 kW element read 2 % apart) neutral, so big loads never built
#: support (live dev server, 2026-11-20).
MATCH_RELATIVE_TOLERANCE = 0.20
_CONTAINS = "contains"
_CORRELATED = "correlated"
_SAME_CONDUCTOR = "same_conductor"
_DORMANT = frozenset((TopologyEdgeState.REJECTED, TopologyEdgeState.RETIRED))


def _wilson_bounds(conflict: float, decisive: float, z: float = CONFLICT_BOUND_Z) -> tuple[float, float]:
    """Wilson score interval of the conflict rate ``conflict / decisive``."""
    if decisive <= 0:
        return 0.0, 1.0
    p = conflict / decisive
    zz = z * z
    denominator = 1.0 + zz / decisive
    centre = p + zz / (2.0 * decisive)
    margin = z * math.sqrt(p * (1.0 - p) / decisive + zz / (4.0 * decisive * decisive))
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)


def _value(item: Any) -> Any:
    """``use_enum_values=True`` contracts store enum *values*; accept both."""
    return getattr(item, "value", item) if item is not None else None


def _relation(value: Any) -> str:
    return str(_value(value))


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


# --------------------------------------------------------------------------- #
# Evidence and observation shapes.
# --------------------------------------------------------------------------- #


class TopologyEvidence(EdgeEvidence):
    """The K03 edge evidence ledger with the AT-02 accessor names.

    ``supporting`` / ``conflicting`` are read-only aliases of the contract's
    ``support`` / ``conflict`` counters, so an AT-02 caller can read the ledger
    without depending on the K03 field naming.  No new persisted field is
    introduced -- ``model_dump`` is byte-identical to :class:`EdgeEvidence`.
    """

    @property
    def supporting(self) -> int:
        return self.support

    @property
    def conflicting(self) -> int:
        return self.conflict


def _classify_step(
    parent_move: float,
    child_delta: float,
    *,
    sigma_p: float,
    sigma_c: float,
    beta: float = 1.0,
    parent_spread: float | None = None,
    span_deviation: float | None = None,
) -> str | None:
    """``"support"``, ``"conflict"`` or ``None`` (neutral) for one child step.

    * support: the parent *moved* (beyond its own 3 sigma) by the child's step,
      within the combined 3-sigma noise plus :data:`MATCH_RELATIVE_TOLERANCE`,
      *and* the child's step is resolvable at the parent (spec P7).
    * conflict: the parent stayed within its 3-sigma noise while the child moved
      beyond it, and the parent's change is outside the support tolerance
      (design §5 T5: "a child step with no matching parent step beyond
      3 sigma_p").  The bands are disjoint, so one reading is never both.
    * neutral: anything else -- a parent that moved by another amount carries
      concurrent loads, and a child step inside the parent's noise is invisible.

    ``parent_spread`` is the larger of the parent's pre-/post-window spreads,
    measured by the runtime's anchor.  A net-zero level change whose windows
    are *not* themselves flat is a concurrent opposite step on a sibling: it
    proves nothing, so the verdict is neutral instead of conflict (spec P6).
    ``None`` means no spread is known (a direct caller that did not measure it),
    which keeps the historical single-number behaviour.

    ``span_deviation`` is the parent's largest deviation from its pre-step
    level over the *whole* anchored span, the unmeasured gap between the
    windows included (spec P8).  Conflict requires the parent flat over that
    span; a parent that moved anywhere in it (a concurrent change, its own copy
    of the step one sample early/late, a short child pulse) is neutral.
    """
    scaled = beta * child_delta
    flat_band = _SIGMA_FACTOR * sigma_p
    tolerance = _SIGMA_FACTOR * math.sqrt(sigma_p**2 + (beta * sigma_c) ** 2)
    tolerance += MATCH_RELATIVE_TOLERANCE * abs(scaled)
    if parent_spread is not None and parent_spread > flat_band:
        # The parent's window contains its own steps.  Its level change (even a
        # net zero one) cannot be read as "the parent did/did not follow".
        return None
    # P7: a step smaller than the combined noise of parent and child is not
    # resolvable at the parent, so matching it is coincidence, not evidence.
    resolvable = _SIGMA_FACTOR * math.sqrt(sigma_p**2 + sigma_c**2)
    if abs(parent_move) > flat_band and abs(parent_move - scaled) <= tolerance:
        if abs(scaled) > resolvable:
            return "support"
        return None
    if (
        abs(parent_move) <= flat_band
        and abs(scaled) > flat_band
        and abs(parent_move - scaled) > tolerance
    ):
        if span_deviation is not None and span_deviation > flat_band:
            return None
        return "conflict"
    return None


def _fold_verdict(
    evidence: EdgeEvidence, verdict: str, *, at: datetime
) -> dict[str, Any]:
    """Return the ``model_copy`` update for one decisive verdict (ADR-0043).

    Every decisive verdict decays *both* weights and adds 1 to the one that
    fired: ``support_w = D * support_w + (1 if support else 0)`` and
    ``conflict_w = D * conflict_w + (1 if conflict else 0)``.  The matching
    integer lifetime counter moves too.  A parent-only verdict never reaches
    here (it only moves ``parent_only``, decaying nothing).
    """
    support = verdict == "support"
    # Seed an unweighted legacy row from its lifetime counts before folding,
    # exactly as migration v029 does (support_w=support when no weight exists).
    sw = evidence.support_w or float(evidence.support)
    cw = evidence.conflict_w or float(evidence.conflict)
    update: dict[str, Any] = {
        "support_w": EVIDENCE_DECAY * sw + (1.0 if support else 0.0),
        "conflict_w": EVIDENCE_DECAY * cw + (0.0 if support else 1.0),
    }
    if support:
        update.update(support=evidence.support + 1, last_support_at=at)
    else:
        update.update(conflict=evidence.conflict + 1, last_conflict_at=at)
    return update


def _observed_entry(value: Any) -> tuple[float | None, float | None]:
    """``observed`` values are a sigma, or ``(sigma, measured_delta)``.

    ``measured_delta`` is the reader's level change across the group's window
    (post median minus pre median), or ``None`` when it could not be measured.
    """
    if isinstance(value, tuple):
        sigma = value[0] if len(value) > 0 else None
        measured = value[1] if len(value) > 1 else None
        return (
            float(sigma) if sigma is not None else None,
            float(measured) if measured is not None else None,
        )
    return (float(value) if value is not None else None, None)


@dataclass(frozen=True, slots=True)
class DeltaObservation:
    """One FINAL event group reduced to signed per-reader deltas.

    This is the production-neutral input to the ledger.  P4/P5 producers (or a
    test, or the HB §17 adapter below) create these from ``EventGroup``
    contracts; the engine itself never derives a delta from a window.
    """

    group_id: str
    deltas: Mapping[str, tuple[float, float | None]]
    at: datetime | None = None
    cause_id: str | None = None

    @classmethod
    def from_group(
        cls, group: EventGroup, *, sigma_floor_w: float = 2.0
    ) -> DeltaObservation:
        """Reduce an ``EventGroup`` contract to a :class:`DeltaObservation`.

        A reader with ``delta_w is None`` stays absent (HB §18): a missing
        reading is *not* a zero step and is never used as evidence.
        """
        deltas: dict[str, tuple[float, float | None]] = {}
        for delta in group.reader_deltas:
            if delta.delta_w is None:
                continue
            sigma = delta.sigma_w if delta.sigma_w is not None else sigma_floor_w
            deltas[delta.reader_id] = (float(delta.delta_w), float(sigma))
        cause: str | None = None
        for sub in group.sub_events:
            ref = sub.ref
            if ref is not None and getattr(ref, "id", None):
                cause = f"{_value(getattr(ref, 'kind', None)) or 'ref'}::{ref.id}"
                break
        return cls(
            group_id=group.group_id,
            deltas=deltas,
            at=group.closed_at or group.opened_at,
            cause_id=cause,
        )


def _coerce_group(value: EventGroup | DeltaObservation, sigma_floor_w: float) -> DeltaObservation:
    if isinstance(value, DeltaObservation):
        return value
    if isinstance(value, EventGroup):
        return DeltaObservation.from_group(value, sigma_floor_w=sigma_floor_w)
    raise TypeError(f"expected EventGroup or DeltaObservation, got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# Pure guards, ported from nesting.py / hierarchy.py.
# --------------------------------------------------------------------------- #


def _aligned_series(series: Mapping[str, Sequence[float | None]]) -> dict[str, list[float]]:
    """Forward-fill every reader onto the union of timestamps (none stays None)."""
    if not series:
        return {}
    # Values only; the caller supplies already time-indexed series.  Equal
    # lengths are the contract here, so pad trailing unknowns with nan.
    lengths = {len(values) for values in series.values()}
    if len(lengths) > 1:
        return {}
    return {rid: [float(v) if _finite(v) else math.nan for v in values] for rid, values in series.items()}


def _containment_fraction(
    parent: Sequence[float] | None,
    child: Sequence[float] | None,
) -> tuple[float, int]:
    """``(fraction, comparable)`` of skew-tolerant containment, or ``(1.0, 0)``.

    ``comparable == 0`` means "not evaluated" -- absent, unequal-length or
    all-incomparable series carry no evidence either way (E3 §1).  The fraction
    is reported as 1.0 in that case so a caller can treat a not-evaluated gate
    as passing without a second flag.
    """
    if parent is None or child is None or not parent or not child:
        return 1.0, 0
    if len(parent) != len(child):
        return 1.0, 0
    # The runtime hands the *same* series objects to every settle until the
    # aligned series is rebuilt (at most every few minutes), while a settle asks
    # for every ledger edge: memoise on object identity so a week of 30 s
    # buckets is compared once per pair per rebuild, not once per edge per fold
    # (a 4x slower replay before this cache).  The entry keeps both objects, so
    # an id is never reused while it is cached; the cache is bounded.
    key = (id(parent), id(child))
    hit = _CONTAINMENT_MEMO.get(key)
    if hit is not None and hit[0] is parent and hit[1] is child:
        return hit[2]
    result = containment_tolerant(list(parent), list(child))
    if len(_CONTAINMENT_MEMO) >= _CONTAINMENT_MEMO_MAX:
        _CONTAINMENT_MEMO.clear()
    _CONTAINMENT_MEMO[key] = (parent, child, result)
    return result


#: Identity memo for :func:`_containment_fraction` (see there).
_CONTAINMENT_MEMO: dict[tuple[int, int], tuple[Any, Any, tuple[float, int]]] = {}
_CONTAINMENT_MEMO_MAX = 4096


def _containment_gate(
    parent: Sequence[float] | None,
    child: Sequence[float] | None,
) -> bool:
    """Skew-tolerant absolute containment >= 0.995, once there is enough data.

    Below :data:`CONTAINMENT_MIN_INSTANTS` comparable instants (one hour of 30 s
    buckets) the gate is not evaluated and returns True, as before (E3 §1).
    """
    fraction, comparable = _containment_fraction(parent, child)
    if comparable < CONTAINMENT_MIN_INSTANTS:
        return True
    return fraction >= CONTAINMENT_MIN


def _level_conflict(
    parent: str,
    child: str,
    absolute: Mapping[str, Sequence[float | None]],
) -> bool:
    """Decisive level evidence that ``child`` is not inside ``parent`` (E3 §1).

    Requires a full day of comparable instants and a containment fraction below
    the normative bound.  With less data the gate is merely "not evaluated",
    which is never evidence against an edge.
    """
    fraction, comparable = _containment_fraction(absolute.get(parent), absolute.get(child))
    return comparable >= CONTAINMENT_DECISIVE_INSTANTS and fraction < CONTAINMENT_MIN


def _e4_fraction(
    parent: str,
    child: str,
    pairs: PairAnalysis | None,
    absolute: Mapping[str, Sequence[float | None]],
) -> tuple[float, int]:
    """E3 containment fraction, served from the E4 cache when available.

    The pair analysis computes the skew-tolerant fraction at the aligned rebuild
    alongside its other quantities, so the gate never runs a containment kernel
    in a fold (E4 §1/§2).  Without a cache it falls back to the memoised kernel.
    """
    if pairs is not None:
        stats = pairs.stats_for(parent, child)
        if stats is not None:
            return stats.contain_fraction, stats.contain_comparable
    return _containment_fraction(absolute.get(parent), absolute.get(child))


def _e4_containment(
    parent: str, child: str, pairs: PairAnalysis | None
) -> TopologyEdgeState | None:
    """E4 §2 containment / zero-floor veto, or ``None`` when not evaluated.

    Replaces E3's fraction rule for edges where the pair analysis is available:
    over informative buckets (child above the parent's 5th percentile) more than
    zero violations per 1000 caps the edge at CANDIDATE, and at least five per
    1000 over a day of informative buckets makes it CONFLICTED.  A leaf parent
    that sits at the floor while the child draws is likewise capped.
    """
    if pairs is None:
        return None
    stats = pairs.stats_for(parent, child)
    if stats is None:
        return None
    if stats.informative_n >= CONTAIN_MIN_INFORMATIVE:
        per_1000 = stats.violations * 1000.0 / stats.informative_n
        if (
            stats.informative_n >= CONTAIN_DECISIVE_INFORMATIVE
            and per_1000 >= 5.0
        ):
            return TopologyEdgeState.CONFLICTED
        # E6 §2 / ADR-0045: the cap needs >=10 violations per 1000 informative
        # buckets (over >=120 buckets now, not the old day).  Drop the old
        # ">0 caps at CANDIDATE" rule: the lift/KCL/level vetoes are stronger.
        if per_1000 >= 10.0:
            return TopologyEdgeState.CANDIDATE
    if stats.zero_floor:
        return TopologyEdgeState.CANDIDATE
    return None


def _transition_text(edge: TopologyEdgeV3, new: TopologyEdgeState, why: str) -> str:
    """A settle transition string (used by the ambiguity cap, E3 §2)."""
    return f"{edge.edge_id} ({why}): {_value(edge.lifecycle)} -> {_value(new)}"


def _contains_ancestry(edges: Sequence[TopologyEdgeV3]) -> dict[str, set[str]]:
    """``child -> {ancestors}`` over non-CONFLICTED CONTAINS edges (E3 §2).

    Only edges whose evidence state is PROVISIONAL or STRUCTURAL count: these
    are the candidate parents an ambiguity is evaluated between.  The chain is
    transitive, so a child whose candidates are a main -> sub -> child chain is
    recognised as an ancestor chain and is *not* ambiguous.
    """
    parents: dict[str, set[str]] = {}
    for edge in edges:
        if (
            _relation(edge.relation) == _CONTAINS
            and edge.lifecycle in (TopologyEdgeState.PROVISIONAL, TopologyEdgeState.STRUCTURAL)
        ):
            parents.setdefault(edge.target_endpoint_id, set()).add(edge.source_endpoint_id)
    ancestors: dict[str, frozenset[str]] = {}

    def walk(node: str) -> frozenset[str]:
        if node in ancestors:
            return ancestors[node]
        ancestors[node] = frozenset()  # cycle guard
        seen: set[str] = set()
        stack = list(parents.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(parents.get(current, ()))
        ancestors[node] = frozenset(seen)
        return ancestors[node]

    return {node: set(walk(node)) for node in parents}


def _ambiguous_children(
    edges: Sequence[TopologyEdgeV3],
    *,
    absolute: Mapping[str, Sequence[float | None]] | None = None,
    broken: frozenset[tuple[str, str]] = frozenset(),
    pairs: PairAnalysis | None = None,
) -> frozenset[str]:
    """Children with two indistinguishable candidate parents (E3 §2).

    For each child, candidate parents are the sources of its CONTAINS edges in
    PROVISIONAL/STRUCTURAL state.  Two parents ``P`` and ``Q`` are ambiguous
    when neither is an ancestor of the other, both child edges have comparable
    decaying support (``min >= 0.5 * max`` of ``support_w``), both have a recent
    conflict ratio below :data:`CONFLICT_RATIO_PROVISIONAL`, and whose level
    gate passed.  A candidate whose gate is evaluated and fails (E3 §1) is
    positively excluded by the levels and cannot be an indistinguishable
    alternative.  The child is then recorded as leg-unidentified, and every edge
    in its ambiguous set is capped at PROVISIONAL (done by the caller).
    """
    ancestry = _contains_ancestry(edges)
    by_child: dict[str, list[TopologyEdgeV3]] = {}
    for edge in edges:
        if (
            _relation(edge.relation) == _CONTAINS
            and edge.lifecycle in (TopologyEdgeState.PROVISIONAL, TopologyEdgeState.STRUCTURAL)
            and not edge.pinned
            and (edge.source_endpoint_id, edge.target_endpoint_id) not in broken
        ):
            by_child.setdefault(edge.target_endpoint_id, []).append(edge)
    out: set[str] = set()
    for child, candidates in by_child.items():
        if len(candidates) < 2:
            continue
        child_levels = (absolute or {}).get(child)
        # "Whose gate passed" (E3 §2): a candidate parent whose level gate is
        # evaluated and fails is positively excluded, not indistinguishable.
        viable = []
        for e in candidates:
            if absolute is None or child_levels is None:
                viable.append(e)
                continue
            fraction, comparable = _e4_fraction(
                e.source_endpoint_id, child, pairs, absolute
            )
            # E3 §2 candidates are edges "whose gate passed".  A gate that is
            # evaluated and fails is not a viable alternative; a not-evaluated
            # gate (fewer than CONTAINMENT_MIN_INSTANTS) passes by definition.
            if comparable >= CONTAINMENT_MIN_INSTANTS and fraction < CONTAINMENT_MIN:
                continue
            viable.append(e)
        for i, a in enumerate(viable):
            pa = a.source_endpoint_id
            sw_a = a.evidence.support_w or float(a.evidence.support)
            for b in viable[i + 1 :]:
                pb = b.source_endpoint_id
                if pa == pb:
                    continue
                # An ancestor chain is not an ambiguous sibling pair.
                if pa in ancestry.get(pb, ()) or pb in ancestry.get(pa, ()):
                    continue
                sw_b = b.evidence.support_w or float(b.evidence.support)
                if min(sw_a, sw_b) < 0.5 * max(sw_a, sw_b):
                    continue
                if (
                    a.evidence.conflict_ratio >= CONFLICT_RATIO_PROVISIONAL
                    or b.evidence.conflict_ratio >= CONFLICT_RATIO_PROVISIONAL
                ):
                    continue
                # E4 §2: mirroring is asymmetric evidence.  If exactly one of
                # the two candidates mirrors its child (>= MIRROR_MIN while the
                # other is below half that, or None), the tie is broken and the
                # pair is not ambiguous.  A parent's mirror is never computed
                # against itself (handled in PairAnalysis).
                if pairs is not None:
                    ma, mb = pairs.mirror(pa, child), pairs.mirror(pb, child)
                    if ma is not None and ma >= MIRROR_MIN and (
                        mb is None or mb < MIRROR_MIN / 2.0
                    ):
                        continue
                    if mb is not None and mb >= MIRROR_MIN and (
                        ma is None or ma < MIRROR_MIN / 2.0
                    ):
                        continue
                out.add(child)
                break
            else:
                continue
            break
    return frozenset(out)


def _scaled_copy(
    parent: Sequence[float] | None,
    child: Sequence[float] | None,
) -> bool:
    """True when the child is a fixed fraction of the parent (one conductor)."""
    if parent is None or child is None or len(parent) != len(child) or not parent:
        return False
    ratios: list[float] = []
    parent_values: list[float] = []
    for p, c in zip(parent, child, strict=True):
        if not (_finite(p) and _finite(c)) or abs(float(p)) < MIN_PAIR_ACTIVE_W:
            continue
        ratios.append(float(c) / float(p))
        parent_values.append(float(p))
    if len(ratios) < 2:
        return False
    # A constant parent series has no variation from which a fixed ratio could
    # be distinguished from coincidence; it is not a scaled copy.
    if max(parent_values) - min(parent_values) < MIN_PAIR_ACTIVE_W:
        return False
    ordered = sorted(ratios)
    center = abs(ordered[len(ordered) // 2]) or 1.0
    return (ordered[-1] - ordered[0]) / center <= SCALED_COPY_RATIO_SPREAD_MAX


def _phase_gate(
    child_delta: float,
    parent_delta: float,
    child_sigma: float,
    parent_sigma: float,
    channel_meta: Mapping[str, Mapping[str, Any]],
    child: str,
    parent: str,
) -> bool:
    """Legs may pair by suffix, but only measured agreement decides.

    Declared phase labels are contradicted by measurement on the reference
    installation (analysis C T9), so a label mismatch is recorded as metadata,
    never used to reject a pair.  Sign agreement within the combined noise band
    is the gate.
    """
    tol = _SIGMA_FACTOR * math.sqrt(child_sigma**2 + parent_sigma**2)
    # A step buried in noise is not evidence either way.  A declared phase
    # label is never consulted -- only the signed deltas and their sigmas.
    if abs(child_delta) <= tol or abs(parent_delta) <= tol:
        return True
    # Opposite signs are never compatible for a containment edge: a child that
    # moves the other way is not inside its parent.  The declared phase label
    # cannot override this measured fact (T9).
    return (child_delta > 0) == (parent_delta > 0)


def detect_same_conductor(
    series: Mapping[str, Sequence[float | None]],
    *,
    at: datetime | None = None,
    channel_meta: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[TopologyEdgeV3]:
    """Emit SAME_CONDUCTOR edges from the ported duplicate detector (T5 step 4)."""
    from .nesting_math import (
        DUPLICATE_ACTIVITY_RATIO_MIN,
        DUPLICATE_RATIO_MAX,
        DUPLICATE_REL_DIFF_MAX,
    )

    meta = channel_meta or {}
    aligned = _aligned_series(series)
    # Leg nodes by suffix (T9): a declared `_l1`/`_l2` pair measures one meter
    # point.  Sum its members into one series before comparing, so a leg pair is
    # never mistaken for a duplicate of one of its own members.
    nodes = build_nodes(
        {reader: meta.get(reader, {}) for reader in aligned}
    ) if meta else {}
    node_series: dict[str, list[float]] = {}
    if nodes:
        for node_id, node in nodes.items():
            members = [m for m in node.members if m in aligned]
            if not members:
                continue
            length = len(aligned[members[0]])
            node_series[node_id] = [
                float(sum(aligned[m][i] for m in members if i < len(aligned[m])))
                for i in range(length)
            ]
    comparison = node_series or aligned

    out: list[TopologyEdgeV3] = []
    readers = sorted(comparison)
    for i, a in enumerate(readers):
        for b in readers[i + 1 :]:
            evidence = duplicate_evidence(comparison[a], comparison[b])
            if evidence is None:
                continue
            # Reuse the nesting.py guards verbatim.
            if evidence.activity_ratio < DUPLICATE_ACTIVITY_RATIO_MIN:
                continue
            if evidence.rel_diff > DUPLICATE_REL_DIFF_MAX or evidence.ratio > DUPLICATE_RATIO_MAX:
                continue
            canonical, alias = sorted((a, b))
            out.append(
                TopologyEdgeV3(
                    edge_id=f"te-same::{canonical}::{alias}",
                    relation=RelationType.SAME_CONDUCTOR,
                    source_endpoint_id=canonical,
                    target_endpoint_id=alias,
                    lifecycle=TopologyEdgeState.CANDIDATE,
                    declared_by="inference",
                    evidence=TopologyEvidence(),
                    valid_from=at or datetime(2026, 1, 1, tzinfo=UTC),
                    pinned=False,
                )
            )
    return out


# E6 §4: inferred SAME_CONDUCTOR from an affine copy on 2-minute means.
DUP_CORR_MIN = 0.985
DUP_MIN_OVERLAP_H = 24.0
DUP_SLOPE_MIN = 0.9
DUP_SLOPE_MAX = 1.1
DUP_OFFSET_MAX_W = 150.0
#: E6 §4 / §6 (simple check for E6a): readers are co-sampled when >=90% of one
#: reader's sample timestamps have a counterpart within 2 s on the other.
COSAMPLE_WINDOW_S = 2.0
COSAMPLE_MIN_RATIO = 0.90


def co_sampled(
    timestamps_a: Sequence[float],
    timestamps_b: Sequence[float],
    *,
    window_s: float = COSAMPLE_WINDOW_S,
    min_ratio: float = COSAMPLE_MIN_RATIO,
) -> bool:
    """E6 §4/§6: True when the two readers are co-sampled (one monitor batch).

    Co-sampled channels publish in one batch, so their timestamps coincide.
    E6a uses this simple timestamp check: for each reader, at least
    ``min_ratio`` of its timestamps must have a counterpart within ``window_s``
    on the other.  E6b refines this from the runtime's fast-path sample tracking.
    """
    a = sorted(float(t) for t in timestamps_a if _finite(t))
    b = sorted(float(t) for t in timestamps_b if _finite(t))
    if not a or not b:
        return False

    def matched(src: Sequence[float], other: Sequence[float]) -> float:
        hits = 0
        j = 0
        for t in src:
            while j < len(other) and other[j] < t - window_s:
                j += 1
            if j < len(other) and abs(other[j] - t) <= window_s:
                hits += 1
        return hits / len(src)

    return matched(a, b) >= min_ratio and matched(b, a) >= min_ratio


def detect_inferred_duplicates(
    series: Mapping[str, Sequence[float | None]],
    *,
    timestamps: Mapping[str, Sequence[float]] | None = None,
    at: datetime | None = None,
) -> list[TopologyEdgeV3]:
    """E6 §4: infer SAME_CONDUCTOR between affine copies that are not co-sampled.

    Two readers are duplicates when the correlation of their 2-minute means is
    >= 0.985 over >= 24 h of overlap, the fitted slope is within [0.9, 1.1] and
    the offset is at most 150 W, each is the other's best match, and they are
    **not co-sampled**.  A declaration always wins (callers filter out pairs
    already declared).  Two co-sampled readers that pass are the legs of a
    balanced 240 V pair, not duplicates.  The fitted slope and offset are
    exposed as API ``evidence_detail`` diagnostics by
    :func:`inferred_duplicate_details` (never in a contract model).
    """
    readers = sorted(series)
    if len(readers) < 2:
        return []
    ts = timestamps or {}
    need_buckets = int(DUP_MIN_OVERLAP_H * 3600.0 / 120.0)
    aligned = _aligned_series(series)

    def two_min_means(values: Sequence[float]) -> np.ndarray:
        arr = np.asarray(values, dtype=np.float64)
        if arr.size < 2:
            return np.empty(0, dtype=np.float64)
        trimmed = arr[: arr.size // 2 * 2]
        means = trimmed.reshape(-1, 2).mean(axis=1)
        return means

    means = {reader: two_min_means(aligned[reader]) for reader in readers}
    qualified: dict[str, np.ndarray] = {}
    for reader in readers:
        valid = means[reader][np.isfinite(means[reader])]
        if valid.size >= need_buckets:
            qualified[reader] = means[reader]

    correlations: dict[tuple[str, str], float] = {}
    fits: dict[tuple[str, str], tuple[float, float]] = {}
    for i, a in enumerate(readers):
        if a not in qualified:
            continue
        for b in readers[i + 1 :]:
            if b not in qualified:
                continue
            x, y = qualified[a], qualified[b]
            mask = np.isfinite(x) & np.isfinite(y)
            if int(mask.sum()) < need_buckets:
                continue
            xs, ys = x[mask], y[mask]
            if float(np.std(xs)) == 0 or float(np.std(ys)) == 0:
                continue
            corr = float(np.corrcoef(xs, ys)[0, 1])
            if corr < DUP_CORR_MIN:
                continue
            slope, offset = np.polyfit(xs, ys, 1)
            slope, offset = float(slope), float(offset)
            if not (DUP_SLOPE_MIN <= slope <= DUP_SLOPE_MAX) or abs(offset) > DUP_OFFSET_MAX_W:
                continue
            if ts and co_sampled(ts.get(a, ()), ts.get(b, ())):
                continue  # co-sampled legs of a balanced 240 V pair, not copies
            correlations[(a, b)] = corr
            fits[(a, b)] = (slope, offset)

    best: dict[str, tuple[str, float]] = {}
    for (a, b), corr in correlations.items():
        if a not in best or corr > best[a][1]:
            best[a] = (b, corr)
        if b not in best or corr > best[b][1]:
            best[b] = (a, corr)

    out: list[TopologyEdgeV3] = []
    for (a, b) in sorted(correlations):
        if best.get(a, (None, 0))[0] != b or best.get(b, (None, 0))[0] != a:
            continue
        canonical, alias = sorted((a, b))
        out.append(
            TopologyEdgeV3(
                edge_id=f"te-inf-same::{canonical}::{alias}",
                relation=RelationType.SAME_CONDUCTOR,
                source_endpoint_id=canonical,
                target_endpoint_id=alias,
                lifecycle=TopologyEdgeState.CANDIDATE,
                declared_by="inference",
                evidence=TopologyEvidence(),
                valid_from=at or datetime(2026, 1, 1, tzinfo=UTC),
                pinned=False,
            )
        )
    return out


def inferred_duplicate_details(
    series: Mapping[str, Sequence[float | None]],
    *,
    timestamps: Mapping[str, Sequence[float]] | None = None,
) -> dict[tuple[str, str], dict[str, float]]:
    """E6 §4: fitted slope/offset/correlation for each inferred copy pair.

    Keyed by ``(canonical, alias)``; lives in API ``evidence_detail`` payloads,
    never in a contract model.  A caller may expose these alongside the edges
    emitted by :func:`detect_inferred_duplicates`.
    """
    edges = detect_inferred_duplicates(series, timestamps=timestamps)
    out: dict[tuple[str, str], dict[str, float]] = {}
    readers = sorted(series)
    aligned = _aligned_series(series)
    need = int(DUP_MIN_OVERLAP_H * 3600.0 / 120.0)
    means = {}
    for reader in readers:
        arr = np.asarray(aligned[reader], dtype=np.float64)
        trimmed = arr[: arr.size // 2 * 2]
        means[reader] = trimmed.reshape(-1, 2).mean(axis=1) if trimmed.size else arr
    for edge in edges:
        a, b = edge.source_endpoint_id, edge.target_endpoint_id
        x, y = means[a], means[b]
        mask = np.isfinite(x) & np.isfinite(y)
        if int(mask.sum()) < need:
            continue
        slope, offset = np.polyfit(x[mask], y[mask], 1)
        out[(a, b)] = {
            "slope": round(float(slope), 4),
            "offset": round(float(offset), 4),
            "correlation": round(float(np.corrcoef(x[mask], y[mask])[0, 1]), 4),
        }
    return out


# --------------------------------------------------------------------------- #
# The inference view: functional, immutable updates.
# --------------------------------------------------------------------------- #


class _StructureCache:
    """Last :class:`StructureAnalysis`, reused while the series object is the same.

    Shared (not copied) by every view derived with ``replace``: the analysis is a
    pure function of the series, so sharing it never changes a result, and it
    keeps the O(readers^2) detectors out of every fold (ADR-0033).
    """

    __slots__ = ("analysis",)

    def __init__(self) -> None:
        self.analysis: StructureAnalysis | None = None

    def get(self, series: Mapping[str, Sequence[float | None]]) -> StructureAnalysis:
        current = self.analysis
        if current is not None and current.source is series:
            return current
        current = analyse(series)
        self.analysis = current
        return current


@dataclass(frozen=True)
class TopologyInferenceView:
    """An immutable snapshot of the per-pair evidence ledger.

    Every ``observe_*`` method returns a **new** view; snapshots handed to a
    caller are never mutated.  That is what makes replay deterministic.
    """

    edges: tuple[TopologyEdgeV3, ...] = ()
    clock: Callable[[], datetime] = lambda: datetime(2026, 1, 1, tzinfo=UTC)
    versions: Any | None = None
    sigma_floor_w: float = 2.0
    at: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    absolute: Mapping[str, Sequence[float | None]] = field(default_factory=dict)
    basis_by_reader: Mapping[str, MeasurementBasis | str] = field(default_factory=dict)
    channel_meta: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    seen_groups: frozenset[str] = frozenset()
    causes_by_edge: Mapping[tuple[str, str, str], frozenset[str]] = field(default_factory=dict)
    co_response_causes: Mapping[frozenset[str], frozenset[str]] = field(default_factory=dict)
    co_response_events: Mapping[frozenset[str], int] = field(default_factory=dict)
    version_id: str | None = None
    #: Last inferred topology-version publication time (ADR-0043 §4).  Used
    #: only for the debounce; an edge *leaving* STRUCTURAL publishes regardless.
    last_version_at: datetime | None = None
    #: Refuse to CONFIRM a containment whose reverse is an equally supported,
    #: uncontradicted alternative (see :meth:`_direction_cap`).  The runtime
    #: sets it: an online ledger must never promote a containment cycle.
    require_direction: bool = False
    #: Rolling per-edge health of STRUCTURAL edges (M3): every decisive verdict
    #: on a confirmed edge is recorded; a BROKEN inferred edge is demoted to
    #: PROVISIONAL in :meth:`_settle` (topology linking v2).
    health: HealthBook = field(default_factory=HealthBook)
    #: Aligned absolute series (one grid, ``None`` = absent) used only for the
    #: structural pass (:func:`~.topology_resolve.resolve_structure`).  Kept
    #: apart from ``absolute`` so the per-edge containment gate is not run on
    #: hours of samples at every fold.
    aligned: Mapping[str, Sequence[float | None]] = field(default_factory=dict)
    #: Sticky structural decisions (child -> direct parent) of earlier settles.
    #: A decided parent the structure STRUCTURAL stays STRUCTURAL (even below
    #: MIN_SUPPORT_STRUCTURAL, and while no aligned series are available) until
    #: the structure positively disagrees, its conflict ratio passes the
    #: PROVISIONAL bound or its health is BROKEN (design §5 T5, linking v2).
    #: The runtime persists it next to the edge-health book.
    structure_decisions: Mapping[str, str] = field(default_factory=dict)
    #: Children whose crossing was kept soft because a balanced 240 V load fits
    # two sibling parents alike (spec P5).  Kept off the persisted decisions:
    # it is a fresh, per-settle structural observation, not a sticky decision.
    leg_unidentified: frozenset[str] = frozenset()
    structure_cache: _StructureCache = field(
        default_factory=lambda: _StructureCache(), compare=False, repr=False
    )
    #: E4 §1-2: chance-corrected pair analysis, computed once per aligned
    #: rebuild by the runtime and shared (not copied) by every derived view.
    #: ``None`` means the caller has no aligned series: behaviour is as before
    #: (E3 containment gate and no lift/KCL/mirror vetoes).
    pairs: PairAnalysis | None = field(default=None, compare=False, repr=False)
    #: E5 §4: per-reader voltage-sag leg indices and branch-current tags, built
    #: once at each aligned rebuild.  Vetoes/caps are applied in ``_settle``;
    #: ``None`` when no companion data (behaviour as before).
    leg_report: Any | None = field(default=None, compare=False, repr=False)
    #: E6 §1: one display/diagnostic placement per reader.  Lazily computed on
    #: first read from the ledger (pure function of edges and veto inputs);
    #: never affects H (which still uses STRUCTURAL edges only).
    _placements: PlacementReport | None = field(default=None, compare=False, repr=False)
    #: Inputs to the lazy placement computation (set at each settle).
    _placement_inputs: tuple[Any, Any, Any, Any] = field(
        default_factory=lambda: (frozenset(), {}, None, frozenset()), compare=False, repr=False
    )
    #: E6 §3: raw-current vetoes and rating classes from the companion ring.
    current_report: Any | None = field(default=None, compare=False, repr=False)
    #: E6 §6: co-sampled reader pairs, measured from sample timestamps.
    cosampled: frozenset[frozenset[str]] = field(default=frozenset(), compare=False, repr=False)

    # -- reads -------------------------------------------------------------- #
    def edge(
        self, source: str, target: str, relation: str = _CONTAINS
    ) -> TopologyEdgeV3 | None:
        wanted = _relation(relation)
        for edge in self.edges:
            if (
                edge.source_endpoint_id == source
                and edge.target_endpoint_id == target
                and _relation(edge.relation) == wanted
            ):
                return edge
        return None

    def contains_edges(self, *, lifecycles: Sequence[TopologyEdgeState] | None = None) -> tuple[TopologyEdgeV3, ...]:
        wanted = frozenset(lifecycles) if lifecycles else None
        return tuple(
            e
            for e in self.edges
            if _relation(e.relation) == _CONTAINS and (wanted is None or e.lifecycle in wanted)
        )

    @property
    def placements(self) -> PlacementReport:
        """E6 §1: one placement per reader, computed once and cached (lazy).

        Computing this at every settle cost a full edge scan in a busy replay;
        the API reads it at most once per version, so derive it on first access.
        The ledger is frozen-derived, so caching on this view is safe.
        """
        if self._placements is None:
            declared, canonical, pairs, cosampled = self._placement_inputs
            object.__setattr__(
                self,
                "_placements",
                self._compute_placements(
                    self.edges, declared, canonical, self.leg_unidentified, pairs, cosampled
                ),
            )
        return self._placements  # type: ignore[return-value]

    # -- incremental observations ------------------------------------------- #
    def observe_parent_only(self, reader_id: str, *, delta_w: float) -> TopologyInferenceView:
        """Record a parent-only step as neutral residual evidence (AT-02).

        Only edges for which ``reader_id`` is the parent are touched, and only
        their ``parent_only`` counter moves: support, conflict and lifecycle are
        unchanged.  A user-pinned / REJECTED edge is left entirely alone.
        """
        edges: list[TopologyEdgeV3] = []
        changed = False
        for edge in self.edges:
            if (
                _relation(edge.relation) != _CONTAINS
                or edge.source_endpoint_id != reader_id
                or edge.pinned
                or edge.lifecycle in _DORMANT
            ):
                edges.append(edge)
                continue
            evidence = edge.evidence.model_copy(
                update={"parent_only": edge.evidence.parent_only + 1}
            )
            edges.append(edge.model_copy(update={"evidence": evidence}))
            changed = True
        if not changed:
            return self
        return replace(self, edges=tuple(edges), _placements=None)

    def observe_group(
        self,
        group: EventGroup | DeltaObservation,
        *,
        cause_id: str | None = None,
        absolute: Mapping[str, Sequence[float | None]] | None = None,
        basis_by_reader: Mapping[str, MeasurementBasis | str] | None = None,
        channel_meta: Mapping[str, Mapping[str, Any]] | None = None,
        observed: Mapping[str, Any] | None = None,
        levels: Mapping[str, float] | None = None,
        measured_by_child: Mapping[str, Mapping[str, float]] | None = None,
        group_moves: Mapping[str, float | None] | None = None,
        aligned: Mapping[str, Sequence[float | None]] | None = None,
        scales: Mapping[str, float] | None = None,
        parent_spreads: Mapping[str, tuple[float, float] | float] | None = None,
        declared_pairs: frozenset[frozenset[str]] = frozenset(),
        alias_canonical: Mapping[str, str] | None = None,
        canonical_fresh: frozenset[str] | None = None,
        local_sigmas: Mapping[str, float] | None = None,
        pairs: PairAnalysis | None = None,
        leg_report: Any | None = None,
        current_report: Any | None = None,
        cosampled: frozenset[frozenset[str]] = frozenset(),
        sync_changes: Mapping[str, Mapping[str, float]] | None = None,
        pulse_evidence: Mapping[str, Mapping[str, Sequence[tuple[str, float, float]]]] | None = None,
    ) -> TopologyInferenceView:
        """Fold one FINAL event group into every affected pair's ledger.

        Several children stepping in one group (topology linking v2, M1): per
        parent, the STRUCTURAL children are explained away and verified (edge
        health), and the other stepping readers are matched as a subset of the
        remainder; a stepping 240 V leg pair counts as one candidate.  With a
        single stepping candidate the classic one-to-one rule below applies
        unchanged.  ``group_moves`` maps a parent to its level change across the
        whole group (earliest member step to latest); ``aligned`` replaces the
        aligned absolute series the structural pass resolves parents from.

        ``measured_by_child`` maps a stepping child to each candidate parent's
        level change measured across *that child's own step* (its last pre-step
        and first post-step samples).  It is the most direct evidence there is
        and wins over both P3's group delta for the parent and the group-level
        ``observed`` measurement: a group's onset can sit a minute or more away
        from a member's step, so a busy feed measured around the onset mixes in
        unrelated changes (live: true parents read flat or off-size).

        ``levels`` maps readers to their typical (recent median) power.  When a
        child ends up with several STRUCTURAL inferred parents, containment is
        transitive (a grandparent feed sees every step of its grandchildren), so
        only the parent with the lowest level is the direct one; the others are
        kept as PROVISIONAL soft alternatives.

        ``observed`` maps each reader that held a usable reading over the
        group's window to its noise sigma (``None`` = unknown, the floor is
        used).  With it, a child step whose candidate parent was observed but
        did *not* move counts as conflict for that edge (design §5 T5:
        "a child step with no matching parent step beyond 3 sigma_p").  This is
        what tells the two directions of an equal-delta pair apart: the true
        parent also steps for its other loads, so the reversed edge collects
        conflict.  Without ``observed`` (no reader health known) a non-moving
        reader is never evidence either way.

        ``scales`` maps a child reader to ``1 / multiplier`` for a
        monitor-applied measurement factor in force at its step (spec P3): the
        child's delta and sigma are divided by ``multiplier`` before any
        classification, so a x2 child under parents that move r / 2 is compared
        on the same basis.  A missing factor means scale 1.0; engine factors
        (already removed in P1) are never in this mapping.

        ``parent_spreads`` maps a parent to its pre/post-window spread (spec
        P6): a parent whose anchor window is not flat is neutral, however its
        net level change reads.  ``declared_pairs`` are user-declared
        PAIRED_LEG/SAME_CONDUCTOR pairs (spec P4): CONTAINS between their legs
        is never scored.  ``alias_canonical`` maps a SAME_CONDUCTOR alias to the
        canonical reader in force; an alias whose canonical reader is present
        never becomes a CONTAINS parent or child.  ``canonical_fresh`` is the
        set of canonical readers that reported within the staleness window: an
        alias of a fresh canonical is dropped from every pass (co-step,
        observed, multi-child, parent-only, co-response), and an alias of a
        stale canonical is renamed to the canonical, so a declared alias is
        never itself a CONTAINS endpoint.  ``None`` keeps the legacy rule
        (suppressed only while the canonical steps in the same group).

        ``leg_report`` is the E5 §4 aligned companion analysis (per-reader
        ``leg``/``leg_n`` and branch-current tags).  It caps a cross-leg
        CONTAINS edge at CANDIDATE once both readers have enough steps, keeps
        E3's ``leg_unidentified`` for a ``both`` child with ambiguous parents,
        lets a definite leg break that tie, and prefers a not-branch-circuit
        parent at equal standing.  ``None`` leaves all of this inactive.

        E6 §6: ``cosampled`` holds unordered reader pairs whose samples land in
        one monitor batch.  For those pairs ``sync_changes`` gives the parent's
        change across exactly the child's sample pair; the verdict is the
        synchronous one (no anchored window, no P6 spread veto).  Pairs not in
        ``cosampled`` keep today's anchored-window verdict.  ``pulse_evidence``
        carries two synchronous verdicts for a confirmed single-sample pulse.
        """
        obs = _coerce_group(group, self.sigma_floor_w)
        if obs.group_id in self.seen_groups:
            # A repeated group id is a duplicate delivery, not new evidence.
            return self
        cause = cause_id or obs.cause_id or obs.group_id
        at = obs.at or self.at

        edges = list(self.edges)
        by_index: dict[tuple[str, str, str], int] = {}
        for position, edge in enumerate(edges):
            by_index[(edge.source_endpoint_id, edge.target_endpoint_id, _relation(edge.relation))] = position
        causes: dict[tuple[str, str, str], set[str]] = {
            key: set(values) for key, values in self.causes_by_edge.items()
        }
        co_response: dict[frozenset[str], set[str]] = {
            key: set(values) for key, values in self.co_response_causes.items()
        }
        co_events: dict[frozenset[str], int] = dict(self.co_response_events)
        # Keep the very same mapping when nothing new arrives: the structural
        # analysis is cached per series object.
        absolute_now: Mapping[str, Sequence[float | None]] = (
            {**self.absolute, **absolute} if absolute else self.absolute
        )
        aligned_now = aligned if aligned is not None else self.aligned
        health = self.health
        basis_now = dict(self.basis_by_reader)
        if basis_by_reader:
            basis_now.update(basis_by_reader)
        meta_now = dict(self.channel_meta)
        if channel_meta:
            meta_now.update(channel_meta)

        decisive = {
            reader: values for reader, values in obs.deltas.items() if values[0] is not None
        }
        # A child's own noise around its step floors its long-run sigma (a
        # bursty load's MAD over hours reads ~1 W while it swings by 100 W).
        if local_sigmas:
            decisive = {
                reader: (
                    delta,
                    max(float(sigma if sigma is not None else self.sigma_floor_w), float(local_sigmas[reader]))
                    if reader in local_sigmas
                    else sigma,
                )
                for reader, (delta, sigma) in decisive.items()
            }
        # P3: divide a monitor-applied factor out of the child's step before any
        # classification (delta *and* sigma); absent factor means scale 1.0.
        if scales:
            decisive = {
                reader: (
                    float(delta) * float(scales.get(reader, 1.0)),
                    None if sigma is None else float(sigma) * float(scales.get(reader, 1.0)),
                )
                for reader, (delta, sigma) in decisive.items()
            }
        # P4: declared pairs and present aliases never take part in CONTAINS.
        # Normalise pairs to unordered reader sets in either direction.
        blocked: set[frozenset[str]] = {frozenset(pair) for pair in declared_pairs}
        canonical = dict(alias_canonical or {})
        suppressed: set[str] = set()
        rename: dict[str, str] = {}
        if canonical:
            blocked |= {frozenset((alias, target)) for alias, target in canonical.items()}
            for alias, target in canonical.items():
                if canonical_fresh is None:
                    # Legacy callers: skip the alias while its canonical reader
                    # holds a reading this group (HB §18: absent is not zero).
                    if target in decisive:
                        suppressed.add(alias)
                elif target in canonical_fresh or target in decisive:
                    # The canonical reported recently: the alias is the same
                    # conductor read again and is never a CONTAINS endpoint.
                    suppressed.add(alias)
                else:
                    # Canonical stale (offline): the alias stands in for it, and
                    # its evidence is folded under the canonical's identity.
                    rename[alias] = target
        if suppressed or rename:

            def _alias_keys(mapping: Mapping[str, Any] | None) -> dict[str, Any] | None:
                if mapping is None:
                    return None
                return {
                    rename.get(key, key): value
                    for key, value in mapping.items()
                    if key not in suppressed
                }

            decisive = _alias_keys(decisive) or {}
            observed = _alias_keys(observed)
            group_moves = _alias_keys(group_moves)
            parent_spreads = {
                key: (_alias_keys(value) if isinstance(value, Mapping) else value)
                for key, value in (_alias_keys(parent_spreads) or {}).items()
            }
            if measured_by_child is not None:
                measured_by_child = {
                    rename.get(child, child): _alias_keys(changes) or {}
                    for child, changes in measured_by_child.items()
                    if child not in suppressed
                }

        def _pair_blocked(a: str, b: str) -> bool:
            return a != b and frozenset((a, b)) in blocked

        def _suppressed(reader: str) -> bool:
            return reader in suppressed

        def _flatness(parent: str, child: str) -> dict[str, float | None]:
            """Anchor flatness of ``parent`` around ``child``'s step (P6, P8).

            Accepts the per-child form ``{child: {parent: (spread, span)}}``
            and the legacy per-parent form ``{parent: spread | (pre, post)}``.
            """
            spreads = parent_spreads or {}
            nested = spreads.get(child)
            if isinstance(nested, Mapping):
                entry = nested.get(parent)
                if entry is None:
                    return {"parent_spread": None, "span_deviation": None}
                return {"parent_spread": float(entry[0]), "span_deviation": float(entry[1])}
            entry = spreads.get(parent)
            if entry is None or isinstance(entry, Mapping):
                return {"parent_spread": None, "span_deviation": None}
            if isinstance(entry, (tuple, list)):
                return {"parent_spread": max(float(entry[0]), float(entry[1])), "span_deviation": None}
            return {"parent_spread": float(entry), "span_deviation": None}

        # -- parent-only steps: neutral residual evidence -------------------- #
        for edge in list(edges):
            if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                continue
            parent, child = edge.source_endpoint_id, edge.target_endpoint_id
            if parent in decisive and child not in decisive:
                key = (parent, child, _CONTAINS)
                current = edges[by_index[key]]
                evidence = current.evidence.model_copy(
                    update={"parent_only": current.evidence.parent_only + 1}
                )
                edges[by_index[key]] = current.model_copy(update={"evidence": evidence})

        anchored_by_child = measured_by_child or {}
        structure = self._structure_source(aligned_now, absolute_now)
        # E4 §2: the runtime may pass a pair analysis built once at the aligned
        # rebuild.  Keep the very same object so all derived views share the
        # cached per-pair stats; absent means legacy (E3) behaviour.
        pairs_now = pairs if pairs is not None else self.pairs
        multi = self._multi_child_verdicts(
            decisive,
            observed,
            group_moves,
            edges,
            self.structure_cache.get(structure) if structure is not None else None,
        )
        confirmed_now = {
            (e.source_endpoint_id, e.target_endpoint_id)
            for e in edges
            if _relation(e.relation) == _CONTAINS
            and _value(e.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
        }

        def _track(parent: str, child: str, verdict: str) -> None:
            nonlocal health
            if (parent, child) in confirmed_now or (parent, child) in health.entries:
                health = health.record(parent, child, verdict, at)

        # E6 §6: pairs already folded by a co-step/observed pass this group;
        # the synchronous pass below folds only the remaining ones (no double
        # counting of the same event).
        folded_this_group: set[tuple[str, str]] = set()

        # -- child steps against every candidate parent --------------------- #
        for child, (child_delta, child_sigma) in decisive.items():
            if _suppressed(child):
                continue
            anchored = anchored_by_child.get(child) or {}
            for parent, (parent_delta, parent_sigma) in decisive.items():
                if parent == child or _suppressed(parent) or _pair_blocked(parent, child):
                    continue
                if parent in anchored and anchored[parent] is not None:
                    # The parent's level across the child's own step.
                    parent_delta = float(anchored[parent])
                key = (parent, child, _CONTAINS)
                position = by_index.get(key)
                if position is None:
                    edge = TopologyEdgeV3(
                        edge_id=f"te-inf::{parent}::{child}",
                        relation=RelationType.CONTAINS,
                        source_endpoint_id=parent,
                        target_endpoint_id=child,
                        lifecycle=TopologyEdgeState.CANDIDATE,
                        declared_by="inference",
                        beta=1.0,
                        beta_basis="unity",
                        evidence=TopologyEvidence(),
                        valid_from=at,
                        pinned=False,
                    )
                    edges.append(edge)
                    position = len(edges) - 1
                    by_index[key] = position
                edge = edges[position]
                if edge.pinned or edge.lifecycle in _DORMANT:
                    # User authority wins: inference never edits these pairs.
                    continue
                beta = edge.beta if edge.beta is not None else 1.0
                sigma_p = parent_sigma if parent_sigma is not None else self.sigma_floor_w
                sigma_c = child_sigma if child_sigma is not None else self.sigma_floor_w
                # Design §5: support when the parent moved by the child's step,
                # conflict when it did not move (within 3 sigma_p of zero) while
                # the child clearly did.  Both moved by different amounts: the
                # parent's step also carries other loads changing in the same
                # window -- neutral, not evidence against containment (live
                # mains almost always carry concurrent changes).
                # The step-anchored one-to-one measurement wins whenever it
                # exists: the multi-child explanation uses the parent's net move
                # over the whole group, where unrelated steps cancel, so it is
                # only a fallback (real-history replay, 2026-11-20).
                if frozenset((parent, child)) in cosampled:
                    # E6 §6: the parent's change across exactly the child's
                    # sample pair, with no anchored window and no P6 spread veto.
                    sync = (sync_changes or {}).get(child, {}).get(parent)
                    verdict = sync_verdict(
                        sync,
                        child_delta,
                        sigma_p=sigma_p,
                        beta=beta,
                        sync_scale=float((scales or {}).get(child, 1.0)),
                    )
                elif key[:2] in multi and anchored.get(parent) is None:
                    verdict = multi[key[:2]]
                else:
                    verdict = _classify_step(
                        parent_delta, child_delta, sigma_p=sigma_p, sigma_c=sigma_c, beta=beta,
                        **_flatness(parent, child),
                    )
                if verdict is None:
                    continue
                folded_this_group.add((parent, child))
                _track(parent, child, verdict)
                causes.setdefault(key, set()).add(cause)
                evidence = edge.evidence.model_copy(
                    update={
                        **_fold_verdict(edge.evidence, verdict, at=at),
                        "independent_causes": len(causes[key]),
                    }
                )
                edges[position] = edge.model_copy(update={"evidence": evidence})

        # -- child steps whose observed parent did not move: conflict -------- #
        if observed:
            for position, edge in enumerate(list(edges)):
                if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                    continue
                parent, child = edge.source_endpoint_id, edge.target_endpoint_id
                if child not in decisive or parent in decisive or parent not in observed:
                    continue
                if _suppressed(parent) or _suppressed(child) or _pair_blocked(parent, child):
                    continue
                child_delta, child_sigma = decisive[child]
                sigma_p, measured = _observed_entry(observed.get(parent))
                sigma_p = max(sigma_p or self.sigma_floor_w, self.sigma_floor_w)
                anchored = anchored_by_child.get(child) or {}
                if anchored.get(parent) is not None:
                    measured = float(anchored[parent])
                beta = edge.beta if edge.beta is not None else 1.0
                # No measurement: P3 saw no step on an observed parent, i.e. it
                # did not move.  A measured level (P3 may simply not have flagged
                # a small step on a busy feed) is classified like a co-step.
                if frozenset((parent, child)) in cosampled:
                    sync = (sync_changes or {}).get(child, {}).get(parent)
                    verdict = sync_verdict(
                        sync,
                        child_delta,
                        sigma_p=sigma_p,
                        beta=beta,
                        sync_scale=float((scales or {}).get(child, 1.0)),
                    )
                elif (parent, child) in multi and anchored.get(parent) is None:
                    verdict = multi[(parent, child)]
                else:
                    verdict = _classify_step(
                        0.0 if measured is None else measured,
                        child_delta,
                        sigma_p=sigma_p,
                        sigma_c=child_sigma if child_sigma is not None else self.sigma_floor_w,
                        beta=beta,
                        **_flatness(parent, child),
                    )
                if verdict is None:
                    continue
                folded_this_group.add((parent, child))
                _track(parent, child, verdict)
                key = (parent, child, _CONTAINS)
                causes.setdefault(key, set()).add(cause)
                current = edges[position]
                evidence = current.evidence.model_copy(
                    update={
                        **_fold_verdict(current.evidence, verdict, at=at),
                        "independent_causes": len(causes[key]),
                    }
                )
                edges[position] = current.model_copy(update={"evidence": evidence})

            # A measured parent that moved with the child is support even when
            # no edge exists yet (P3 did not flag the parent's step, so the
            # co-step pass above never created the pair).  A flat or unmeasured
            # parent never creates an edge: there is nothing to count against.
            for child, (child_delta, child_sigma) in decisive.items():
                if _suppressed(child):
                    continue
                anchored = anchored_by_child.get(child) or {}
                for parent, entry in observed.items():
                    if parent == child or parent in decisive or _suppressed(parent) or _pair_blocked(parent, child):
                        continue
                    key = (parent, child, _CONTAINS)
                    if key in by_index:
                        continue
                    sigma_p, measured = _observed_entry(entry)
                    if anchored.get(parent) is not None:
                        measured = float(anchored[parent])
                    if measured is None:
                        continue
                    if (parent, child) in multi and anchored.get(parent) is None:
                        verdict = multi[(parent, child)]
                    else:
                        verdict = _classify_step(
                            measured,
                            child_delta,
                            sigma_p=max(sigma_p or self.sigma_floor_w, self.sigma_floor_w),
                            sigma_c=child_sigma if child_sigma is not None else self.sigma_floor_w,
                            **_flatness(parent, child),
                        )
                    if verdict != "support":
                        continue
                    folded_this_group.add((parent, child))
                    causes.setdefault(key, set()).add(cause)
                    edges.append(
                        TopologyEdgeV3(
                            edge_id=f"te-inf::{parent}::{child}",
                            relation=RelationType.CONTAINS,
                            source_endpoint_id=parent,
                            target_endpoint_id=child,
                            lifecycle=TopologyEdgeState.CANDIDATE,
                            declared_by="inference",
                            beta=1.0,
                            beta_basis="unity",
                            evidence=TopologyEvidence(
                                support=1,
                                support_w=1.0,
                                last_support_at=at,
                                independent_causes=len(causes[key]),
                            ),
                            valid_from=at,
                            pinned=False,
                        )
                    )
                    by_index[key] = len(edges) - 1

        # -- E6 §6: synchronous verdicts for co-sampled pairs ---------------- #
        # A co-sampled parent whose P3 step was not flagged (a flat wrong parent,
        # or a parent measured only by its paired channel) still gets a
        # synchronous verdict measured across the child's sample pair.  Pulses
        # produce two such verdicts each (ON and OFF) instead of being dropped.
        for child, by_parent in (sync_changes or {}).items():
            if _suppressed(child) or child not in decisive:
                continue
            child_delta, child_sigma = decisive[child]
            for parent, change in by_parent.items():
                if parent == child or _suppressed(parent) or _pair_blocked(parent, child):
                    continue
                if (parent, child) in folded_this_group:
                    continue
                sigma_p = max(
                    float((observed or {}).get(parent, (self.sigma_floor_w, None))[0] or self.sigma_floor_w),
                    self.sigma_floor_w,
                )
                verdict = sync_verdict(
                    change,
                    child_delta,
                    sigma_p=sigma_p,
                    sync_scale=float((scales or {}).get(child, 1.0)),
                )
                if verdict is None:
                    continue
                key = (parent, child, _CONTAINS)
                position = by_index.get(key)
                cause_here = f"{cause}::sync::{parent}::{child}"
                if position is None:
                    if verdict != "support":
                        # A conflict-only pair with no edge is ledger noise; the
                        # legacy fold never creates one either.
                        continue
                    causes.setdefault(key, set()).add(cause_here)
                    edges.append(
                        TopologyEdgeV3(
                            edge_id=f"te-inf::{parent}::{child}",
                            relation=RelationType.CONTAINS,
                            source_endpoint_id=parent,
                            target_endpoint_id=child,
                            lifecycle=TopologyEdgeState.CANDIDATE,
                            declared_by="inference",
                            beta=1.0,
                            beta_basis="unity",
                            evidence=TopologyEvidence(
                                support=1,
                                support_w=1.0,
                                last_support_at=at,
                                independent_causes=len(causes[key]),
                            ),
                            valid_from=at,
                            pinned=False,
                        )
                    )
                    by_index[key] = len(edges) - 1
                else:
                    edge = edges[position]
                    if edge.pinned or edge.lifecycle in _DORMANT:
                        continue
                    causes.setdefault(key, set()).add(cause_here)
                    evidence = edge.evidence.model_copy(
                        update={
                            **_fold_verdict(edge.evidence, verdict, at=at),
                            "independent_causes": len(causes[key]),
                        }
                    )
                    edges[position] = edge.model_copy(update={"evidence": evidence})

        # A confirmed single-sample pulse yields two synchronous verdicts, one
        # on the ON step and one on the OFF step, with distinct causes.
        for child, by_parent in (pulse_evidence or {}).items():
            if _suppressed(child):
                continue
            for parent, verdicts in by_parent.items():
                if _suppressed(parent) or _pair_blocked(parent, child):
                    continue
                key = (parent, child, _CONTAINS)
                position = by_index.get(key)
                for index, (verdict, _change, _child_delta) in enumerate(verdicts):
                    cause_here = f"{cause}::pulse::{parent}::{child}::{index}"
                    if position is None:
                        if verdict != "support":
                            continue
                        causes.setdefault(key, set()).add(cause_here)
                        edges.append(
                            TopologyEdgeV3(
                                edge_id=f"te-inf::{parent}::{child}",
                                relation=RelationType.CONTAINS,
                                source_endpoint_id=parent,
                                target_endpoint_id=child,
                                lifecycle=TopologyEdgeState.CANDIDATE,
                                declared_by="inference",
                                beta=1.0,
                                beta_basis="unity",
                                evidence=TopologyEvidence(
                                    support=1,
                                    support_w=1.0,
                                    last_support_at=at,
                                    independent_causes=len(causes[key]),
                                ),
                                valid_from=at,
                                pinned=False,
                            )
                        )
                        position = by_index[key] = len(edges) - 1
                    else:
                        edge = edges[position]
                        if edge.pinned or edge.lifecycle in _DORMANT:
                            continue
                        causes.setdefault(key, set()).add(cause_here)
                        evidence = edge.evidence.model_copy(
                            update={
                                **_fold_verdict(edge.evidence, verdict, at=at),
                                "independent_causes": len(causes[key]),
                            }
                        )
                        edges[position] = edge.model_copy(update={"evidence": evidence})

        # -- co-response without containment -> CORRELATED ------------------- #
        readers = sorted(decisive)
        for i, a in enumerate(readers):
            for b in readers[i + 1 :]:
                da, db = decisive[a][0], decisive[b][0]
                if da == 0.0 or db == 0.0 or (da > 0) != (db > 0):
                    continue
                pair = frozenset((a, b))
                # Co-response needs *raw* containment: it asks whether one
                # reader's level covers the other's at all, on any length of
                # series.  The new min-instant gate (E3 §1) would call a short
                # crossing "not evaluated" and let it become CORRELATED.
                contained = containment(list(absolute_now.get(a) or ()), list(absolute_now.get(b) or ()))[0] >= CONTAINMENT_MIN or containment(
                    list(absolute_now.get(b) or ()), list(absolute_now.get(a) or ())
                )[0] >= CONTAINMENT_MIN
                if contained:
                    continue
                co_response.setdefault(pair, set()).add(cause)
                co_events[pair] = co_events.get(pair, 0) + 1
                if co_events[pair] < MIN_CORRELATED_EVENTS:
                    continue
                src, dst = sorted(pair)
                if not any(
                    _relation(e.relation) == _CORRELATED
                    and {e.source_endpoint_id, e.target_endpoint_id} == pair
                    for e in edges
                ):
                    edges.append(
                        TopologyEdgeV3(
                            edge_id=f"te-cor::{src}::{dst}",
                            relation=RelationType.CORRELATED,
                            source_endpoint_id=src,
                            target_endpoint_id=dst,
                            lifecycle=TopologyEdgeState.CANDIDATE,
                            declared_by="inference",
                            evidence=TopologyEvidence(),
                            valid_from=at,
                            pinned=False,
                        )
                    )

        version_transitions, decisions, unidentified = self._settle(
            edges,
            causes,
            absolute_now,
            basis_now,
            meta_now,
            levels,
            structure=structure,
            health=health,
            declared_pairs=frozenset(blocked),
            declared_aliases=frozenset(canonical),
            pairs=pairs_now,
            leg_report=leg_report if leg_report is not None else self.leg_report,
            current_report=current_report if current_report is not None else self.current_report,
        )
        health = _prune_health(health, edges)

        view = replace(
            self,
            edges=tuple(edges),
            health=health,
            structure_decisions=decisions,
            leg_unidentified=unidentified,
            _placements=None,  # lazy: compute at first API read, not per fold
            _placement_inputs=(frozenset(blocked), canonical, pairs_now, cosampled),
            cosampled=cosampled if cosampled else self.cosampled,
            aligned=aligned_now,
            at=at,
            seen_groups=frozenset({*self.seen_groups, obs.group_id}),
            causes_by_edge={key: frozenset(values) for key, values in causes.items()},
            co_response_causes={key: frozenset(values) for key, values in co_response.items()},
            co_response_events=co_events,
            absolute=absolute_now,
            basis_by_reader=basis_now,
            channel_meta=meta_now,
            pairs=pairs_now,
            leg_report=leg_report if leg_report is not None else self.leg_report,
            current_report=current_report if current_report is not None else self.current_report,
        )
        if version_transitions:
            view = view._propose_version(version_transitions)
        return view

    def _compute_placements(
        self,
        edges: Sequence[TopologyEdgeV3],
        declared_pairs: frozenset[frozenset[str]],
        alias_canonical: Mapping[str, str],
        leg_unidentified: frozenset[str],
        pairs: PairAnalysis | None,
        cosampled: frozenset[frozenset[str]] = frozenset(),
    ) -> PlacementReport:
        """E6 §1: display placements from this settle's ledger (pure, no I/O).

        The per-child lift is read from the cached pair analysis when available;
        a pair with fewer than 8 steps falls back to the fixed low-plateau lift.
        Nothing here writes an edge or changes H.
        """
        from .topology_placement import MIN_STEPS_FOR_WLIFT

        wlift: dict[tuple[str, str], float] = {}
        n_by_child: dict[str, int] = {}
        readers = sorted({e.source_endpoint_id for e in edges} | {e.target_endpoint_id for e in edges})
        if pairs is not None:
            for edge in edges:
                if _relation(edge.relation) != _CONTAINS:
                    continue
                parent, child = edge.source_endpoint_id, edge.target_endpoint_id
                stats = pairs.stats_for(parent, child)
                if stats is None:
                    continue
                n_by_child[child] = max(n_by_child.get(child, 0), int(stats.n))
                if int(stats.n) >= MIN_STEPS_FOR_WLIFT:
                    wlift[(parent, child)] = float(stats.wlift)
        return compute_placements(
            edges,
            readers=readers,
            wlift_by_pair=wlift,
            n_by_child=n_by_child,
            leg_unidentified=leg_unidentified,
            declared_pairs=declared_pairs,
            alias_canonical=alias_canonical,
            cosampled=cosampled,
        )

    # -- lifecycle / versioning internals ----------------------------------- #
    def _settle(
        self,
        edges: list[TopologyEdgeV3],
        causes: Mapping[tuple[str, str, str], set[str]],
        absolute_now: Mapping[str, Sequence[float | None]],
        basis_now: Mapping[str, MeasurementBasis | str],
        meta_now: Mapping[str, Mapping[str, Any]],
        levels: Mapping[str, float] | None,
        *,
        structure: Mapping[str, Sequence[float | None]] | None = None,
        health: HealthBook | None = None,
        declared_pairs: frozenset[frozenset[str]] = frozenset(),
        declared_aliases: frozenset[str] = frozenset(),
        pairs: PairAnalysis | None = None,
        leg_report: Any | None = None,
        current_report: Any | None = None,
    ) -> tuple[list[str], dict[str, str], frozenset[str]]:
        """Apply lifecycle transitions in place; return the version-worthy ones.

        Also returns the structural decisions (child -> direct parent) to keep.
        Per edge, only the *net* change of this settle is reported (a stage may
        undo what an earlier one did -- the evidence rule re-confirms an edge the
        structural pass then demotes again -- which is no transition at all).

        Stages: (1) the evidence rule per edge -- the lifecycle is a pure
        function of recent weights, gates and the current state (ADR-0043);
        (2) edge health: BROKEN edges are demoted and leave the candidates;
        (3) one direct parent per child -- the structural pass on adequate
        aligned series, else the remembered decisions plus the lowest-level
        rule for undecided children.  Remembered decisions may choose a parent
        or demote a transitive grandparent, but never keep an edge STRUCTURAL
        whose evidence is below PROVISIONAL.
        """
        original = [_value(edge.lifecycle) for edge in edges]
        previous = {edge.edge_id: str(_value(edge.lifecycle)) for edge in edges}
        labels: dict[int, str] = {}
        decided = dict(self.structure_decisions)
        unidentified: frozenset[str] = frozenset()

        def note(stage: Sequence[str], before: Sequence[TopologyEdgeV3]) -> None:
            for index, (old, new) in enumerate(zip(before, edges, strict=True)):
                if _value(old.lifecycle) == _value(new.lifecycle):
                    continue
                labels[index] = new.edge_id
                for text in stage:
                    head = text.rsplit(": ", 1)[0]
                    if head == new.edge_id or head.startswith(new.edge_id + " ("):
                        labels[index] = head

        # (0) Declared PAIRED_LEG / SAME_CONDUCTOR authority (spec P4): an
        # inferred CONTAINS between the two legs of a declared pair is never
        # scored.  It becomes REJECTED with a `declared_pair` reason so the
        # transition text explains why.  Dormant (already REJECTED/RETIRED) and
        # user-pinned edges are left alone.
        if declared_pairs:
            for index, edge in enumerate(edges):
                if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                    continue
                if frozenset((edge.source_endpoint_id, edge.target_endpoint_id)) in declared_pairs:
                    if edge.lifecycle != TopologyEdgeState.REJECTED:
                        labels[index] = f"{edge.edge_id} (declared_pair)"
                    edges[index] = edge.model_copy(
                        update={"lifecycle": TopologyEdgeState.REJECTED}
                    )
        # A declared SAME_CONDUCTOR alias is the canonical conductor read again:
        # it is never a CONTAINS endpoint.  An inferred edge left over from
        # before the declaration is RETIRED (not wrong, redundant).
        if declared_aliases:
            for index, edge in enumerate(edges):
                if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                    continue
                if {edge.source_endpoint_id, edge.target_endpoint_id} & declared_aliases:
                    labels[index] = f"{edge.edge_id} (declared_alias)"
                    edges[index] = edge.model_copy(
                        update={"lifecycle": TopologyEdgeState.RETIRED}
                    )

        # (1) Evidence.
        for index, edge in enumerate(edges):
            if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                continue
            new_lifecycle = self._lifecycle(edge, causes, absolute_now, basis_now, meta_now, pairs=pairs)
            if (
                self.require_direction
                and new_lifecycle == TopologyEdgeState.STRUCTURAL
                and edge.declared_by != "user"
            ):
                new_lifecycle = self._direction_cap(edge, edges, new_lifecycle, absolute_now)
            if new_lifecycle != edge.lifecycle:
                labels[index] = edge.edge_id
                if new_lifecycle == TopologyEdgeState.CONFLICTED and (
                    _e4_containment(edge.source_endpoint_id, edge.target_endpoint_id, pairs)
                    == TopologyEdgeState.CONFLICTED
                    or _level_conflict(edge.source_endpoint_id, edge.target_endpoint_id, absolute_now)
                ):
                    labels[index] = f"{edge.edge_id} (containment)"
                edges[index] = edge.model_copy(update={"lifecycle": new_lifecycle})

        # (1c) E6 §3: current-based vetoes.  Raw current (factor-free,
        # polarity-free) on 1-minute means catches a reversed CT the power
        # containment veto misses.  A veto caps an inferred edge at CANDIDATE
        # (it never promotes or rejects); absent current is a silent skip.
        if current_report is not None:
            from .current_rules import child_above_parent, current_containment_veto

            for index, edge in enumerate(edges):
                if (
                    _relation(edge.relation) != _CONTAINS
                    or edge.pinned
                    or _value(edge.lifecycle) in _DORMANT
                ):
                    continue
                parent, child = edge.source_endpoint_id, edge.target_endpoint_id
                if (
                    current_containment_veto(current_report, parent, child)
                    or child_above_parent(current_report, parent, child)
                ) and _value(edge.lifecycle) in (
                    TopologyEdgeState.STRUCTURAL,
                    TopologyEdgeState.PROVISIONAL,
                ):
                    edges[index] = edge.model_copy(
                        update={"lifecycle": TopologyEdgeState.CANDIDATE}
                    )
                    labels[index] = f"{edge.edge_id} (current_veto)"

        # (2) Edge health (M3) BEFORE the structure: a confirmed inferred edge
        # whose parent stopped following its child is no longer structural, and
        # a BROKEN edge never competes for a child (it must not win and demote
        # its rival).
        broken: set[tuple[str, str]] = set(health.broken()) if health is not None else set()
        if health is not None and health.entries:
            reasons = dict(demotions(health, edges))
            if reasons:
                before = list(edges)
                stage = []
                for index, edge in enumerate(edges):
                    reason = reasons.get(edge.edge_id)
                    if reason is None:
                        continue
                    stage.append(
                        f"{edge.edge_id} ({reason}): {_value(edge.lifecycle)} -> "
                        f"{_value(TopologyEdgeState.PROVISIONAL)}"
                    )
                    edges[index] = edge.model_copy(
                        update={"lifecycle": TopologyEdgeState.PROVISIONAL}
                    )
                note(stage, before)
        for parent, child in broken:
            if decided.get(child) == parent:
                del decided[child]

        # (1b) Sibling-parent ambiguity (E3 §2): for a child with two
        # indistinguishable candidate parents, no single crossing may become
        # STRUCTURAL from evidence alone.  Cap every ambiguous edge at
        # PROVISIONAL and record the child in ``leg_unidentified``.  Runs after
        # the evidence lifecycles (stage 1) and health (stage 2) and before the
        # structural pass: a BROKEN or conflicted rival is asymmetric evidence
        # that breaks the tie, so it is never counted as an ambiguous candidate.
        ambiguous = _ambiguous_children(
            edges, absolute=absolute_now, broken=frozenset(broken), pairs=pairs
        )
        # E5 §4.3: leg evidence refines the ambiguity set and caps cross-leg
        # crossings.  All calls are pure functions of the aligned companion
        # analysis; no per-fold cost and no scenario knowledge.
        if leg_report is not None:
            from .leg_classifier import cross_leg_veto, same_leg_parent

            legs = getattr(leg_report, "leg", {}) or {}
            leg_ns = getattr(leg_report, "leg_n", {}) or {}
            tags = getattr(leg_report, "branch_tag", {}) or {}
            # (a) Cross-leg veto: a CONTAINS edge between L and R readers is
            # capped at CANDIDATE once both have enough steps.  A veto never
            # promotes or rejects; it only removes a too-strong edge.
            for index, edge in enumerate(edges):
                if _relation(edge.relation) != _CONTAINS or edge.pinned:
                    continue
                if edge.lifecycle in (TopologyEdgeState.REJECTED, TopologyEdgeState.RETIRED):
                    continue
                if cross_leg_veto(
                    str(legs.get(edge.source_endpoint_id, "unknown")),
                    str(legs.get(edge.target_endpoint_id, "unknown")),
                    int(leg_ns.get(edge.source_endpoint_id, 0)),
                    int(leg_ns.get(edge.target_endpoint_id, 0)),
                ):
                    before = edges[index]
                    if before.lifecycle in (
                        TopologyEdgeState.STRUCTURAL,
                        TopologyEdgeState.PROVISIONAL,
                    ):
                        edges[index] = before.model_copy(
                            update={"lifecycle": TopologyEdgeState.CANDIDATE}
                        )
                        labels[index] = f"{before.edge_id} (cross_leg_veto)"
            # (b) A child classified 'both' with two ambiguous parents keeps
            # E3's leg_unidentified, with the reason 240v in the text.
            # (c) A child with a definite leg breaks the tie in favour of the
            # single candidate parent on that leg (asymmetric evidence).
            for child in list(ambiguous):
                child_leg = str(legs.get(child, "unknown"))
                candidates = {
                    str(edge.source_endpoint_id): edge
                    for edge in edges
                    if _relation(edge.relation) == _CONTAINS
                    and edge.target_endpoint_id == child
                    and edge.lifecycle
                    in (TopologyEdgeState.PROVISIONAL, TopologyEdgeState.STRUCTURAL)
                }
                parent_legs = {p: str(legs.get(p, "unknown")) for p in candidates}
                winner = same_leg_parent(child_leg, parent_legs)
                if winner is not None:
                    # Keep only the same-leg parent structural; demote the
                    # cross-leg rival.  This is asymmetric, so not ambiguous.
                    for index, edge in enumerate(edges):
                        if edge.target_endpoint_id != child or _relation(edge.relation) != _CONTAINS:
                            continue
                        if edge.source_endpoint_id == winner:
                            continue
                        if edge.lifecycle == TopologyEdgeState.STRUCTURAL and not edge.pinned:
                            edges[index] = edge.model_copy(
                                update={"lifecycle": TopologyEdgeState.PROVISIONAL}
                            )
                            labels[index] = f"{edge.edge_id} (same_leg_parent)"
                    ambiguous = frozenset(p for p in ambiguous if p != child)
                elif child_leg == "both":
                    for index, edge in enumerate(edges):
                        if (
                            edge.target_endpoint_id == child
                            and _relation(edge.relation) == _CONTAINS
                            and edge.lifecycle == TopologyEdgeState.STRUCTURAL
                            and not edge.pinned
                        ):
                            labels[index] = f"{edge.edge_id} (leg_unidentified: 240v)"
            # (d) Branch-current tag is a tie-break only: when two candidate
            # parents of a child have equal standing, prefer one tagged
            # not_branch_circuit.  Implemented as a demotion of an untagged
            # rival when the other is tagged and neither is otherwise stronger.
            from .leg_classifier import prefer_branch_tag

            for child in {str(e.target_endpoint_id) for e in edges}:
                candidate_list = [
                    str(e.source_endpoint_id)
                    for e in edges
                    if _relation(e.relation) == _CONTAINS
                    and e.target_endpoint_id == child
                    and e.lifecycle == TopologyEdgeState.STRUCTURAL
                    and not e.pinned
                    and str(e.declared_by) == "inference"
                ]
                if len(candidate_list) != 2:
                    continue
                ordered = prefer_branch_tag(candidate_list, tags)
                if not tags.get(ordered[0], False) or tags.get(ordered[1], False):
                    continue
                for index, edge in enumerate(edges):
                    if (
                        edge.target_endpoint_id == child
                        and edge.source_endpoint_id == ordered[1]
                        and _relation(edge.relation) == _CONTAINS
                        and edge.lifecycle == TopologyEdgeState.STRUCTURAL
                    ):
                        edges[index] = edge.model_copy(
                            update={"lifecycle": TopologyEdgeState.PROVISIONAL}
                        )
                        labels[index] = f"{edge.edge_id} (branch_tie_break)"
        if ambiguous:
            before_amb = list(edges)
            amb_texts: list[str] = []
            for index, edge in enumerate(edges):
                if edge.target_endpoint_id not in ambiguous or not (
                    _relation(edge.relation) == _CONTAINS
                    and edge.lifecycle == TopologyEdgeState.STRUCTURAL
                    and not edge.pinned
                    and edge.declared_by == "inference"
                ):
                    continue
                amb_texts.append(
                    _transition_text(
                        edge,
                        TopologyEdgeState.PROVISIONAL,
                        f"leg_unidentified: true; {edge.source_endpoint_id} is ambiguous",
                    )
                )
                edges[index] = edge.model_copy(update={"lifecycle": TopologyEdgeState.PROVISIONAL})
            note(amb_texts, before_amb)

        # (3) One direct parent per child: a grandparent feed is transitive.
        # With adequate aligned series the structural pass decides (capacity,
        # paired legs, duplicates, regression); without them the remembered
        # decisions stand and only undecided children fall back to the
        # lowest-level rule.
        series = structure_series(structure)
        before = list(edges)
        if series is not None:
            # Structural decisions build on each other: a sub-panel confirmed
            # in one pass is the direct parent (nested) its circuits need in
            # the next, so a settle resolves until nothing changes (bounded).
            # Each pass restarts from the evidence's lifecycles plus the
            # structural confirmations of the passes before it, so a demotion
            # made before a sub-panel was known never sticks.
            analysis = self.structure_cache.get(series)
            promoted: set[str] = set()
            texts: list[str] = []
            result = None
            for _round in range(STRUCTURE_ROUNDS):
                trial = [
                    edge.model_copy(update={"lifecycle": TopologyEdgeState.STRUCTURAL})
                    if edge.edge_id in promoted
                    and edge.target_endpoint_id not in ambiguous
                    else edge
                    for edge in before
                ]
                result = resolve(
                    trial,
                    series,
                    levels=levels,
                    analysis=analysis,
                    decided=decided,
                    previous=previous,
                    excluded=broken,
                    allow_confirm=self._confirm_gate(absolute_now),
                    declared_pairs=declared_pairs,
                    leg_unidentified=ambiguous,
                    pairs_analysis=pairs,
                    current_kcl_ok=(
                        (lambda p, kids: set_kcl_ok(current_report, p, kids))
                        if current_report is not None
                        else None
                    ),
                )
                texts.extend(result.transitions)
                fresh = {
                    edge.edge_id
                    for old, edge in zip(before, result.edges, strict=True)
                    if _value(edge.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
                    and _value(old.lifecycle) != _value(TopologyEdgeState.STRUCTURAL)
                }
                if fresh <= promoted:
                    break
                promoted |= fresh
            assert result is not None
            edges[:] = result.edges
            # E3 §2 final authority: an ambiguous crossing is capped at
            # PROVISIONAL however the structural pass ranked it.  It only regains
            # STRUCTURAL once asymmetric evidence breaks the tie in a later
            # settle (the ambiguity set is then empty).
            if ambiguous:
                for index, edge in enumerate(edges):
                    if (
                        edge.target_endpoint_id in ambiguous
                        and edge.lifecycle == TopologyEdgeState.STRUCTURAL
                        and not edge.pinned
                        and edge.declared_by == "inference"
                    ):
                        edges[index] = edge.model_copy(
                            update={"lifecycle": TopologyEdgeState.PROVISIONAL}
                        )
            note(texts, before)
            decided = result.decided
            unidentified = frozenset({*result.leg_unidentified, *ambiguous})
        else:
            stage = self._apply_decisions(edges, decided, broken)
            if levels:
                stage += self._keep_direct_parents(edges, levels, skip=frozenset(decided))
            note(stage, before)
            # Without a structural pass the ambiguous set is the freshest
            # observation; keep it so the view always reports leg_unidentified.
            if ambiguous:
                unidentified = frozenset({*unidentified, *ambiguous})
        transitions = [
            f"{labels[index]}: {original[index]} -> {_value(edges[index].lifecycle)}"
            for index in sorted(labels)
            if original[index] != _value(edges[index].lifecycle)
        ]
        # Only a crossing into STRUCTURAL or CONFLICTED promotes a topology
        # version (T5 step 3).  CANDIDATE/PROVISIONAL are soft and accumulate
        # silently; one event must never publish a new version.
        return [
            text
            for text in transitions
            if text.endswith(f"-> {_value(TopologyEdgeState.STRUCTURAL)}")
            # A structural edge becoming CONFLICTED changes the topology; a
            # never-structural suggestion (CANDIDATE/PROVISIONAL) that is
            # contradicted changes nothing H uses, so it is no new version.
            or text.endswith(
                f"{_value(TopologyEdgeState.STRUCTURAL)} -> {_value(TopologyEdgeState.CONFLICTED)}"
            )
            # A confirmed grandparent edge demoted to a soft alternative.
            or text.endswith(
                f"{_value(TopologyEdgeState.STRUCTURAL)} -> {_value(TopologyEdgeState.PROVISIONAL)}"
            )
            # A structural edge rejected by a user declaration changes the
            # published topology (and carries the `declared_pair` reason).
            or text.endswith(
                f"{_value(TopologyEdgeState.STRUCTURAL)} -> {_value(TopologyEdgeState.REJECTED)}"
            )
        ], decided, unidentified

    def _confirm_gate(
        self, absolute: Mapping[str, Sequence[float | None]]
    ) -> Callable[[TopologyEdgeV3, Sequence[TopologyEdgeV3]], bool] | None:
        """The direction cap as a gate for structural confirmation (no cycles)."""
        if not self.require_direction:
            return None

        def gate(edge: TopologyEdgeV3, edges: Sequence[TopologyEdgeV3]) -> bool:
            capped = self._direction_cap(edge, edges, TopologyEdgeState.STRUCTURAL, absolute)
            return capped == TopologyEdgeState.STRUCTURAL

        return gate

    @staticmethod
    def _apply_decisions(
        edges: list[TopologyEdgeV3],
        decided: dict[str, str],
        broken: set[tuple[str, str]],
    ) -> list[str]:
        """Re-apply remembered decisions when no adequate series are available.

        A decided parent stays the direct parent: every other STRUCTURAL inferred
        candidate of its child drops to PROVISIONAL (as the structural pass did).
        A decision whose edge is gone, dormant, CANDIDATE/CONFLICTED or BROKEN
        is forgotten (``decided`` is updated in place).
        """
        live: dict[tuple[str, str], str] = {}
        for edge in edges:
            if _relation(edge.relation) == _CONTAINS and edge.lifecycle not in _DORMANT:
                live[(edge.source_endpoint_id, edge.target_endpoint_id)] = _value(edge.lifecycle)
        kept = (_value(TopologyEdgeState.STRUCTURAL), _value(TopologyEdgeState.PROVISIONAL))
        for child, parent in list(decided.items()):
            if live.get((parent, child)) not in kept or (parent, child) in broken:
                del decided[child]
        transitions: list[str] = []
        for index, edge in enumerate(edges):
            parent = decided.get(edge.target_endpoint_id)
            if (
                parent is None
                or edge.source_endpoint_id == parent
                or _relation(edge.relation) != _CONTAINS
                or edge.declared_by != "inference"
                or edge.pinned
                or edge.lifecycle != TopologyEdgeState.STRUCTURAL
            ):
                continue
            transitions.append(
                f"{edge.edge_id} (structure: direct parent is {parent}): "
                f"{_value(edge.lifecycle)} -> {_value(TopologyEdgeState.PROVISIONAL)}"
            )
            edges[index] = edge.model_copy(update={"lifecycle": TopologyEdgeState.PROVISIONAL})
        return transitions

    def merge_evidence(
        self,
        learned: TopologyInferenceView,
        *,
        levels: Mapping[str, float] | None = None,
    ) -> TopologyInferenceView:
        """Fold a ledger learned elsewhere (a history replay) into this one.

        Per CONTAINS pair the record with more decisive evidence (support +
        conflict) wins, with its independent causes.  Evidence is never summed:
        a replay of recorded history and the live ledger observe the same
        events, so adding them would count each event twice.  The merge is
        therefore idempotent -- running the same replay again changes nothing.
        Pinned, user-declared and dormant (REJECTED/RETIRED) pairs keep this
        ledger's record: user authority wins.  Lifecycles are then settled with
        the normal rules and a version is proposed exactly as for a live event.
        """
        edges = list(self.edges)
        by_key = {
            (e.source_endpoint_id, e.target_endpoint_id, _relation(e.relation)): i
            for i, e in enumerate(edges)
        }
        causes: dict[tuple[str, str, str], set[str]] = {
            key: set(values) for key, values in self.causes_by_edge.items()
        }
        for edge in learned.edges:
            if _relation(edge.relation) != _CONTAINS:
                continue
            key = (edge.source_endpoint_id, edge.target_endpoint_id, _CONTAINS)
            learned_causes = set(learned.causes_by_edge.get(key, ()))
            weight = edge.evidence.support + edge.evidence.conflict
            position = by_key.get(key)
            if position is None:
                if (
                    edge.evidence.support < MIN_SUPPORT_PROVISIONAL
                    and edge.evidence.conflict_ratio > CONFLICT_RATIO_CONFLICTED
                ):
                    # A replay counts every reader pair: one or two coincident
                    # steps against many contradictions is noise, not a pair
                    # worth a CONFLICTED ledger row (live: 625 of ~760 replay
                    # edges).  An absent pair says the same to every rule.
                    continue
                edges.append(edge.model_copy(update={"lifecycle": TopologyEdgeState.CANDIDATE}))
                by_key[key] = len(edges) - 1
                causes[key] = learned_causes
                continue
            current = edges[position]
            if current.pinned or current.declared_by == "user" or current.lifecycle in _DORMANT:
                continue
            if weight > current.evidence.support + current.evidence.conflict:
                edges[position] = current.model_copy(update={"evidence": edge.evidence})
                causes[key] = learned_causes
        version_transitions, decisions, _unidentified = self._settle(
            edges,
            causes,
            self.absolute,
            self.basis_by_reader,
            self.channel_meta,
            levels,
            structure=self._structure_source(self.aligned, self.absolute),
            health=self.health,
        )
        view = replace(
            self,
            edges=tuple(edges),
            structure_decisions=decisions,
            causes_by_edge={key: frozenset(values) for key, values in causes.items()},
            _placements=None,
        )
        if version_transitions:
            view = view._propose_version(version_transitions)
        return view

    @staticmethod
    def _structure_source(
        aligned: Mapping[str, Sequence[float | None]],
        absolute: Mapping[str, Sequence[float | None]],
    ) -> Mapping[str, Sequence[float | None]] | None:
        """The aligned series the structural pass runs on, or None.

        The structural pass must compare factor-scaled children (E4 §2 / spec
        P5): a ×2 child reported unscaled is double counted in every set-KCL
        check.  The runtime's ``absolute`` series have each child divided by
        its monitor factor, so they win when aligned and long enough; plain
        ``aligned`` is the fallback for callers that pass no scaled series.
        """
        return structure_series(absolute) or structure_series(aligned)

    def _multi_child_verdicts(
        self,
        decisive: Mapping[str, tuple[float, float | None]],
        observed: Mapping[str, Any] | None,
        group_moves: Mapping[str, float | None] | None,
        edges: Sequence[TopologyEdgeV3],
        analysis: StructureAnalysis | None,
    ) -> dict[tuple[str, str], str | None]:
        """``(parent, child) -> verdict`` for parents with several stepping children.

        Per parent with a usable move: ``known`` are stepping children with a
        STRUCTURAL edge from it, ``unknown`` every other stepping reader that is
        not the parent, not a confirmed ancestor of it, not its duplicate meter
        and not a confirmed descendant of a known child.  An unknown reader
        stepping like a known child (the same load seen one level down) keeps
        the one-to-one rule.  Unknown readers stepping alike -- a 240 V leg pair,
        or a load seen at two nested levels -- are one candidate, credited to
        each member: two legs are never summed into one parent.  Pairs absent
        from the result use the classic one-to-one rule.
        """
        if len(decisive) < 2:
            return {}
        from .topology_explain import (
            ChildStep,
            classify_with_explanation,
            match_tolerance,
            verify_known,
        )

        children_of: dict[str, set[str]] = {}
        parents_of: dict[str, set[str]] = {}
        for edge in edges:
            if (
                _relation(edge.relation) == _CONTAINS
                and _value(edge.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
            ):
                children_of.setdefault(edge.source_endpoint_id, set()).add(edge.target_endpoint_id)
                parents_of.setdefault(edge.target_endpoint_id, set()).add(edge.source_endpoint_id)

        def closure(start: str, links: Mapping[str, set[str]]) -> set[str]:
            seen: set[str] = set()
            stack = list(links.get(start, ()))
            while stack:
                node = stack.pop()
                if node not in seen:
                    seen.add(node)
                    stack.extend(links.get(node, ()))
            return seen

        # One class per reader pair (a 240 V leg pair is never also a twin).
        links = analysis.classify(edges) if analysis is not None else None
        floor = self.sigma_floor_w
        steps = {
            reader: ChildStep(reader, float(delta), float(sigma) if sigma is not None else floor)
            for reader, (delta, sigma) in decisive.items()
        }

        def alike(a: ChildStep, b: ChildStep) -> bool:
            if (a.delta > 0) != (b.delta > 0):
                return False
            return abs(a.delta - b.delta) <= match_tolerance(a.delta, [b.sigma], a.sigma)

        def group_units(readers: Sequence[str], *, alike_too: bool) -> list[list[str]]:
            """Detected 240 V pairs first, then (``alike_too``) readers stepping alike."""
            units: list[list[str]] = []
            placed: set[str] = set()
            if links is not None:
                for reader in readers:
                    partner = links.partner(reader)
                    if partner in readers and reader not in placed and partner not in placed:
                        units.append(sorted((reader, partner)))
                        placed.update((reader, partner))
            for reader in sorted(readers, key=lambda r: (steps[r].delta, r)):
                if reader in placed:
                    continue
                home = None
                if alike_too:
                    home = next((u for u in units if alike(steps[u[0]], steps[reader])), None)
                if home is None:
                    units.append([reader])
                else:
                    home.append(reader)
                placed.add(reader)
            return units

        def unit_step(unit: Sequence[str]) -> ChildStep:
            """One candidate: the members' mean delta and one member's sigma."""
            return ChildStep(
                "\x00".join(unit),
                sum(steps[m].delta for m in unit) / len(unit),
                max(steps[m].sigma for m in unit),
            )

        parents = sorted({*decisive, *(observed or {})})
        out: dict[tuple[str, str], str | None] = {}
        for parent in parents:
            move: float | None = None
            sigma_p: float = floor
            if group_moves and group_moves.get(parent) is not None:
                move = float(group_moves[parent])  # type: ignore[arg-type]
            if parent in decisive:
                delta, sigma = decisive[parent]
                sigma_p = float(sigma) if sigma is not None else floor
                move = float(delta) if move is None else move
            elif observed and parent in observed:
                sigma, measured = _observed_entry(observed[parent])
                sigma_p = max(sigma or floor, floor)
                if move is None:
                    move = 0.0 if measured is None else measured
            if move is None or not math.isfinite(move):
                continue
            ancestors = closure(parent, parents_of)
            twins = links.twins(parent) if links is not None else frozenset()
            candidates = [
                reader
                for reader in sorted(decisive)
                if reader != parent and reader not in ancestors and reader not in twins
            ]
            known = [reader for reader in candidates if reader in children_of.get(parent, ())]
            below_known: set[str] = set()
            for child in known:
                below_known |= closure(child, children_of)
            unknown = [
                reader
                for reader in candidates
                # A step inside the parent's noise cannot explain any part of it.
                if abs(steps[reader].delta) > _SIGMA_FACTOR * sigma_p
                and reader not in known
                and reader not in below_known
                and not any(alike(steps[reader], steps[child]) for child in known)
            ]
            if len(known) + len(unknown) < 2:
                continue
            unknown_units = group_units(unknown, alike_too=True)
            known_units = group_units(known, alike_too=False)
            unit_steps = [unit_step(unit) for unit in unknown_units]
            known_steps = [unit_step(unit) for unit in known_units]
            verdicts = classify_with_explanation(
                move, sigma_p, known=known_steps, unknown=unit_steps
            )
            for unit, step in zip(unknown_units, unit_steps, strict=True):
                for member in unit:
                    out[(parent, member)] = verdicts.get(step.reader_id)
            if known_steps:
                checked = verify_known(move, sigma_p, known=known_steps, unknown=unit_steps)
                for unit, step in zip(known_units, known_steps, strict=True):
                    for member in unit:
                        out[(parent, member)] = checked.get(step.reader_id)
        return out

    @staticmethod
    def _keep_direct_parents(
        edges: list[TopologyEdgeV3],
        levels: Mapping[str, float],
        *,
        skip: frozenset[str] = frozenset(),
    ) -> list[str]:
        """Demote all but the lowest-level STRUCTURAL inferred parent per child.

        A direct parent always carries no more power than the feed above it, so
        among confirmed parents the one with the lowest typical level is the
        direct one.  User-declared and pinned edges are never touched; a child
        whose parents' levels are unknown is left as it is, and so is a child in
        ``skip`` (its direct parent was decided structurally).
        """
        by_child: dict[str, list[int]] = {}
        for index, edge in enumerate(edges):
            if (
                edge.target_endpoint_id not in skip
                and _relation(edge.relation) == _CONTAINS
                and edge.declared_by == "inference"
                and not edge.pinned
                and _value(edge.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
            ):
                by_child.setdefault(edge.target_endpoint_id, []).append(index)
        transitions: list[str] = []
        for indices in by_child.values():
            if len(indices) < 2:
                continue
            known = [i for i in indices if levels.get(edges[i].source_endpoint_id) is not None]
            if len(known) < len(indices):
                continue
            keep = min(
                indices,
                key=lambda i: (float(levels[edges[i].source_endpoint_id]), edges[i].edge_id),
            )
            for i in indices:
                if i == keep:
                    continue
                edge = edges[i]
                transitions.append(
                    f"{edge.edge_id}: {_value(edge.lifecycle)} -> "
                    f"{_value(TopologyEdgeState.PROVISIONAL)}"
                )
                edges[i] = edge.model_copy(update={"lifecycle": TopologyEdgeState.PROVISIONAL})
        return transitions

    @staticmethod
    def _lifecycle(
        edge: TopologyEdgeV3,
        causes: Mapping[tuple[str, str, str], set[str]],
        absolute: Mapping[str, Sequence[float | None]],
        basis: Mapping[str, MeasurementBasis | str],
        meta: Mapping[str, Mapping[str, Any]],
        pairs: PairAnalysis | None = None,
    ) -> TopologyEdgeState:
        """Pure function of (recent evidence weights, gates, current state) (ADR-0043).

        An edge's state follows recent evidence in both directions.  ``sw`` /
        ``cw`` are the decaying support/conflict weights; ``r = cw/(sw+cw)``.
        Decay is by verdict count, not wall clock, so the same verdicts give the
        same state on a replay.  ``causes`` supplies the independent-cause gate
        (counted from the lifetime audit set, not decayed); for a user edge the
        cause gate is waived because the declaration is itself the cause.
        """
        evidence = edge.evidence
        # A legacy/unseeded row may carry only the lifetime counts (migration
        # v029 seeds weights, but a direct caller or an older payload need not).
        # Use those counts as the initial weights -- exactly the migration's
        # rule for n <= EVIDENCE_WINDOW -- so the lifecycle is never decided
        # from an empty ledger when the counts say otherwise.
        sw = evidence.support_w or float(evidence.support)
        cw = evidence.conflict_w or float(evidence.conflict)
        total = sw + cw
        ratio = evidence.conflict_ratio
        key = (edge.source_endpoint_id, edge.target_endpoint_id, _CONTAINS)
        n_causes = len(causes.get(key, ()))
        declared = edge.declared_by == "user"
        # Leaving the good states judges the recent conflict *rate* with a
        # one-sided Wilson lower bound over the decayed weights, not the point
        # ratio of a handful of verdicts (real-history replay: true pairs with few
        # events ended CONFLICTED on 2 of 6).  CONFLICTED is not absorbing: a
        # recovered edge falls through to PROVISIONAL / STRUCTURAL on the same
        # table.  A user edge moves to CONFLICTED under the same rule -- never
        # deleted -- so the user can re-affirm it (DEC-10 T7, ADR-0043).
        lower, _upper = _wilson_bounds(cw, total)
        if total >= MIN_EVENTS_CONFLICTED and lower > CONFLICT_RATIO_CONFLICTED:
            return TopologyEdgeState.CONFLICTED

        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        # E3 §1: a full day of level evidence that the child spends above its
        # candidate parent's level is decisive against containment, whatever the
        # step counters say.  A user edge may become CONFLICTED (never deleted).
        # E4 §2 replaces this fraction rule with the informative-bucket veto
        # (and the leaf zero-floor veto) wherever the pair analysis is available;
        # E3 stays the fallback.
        if pairs is None:
            if _level_conflict(parent, child, absolute):
                return TopologyEdgeState.CONFLICTED
        else:
            veto = _e4_containment(parent, child, pairs)
            if veto == TopologyEdgeState.CONFLICTED:
                return TopologyEdgeState.CONFLICTED

        # E6 §2 / ADR-0045: calibrated lift gates.  With >=8 child steps an
        # inferred edge must clear a minimum rarity-weighted lift (0.10 for
        # PROVISIONAL, 0.13 for STRUCTURAL) and the raw match floor; below 8
        # steps the gate is not evaluated and the verdict thresholds alone apply.
        stats = pairs.stats_for(parent, child) if pairs is not None else None
        n_steps = int(getattr(stats, "n", 0)) if stats is not None else 0
        wlift = float(getattr(stats, "wlift", 0.0)) if stats is not None else 0.0
        match = float(getattr(stats, "match", 0.0)) if stats is not None else 0.0
        provisional_lift_ok = (
            n_steps < LIFT_SUBSTITUTION_MIN_STEPS
            or (wlift >= WLIFT_MIN_PROVISIONAL and match >= STEP_MATCH_MIN)
        )
        structural_lift_ok = (
            n_steps < LIFT_SUBSTITUTION_MIN_STEPS
            or (
                wlift >= WLIFT_MIN_STRUCTURAL
                and match >= STEP_MATCH_MIN
                and lift_gate_passes(stats)
            )
        )
        # Lift substitution: many clean steps can reach PROVISIONAL even with
        # support_w below the 2-verdict floor (>=8 steps preserves the
        # one-event invariant).
        informative = int(getattr(stats, "informative_n", 0)) if stats is not None else 0
        violations = int(getattr(stats, "violations", 0)) if stats is not None else 0
        violation_rate = violations * 1000.0 / informative if informative else 0.0
        lift_substitution = (
            n_steps >= LIFT_SUBSTITUTION_MIN_STEPS
            and wlift >= WLIFT_MIN_STRUCTURAL
            and match >= STEP_MATCH_MIN
            and violation_rate < LIFT_SUBSTITUTION_MAX_VIOLATIONS_PER_1000
        )

        # The containment gate reads its fraction from the E4 cache (same number
        # as E3, computed once per rebuild) when available.
        if pairs is not None:
            fraction, comparable = _e4_fraction(parent, child, pairs, absolute)
            containment_ok = comparable < CONTAINMENT_MIN_INSTANTS or fraction >= CONTAINMENT_MIN
        else:
            containment_ok = _containment_gate(absolute.get(parent), absolute.get(child))
        gates_ok = (
            (declared or n_causes >= MIN_INDEPENDENT_CAUSES)
            and containment_ok
            and not _scaled_copy(absolute.get(parent), absolute.get(child))
            and _value(basis.get(parent, MeasurementBasis.CT_MEASURED)) != MeasurementBasis.UNKNOWN
            and _value(basis.get(child, MeasurementBasis.CT_MEASURED)) != MeasurementBasis.UNKNOWN
        )
        if gates_ok and structural_lift_ok and sw >= MIN_SUPPORT_STRUCTURAL and ratio < CONFLICT_RATIO_STRUCTURAL:
            return TopologyEdgeState.STRUCTURAL
        if (
            gates_ok
            and structural_lift_ok
            and _value(edge.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
            and sw >= KEEP_SUPPORT_STRUCTURAL
            and lower < KEEP_CONFLICT_RATIO_STRUCTURAL
        ):
            # E6 §2 hysteresis band, kept proportional: promotion needs
            # sw >= 3 with r <= 0.10, but an already-STRUCTURAL edge holds while
            # sw >= 2 and r <= 0.15.  It still falls when recent conflicts
            # accumulate, and the structural pass may demote it as transitive.
            return TopologyEdgeState.STRUCTURAL
        if declared:
            # A user declaration is a prior, not a terminal truth: with no
            # decisive contradiction (handled by the CONFLICTED bound above) it
            # stays STRUCTURAL even through quiet periods.  It must precede the
            # PROVISIONAL branch: E6 §2 raises the provisional conflict bound to
            # 0.34, so a recovering declared edge would otherwise be read as a
            # soft suggestion instead of the declaration it is.
            proposed = TopologyEdgeState.STRUCTURAL
        elif gates_ok and provisional_lift_ok and (
            (sw >= MIN_SUPPORT_PROVISIONAL and ratio < CONFLICT_RATIO_PROVISIONAL)
            or lift_substitution
        ):
            proposed = TopologyEdgeState.PROVISIONAL
        elif evidence.support >= MIN_SUPPORT_CANDIDATE:
            proposed = TopologyEdgeState.CANDIDATE
        else:
            proposed = edge.lifecycle
        # E4 §2: the informative-containment and leaf zero-floor vetoes cap an
        # inferred edge at CANDIDATE (a user declaration is never deleted).
        cap = _e4_containment(parent, child, pairs) if pairs is not None else None
        if (
            cap == TopologyEdgeState.CANDIDATE
            and proposed in (TopologyEdgeState.PROVISIONAL, TopologyEdgeState.STRUCTURAL)
            and edge.declared_by != "user"
        ):
            return TopologyEdgeState.CANDIDATE
        return proposed

    @staticmethod
    def _direction_cap(
        edge: TopologyEdgeV3,
        edges: Sequence[TopologyEdgeV3],
        proposed: TopologyEdgeState,
        absolute: Mapping[str, Sequence[float | None]],
    ) -> TopologyEdgeState:
        """Never CONFIRM a containment whose reverse is equally supported.

        Equal parent/child deltas support both ``p CONTAINS c`` and
        ``c CONTAINS p``; only a step the other reader did not share tells them
        apart (conflict on the wrong direction), and so does the absolute
        containment gate when level series are known.  Until the reverse edge
        is contradicted (conflict ratio >= the PROVISIONAL bound, or it fails
        containment) or absent, the direction is not identified: the edge stays PROVISIONAL (soft, never
        structural), so inference never promotes a containment cycle.
        """
        for other in edges:
            if (
                _relation(other.relation) == _CONTAINS
                and other.source_endpoint_id == edge.target_endpoint_id
                and other.target_endpoint_id == edge.source_endpoint_id
                and other.lifecycle not in _DORMANT
                and other.lifecycle != TopologyEdgeState.CONFLICTED
                and other.evidence.support > 0
                and other.evidence.conflict_ratio < DIRECTION_CAP_CONFLICT_MAX
                and _containment_gate(
                    absolute.get(other.source_endpoint_id), absolute.get(other.target_endpoint_id)
                )
            ):
                return TopologyEdgeState.PROVISIONAL
        return proposed

    def _propose_version(self, transitions: Sequence[str]) -> TopologyInferenceView:
        service = self.versions
        if service is None:
            return self
        # A version is structure: STRUCTURAL inferred edges plus anything a user
        # declared or pinned.  CANDIDATE/PROVISIONAL/CONFLICTED ledger entries
        # are evidence, not topology (design §5); proposing the whole ledger put
        # hundreds of single-event and contradicted pairs into every version.
        structural = [
            edge
            for edge in self.edges
            if edge.pinned
            or edge.declared_by != "inference"
            or _value(edge.lifecycle) == _value(TopologyEdgeState.STRUCTURAL)
        ]
        # User declarations (PAIRED_LEG, SAME_CONDUCTOR, pinned edges) live in
        # the active version, not in the evidence ledger.  Carry them forward:
        # a version built from the ledger alone silently dropped every
        # declaration at the first inferred promotion, and the fold then lost
        # its declared-pair/alias constraints (real-history replay, P4).
        ledger_ids = {edge.edge_id for edge in structural}
        structural.extend(
            edge for edge in self._declared_in_force(service) if edge.edge_id not in ledger_ids
        )
        current = self._active_edge_ids(service)
        if current is not None and {e.edge_id for e in structural} == current:
            # Same structure as the active version: nothing to publish.
            return self
        # Debounce inferred versions (ADR-0043 §4).  A change that *removes* an
        # edge from STRUCTURAL must publish at once (it changes what H uses
        # right now); a promotion may wait, and the transitions inside the
        # window are coalesced into the single version published when it opens.
        structural_value = _value(TopologyEdgeState.STRUCTURAL)
        removal = any(
            f": {structural_value} -> " in text for text in transitions
        )
        if not removal and self.last_version_at is not None:
            elapsed = (self.clock() - self.last_version_at).total_seconds()
            if 0.0 <= elapsed < TOPOLOGY_VERSION_DEBOUNCE_S:
                return self
        reason = "topology evidence ledger: " + "; ".join(transitions)
        version_id = service.propose_topology(structural, None)
        service.promote_topology(version_id, reason)
        return replace(self, version_id=version_id, last_version_at=self.clock())

    @staticmethod
    def _declared_in_force(service: Any) -> list[TopologyEdgeV3]:
        """The active version's user-declared or pinned edges (never inferred ones)."""
        try:
            triple = service.current()
            view = service.topology_view(triple.topology_version_id)
            edges = list(view.edges())
        except Exception:  # noqa: BLE001 - no active version: nothing to carry
            return []
        return [
            edge
            for edge in edges
            if isinstance(edge, TopologyEdgeV3)
            and (edge.pinned or str(edge.declared_by) != "inference")
        ]

    @staticmethod
    def _active_edge_ids(service: Any) -> set[str] | None:
        """Edge ids of the active topology version, or None when unknown."""
        try:
            triple = service.current()
            view = service.topology_view(triple.topology_version_id)
            return {edge.edge_id for edge in view.edges()}
        except Exception:  # noqa: BLE001 - unknown structure: propose as before
            return None


def _prune_health(book: HealthBook, edges: Sequence[TopologyEdgeV3]) -> HealthBook:
    """Drop health entries of edges the ledger no longer holds (bounded book)."""
    if not book.entries:
        return book
    live = {
        (edge.source_endpoint_id, edge.target_endpoint_id)
        for edge in edges
        if _relation(edge.relation) == _CONTAINS and edge.lifecycle not in _DORMANT
    }
    if all(key in live for key in book.entries):
        return book
    return book.prune(live)


# --------------------------------------------------------------------------- #
# HB §17 observation adapter (pure; P4 is not merged yet).
# --------------------------------------------------------------------------- #


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _records(observation: Any) -> list[DeltaObservation | Mapping[str, Any]]:
    if isinstance(observation, (EventGroup, DeltaObservation)):
        return [observation]
    if isinstance(observation, Mapping):
        for key in ("records", "observation", "observations"):
            if key in observation:
                return list(observation[key])
        return [observation]
    if isinstance(observation, Iterable):
        return list(observation)
    raise TypeError(f"unsupported observation: {type(observation).__name__}")


def infer_topology(
    observation: Any,
    *,
    clock: Callable[[], datetime] | None = None,
    versions: Any | None = None,
    sigma_floor_w: float = 2.0,
    step_threshold_w: float = 5.0,
    group_window_s: float = 60.0,
) -> TopologyInferenceView:
    """Build a :class:`TopologyInferenceView` from observation data.

    ``observation`` is either a sequence of HB §17 observation records (each
    carrying ``reader_samples``), a sequence of ``EventGroup`` contracts, or a
    single record/group.  Declared parents in the sample metadata become pinned
    STRUCTURAL CONTAINS edges (H2 writes user edges that way); everything else is
    inferred from signed deltas.  The function is pure for a fixed clock.
    """
    now = clock() if clock is not None else datetime(2026, 1, 1, tzinfo=UTC)
    records = _records(observation)

    # Non-record inputs (EventGroups) go straight through the ledger.
    if all(isinstance(r, (EventGroup, DeltaObservation)) for r in records):
        view = TopologyInferenceView(
            clock=clock or (lambda: now),
            versions=versions,
            sigma_floor_w=sigma_floor_w,
            at=now,
        )
        for group in records:
            view = view.observe_group(group)
        return view

    series: dict[str, list[tuple[datetime, float]]] = {}
    declared: dict[str, str] = {}
    timestamps: list[datetime] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        samples = record.get("reader_samples") or record.get("measurements") or {}
        if not isinstance(samples, Mapping):
            continue
        fallback = _parse_dt(record.get("timestamp")) or now
        for reader, sample in samples.items():
            if not isinstance(sample, Mapping):
                continue
            value = sample.get("value_w")
            if not _finite(value):
                value = sample.get("value")
            at = (
                _parse_dt(sample.get("state_timestamp"))
                or _parse_dt(sample.get("observed_at"))
                or fallback
            )
            if _finite(value):
                series.setdefault(reader, []).append((at, float(value)))
                timestamps.append(at)
            parent = sample.get("parent")
            if isinstance(parent, str) and parent:
                declared[reader] = parent

    # Align every reader onto the union of timestamps (forward fill), then
    # derive FINAL event groups from signed step deltas.
    grid = sorted(set(timestamps))
    aligned: dict[str, list[float]] = {}
    for reader, points in series.items():
        by_time = dict(points)
        last = math.nan
        values: list[float] = []
        for stamp in grid:
            if stamp in by_time:
                last = by_time[stamp]
            values.append(last)
        aligned[reader] = values

    events: list[tuple[datetime, str, float]] = []
    for reader, points in series.items():
        ordered = sorted(points)
        for (_previous, prev_value), (stamp, value) in zip(ordered, ordered[1:], strict=False):
            delta = value - prev_value
            if abs(delta) >= step_threshold_w:
                events.append((stamp, reader, delta))
    events.sort(key=lambda item: (item[0], item[1]))

    buckets: list[list[tuple[datetime, str, float]]] = []
    for event in events:
        if not buckets or (event[0] - buckets[-1][0][0]).total_seconds() > group_window_s:
            buckets.append([event])
        else:
            buckets[-1].append(event)

    edges: list[TopologyEdgeV3] = []
    for child, parent in sorted(declared.items()):
        edges.append(
            TopologyEdgeV3(
                edge_id=f"te-declared::{parent}::{child}",
                relation=RelationType.CONTAINS,
                source_endpoint_id=parent,
                target_endpoint_id=child,
                lifecycle=TopologyEdgeState.STRUCTURAL,
                declared_by="user",
                beta=1.0,
                beta_basis="unity",
                evidence=TopologyEvidence(),
                valid_from=now,
                pinned=False,
            )
        )
    edges.extend(detect_same_conductor(aligned, at=now))
    # E6 §4: also infer SAME_CONDUCTOR from non-co-sampled affine copies.
    # A declaration always wins: refuse an inferred edge for an already declared
    # pair in either direction.
    declared_pairs = {
        frozenset((e.source_endpoint_id, e.target_endpoint_id))
        for e in edges
        if _relation(e.relation) == _SAME_CONDUCTOR
    }
    for edge in detect_inferred_duplicates(aligned, at=now):
        if frozenset((edge.source_endpoint_id, edge.target_endpoint_id)) not in declared_pairs:
            edges.append(edge)

    view = TopologyInferenceView(
        edges=tuple(edges),
        clock=clock or (lambda: now),
        versions=versions,
        sigma_floor_w=sigma_floor_w,
        at=now,
        absolute=aligned,
    )
    for index, bucket in enumerate(buckets):
        deltas: dict[str, tuple[float, float | None]] = {}
        for _stamp, reader, delta in bucket:
            deltas[reader] = (delta, sigma_floor_w)
        view = view.observe_group(
            DeltaObservation(
                group_id=f"replay-group-{index}",
                deltas=deltas,
                at=bucket[0][0],
            )
        )
    return view


# --------------------------------------------------------------------------- #
# Measurement-based nesting proposals (T5, port of the retired nesting.py).
# --------------------------------------------------------------------------- #
# The legacy wizard engine is replaced by this one evidence ledger.  The numeric
# kernels (containment / duplicate / closure / node grouping) already live in
# the pure ``nesting_math`` module; only the orchestration is here.  Nothing in
# this section writes a wiring fact: every result is a *candidate* that a user
# must confirm, and the parent field is named ``parent_id`` (not the retired
# pointer-based ``parent_ct_id``).


@dataclass(frozen=True, slots=True)
class NestingCandidate:
    """A proposed ``child`` fed by ``parent``, with the evidence for it."""

    child_reader_id: str
    parent_id: str
    kind: str  # "nested" | "duplicate"
    evidence: dict[str, Any] = field(default_factory=dict)
    # ADR-0043: there is no topology confirmation; a candidate is simply a
    # proposal whose edge state follows recent evidence (HB §4.3, DEC-10).

    def as_dict(self) -> dict[str, Any]:
        return {
            "child_reader_id": self.child_reader_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "evidence": dict(self.evidence),
            "status": "candidate",
        }


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _ne_node_series(
    nodes: Mapping[str, MeasurementNode],
    series: Mapping[str, Sequence[float]],
) -> dict[str, list[float]]:
    """Sum each node's member series into one watts series (pure)."""
    out: dict[str, list[float]] = {}
    for node_id, node in nodes.items():
        present = [m for m in node.members if m in series]
        if not present:
            continue
        length = len(series[present[0]])
        if any(len(series[m]) != length for m in present):
            raise ValueError("member series must have the same length")
        total = np.zeros(length, dtype=np.float64)
        seen = np.zeros(length, dtype=bool)
        for member in present:
            values = _as_float_array(series[member])
            finite = np.isfinite(values)
            total += np.where(finite, values, 0.0)
            seen |= finite
        out[node_id] = np.where(seen, total, math.nan).tolist()
    return out


def _ne_node_own_loads(
    node: MeasurementNode,
    direct_loads: Mapping[str, Sequence[float | None]],
) -> list[float | None] | None:
    """Sum a node's members' own directly-measured loads (unknown stays None)."""
    contributing = [direct_loads[m] for m in node.members if m in direct_loads]
    if not contributing:
        return None
    length = len(contributing[0])
    if any(len(values) != length for values in contributing):
        raise ValueError("direct_load series must have the same length")
    total = np.zeros(length, dtype=np.float64)
    seen = np.zeros(length, dtype=bool)
    for values in contributing:
        array = _as_float_array(values)
        finite = np.isfinite(array)
        total = np.where(total == 0.0, 0.0, total)
        total = total + np.where(finite, array, 0.0)
        seen |= finite
    totals = total.tolist()
    return [
        value if covered else None
        for value, covered in zip(totals, seen.tolist(), strict=True)
    ]


def _ne_declared_pair(a: str, b: str, channels: Mapping[str, Mapping[str, Any]]) -> bool:
    pa = str(channels.get(a, {}).get("paired_ct_id") or "")
    pb = str(channels.get(b, {}).get("paired_ct_id") or "")
    return bool((pa and pa == b) or (pb and pb == a))


def _ne_ancestor_of(
    child: str, candidate: str, channels: Mapping[str, Mapping[str, Any]]
) -> bool:
    """True when ``candidate`` is a declared ancestor of ``child``."""
    seen: set[str] = set()
    current = str(channels.get(child, {}).get("parent_id") or "")
    while current and current not in seen:
        if current == candidate:
            return True
        seen.add(current)
        current = str(channels.get(current, {}).get("parent_id") or "")
    return False


def _ne_resolve_alias(cid: str, aliases: Mapping[str, str]) -> str:
    seen: set[str] = set()
    while cid in aliases and cid not in seen:
        seen.add(cid)
        cid = aliases[cid]
    return cid


def _ne_leg_matches(parent: MeasurementNode, child_member: str, channels: Mapping[str, Any]) -> str:
    """Pick the parent member on the same declared phase, else the first."""
    phase = str(channels.get(child_member, {}).get("phase") or "unknown")
    for member in parent.members:
        if str(channels.get(member, {}).get("phase") or "unknown") == phase:
            return member
    return parent.members[0]


def infer_nesting(
    channels: Mapping[str, Mapping[str, Any]],
    series: Mapping[str, Sequence[float]],
    *,
    container_ct_ids: Sequence[str] | None = None,
    direct_loads: Mapping[str, Sequence[float | None]] | None = None,
    containment_min: float = CONTAINMENT_MIN,
    excitation_min_ratio: float = EXCITATION_MIN_RATIO,
    closure_min: float = CLOSURE_MIN,
    tol_w: float = CONTAINMENT_TOL_W,
    min_parent_w: float = CONTAINMENT_MIN_PARENT_W,
    report: dict[str, Any] | None = None,
) -> list[NestingCandidate]:
    """Propose nesting / duplicate relationships from aligned measurements.

    ``channels`` maps a reader id to a mapping carrying ``ct_type``, ``phase``,
    ``paired_ct_id`` and optionally ``parent_id`` (a declared wiring fact).
    ``series`` maps the same ids to equally long, equally indexed watt series;
    alignment is the caller's responsibility.  Only ``main`` readers may be
    proposed as a child (a panel feed's level is measurable, a load's is not).
    Every result is marked candidate and needs user confirmation — this never
    writes a wiring fact.  Pure: no DB, no wall clock.
    """
    diagnostics: dict[str, Any] = report if report is not None else {}
    diagnostics.setdefault("ambiguous", [])
    diagnostics.setdefault("suppressed", [])
    diagnostics.setdefault("not_inferable", [])

    candidates: list[NestingCandidate] = []
    ids = [cid for cid in sorted(series) if cid in channels]
    if container_ct_ids is None:
        containers = {
            cid
            for cid in ids
            if str(channels[cid].get("ct_type") or "") in ("main", "branch")
        }
    else:
        containers = {cid for cid in container_ct_ids if cid in series and cid in channels}

    nodes = build_nodes({cid: channels[cid] for cid in ids})
    diagnostics["nodes"] = [node.as_dict() for node in nodes.values()]
    node_series = _ne_node_series(nodes, series)
    node_arrays = {
        node_id: np.asarray(values, dtype=np.float64)
        for node_id, values in node_series.items()
    }
    signed_peak = {node_id: _finite_max(values) for node_id, values in node_arrays.items()}
    activity_peak = {
        node_id: _finite_abs_max(values) for node_id, values in node_arrays.items()
    }

    # -- duplicates first: they change how every other pair reads -----------
    node_ids = sorted(node_series)
    aliases: dict[str, str] = {}
    duplicate_pairs: list[tuple[str, str, DuplicateEvidence]] = []
    for i, a in enumerate(node_ids):
        node_a = nodes[a]
        for b in node_ids[i + 1 :]:
            node_b = nodes[b]
            if set(node_a.members) & set(node_b.members):
                continue
            if not (set(node_a.members) & containers or set(node_b.members) & containers):
                continue
            if activity_peak[a] < ACTIVITY_MIN_W and activity_peak[b] < ACTIVITY_MIN_W:
                continue
            if any(
                _ne_declared_pair(x, y, channels)
                for x in node_a.members
                for y in node_b.members
            ):
                continue
            if any(
                _ne_ancestor_of(x, y, channels) for x in node_a.members for y in node_b.members
            ):
                continue
            if any(
                _ne_ancestor_of(y, x, channels) for x in node_a.members for y in node_b.members
            ):
                continue
            evidence = duplicate_evidence(node_arrays[a], node_arrays[b])
            if evidence is None:
                continue
            if evidence.activity_ratio < DUPLICATE_ACTIVITY_RATIO_MIN:
                continue
            if evidence.rel_diff > DUPLICATE_REL_DIFF_MAX:
                continue
            if evidence.ratio > DUPLICATE_RATIO_MAX:
                continue
            canonical, alias = sorted((a, b))
            duplicate_pairs.append((canonical, alias, evidence))
            aliases[alias] = canonical

    for canonical, alias, evidence in duplicate_pairs:
        root = _ne_resolve_alias(canonical, aliases)
        aliases[alias] = root
        parent_node = nodes[root]
        for member in nodes[alias].members:
            candidates.append(
                NestingCandidate(
                    child_reader_id=member,
                    parent_id=_ne_leg_matches(parent_node, member, channels),
                    kind="duplicate",
                    evidence={
                        "activity_ratio": round(evidence.activity_ratio, 4),
                        "median_relative_difference": round(evidence.rel_diff, 4),
                        "magnitude_ratio": round(evidence.ratio, 3),
                        "median_w": [
                            round(evidence.median_a_w, 1),
                            round(evidence.median_b_w, 1),
                        ],
                        "joint_instants": evidence.joint_instants,
                        "child_node": alias,
                        "parent_node": root,
                        "reason": (
                            "both channels are active together whenever either is, "
                            "and report the same magnitude: one wire measured twice, "
                            "not two feeds"
                        ),
                    },
                )
            )

    def canonical_node(node_id: str) -> str:
        return _ne_resolve_alias(node_id, aliases)

    # -- containment: a panel feed that never exceeds a container -----------
    canonical_containers = sorted(
        {
            canonical_node(n)
            for n in node_ids
            if set(nodes[n].members) & containers
        }
    )
    proposals: dict[str, list[tuple[str, float, int]]] = {}
    nestable_child_types = frozenset({"main"})
    for child in node_ids:
        child_node = nodes[child]
        if child_node.ct_type not in nestable_child_types:
            diagnostics["not_inferable"].append(
                {
                    "reader_id": child_node.members[0] if child_node.members else child,
                    "node_id": child,
                    "reason": "load_level_not_measurable",
                }
            )
            continue
        if any(str(channels.get(m, {}).get("parent_id") or "") for m in child_node.members):
            diagnostics["suppressed"].append(
                {
                    "reader_id": child_node.members[0],
                    "node_id": child,
                    "reason": "parent_already_declared",
                }
            )
            continue
        peak = signed_peak[child]
        if peak <= 0.0:
            diagnostics["suppressed"].append(
                {"reader_id": child_node.members[0], "node_id": child, "reason": "never_active"}
            )
            continue
        for parent in canonical_containers:
            if parent == canonical_node(child):
                continue
            parent_series = node_arrays.get(parent)
            if parent_series is None or len(parent_series) != len(node_arrays[child]):
                continue
            fraction, comparable = containment(
                parent_series, node_arrays[child], tol_w=tol_w, min_outer_w=min_parent_w
            )
            if comparable == 0 or fraction < containment_min:
                continue
            parent_peak = signed_peak[parent]
            if parent_peak <= 0.0 or peak / parent_peak < excitation_min_ratio:
                continue
            proposals.setdefault(child, []).append((parent, fraction, comparable))

    # -- place each child at a level, using closure where it exists ---------
    for child in sorted(proposals):
        child_node = nodes[child]
        child_series = node_series[child]
        supporting: list[dict[str, Any]] = []
        for parent, fraction, comparable in proposals[child]:
            own = _ne_node_own_loads(nodes[parent], direct_loads or {})
            closure: tuple[float, int, float] | None = None
            if own is not None and len(own) == len(child_series):
                accounted = [
                    (None if not _finite(o) or not _finite(c) else float(o) + float(c))
                    for o, c in zip(own, child_series, strict=True)
                ]
                closure = closure_fraction(node_series[parent], accounted)
            if closure is not None and closure[1] and closure[0] < closure_min:
                diagnostics["suppressed"].append(
                    {
                        "reader_id": child_node.members[0],
                        "node_id": child,
                        "reason": "closure_rejected",
                        "parent_node": parent,
                        "closure_fraction": round(closure[0], 4),
                    }
                )
                continue
            supporting.append(
                {"parent": parent, "containment": fraction, "instants": comparable, "closure": closure}
            )
        if not supporting:
            continue

        def rank(row: dict[str, Any]) -> tuple[float, int, str]:
            value = row["closure"]
            if value is None or not value[1]:
                return (float("inf"), 0, row["parent"])
            return (value[2], -value[1], row["parent"])

        ranked = sorted(supporting, key=rank)
        best = ranked[0]
        closure = best["closure"]
        parent_node = nodes[best["parent"]]
        if len(ranked) > 1 and rank(best)[0] == float("inf"):
            diagnostics["ambiguous"].append(
                {
                    "reader_id": child_node.members[0],
                    "node_id": child,
                    "candidate_parents": sorted(row["parent"] for row in ranked),
                    "reason": "no closure evidence to choose a level",
                }
            )
            continue
        evidence: dict[str, Any] = {
            "containment": round(best["containment"], 4),
            "instants": best["instants"],
            "child_node": child,
            "parent_node": best["parent"],
            "reason": (
                "child stays inside the parent at every comparable instant and "
                "carries enough load for that to be evidence rather than idling"
            ),
        }
        if closure is not None and closure[1]:
            evidence["closure_fraction"] = round(closure[0], 4)
            evidence["closure_instants"] = closure[1]
            evidence["closure_median_remainder_w"] = round(closure[2], 1)
        if child_node.is_assumed or parent_node.is_assumed:
            evidence["assumed_grouping"] = sorted(
                {node.grouping for node in (child_node, parent_node) if node.is_assumed}
            )
        if len(ranked) > 1:
            evidence["other_containers"] = sorted(row["parent"] for row in ranked[1:])
            evidence["containment_ambiguous"] = True
        for member in child_node.members:
            candidates.append(
                NestingCandidate(
                    child_reader_id=member,
                    parent_id=_ne_leg_matches(parent_node, member, channels),
                    kind="nested",
                    evidence=dict(evidence),
                )
            )
    candidates.sort(key=lambda c: (c.kind, c.child_reader_id, c.parent_id))
    return candidates
