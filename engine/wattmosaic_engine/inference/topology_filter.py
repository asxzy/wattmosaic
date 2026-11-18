"""Topology-aware filtering of candidate hypotheses (I1, HB §7.2).

The filter is deliberately conservative.  A candidate is rejected **only** when
both of the following hold:

1. every path from its mapped reader to every affected reader crosses a
   CONFIRMED edge whose relation is *not* CONTAINS (a confirmed, non-containing
   structure), and
2. its mapping edge is CONFIRMED to a reader that is not among the affected
   readers ("CONFIRMED elsewhere").

A merely PROVISIONAL incompatibility never rejects: the candidate is kept with
a soft flag in ``evidence_terms`` that I2 consumes as a down-weight in evidence
term E.  Unknown-scope candidates are never rejected — they are the HB §7.5
safety net.

This module is pure: no database, no wall clock, no imports of ``runtime.fakes``.
Optional capabilities of the passed-in views are detected through small local
Protocols; the production dependency is only on the K08 runtime types.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from wattmosaic_engine.contracts import EdgeLifecycleV3, RelationType, TopologyEdgeV3
from wattmosaic_engine.runtime.interfaces import MappingView, ObservationOperator

logger = logging.getLogger(__name__)

#: Evidence-term key used to flag a kept candidate whose topology path is only
#: provisionally incompatible.  I2 reads this when scoring term E.
PROVISIONAL_FLAG = "E_provisional_incompatible"

_NON_CONTAINING = frozenset(
    {
        RelationType.SHARED_LOAD,
        RelationType.PAIRED_LEG,
        RelationType.CORRELATED,
        RelationType.SAME_CONDUCTOR,
    }
)


@dataclass(frozen=True)
class PathEdge:
    """The minimal view of an edge needed for a topology verdict."""

    relation: RelationType | None
    lifecycle: EdgeLifecycleV3 | None
    edge_id: str = ""


@runtime_checkable
class MappingLifecycleView(Protocol):
    """An optional MappingView extension exposing a subject's edge lifecycle."""

    def edge_lifecycle(self, subject: tuple[str, str]) -> EdgeLifecycleV3 | None: ...


@runtime_checkable
class TopologyPathsView(Protocol):
    """An optional view that can enumerate edges on a reader-to-reader path."""

    def path_edges(self, source: str, target: str) -> Sequence[TopologyEdgeV3]: ...


@dataclass(frozen=True)
class FilterVerdict:
    """Outcome of evaluating one subject against the affected readers."""

    action: Literal["keep", "soft_keep", "reject"]
    reason: str | None = None

    @property
    def filtered(self) -> bool:
        return self.action == "reject"

    @property
    def soft_flag(self) -> bool:
        return self.action == "soft_keep"


def _lifecycle(value: Any) -> EdgeLifecycleV3 | None:
    if value is None:
        return None
    try:
        return EdgeLifecycleV3(value)
    except ValueError:
        return None


def _relation(value: Any) -> RelationType | None:
    if value is None:
        return None
    try:
        return RelationType(value)
    except ValueError:
        return None


def _mapping_lifecycle(mapping: MappingView, subject: tuple[str, str]) -> EdgeLifecycleV3 | None:
    """Best-effort lookup of the mapping edge's lifecycle for ``subject``."""
    if isinstance(mapping, MappingLifecycleView):
        return _lifecycle(mapping.edge_lifecycle(subject))

    edges_fn = getattr(mapping, "edges", None)
    if callable(edges_fn):
        try:
            edges = edges_fn()
        except TypeError:  # pragma: no cover - defensive for odd test doubles
            return None
        for edge in edges:
            if getattr(edge, "subject_id", None) != subject[1]:
                continue
            kind = getattr(edge, "subject_kind", None)
            if kind is not None and kind != subject[0]:
                continue
            return _lifecycle(getattr(edge, "lifecycle", None))
    return None


def _path_views(operator: ObservationOperator, mapping: MappingView) -> list[Any]:
    views: list[Any] = []
    for obj in (
        mapping,
        getattr(mapping, "topology", None),
        operator,
        getattr(operator, "topology", None),
    ):
        if obj is not None and isinstance(obj, TopologyPathsView) and obj not in views:
            views.append(obj)
    return views


