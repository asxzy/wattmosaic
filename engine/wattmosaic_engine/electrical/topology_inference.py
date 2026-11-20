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
edge crosses into CONFIRMED or CONFLICTED.  User-pinned and REJECTED pairs are
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
    EdgeLifecycleV3,
    EventGroup,
    MeasurementBasis,
    RelationType,
    TopologyEdgeV3,
)

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
    duplicate_evidence,
)

__all__: list[str] = [
    "CONTAINMENT_MIN",
    "NestingCandidate",
    "infer_nesting",
    "MIN_INDEPENDENT_CAUSES",
    "MIN_SUPPORT_CONFIRMED",
    "MIN_SUPPORT_PROVISIONAL",
    "TopologyEvidence",
    "DeltaObservation",
    "TopologyInferenceView",
    "detect_same_conductor",
    "infer_topology",
]

# --------------------------------------------------------------------------- #
# Thresholds (design §5 / DEC-10).  Configurable at the call site where a knob
# exists; the defaults here are the normative ones.
# --------------------------------------------------------------------------- #

MIN_SUPPORT_CANDIDATE = 1
MIN_SUPPORT_PROVISIONAL = 3
MIN_SUPPORT_CONFIRMED = 8
MIN_INDEPENDENT_CAUSES = 2
CONFLICT_RATIO_PROVISIONAL = 0.10
CONFLICT_RATIO_CONFIRMED = 0.05
CONFLICT_RATIO_CONFLICTED = 0.25
MIN_EVENTS_CONFLICTED = 6
MIN_CORRELATED_EVENTS = 5

#: hierarchy.py:104 -- a constant child/parent ratio is one conductor with a
#: multiplier, not two nested feeds (scaled-copy refusal).
SCALED_COPY_RATIO_SPREAD_MAX = 0.02
#: hierarchy.py:84 -- below this a ratio is noise.
MIN_PAIR_ACTIVE_W = 200.0

_SIGMA_FACTOR = 3.0
_CONTAINS = "contains"
_CORRELATED = "correlated"
_SAME_CONDUCTOR = "same_conductor"
_DORMANT = frozenset((EdgeLifecycleV3.REJECTED, EdgeLifecycleV3.RETIRED))


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


def _containment_gate(
    parent: Sequence[float] | None,
    child: Sequence[float] | None,
) -> bool:
    """Absolute containment >= 0.995.  No data => gate not evaluated (allowed)."""
    if parent is None or child is None or not parent or not child:
        return True
    if len(parent) != len(child):
        return True
    fraction, comparable = containment(list(parent), list(child))
    if comparable == 0:
        return True
    return fraction >= CONTAINMENT_MIN


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
                    lifecycle=EdgeLifecycleV3.CANDIDATE,
                    declared_by="inference",
                    evidence=TopologyEvidence(),
                    valid_from=at or datetime(2026, 1, 1, tzinfo=UTC),
                    pinned=False,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# The inference view: functional, immutable updates.
