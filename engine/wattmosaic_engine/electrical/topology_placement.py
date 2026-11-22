"""E6 §1: exactly one placement per non-root reader, shown at any confidence.

A placement is **display and diagnostic only**: it never changes the observation
operator H, which still uses STRUCTURAL CONTAINS edges.  It is computed per
settle from the same evidence ledger the lifecycles use, and lives in API payload
dicts (no contract change).

The rules (spec ``E6-placement-current-sync.md`` §1):

1. Candidate parents of a child are readers with a CONTAINS edge that is not
   CONFLICTED/REJECTED/RETIRED, that no veto blocks, and that is not a declared
   alias or declared paired leg of the child.
2. ``score = max(wlift, 0) * (s + 0.5) / (s + k + 1)`` where ``s``/``k`` are the
   edge's support/conflict counts.  With fewer than 8 steps ``wlift`` is 0.05.
3. Prefer the deeper parent in a CONTAINS chain whose links clear PROVISIONAL.
4. Exactly one parent per child; the placement is acyclic.
5. Confidence band from the edge state and the score (high / medium / low /
   unplaced).
6. A leg-unidentified child gets ``both_legs`` with its two sibling parents,
   band ``medium`` and reason ``leg_unidentified``.

The module is pure: no clock, no I/O, no database and no scenario names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import TopologyEdgeState, TopologyEdgeV3

__all__ = [
    "BAND_HIGH",
    "BAND_MEDIUM",
    "BAND_LOW",
    "BAND_UNPLACED",
    "LOW_WLIFT",
    "MIN_STEPS_FOR_WLIFT",
    "SCORE_HIGH",
    "SCORE_MEDIUM",
    "Placement",
    "PlacementReport",
    "apply_user_placements",
    "exclude_readers",
    "compute_placements",
]

# Band names (API payload vocabulary).
BAND_HIGH = "high"
BAND_MEDIUM = "medium"
BAND_LOW = "low"
BAND_UNPLACED = "unplaced"

#: E6 §1: when the pair analysis has fewer than 8 steps, use this fixed lift.
MIN_STEPS_FOR_WLIFT = 8
LOW_WLIFT = 0.05
#: Confidence band cut-offs on the placement score.
SCORE_HIGH = 0.15
SCORE_MEDIUM = 0.05

_DORMANT = frozenset((TopologyEdgeState.REJECTED, TopologyEdgeState.RETIRED))
_BLOCKED = _DORMANT | frozenset((TopologyEdgeState.CONFLICTED,))
_SOFT = frozenset((TopologyEdgeState.PROVISIONAL, TopologyEdgeState.STRUCTURAL))


@dataclass(frozen=True, slots=True)
class Placement:
    """One child's placement (an API payload dict in :meth:`as_dict`)."""

    child: str
    parent: str | None = None
    band: str = BAND_UNPLACED
    score: float = 0.0
    reason: str | None = None
    alternatives: tuple[tuple[str, float], ...] = ()
    #: Set for a leg-unidentified child: both sibling parents are listed.
    both_legs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "band": self.band,
            "score": round(self.score, 4),
            "reason": self.reason,
            "alternatives": [
                {"parent": parent, "score": round(score, 4)}
                for parent, score in self.alternatives
            ],
            "both_legs": list(self.both_legs),
        }


@dataclass(slots=True)
class PlacementReport:
    """All placements at one settle, keyed by child reader id."""

    by_child: dict[str, Placement] = field(default_factory=dict)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {child: placement.as_dict() for child, placement in self.by_child.items()}


def _value(item: Any) -> Any:
    return getattr(item, "value", item) if item is not None else None


def _score(edge: TopologyEdgeV3, wlift: float) -> float:
    """E6 §1 step 2: ``max(wlift, 0) * (s + 0.5) / (s + k + 1)``.

    ``s``/``k`` are the edge's support / conflict counts (lifetime counters,
    which are the evidence available for every edge including CANDIDATE ones).
    """
    evidence = edge.evidence
    s, k = float(evidence.support), float(evidence.conflict)
    return max(float(wlift), 0.0) * (s + 0.5) / (s + k + 1.0)


def _deeper_parent(
    child: str,
    candidates: Mapping[str, TopologyEdgeV3],
    soft_parents: Mapping[str, Sequence[str]],
) -> str | None:
    """E6 §1 step 3: the deepest parent in a soft CONTAINS chain.

    If the best-scored candidate ``P`` has a candidate parent ``Q`` whose link
    to ``P`` is at least PROVISIONAL (and is not the child itself), ``Q`` is
    deeper in the chain, so the child is placed under ``P`` already — pick the
    deepest such ancestor.  This only collapses a grandparent/child chain; two
    unrelated roots are never merged here.
    """
    best = max(candidates, key=lambda p: (candidates[p].evidence.support, p))

    def chain_parent(reader: str) -> str | None:
        for parent in soft_parents.get(reader, ()):
            if parent in candidates and parent != child:
                return parent
        return None

    seen = {best}
    current = best
    while True:
        parent = chain_parent(current)
        if parent is None or parent in seen:
            return current
        seen.add(parent)
        current = parent