def _entries_path_edges(
    source: str,
    target: str,
    subject: tuple[str, str],
    operator: ObservationOperator,
) -> tuple[PathEdge, ...]:
    """Fallback path evidence derived from the operator's soft alternatives.

    The K08 operator does not carry CONFIRMED path relations, so a confirmed
    incompatibility can only be asserted by a view implementing
    :class:`TopologyPathsView`.  Soft (PROVISIONAL) alternatives *are* carried
    by the operator and are enough to detect a provisional incompatibility.
    """
    edges: list[PathEdge] = []
    for alt in operator.soft_alternatives:
        if {alt.source_endpoint_id, alt.target_endpoint_id} == {source, target}:
            edges.append(
                PathEdge(
                    relation=_relation(alt.relation),
                    lifecycle=_lifecycle(alt.lifecycle),
                    edge_id=alt.edge_id,
                )
            )
    if edges:
        return tuple(edges)

    try:
        column = operator.subjects.index(subject)
    except ValueError:
        column = None
    row_ids = {row.reader_id: i for i, row in enumerate(operator.rows)}
    for reader in (source, target):
        row = row_ids.get(reader)
        if row is None:
            continue
        if column is not None:
            meta = operator.entries.get((row, column))
            if meta is not None and meta.edge_lifecycle is not None:
                edges.append(PathEdge(relation=None, lifecycle=_lifecycle(meta.edge_lifecycle)))
            continue
        for (i, j), meta in operator.entries.items():
            if i != row or meta.edge_lifecycle is None:
                continue
            try:
                if operator.H[i, j] != 0:
                    edges.append(PathEdge(relation=None, lifecycle=_lifecycle(meta.edge_lifecycle)))
                    break
            except (IndexError, TypeError):  # pragma: no cover - defensive
                continue
    return tuple(edges)


def _path_edges(
    source: str,
    target: str,
    subject: tuple[str, str],
    operator: ObservationOperator,
    mapping: MappingView,
) -> tuple[PathEdge, ...]:
    for view in _path_views(operator, mapping):
        edges = view.path_edges(source, target)
        result = tuple(
            PathEdge(
                relation=_relation(getattr(edge, "relation", None)),
                lifecycle=_lifecycle(getattr(edge, "lifecycle", None)),
                edge_id=str(getattr(edge, "edge_id", "")),
            )
            for edge in edges
        )
        if result:
            return result
    return _entries_path_edges(source, target, subject, operator)


def _non_containing(edge: PathEdge, lifecycle: EdgeLifecycleV3) -> bool:
    return edge.lifecycle is lifecycle and edge.relation in _NON_CONTAINING


def _soft_or_keep(
    subject: tuple[str, str],
    affected: tuple[str, ...],
    mapped: str | None,
    operator: ObservationOperator,
    mapping: MappingView,
) -> FilterVerdict:
    for reader in affected:
        start = mapped if mapped is not None else reader
        edges = _path_edges(start, reader, subject, operator, mapping)
        if any(_non_containing(edge, EdgeLifecycleV3.PROVISIONAL) for edge in edges):
            return FilterVerdict(
                "soft_keep", "provisional non-containing topology path (soft, kept)"
            )
    return FilterVerdict("keep")


def evaluate_topology(
    subject: tuple[str, str],
    affected_readers: Sequence[str],
    operator: ObservationOperator,
    mapping: MappingView,
) -> FilterVerdict:
    """Evaluate one subject against the group's affected readers.

    ``subject`` is a ``(subject_kind, subject_id)`` pair.  The verdict records
    whether the candidate should be rejected, kept with a soft flag, or kept
    unflagged.  Nothing here mutates its inputs.
    """
    affected = tuple(affected_readers)
    if not affected or subject[0] == "unknown_scope":
        return FilterVerdict("keep")

    try:
        mapped = mapping.most_specific_reader(subject)
    except (TypeError, KeyError):  # pragma: no cover - defensive
        mapped = None

    mapping_lifecycle = _mapping_lifecycle(mapping, subject)
    confirmed_elsewhere = (
        mapping_lifecycle is EdgeLifecycleV3.CONFIRMED and mapped not in affected
    )
    if not confirmed_elsewhere:
        # A PROVISIONAL/CANDIDATE/absent mapping edge is soft, never a hard
        # rejection reason (DEC-10/DEC-11, README non-negotiables).
        return _soft_or_keep(subject, affected, mapped, operator, mapping)

    saw_path = False
    for reader in affected:
        start = mapped if mapped is not None else reader
        edges = _path_edges(start, reader, subject, operator, mapping)
        if edges:
            saw_path = True
        if not any(_non_containing(edge, EdgeLifecycleV3.CONFIRMED) for edge in edges):
            return _soft_or_keep(subject, affected, mapped, operator, mapping)

    if saw_path:
        reason = "confirmed non-containing topology path; mapping edge CONFIRMED elsewhere"
        logger.debug("filtering candidate %s: %s", subject, reason)
        return FilterVerdict("reject", reason)
    return _soft_or_keep(subject, affected, mapped, operator, mapping)


__all__ = [
    "PROVISIONAL_FLAG",
    "FilterVerdict",
    "MappingLifecycleView",
    "PathEdge",
    "TopologyPathsView",
    "evaluate_topology",
]