# --------------------------------------------------------------------------- #


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
    #: Refuse to CONFIRM a containment whose reverse is an equally supported,
    #: uncontradicted alternative (see :meth:`_direction_cap`).  The runtime
    #: sets it: an online ledger must never promote a containment cycle.
    require_direction: bool = False

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

    def contains_edges(self, *, lifecycles: Sequence[EdgeLifecycleV3] | None = None) -> tuple[TopologyEdgeV3, ...]:
        wanted = frozenset(lifecycles) if lifecycles else None
        return tuple(
            e
            for e in self.edges
            if _relation(e.relation) == _CONTAINS and (wanted is None or e.lifecycle in wanted)
        )

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
        return replace(self, edges=tuple(edges))

    def observe_group(
        self,
        group: EventGroup | DeltaObservation,
        *,
        cause_id: str | None = None,
        absolute: Mapping[str, Sequence[float | None]] | None = None,
        basis_by_reader: Mapping[str, MeasurementBasis | str] | None = None,
        channel_meta: Mapping[str, Mapping[str, Any]] | None = None,
        observed: Mapping[str, float | None] | None = None,
    ) -> TopologyInferenceView:
        """Fold one FINAL event group into every affected pair's ledger.

        ``observed`` maps each reader that held a usable reading over the
        group's window to its noise sigma (``None`` = unknown, the floor is
        used).  With it, a child step whose candidate parent was observed but
        did *not* move counts as conflict for that edge (design §5 T5:
        "a child step with no matching parent step beyond 3 sigma_p").  This is
        what tells the two directions of an equal-delta pair apart: the true
        parent also steps for its other loads, so the reversed edge collects
        conflict.  Without ``observed`` (no reader health known) a non-moving
        reader is never evidence either way.
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
        absolute_now = dict(self.absolute)
        if absolute:
            absolute_now.update(absolute)
        basis_now = dict(self.basis_by_reader)
        if basis_by_reader:
            basis_now.update(basis_by_reader)
        meta_now = dict(self.channel_meta)
        if channel_meta:
            meta_now.update(channel_meta)

        decisive = {
            reader: values for reader, values in obs.deltas.items() if values[0] is not None
        }

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

        # -- child steps against every candidate parent --------------------- #
        for child, (child_delta, child_sigma) in decisive.items():
            for parent, (parent_delta, parent_sigma) in decisive.items():
                if parent == child:
                    continue
                key = (parent, child, _CONTAINS)
                position = by_index.get(key)
                if position is None:
                    edge = TopologyEdgeV3(
                        edge_id=f"te-inf::{parent}::{child}",
                        relation=RelationType.CONTAINS,
                        source_endpoint_id=parent,
                        target_endpoint_id=child,
                        lifecycle=EdgeLifecycleV3.CANDIDATE,
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
                tol = _SIGMA_FACTOR * math.sqrt(sigma_p**2 + (beta * sigma_c) ** 2)
                if abs(parent_delta - beta * child_delta) <= tol:
                    field_name, stamp = "support", "last_support_at"
                elif abs(parent_delta) <= _SIGMA_FACTOR * sigma_p < abs(beta * child_delta):
                    # Design §5: the parent did not move (its delta is within
                    # 3 sigma_p of zero) while the child moved beyond it.
                    field_name, stamp = "conflict", "last_conflict_at"
                else:
                    # Both readers moved, by different amounts: the parent's step
                    # also carries other loads changing in the same window.  That
                    # is neutral, not evidence against containment (live mains
                    # almost always carry concurrent changes).
                    continue
                causes.setdefault(key, set()).add(cause)
                evidence = edge.evidence.model_copy(
                    update={
                        field_name: getattr(edge.evidence, field_name) + 1,
                        stamp: at,
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
                child_delta = decisive[child][0]
                sigma_p, measured = _observed_entry(observed.get(parent))
                sigma_p = max(sigma_p or self.sigma_floor_w, self.sigma_floor_w)
                beta = edge.beta if edge.beta is not None else 1.0
                noise = _SIGMA_FACTOR * sigma_p
                if abs(beta * child_delta) <= noise:
                    # Hidden in the parent's noise: not evidence either way.
                    continue
                field_name, stamp = "conflict", "last_conflict_at"
                if measured is not None:
                    # The parent's level was measured across the window: P3 may
                    # simply not have flagged a small step on a busy feed.
                    if abs(measured - beta * child_delta) <= noise:
                        field_name, stamp = "support", "last_support_at"
                    elif abs(measured) > noise:
                        # The parent moved, just not by the child's amount:
                        # concurrent loads, not a contradiction (neutral).
                        continue
                key = (parent, child, _CONTAINS)
                causes.setdefault(key, set()).add(cause)
                current = edges[position]
                evidence = current.evidence.model_copy(
                    update={
                        field_name: getattr(current.evidence, field_name) + 1,
                        stamp: at,
                        "independent_causes": len(causes[key]),
                    }
                )
                edges[position] = current.model_copy(update={"evidence": evidence})

            # A measured parent that moved with the child is support even when
            # no edge exists yet (P3 did not flag the parent's step, so the
            # co-step pass above never created the pair).  A flat or unmeasured
            # parent never creates an edge: there is nothing to count against.
            for child, (child_delta, _child_sigma) in decisive.items():
                for parent, entry in observed.items():
                    if parent == child or parent in decisive:
                        continue
                    key = (parent, child, _CONTAINS)
                    if key in by_index:
                        continue
                    sigma_p, measured = _observed_entry(entry)
                    if measured is None:
                        continue
                    noise = _SIGMA_FACTOR * max(sigma_p or self.sigma_floor_w, self.sigma_floor_w)
                    if abs(child_delta) <= noise or abs(measured - child_delta) > noise:
                        continue
                    causes.setdefault(key, set()).add(cause)
                    edges.append(
                        TopologyEdgeV3(
                            edge_id=f"te-inf::{parent}::{child}",
                            relation=RelationType.CONTAINS,
                            source_endpoint_id=parent,
                            target_endpoint_id=child,
                            lifecycle=EdgeLifecycleV3.CANDIDATE,
                            declared_by="inference",
                            beta=1.0,
                            beta_basis="unity",
                            evidence=TopologyEvidence(
                                support=1,
                                last_support_at=at,
                                independent_causes=len(causes[key]),
                            ),
                            valid_from=at,
                            pinned=False,
                        )
                    )
                    by_index[key] = len(edges) - 1

        # -- co-response without containment -> CORRELATED ------------------- #
        readers = sorted(decisive)
        for i, a in enumerate(readers):
            for b in readers[i + 1 :]:
                da, db = decisive[a][0], decisive[b][0]
                if da == 0.0 or db == 0.0 or (da > 0) != (db > 0):
                    continue
                pair = frozenset((a, b))
                contained = _containment_gate(absolute_now.get(a), absolute_now.get(b)) or _containment_gate(
                    absolute_now.get(b), absolute_now.get(a)
                )
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
                            lifecycle=EdgeLifecycleV3.CANDIDATE,
                            declared_by="inference",
                            evidence=TopologyEvidence(),
                            valid_from=at,
                            pinned=False,
                        )
                    )

        # -- lifecycle transitions with the absolute guards ----------------- #
        transitions: list[str] = []
        for index, edge in enumerate(edges):
            if _relation(edge.relation) != _CONTAINS or edge.pinned or edge.lifecycle in _DORMANT:
                continue
            new_lifecycle = self._lifecycle(edge, causes, absolute_now, basis_now, meta_now)
            if (
                self.require_direction
                and new_lifecycle == EdgeLifecycleV3.CONFIRMED
                and edge.declared_by != "user"
            ):
                new_lifecycle = self._direction_cap(edge, edges, new_lifecycle, absolute_now)
            if new_lifecycle != edge.lifecycle:
                transitions.append(
                    f"{edge.edge_id}: {_value(edge.lifecycle)} -> {_value(new_lifecycle)}"
                )
                edges[index] = edge.model_copy(update={"lifecycle": new_lifecycle})
        # Only a crossing into CONFIRMED or CONFLICTED promotes a topology
        # version (T5 step 3).  CANDIDATE/PROVISIONAL are soft and accumulate
        # silently; one event must never publish a new version.
        version_transitions = [
            text
            for text in transitions
            if text.endswith(f"-> {_value(EdgeLifecycleV3.CONFIRMED)}")
            # A structural edge becoming CONFLICTED changes the topology; a
            # never-structural suggestion (CANDIDATE/PROVISIONAL) that is
            # contradicted changes nothing H uses, so it is no new version.
            or text.endswith(
                f"{_value(EdgeLifecycleV3.CONFIRMED)} -> {_value(EdgeLifecycleV3.CONFLICTED)}"
            )
        ]

        view = replace(
            self,
            edges=tuple(edges),
            at=at,
            seen_groups=frozenset({*self.seen_groups, obs.group_id}),
            causes_by_edge={key: frozenset(values) for key, values in causes.items()},
            co_response_causes={key: frozenset(values) for key, values in co_response.items()},
            co_response_events=co_events,
            absolute=absolute_now,
            basis_by_reader=basis_now,
            channel_meta=meta_now,
        )
        if version_transitions:
            view = view._propose_version(version_transitions)
        return view

    # -- lifecycle / versioning internals ----------------------------------- #
    @staticmethod
    def _lifecycle(
        edge: TopologyEdgeV3,
        causes: Mapping[tuple[str, str, str], set[str]],
        absolute: Mapping[str, Sequence[float | None]],
        basis: Mapping[str, MeasurementBasis | str],
        meta: Mapping[str, Mapping[str, Any]],
    ) -> EdgeLifecycleV3:
        evidence = edge.evidence
        support, conflict = evidence.support, evidence.conflict
        decisive = support + conflict
        ratio = evidence.conflict_ratio
        key = (edge.source_endpoint_id, edge.target_endpoint_id, _CONTAINS)
        n_causes = len(causes.get(key, ()))

        if decisive >= MIN_EVENTS_CONFLICTED and ratio > CONFLICT_RATIO_CONFLICTED:
            # DEC-10 (T7): a user-declared CONFIRMED CONTAINS edge is a fact, but
            # persistent, strong contradiction (>= 6 independent events with a
            # conflict ratio > 0.25) must move it to CONFLICTED — never delete it.
            # The edge row and its history are retained; only the lifecycle moves
            # so downstream confidence is degraded while the user can re-affirm.
            return EdgeLifecycleV3.CONFLICTED

        parent, child = edge.source_endpoint_id, edge.target_endpoint_id
        gates_ok = (
            n_causes >= MIN_INDEPENDENT_CAUSES
            and _containment_gate(absolute.get(parent), absolute.get(child))
            and not _scaled_copy(absolute.get(parent), absolute.get(child))
            and _value(basis.get(parent, MeasurementBasis.CT_MEASURED)) != MeasurementBasis.UNKNOWN
            and _value(basis.get(child, MeasurementBasis.CT_MEASURED)) != MeasurementBasis.UNKNOWN
        )
        if (
            gates_ok
            and support >= MIN_SUPPORT_CONFIRMED
            and ratio < CONFLICT_RATIO_CONFIRMED
        ):
            return EdgeLifecycleV3.CONFIRMED
        if (
            gates_ok
            and support >= MIN_SUPPORT_PROVISIONAL
            and ratio < CONFLICT_RATIO_PROVISIONAL
        ):
            return EdgeLifecycleV3.PROVISIONAL
        if support >= MIN_SUPPORT_CANDIDATE:
            if edge.lifecycle == EdgeLifecycleV3.CONFIRMED and edge.declared_by == "user":
                # DEC-10: a user-declared parent is a fact.  Inference never
                # downgrades it; only the user may pin/reject it.  Absence of
                # delta evidence is not evidence against a wiring fact.
                return EdgeLifecycleV3.CONFIRMED
            return EdgeLifecycleV3.CANDIDATE
        return edge.lifecycle

    @staticmethod
    def _direction_cap(
        edge: TopologyEdgeV3,
        edges: Sequence[TopologyEdgeV3],
        proposed: EdgeLifecycleV3,
        absolute: Mapping[str, Sequence[float | None]],
    ) -> EdgeLifecycleV3:
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
                and other.lifecycle != EdgeLifecycleV3.CONFLICTED
                and other.evidence.support > 0
                and other.evidence.conflict_ratio < CONFLICT_RATIO_PROVISIONAL
                and _containment_gate(
                    absolute.get(other.source_endpoint_id), absolute.get(other.target_endpoint_id)
                )
            ):
                return EdgeLifecycleV3.PROVISIONAL
        return proposed

    def _propose_version(self, transitions: Sequence[str]) -> TopologyInferenceView:
        service = self.versions
        if service is None:
            return self
        reason = "topology evidence ledger: " + "; ".join(transitions)
        version_id = service.propose_topology(list(self.edges), None)
        service.promote_topology(version_id, reason)
        return replace(self, version_id=version_id)


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
    CONFIRMED CONTAINS edges (H2 writes user edges that way); everything else is
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
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                beta=1.0,
                beta_basis="unity",
                evidence=TopologyEvidence(),
                valid_from=now,
                pinned=False,
            )
        )
    edges.extend(detect_same_conductor(aligned, at=now))

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
    requires_confirmation: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "child_reader_id": self.child_reader_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "evidence": dict(self.evidence),
            "requires_confirmation": self.requires_confirmation,
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
