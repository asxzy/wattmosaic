"""L0–L4 column tree builder (M1, design §8 / HB §9.1).

The primary display hierarchy comes from T1's ``primary_display_tree()``; this
module normalises that structure (plus the secondary topology edges) into the
``ColumnNode`` tree owned by K05.  Every measurement endpoint becomes exactly
one L4 reader leaf.  Secondary relations (SHARED_LOAD, PAIRED_LEG,
SAME_CONDUCTOR, CORRELATED and PROVISIONAL CONTAINS) are attached as
``annotations`` to leaves — they never create duplicate leaves (AT-13).

Input shapes accepted for the display tree
------------------------------------------
T1 may return either

* a nested mapping ``{label: children}`` where a leaf value is an endpoint
  (or an empty map whose key names an endpoint); or
* an explicit node list, each node a mapping/object with ``label``, ``level``
  (``L1``..``L4``), ``children`` and optionally ``reader_id`` / ``endpoint_id``.

The function is pure: it reads the injected topology view and endpoints and
touches no clock and no database.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from typing import Any

from wattmosaic_engine.contracts import (
    ColumnAnnotation,
    ColumnNode,
    EdgeLifecycleV3,
    RelationType,
)

__all__: list[str] = ["build_columns"]

_DOMAIN_LABEL = "Electrical measurement domain"
_UNASSIGNED_LABEL = "Unassigned measurements"

_SECONDARY_KINDS: dict[str, str] = {
    RelationType.SHARED_LOAD.value: "SHARED_LOAD",
    RelationType.PAIRED_LEG.value: "PAIRED_LEG",
    RelationType.SAME_CONDUCTOR.value: "SAME_CONDUCTOR",
    RelationType.CORRELATED.value: "CORRELATED",
}

# Depth -> (level, kind) for nested-map trees without an explicit level.
_DEPTH_LEVELS: tuple[tuple[str, str], ...] = (
    ("L1", "panel"),
    ("L2", "subpanel"),
    ("L3", "circuit"),
)
_LEVEL_KIND: dict[str, str] = {
    "L1": "panel",
    "L2": "subpanel",
    "L3": "circuit",
}

# Endpoint ids known to the current ``build_columns`` call.  Grouping labels
# that also name an endpoint emit a reader leaf at that branch (a parent CT is
# itself a measurement endpoint, HB §9.1).  Set once per pure call, so there is
# no cross-call state.
_ENDPOINT_IDS: frozenset[str] = frozenset()


def _get(item: Any, *names: str) -> Any:
    if isinstance(item, MappingABC):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _reader_id_of(endpoint: Any) -> str | None:
    if isinstance(endpoint, str):
        return endpoint
    value = _get(endpoint, "reader_id", "endpoint_id", "id")
    return value if isinstance(value, str) and value else None


def _label_of(endpoint: Any, fallback: str = "") -> str:
    if isinstance(endpoint, str):
        return endpoint
    value = _get(endpoint, "display_name", "label", "name")
    if isinstance(value, str) and value:
        return value
    return fallback or str(_reader_id_of(endpoint) or endpoint)


def _is_endpoint(item: Any) -> bool:
    if isinstance(item, str):
        return True
    if isinstance(item, MappingABC):
        if "children" in item and (
            "label" in item or "level" in item or "reader_id" in item
        ):
            return False  # explicit node, not a bare endpoint
        return any(k in item for k in ("endpoint_id", "reader_id"))
    return any(hasattr(item, k) for k in ("endpoint_id", "reader_id"))


# --------------------------------------------------------------------------- #
# Normalisation into a list of internal nodes:
#   (label, level|None, kind|None, reader_id|None, children)
# --------------------------------------------------------------------------- #


def _normalise_nodes(tree: Any, depth: int = 0) -> list[tuple[Any]]:
    if tree is None:
        return []
    if isinstance(tree, MappingABC):
        if _is_endpoint(tree):
            return [(_label_of(tree), "L4", "reader", _reader_id_of(tree), [])]
        result: list[tuple[Any]] = []
        for label, value in tree.items():
            result.extend(_normalise_mapping_entry(str(label), value, depth))
        return result
    if isinstance(tree, Sequence) and not isinstance(tree, (str, bytes)):
        result: list[tuple[Any]] = []
        for item in tree:
            result.extend(_normalise_nodes(item, depth))
        return result
    # A node object with label/level/children attributes.
    return _normalise_explicit(tree, depth)


def _normalise_mapping_entry(
    label: str, value: Any, depth: int
) -> list[tuple[Any]]:
    if _is_endpoint(value):
        rid = _reader_id_of(value)
        return [(_label_of(value, label), "L4", "reader", rid or label, [])]
    if value is None or (isinstance(value, MappingABC) and not value):
        # Empty map: the key names a reader leaf.
        return [(label, "L4", "reader", label, [])]
    # A grouping with children.  If the grouping label itself names an
    # endpoint (e.g. a parent CT that is also a reader), it gets its own
    # reader leaf alongside the sub-groups (one endpoint == one leaf).
    level, kind = _DEPTH_LEVELS[min(depth, len(_DEPTH_LEVELS) - 1)]
    children = _normalise_nodes(value, depth + 1)
    own_reader = label if label in _ENDPOINT_IDS else None
    node = (label, level, kind, None, children)
    if own_reader is not None:
        return [
            (_label_of(label, label), "L4", "reader", own_reader, []),
            node,
        ]
    return [node]


def _normalise_explicit(node: Any, depth: int) -> list[tuple[Any]]:
    label = str(_get(node, "label", "name") or "")
    reader_id = _reader_id_of(node)
    level = _get(node, "level")
    children_raw = _get(node, "children", ()) or ()
    children = _normalise_nodes(children_raw, depth + 1)
    if reader_id is not None:
        return [(_label_of(node, label), "L4", "reader", reader_id, [])]
    level_text = str(level) if level is not None else _DEPTH_LEVELS[min(depth, len(_DEPTH_LEVELS) - 1)][0]
    kind = _get(node, "kind") or _LEVEL_KIND.get(level_text, "panel")
    return [(label, level_text, str(kind), None, children)]


# --------------------------------------------------------------------------- #
# ColumnNode construction.
# --------------------------------------------------------------------------- #


def _build_column(node: tuple[Any]) -> ColumnNode | None:
    label, level, kind, reader_id, children = node
    built_children = [child for child in (_build_column(c) for c in children) if child is not None]
    if reader_id is not None and kind == "reader":
        # A reader node is a leaf at L4.
        return ColumnNode(
            node_id=f"col::reader::{reader_id}",
            level="L4",
            label=str(label),
            kind="reader",
            reader_id=str(reader_id),
        )
    if not built_children:
        # A grouping node with no real children is not emitted.
        return None
    return ColumnNode(
        node_id=f"col::{kind}::{label}",
        level=level,
        label=str(label),
        kind=kind,
        children=built_children,
    )


def _reader_leaves(node: ColumnNode) -> list[ColumnNode]:
    out: list[ColumnNode] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.kind == "reader":
            out.append(current)
        else:
            stack.extend(reversed(current.children))
    return out


def _annotations_for(
    topology_view: Any, reader_id: str, all_reader_ids: set[str]
) -> list[ColumnAnnotation]:
    edges_getter = getattr(topology_view, "edges", None)
    if not callable(edges_getter):
        return []
    annotations: list[ColumnAnnotation] = []
    for edge in edges_getter():
        relation = _get(edge, "relation")
        lifecycle = _get(edge, "lifecycle")
        source = _get(edge, "source_endpoint_id")
        target = _get(edge, "target_endpoint_id")
        if source != reader_id and target != reader_id:
            continue
        other = target if source == reader_id else source
        rel = str(relation.value) if hasattr(relation, "value") else str(relation)
        life = str(lifecycle.value) if hasattr(lifecycle, "value") else str(lifecycle)
        kind: str | None = _SECONDARY_KINDS.get(rel)
        if kind is None and (
            rel == RelationType.CONTAINS.value
            and life == EdgeLifecycleV3.PROVISIONAL.value
        ):
            kind = "CONTAINS"
        if kind is None:
            continue
        annotations.append(
            ColumnAnnotation(
                kind=kind,  # type: ignore[arg-type]
                related_reader_id=other if other in all_reader_ids else None,
            )
        )
    return annotations


def build_columns(topology_view: Any, endpoints: Sequence[Any]) -> list[ColumnNode]:
    """Build the L0–L4 column tree.

    Every endpoint becomes exactly one L4 reader leaf.  Endpoints the display
    tree does not place go under a single L1 ``"Unassigned measurements"``
    branch, so the leaf count always equals the endpoint count.
    """
    global _ENDPOINT_IDS
    endpoint_index: dict[str, Any] = {}
    for endpoint in endpoints:
        rid = _reader_id_of(endpoint)
        if rid is not None:
            endpoint_index[rid] = endpoint
    _ENDPOINT_IDS = frozenset(endpoint_index)

    tree_getter = getattr(topology_view, "primary_display_tree", None)
    tree = tree_getter() if callable(tree_getter) else None
    normalised = _normalise_nodes(tree)

    top_children: list[ColumnNode] = []
    placed: set[str] = set()
    for node in normalised:
        column = _build_column(node)
        if column is None:
            continue
        for leaf in _reader_leaves(column):
            placed.add(str(leaf.reader_id))
        top_children.append(column)

    unassigned: list[ColumnNode] = []
    for endpoint in endpoints:
        rid = _reader_id_of(endpoint)
        if rid and rid not in placed:
            unassigned.append(
                ColumnNode(
                    node_id=f"col::reader::{rid}",
                    level="L4",
                    label=_label_of(endpoint, rid),
                    kind="reader",
                    reader_id=rid,
                )
            )
    if unassigned:
        top_children.append(
            ColumnNode(
                node_id="col::unassigned",
                level="L1",
                label=_UNASSIGNED_LABEL,
                kind="unassigned",
                children=unassigned,
            )
        )

    all_reader_ids = {rid for rid in placed} | {
        str(_reader_id_of(e)) for e in endpoints if _reader_id_of(e)
    }
    for column in top_children:
        for leaf in _reader_leaves(column):
            anns = _annotations_for(topology_view, str(leaf.reader_id), all_reader_ids)
            if anns:
                leaf.annotations = anns

    domain = ColumnNode(
        node_id="col::domain",
        level="L0",
        label=_DOMAIN_LABEL,
        kind="domain",
        children=top_children,
    )
    return [domain]