def compute_placements(
    edges: Sequence[TopologyEdgeV3],
    *,
    readers: Sequence[str] = (),
    wlift_by_pair: Mapping[tuple[str, str], float] | None = None,
    n_by_child: Mapping[str, int] | None = None,
    leg_unidentified: frozenset[str] = frozenset(),
    declared_pairs: frozenset[frozenset[str]] = frozenset(),
    alias_canonical: Mapping[str, str] | None = None,
    cosampled: frozenset[frozenset[str]] = frozenset(),
) -> PlacementReport:
    """Compute one placement per non-root reader from the current ledger.

    ``edges`` is the full typed edge set.  ``wlift_by_pair`` maps
    ``(parent, child)`` to the E4 rarity-weighted lift; a pair with fewer than
    :data:`MIN_STEPS_FOR_WLIFT` steps (or absent from the mapping) uses
    :data:`LOW_WLIFT`.  ``n_by_child`` carries the step counts.  The other
    arguments mirror the ledger's veto inputs so an alias or a declared paired
    leg is never presented as a candidate parent.  E6 §6 contrast ranking
    multiplies each co-sampled candidate's score by ``(1 - sync_conflict_ratio)``
    (the edge's conflict fraction), preferring the lower-conflict parent.

    A child in ``leg_unidentified`` with two soft candidates is reported as
    ``both_legs`` (band medium, reason ``leg_unidentified``) instead of picking
    one arbitrary parent.  Acyclic placement is enforced by rejecting any
    candidate that is a descendant of the child through a soft CONTAINS chain.
    """
    wlift_by_pair = wlift_by_pair or {}
    n_by_child = n_by_child or {}
    aliases = set(alias_canonical or {})

    contains = [
        edge
        for edge in edges
        if _value(edge.relation) == "contains" and _value(edge.lifecycle) not in _DORMANT
    ]
    children = set(readers) | {
        str(edge.target_endpoint_id) for edge in contains
    }
    roots = {str(edge.source_endpoint_id) for edge in contains} - children
    # A reader with no declared parent and no children is also a root (it is
    # not a child to place), even when it only appears in ``readers``.
    has_parent = {str(edge.target_endpoint_id) for edge in contains}
    all_readers = set(readers) | {str(e.source_endpoint_id) for e in contains} | children

    by_child: dict[str, list[TopologyEdgeV3]] = {}
    soft_children: dict[str, list[str]] = {}
    for edge in contains:
        by_child.setdefault(str(edge.target_endpoint_id), []).append(edge)
        if _value(edge.lifecycle) in _SOFT:
            soft_children.setdefault(str(edge.source_endpoint_id), []).append(
                str(edge.target_endpoint_id)
            )

    # Transitive descendants over soft CONTAINS edges, computed once (O(E+V))
    # instead of a per-child edge scan.  Reused by the acyclic guard.
    descendants_of: dict[str, set[str]] = {}

    def descendants(child: str) -> set[str]:
        cached = descendants_of.get(child)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack = list(soft_children.get(child, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(soft_children.get(node, ()))
        descendants_of[child] = seen
        return seen

    # The deeper-parent rule needs the soft parents of a reader; build once.
    soft_parents: dict[str, list[str]] = {}
    for edge in contains:
        if _value(edge.lifecycle) in _SOFT:
            soft_parents.setdefault(str(edge.target_endpoint_id), []).append(
                str(edge.source_endpoint_id)
            )

    report = PlacementReport()
    for child in sorted(children):
        if child in roots or child not in has_parent:
            # A root (or a reader that never appears as a child) is unplaced by
            # definition; still report it so the panel can list unplaced readers.
            report.by_child[child] = Placement(child=child)
            continue

        def blocked_pair(p: str, child: str = child) -> bool:
            return frozenset((p, child)) in declared_pairs

        below = descendants(child)
        candidates: dict[str, TopologyEdgeV3] = {}
        for edge in by_child.get(child, ()):
            parent = str(edge.source_endpoint_id)
            if _value(edge.lifecycle) in _BLOCKED:
                continue
            if parent == child or parent in below:
                continue  # never place a reader under its own descendant
            if parent in aliases or blocked_pair(parent):
                continue
            candidates[parent] = edge

        if not candidates:
            report.by_child[child] = Placement(child=child)
            continue

        soft = {
            parent: edge for parent, edge in candidates.items()
            if _value(edge.lifecycle) in _SOFT
        }

        # E6 §1 step 6: leg-unidentified children get both sibling parents.
        if child in leg_unidentified and len(soft) >= 2:
            both = tuple(sorted(soft)[:2])
            report.by_child[child] = Placement(
                child=child,
                parent=None,
                band=BAND_MEDIUM,
                score=0.0,
                reason="leg_unidentified",
                alternatives=tuple(
                    (parent, _score(soft[parent], wlift_by_pair.get((parent, child), LOW_WLIFT)))
                    for parent in sorted(soft)
                ),
                both_legs=both,
            )
            continue

        n = int(n_by_child.get(child, 0))

        def wlift_of(p: str, child: str = child, n: int = n) -> float:
            return wlift_by_pair.get((p, child), LOW_WLIFT if n < MIN_STEPS_FOR_WLIFT else 0.0)

        scored: dict[str, float] = {}
        for parent, edge in candidates.items():
            score = _score(edge, wlift_of(parent))
            # E6 §6 contrast ranking: a co-sampled parent with many synchronous
            # conflicts is a worse explanation than a quieter rival.  A ratio of
            # 0 (no conflict) leaves the score unchanged; 1 zeroes it.
            if frozenset((parent, child)) in cosampled:
                ev = edge.evidence
                decisive = float(ev.support_w + ev.conflict_w) or float(ev.support + ev.conflict)
                conflict_ratio = (
                    float(ev.conflict_w) / float(ev.support_w + ev.conflict_w)
                    if float(ev.support_w + ev.conflict_w) > 0
                    else (float(ev.conflict) / decisive if decisive > 0 else 0.0)
                )
                score *= 1.0 - min(1.0, max(0.0, conflict_ratio))
            scored[parent] = score
        best = max(scored, key=lambda p: (scored[p], p))

        # Prefer the deeper parent in a soft chain; its score is the same edge's
        # score, so the chain rule never contradicts the argmax on unrelated
        # roots (it only resolves a grandparent/child chain).
        if best in soft:
            deep = _deeper_parent(child, soft, soft_parents)
            if deep in scored and scored[deep] >= scored[best]:
                best = deep

        edge = candidates[best]
        score = scored[best]
        structural = _value(edge.lifecycle) == TopologyEdgeState.STRUCTURAL
        if structural or score >= SCORE_HIGH:
            band = BAND_HIGH
        elif score >= SCORE_MEDIUM:
            band = BAND_MEDIUM
        else:
            band = BAND_LOW
        alternatives = tuple(
            (parent, value)
            for parent, value in sorted(scored.items(), key=lambda item: (-item[1], item[0]))
            if parent != best
        )
        reason = "structural" if structural else ("low_confidence" if band == BAND_LOW else None)
        report.by_child[child] = Placement(
            child=child,
            parent=best,
            band=band,
            score=score,
            reason=reason,
            alternatives=alternatives,
        )

    # Keep the report deterministic and complete over the declared readers.
    for reader in all_readers:
        report.by_child.setdefault(reader, Placement(child=reader))
    return report


def _is_contains(edge: TopologyEdgeV3) -> bool:
    return _value(edge.relation) == "contains"


def _in_force(edge: TopologyEdgeV3) -> bool:
    """A still-valid, non-dormant edge (the definition used by T1)."""
    return (
        edge.valid_until is None
        and _value(edge.lifecycle) not in _DORMANT
    )


def apply_user_placements(
    report: PlacementReport,
    edges: Sequence[TopologyEdgeV3],
) -> PlacementReport:
    """Apply persisted user declarations as a read-time override on placements.

    ``edges`` is *every* persisted CONTAINS row (all lifecycles, including closed
    and rejected history).  The live inference ledger is never mutated: this is
    a pure overlay computed from the event-sourced edge rows, so a user move wins
    over a higher-scoring inferred parent and survives later inference runs.

    Rules, per child:

    1. An in-force user/pinned CONTAINS edge declaring a parent wins outright
       (latest ``valid_from``, then largest ``edge_id`` on a tie).
    2. Otherwise, if the current parent pair was rejected, fall back to the
       first alternative whose pair was not rejected; none left means an
       explicit top-level placement (``reason="user_root"``).
    3. Any child not covered is copied unchanged.

    A new :class:`PlacementReport` is returned; the input is never mutated.
    """
    # Index in-force user declarations and rejected pairs by child, once.
    by_child_user: dict[str, list[TopologyEdgeV3]] = {}
    rejected_pairs: set[tuple[str, str]] = set()
    rejected_children: set[str] = set()
    for edge in edges:
        if not _is_contains(edge):
            continue
        child = str(edge.target_endpoint_id)
        parent = str(edge.source_endpoint_id)
        lifecycle = _value(edge.lifecycle)
        if lifecycle == TopologyEdgeState.REJECTED:
            rejected_pairs.add((parent, child))
            rejected_children.add(child)
        if (
            _in_force(edge)
            and lifecycle == TopologyEdgeState.STRUCTURAL
            and (edge.declared_by == "user" or edge.pinned)
        ):
            by_child_user.setdefault(child, []).append(edge)

    def key(edge: TopologyEdgeV3) -> tuple[datetime, str]:
        return (edge.valid_from, str(edge.edge_id))

    result = PlacementReport()
    for child, placement in report.by_child.items():
        declarations = by_child_user.get(child)
        if declarations:
            chosen = max(declarations, key=key)
            parent = str(chosen.source_endpoint_id)
            result.by_child[child] = Placement(
                child=child,
                parent=parent,
                band=BAND_HIGH,
                score=1.0,
                reason="user",
                alternatives=tuple(
                    (alt_parent, score)
                    for alt_parent, score in placement.alternatives
                    if alt_parent != parent
                ),
                both_legs=(),
            )
            continue

        current = placement.parent
        if current is not None and (str(current), child) in rejected_pairs:
            fallback = next(
                (
                    (alt_parent, score)
                    for alt_parent, score in placement.alternatives
                    if (str(alt_parent), child) not in rejected_pairs
                ),
                None,
            )
            if fallback is None:
                result.by_child[child] = Placement(
                    child=child, parent=None, reason="user_root"
                )
            else:
                alt_parent, score = fallback
                band = (
                    BAND_HIGH if score >= SCORE_HIGH
                    else BAND_MEDIUM if score >= SCORE_MEDIUM
                    else BAND_LOW
                )
                result.by_child[child] = Placement(
                    child=child,
                    parent=alt_parent,
                    band=band,
                    score=score,
                    reason=None,
                    alternatives=tuple(
                        item for item in placement.alternatives if item[0] != alt_parent
                    ),
                    both_legs=(),
                )
            continue

        # The live ledger adopts user rejections too (ADR-0046), so a child the
        # user moved to the top level arrives here already unplaced.  It is
        # still an explicit top-level decision, not a missing measurement.
        if current is None and not placement.both_legs and child in rejected_children:
            result.by_child[child] = Placement(child=child, parent=None, reason="user_root")
            continue

        result.by_child[child] = placement
    return result


def exclude_readers(report: PlacementReport, readers: frozenset[str] | set[str]) -> PlacementReport:
    """Remove ``readers`` from a placement report and re-home their children.

    A disabled sensor (``WM_DISABLED_SENSORS``) must vanish from the hierarchy.
    Its own placement is dropped; a child placed under it moves to its best
    remaining alternative parent, or becomes unplaced when none is left.  A
    ``both_legs`` pair that loses a leg keeps only the remaining one as parent.
    Pure; the input is never mutated.
    """
    gone = {str(r) for r in readers}
    if not gone:
        return report
    result = PlacementReport()
    for child, placement in report.by_child.items():
        if child in gone:
            continue
        alternatives = tuple(item for item in placement.alternatives if str(item[0]) not in gone)
        both = tuple(p for p in placement.both_legs if str(p) not in gone)
        if placement.both_legs and len(both) < len(placement.both_legs):
            if both:
                result.by_child[child] = Placement(
                    child=child, parent=both[0], band=BAND_MEDIUM,
                    score=placement.score, reason=placement.reason,
                    alternatives=alternatives, both_legs=(),
                )
            else:
                result.by_child[child] = Placement(child=child)
            continue
        if placement.parent is not None and str(placement.parent) in gone:
            if alternatives:
                parent, score = alternatives[0]
                band = (
                    BAND_HIGH if score >= SCORE_HIGH
                    else BAND_MEDIUM if score >= SCORE_MEDIUM
                    else BAND_LOW
                )
                result.by_child[child] = Placement(
                    child=child, parent=parent, band=band, score=score,
                    reason=None, alternatives=alternatives[1:], both_legs=(),
                )
            else:
                result.by_child[child] = Placement(child=child)
            continue
        if alternatives != placement.alternatives:
            result.by_child[child] = Placement(
                child=child, parent=placement.parent, band=placement.band,
                score=placement.score, reason=placement.reason,
                alternatives=alternatives, both_legs=placement.both_legs,
            )
            continue
        result.by_child[child] = placement
    return result
