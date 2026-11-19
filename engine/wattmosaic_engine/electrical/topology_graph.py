"""Typed topology graph built from lifecycle-filtered edges (T1, design §5).

Edges are the authority: a :class:`TopologyGraph` is built from a set of
reader↔reader :class:`~wattmosaic_engine.contracts.TopologyEdgeV3` rows and a
set of :class:`~wattmosaic_engine.contracts.MeasurementEndpoint` rows.  Only
CONFIRMED CONTAINS edges form hard structure.  PROVISIONAL CONTAINS edges are
*soft alternatives*: they are never applied to hierarchy walks, but are exposed
through :meth:`TopologyGraph.soft_alternatives` for T2's operator.  CANDIDATE
and CORRELATED edges never shape the hierarchy.

The module is pure.  It takes an injected ``at`` timestamp and reads no clock
and no database.  It implements the :class:`~wattmosaic_engine.runtime.interfaces.TopologyView`
Protocol and is the production counterpart of the K08 ``FakeTopology``.

Validation (ported from ``hierarchy.py:930-1050``) rejects self edges, cycles
and a second CONFIRMED parent for one reader.  A contradictory edge is moved to
CONFLICTED with a recorded reason rather than silently deleted.  A pinned or
rejected *user* edge overrides an inference edge for the same (parent, child)
pair (DEC-10).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from wattmosaic_engine.contracts import (
    EdgeLifecycleV3,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.runtime.interfaces import CONFIRMED

__all__: list[str] = ["TopologyGraph", "DisplayNode", "topology_root_readers"]


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def topology_root_readers(topology: object) -> tuple[str, ...]:
    """The main reader(s) of a topology: the roots of its CONFIRMED hierarchy.

    A root is a reader that is the source of a CONFIRMED ``CONTAINS`` edge and
    the target of none.  Several roots (parallel mains) are all returned; so is
    the verified partner of a root's complete ``PAIRED_LEG`` pair (the legs of
    a multi-leg main observe the household together and are summed).  A
    ``SAME_CONDUCTOR`` alias of a returned reader is dropped, so no conductor
    is counted twice.

    Only the topology decides -- never a reader's name.  A topology with no
    CONFIRMED ``CONTAINS`` edge has no root and returns ``()``; callers then
    publish ``main_w`` / ``residual_w`` as ``None`` (HB §18) rather than pick
    an arbitrary reader.  Works on any :class:`TopologyView` duck type.
    """
    try:
        edges = tuple(topology.edges())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no usable view: no roots
        return ()
    confirmed = _enum_text(EdgeLifecycleV3.CONFIRMED)
    active = [e for e in edges if _enum_text(e.lifecycle) == confirmed]

    def of(relation: RelationType) -> list[TopologyEdgeV3]:
        return [e for e in active if _enum_text(e.relation) == relation.value]

    contains = of(RelationType.CONTAINS)
    children = {e.target_endpoint_id for e in contains}
    roots = sorted({e.source_endpoint_id for e in contains} - children)
    partners = {e.source_endpoint_id: e.target_endpoint_id for e in of(RelationType.PAIRED_LEG)}

    out: list[str] = []
    for root in roots:
        leg = partners.get(root)
        # A verified pair declares both directions (see ``paired_leg``).
        verified = leg if leg is not None and partners.get(leg) == root else None
        for reader in (root, verified):
            if reader is not None and reader not in out and reader not in children:
                out.append(reader)

    aliases: dict[str, set[str]] = {}
    for e in of(RelationType.SAME_CONDUCTOR):
        aliases.setdefault(e.source_endpoint_id, set()).add(e.target_endpoint_id)
        aliases.setdefault(e.target_endpoint_id, set()).add(e.source_endpoint_id)
    kept: list[str] = []
    for reader in out:
        if not aliases.get(reader, set()) & set(kept):
            kept.append(reader)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class DisplayNode:
    """One node of T1's explicit L1–L4 display tree (M1 consumes it).

    Carrying ``level``/``label``/``children``/``reader_id`` as attributes makes
    M1's ``build_columns`` take its clean ``_normalise_explicit`` path, which
    emits exactly one L4 leaf per endpoint and never treats dict keys as nodes.
    """

    label: str
    level: str
    kind: str | None = None
    reader_id: str | None = None
    children: tuple[DisplayNode, ...] = ()


# Lifecycles that participate in structure when explicitly requested.  Only
# CONFIRMED is structural by default; PROVISIONAL is a soft alternative that a
# caller may opt into via the ``lifecycles`` argument.
_STRUCTURAL = frozenset(
    (EdgeLifecycleV3.CONFIRMED, EdgeLifecycleV3.PROVISIONAL)
)
_SOFT = EdgeLifecycleV3.PROVISIONAL


@dataclass(slots=True)
class TopologyGraph:
    """A validated, indexed, lifecycle-filtered view over a typed edge set."""

    edges_all: tuple[TopologyEdgeV3, ...] = ()
    endpoints: tuple[MeasurementEndpoint, ...] = ()
    at: datetime | None = None

    #: Active (non-conflicted, non-rejected, valid, user-overridden) edges.
    active_edges: tuple[TopologyEdgeV3, ...] = field(default_factory=tuple, init=False)
    #: Validation / override problems, human readable with reasons.
    problems: list[str] = field(default_factory=list, init=False)
    #: Edge ids moved to CONFLICTED, with their reason.
    moved_to_conflicted: dict[str, str] = field(default_factory=dict, init=False)

    # Indices built in __post_init__ -------------------------------------------
    _by_id: dict[str, TopologyEdgeV3] = field(default_factory=dict, init=False)
    _parent_of: dict[str, str] = field(default_factory=dict, init=False)
    _children: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _ancestors: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _endpoint_ids: frozenset[str] = field(default_factory=frozenset, init=False)
    _declared_endpoints: frozenset[str] = field(default_factory=frozenset, init=False)
    _paired: dict[str, str] = field(default_factory=dict, init=False)
    _complete_pairs: tuple[frozenset[str], ...] = field(default_factory=tuple, init=False)
    _conductor_groups: dict[str, tuple[str, ...]] = field(default_factory=dict, init=False)
    _canonical: dict[str, str] = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_edges(
        cls,
        edges: Iterable[TopologyEdgeV3],
        endpoints: Iterable[MeasurementEndpoint] = (),
        at: datetime | None = None,
    ) -> TopologyGraph:
        """Build a graph from edges/endpoints, valid at ``at`` (pure)."""
        return cls(tuple(edges), tuple(endpoints), at)

    def __post_init__(self) -> None:
        self.problems = []
        self.moved_to_conflicted = {}
        self._by_id = {}
        self._parent_of = {}
        self._children = {}
        self._ancestors = {}
        self._paired = {}
        self._complete_pairs = ()
        self._conductor_groups = {}
        self._canonical = {}
        self._declared_endpoints = frozenset(e.endpoint_id for e in self.endpoints)
        self._endpoint_ids = self._declared_endpoints | frozenset(
            eid for e in self.edges_all for eid in (e.source_endpoint_id, e.target_endpoint_id)
        )

        self._apply_user_overrides()
        self._validate_active_edges()
        self._index_contains()
        self._index_pairs()
        self._index_conductors()

    # -- lifecycle / validity filtering --------------------------------------
    def _in_force(self, edge: TopologyEdgeV3) -> bool:
        """Whether an edge is currently valid (ignores lifecycle)."""
        if edge.lifecycle in (EdgeLifecycleV3.REJECTED, EdgeLifecycleV3.RETIRED):
            return False
        if self.at is not None:
            if edge.valid_until is not None and edge.valid_until <= self.at:
                return False
            if edge.valid_from > self.at:
                return False
        return True

    def _apply_user_overrides(self) -> None:
        """Pinned/rejected user edges override inference edges for a pair.

        Only CONTAINS edges can be structural.  The override key is the ordered
        (source, target) pair so a parent/child reversal is a different edge.
        """
        inference_by_pair: dict[tuple[str, str], list[TopologyEdgeV3]] = {}
        user_by_pair: dict[tuple[str, str], list[TopologyEdgeV3]] = {}
        for edge in self.edges_all:
            if edge.relation != RelationType.CONTAINS:
                continue
            key = (edge.source_endpoint_id, edge.target_endpoint_id)
            if edge.source_endpoint_id == edge.target_endpoint_id:
                # Self edges are rejected during validation below.
                continue
            if edge.declared_by == "user":
                user_by_pair.setdefault(key, []).append(edge)
            else:
                inference_by_pair.setdefault(key, []).append(edge)

        active = [e for e in self.edges_all if self._in_force(e) and e.relation == RelationType.CONTAINS]
        # Drop inference edges for any pair that a user has pinned or rejected.
        overridden: set[str] = set()
        for key, user_edges in user_by_pair.items():
            if any(u.pinned or u.lifecycle == EdgeLifecycleV3.REJECTED for u in user_edges):
                for inference in inference_by_pair.get(key, ()):
                    overridden.add(inference.edge_id)
        if overridden:
            self.problems.append(
                "user pin/reject overrides inference edges: "
                + ", ".join(sorted(overridden))
            )
        active = [e for e in active if e.edge_id not in overridden]

        # Non-CONTAINS edges (PAIRED_LEG, SAME_CONDUCTOR, CORRELATED, ...) stay
        # active for their own lookups, filtered only by validity.
        others = [
            e
            for e in self.edges_all
            if self._in_force(e) and e.relation != RelationType.CONTAINS
        ]
        self.active_edges = tuple(active) + tuple(others)
        self._by_id = {e.edge_id: e for e in self.active_edges}

    # -- validation -----------------------------------------------------------
    def _validate_active_edges(self) -> None:
        """Reject self edges, cycles and a second CONFIRMED parent.

        A contradictory weaker edge is *moved to CONFLICTED* with a reason; it
        is no longer applied to structure but is not silently deleted.
        """
        # 1. Self edges (defensive: the contract validator also refuses them).
        kept: list[TopologyEdgeV3] = []
        for edge in self.active_edges:
            if edge.source_endpoint_id == edge.target_endpoint_id:
                reason = "self_edge"
                self.problems.append(f"self edge discarded: {edge.edge_id} ({reason})")
                self.moved_to_conflicted[edge.edge_id] = reason
                continue
            kept.append(edge)
        self.active_edges = tuple(kept)

        # 2. At most one CONFIRMED CONTAINS parent per child.  We keep the
        #    strongest declaration: a pinned user edge, then user, then the
        #    strongest lifecycle, then the edge id as a deterministic tie break.
        #    A weaker edge for the *same child but a different parent* is moved
        #    to CONFLICTED (DEC-10: a contradiction never silently deletes an
        #    edge).  A PROVISIONAL edge to the *same parent* is a soft
        #    alternative and is left for ``soft_alternatives``.
        contains_by_child: dict[str, list[TopologyEdgeV3]] = {}
        for edge in self.active_edges:
            if edge.relation == RelationType.CONTAINS and edge.lifecycle in _STRUCTURAL:
                contains_by_child.setdefault(edge.target_endpoint_id, []).append(edge)
        dropped: set[str] = set()
        for _child, edges in contains_by_child.items():
            confirmed = [
                e for e in edges if e.lifecycle == EdgeLifecycleV3.CONFIRMED
            ]
            winner = min(confirmed or edges, key=self._edge_rank)
            for edge in edges:
                if edge.edge_id == winner.edge_id:
                    continue
                if edge.lifecycle != EdgeLifecycleV3.CONFIRMED and edge.source_endpoint_id == winner.source_endpoint_id:
                    # A soft alternative to the same parent is kept, not moved.
                    continue
                reason = f"conflicting_contains_parent; winner={winner.edge_id}"
                self.problems.append(
                    f"edge {edge.edge_id} moved to CONFLICTED ({reason})"
                )
                self.moved_to_conflicted[edge.edge_id] = reason
                dropped.add(edge.edge_id)
        if dropped:
            self.active_edges = tuple(
                e for e in self.active_edges if e.edge_id not in dropped
            )

        # 3. Transitive cycles: break each cycle deterministically by dropping
        #    the edge with the largest edge id on the closing link.
        self._break_cycles()

        self._by_id = {e.edge_id: e for e in self.active_edges}

    @staticmethod
    def _edge_rank(edge: TopologyEdgeV3) -> tuple[int, int, str]:
        user = 0 if edge.declared_by == "user" else 1
        pin = 0 if edge.pinned else 1
        return (user, pin, edge.edge_id)

    def _break_cycles(self) -> None:
        parent = {
            e.target_endpoint_id: e.source_endpoint_id
            for e in self.active_edges
            if e.relation == RelationType.CONTAINS
            and e.lifecycle == EdgeLifecycleV3.CONFIRMED
        }
        while True:
            cycle = self._find_cycle(parent)
            if cycle is None:
                break
            # Drop every edge whose both endpoints lie on the cycle: leaving
            # any one would still make one of the cycle's readers a child.
            links = [
                e
                for e in self.active_edges
                if e.relation == RelationType.CONTAINS
                and e.lifecycle == EdgeLifecycleV3.CONFIRMED
                and e.target_endpoint_id in cycle
                and e.source_endpoint_id in cycle
            ]
            if not links:  # pragma: no cover - defensive
                break
            reason = "cycle: " + " -> ".join(cycle)
            for victim in links:
                self.problems.append(
                    f"cycle edge discarded: {victim.edge_id} ({reason})"
                )
                self.moved_to_conflicted[victim.edge_id] = reason
                parent.pop(victim.target_endpoint_id, None)
            dropped_ids = {e.edge_id for e in links}
            self.active_edges = tuple(
                e for e in self.active_edges if e.edge_id not in dropped_ids
            )

    @staticmethod
    def _find_cycle(parent: Mapping[str, str]) -> tuple[str, ...] | None:
        state: dict[str, int] = {}
        for start in parent:
            if state.get(start, 0) != 0:
                continue
            trail: list[str] = []
            current: str | None = start
            while current is not None:
                if state.get(current, 0) == 1:
                    idx = trail.index(current)
                    return tuple(trail[idx:] + [current])
                if state.get(current, 0) == 2:
                    break
                state[current] = 1
                trail.append(current)
                current = parent.get(current)
            for node in trail:
                state[node] = 2
        return None

    # -- indices --------------------------------------------------------------
    def _index_contains(self) -> None:
        children: dict[str, list[str]] = {rid: [] for rid in self._endpoint_ids}
        self._parent_of = {}
        for edge in self.active_edges:
            if edge.relation != RelationType.CONTAINS:
                continue
            if edge.lifecycle not in _STRUCTURAL:
                continue
            child = edge.target_endpoint_id
            parent_id = edge.source_endpoint_id
            self._parent_of[child] = parent_id
            children.setdefault(parent_id, []).append(child)
        self._children = {rid: tuple(ids) for rid, ids in children.items()}

        self._ancestors = {}
        for rid in self._endpoint_ids:
            chain: list[str] = []
            seen = {rid}
            current = self._parent_of.get(rid)
            while current is not None and current not in seen:
                chain.append(current)
                seen.add(current)
                current = self._parent_of.get(current)
            self._ancestors[rid] = tuple(chain)

    def _index_pairs(self) -> None:
        """Complete PAIRED_LEG pairs only (both directions declared)."""
        partners: dict[str, str] = {}
        for edge in self.active_edges:
            if edge.relation != RelationType.PAIRED_LEG:
                continue
            partners[edge.source_endpoint_id] = edge.target_endpoint_id
        self._paired = partners
        pairs: list[frozenset[str]] = []
        seen: set[frozenset[str]] = set()
        for reader, partner in partners.items():
            if partner == reader:
                self.problems.append(f"PAIRED_LEG {reader} pairs with itself")
                continue
            if partners.get(partner) != reader:
                # Asymmetric pairing is not a verified complete pair.
                continue
            key = frozenset((reader, partner))
            if key not in seen:
                seen.add(key)
                pairs.append(key)
        self._complete_pairs = tuple(pairs)

    def _index_conductors(self) -> None:
        """SAME_CONDUCTOR alias groups (canonical = smallest id, fallback only)."""
        neighbours: dict[str, set[str]] = {}
        for edge in self.active_edges:
            if edge.relation != RelationType.SAME_CONDUCTOR:
                continue
            a, b = edge.source_endpoint_id, edge.target_endpoint_id
            neighbours.setdefault(a, set()).add(b)
            neighbours.setdefault(b, set()).add(a)
        seen: set[str] = set()
        groups: dict[str, tuple[str, ...]] = {}
        # A member that carries structural (CONTAINS) edges is the real row;
        # pure aliases are fallbacks.  Otherwise fall back to the smallest id.
        structural = {
            e.source_endpoint_id
            for e in self.active_edges
            if e.relation == RelationType.CONTAINS
        } | {
            e.target_endpoint_id
            for e in self.active_edges
            if e.relation == RelationType.CONTAINS
        }
        for node in sorted(neighbours):
            if node in seen:
                continue
            stack = [node]
            component: set[str] = set()
            while stack:
                cur = stack.pop()
                if cur in component:
                    continue
                component.add(cur)
                stack.extend(neighbours.get(cur, ()))
            seen |= component
            in_tree = sorted(component & structural)
            canonical = in_tree[0] if in_tree else sorted(component)[0]
            group = (canonical, *sorted(component - {canonical}))
            for member in group:
                groups[member] = group
                self._canonical[member] = canonical
        self._conductor_groups = groups

    # ------------------------------------------------------------------ #
    # TopologyView Protocol.
    # ------------------------------------------------------------------ #
    def edges(
        self, lifecycles: Sequence[EdgeLifecycleV3] | None = None
    ) -> tuple[TopologyEdgeV3, ...]:
        if lifecycles is None:
            return self.active_edges
        wanted = frozenset(lifecycles)
        return tuple(e for e in self.active_edges if e.lifecycle in wanted)

    def _contains(self, lifecycles: Sequence[EdgeLifecycleV3]):
        wanted = frozenset(lifecycles)
        return tuple(
            e
            for e in self.active_edges
            if e.relation == RelationType.CONTAINS and e.lifecycle in wanted
        )

    def parent(
        self,
        reader_id: str,
        lifecycles: Sequence[EdgeLifecycleV3] = CONFIRMED,
    ) -> str | None:
        for edge in self._contains(lifecycles):
            if edge.target_endpoint_id == reader_id:
                return edge.source_endpoint_id
        return None

    def ancestors(
        self,
        reader_id: str,
        lifecycles: Sequence[EdgeLifecycleV3] = CONFIRMED,
    ) -> tuple[str, ...]:
        """Nearest ancestor first, root last (CONFIRMED by default)."""
        result: list[str] = []
        seen = {reader_id}
        current = self.parent(reader_id, lifecycles)
        while current is not None and current not in seen:
            result.append(current)
            seen.add(current)
            current = self.parent(current, lifecycles)
        return tuple(result)

    def children(
        self,
        reader_id: str,
        lifecycles: Sequence[EdgeLifecycleV3] = CONFIRMED,
    ) -> tuple[str, ...]:
        return tuple(
            e.target_endpoint_id
            for e in self._contains(lifecycles)
            if e.source_endpoint_id == reader_id
        )

    def soft_alternatives(self) -> tuple[TopologyEdgeV3, ...]:
        """PROVISIONAL edges — soft, never applied to hierarchy walks."""
        return self.edges((_SOFT,))

    # ------------------------------------------------------------------ #
    # PAIRED_LEG semantics (ported from topology.py:349-393).
    # ------------------------------------------------------------------ #
    def paired_leg(self, reader_id: str) -> str | None:
        """The other leg of a *verified*, complete PAIRED_LEG pair or ``None``.

        A leg is only a real partner when both directions are declared — a
        cross-device pairing must never be summed, or each device claims the
        other's power (port of ``double_pole_leg``).
        """
        partner = self._paired.get(reader_id)
        if partner is None or partner == reader_id:
            return None
        if self._paired.get(partner) != reader_id:
            return None
        return partner

    def complete_leg_pairs(self) -> tuple[frozenset[str], ...]:
        return self._complete_pairs

    def leg_coefficient(self, reader_id: str) -> float | None:
        """Coefficient of one leg in a complete PAIRED_LEG pair.

        A complete pair is fully observed: the two rows sum to the load exactly,
        so each leg carries half, independent of any declared split.  Returns
        ``None`` when the reader is not part of a verified pair (absent means
        ``None``, never 0.0).
        """
        if self.paired_leg(reader_id) is not None:
            return 0.5
        return None

    # ------------------------------------------------------------------ #
    # SAME_CONDUCTOR semantics (alias is a fallback, never a row — matrix.py).
    # ------------------------------------------------------------------ #
    def same_conductor_group(self, reader_id: str) -> tuple[str, ...]:
        """All readers on the same conductor, canonical first."""
        return self._conductor_groups.get(reader_id, ())

    def canonical_reader(self, reader_id: str) -> str:
        """The canonical reader to keep as a row; aliases become fallbacks."""
        return self._canonical.get(reader_id, reader_id)

    # ------------------------------------------------------------------ #
    # Primary display tree (input to M1 build_columns).
    # ------------------------------------------------------------------ #
    def primary_display_tree(self) -> tuple[DisplayNode, ...]:
        """Explicit L1–L4 level nodes for the CONFIRMED CONTAINS hierarchy.

        Each CONFIRMED CONTAINS node becomes a grouping node at L1/L2/L3 by
        depth, with its own L4 reader leaf as a child.  This is the explicit
        level-node shape M1's ``build_columns`` consumes; on the HB §17 tree it
        yields the five levels L0..L4.  Any declared endpoint not placed by a
        CONTAINS edge is emitted under a single L1 ``"Unassigned measurements"``
        node so it still gets exactly one leaf.
        """
        roots = self._roots()
        nodes = [self._display_node(rid, depth=1) for rid in roots]
        placed = self._placed_readers(roots)
        unassigned = [rid for rid in sorted(self._declared_endpoints) if rid not in placed]
        if unassigned:
            nodes.append(
                DisplayNode(
                    label="Unassigned measurements",
                    level="L1",
                    kind="unassigned",
                    children=tuple(
                        DisplayNode(
                            label=self._label(rid), level="L4", reader_id=rid
                        )
                        for rid in unassigned
                    ),
                )
            )
        return tuple(nodes)

    def _roots(self) -> tuple[str, ...]:
        """CONFIRMED CONTAINS roots (readers with no confirmed parent)."""
        return tuple(
            rid
            for rid in sorted(self._endpoint_ids)
            if rid not in self._parent_of and self.children(rid)
        )

    def _placed_readers(self, roots: Sequence[str]) -> set[str]:
        placed: set[str] = set()

        def walk(rid: str) -> None:
            placed.add(rid)
            for child in self.children(rid):
                walk(child)

        for root in roots:
            walk(root)
        return placed

    def _display_node(self, rid: str, depth: int) -> DisplayNode:
        """A grouping node at ``depth`` whose first child is the L4 leaf."""
        level = f"L{min(depth, 3)}"
        kind = {1: "panel", 2: "subpanel", 3: "circuit"}.get(
            min(depth, 3), "circuit"
        )
        label = self._label(rid)
        children = [DisplayNode(label=label, level="L4", reader_id=rid)]
        for child in self.children(rid):
            children.append(self._display_node(child, depth + 1))
        return DisplayNode(label=label, level=level, kind=kind, children=tuple(children))

    def _label(self, rid: str) -> str:
        for endpoint in self.endpoints:
            if endpoint.endpoint_id == rid:
                return endpoint.display_name or rid
        return rid

    # ------------------------------------------------------------------ #
    # Validation helpers.
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        return list(self.problems)

    def is_consistent(self) -> bool:
        return not self.problems
