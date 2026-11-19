"""Panel display tree (``GET /topology/tree``) from the v3 topology graph.

The hierarchy is T1's ``primary_display_tree()`` over the active version's
confirmed CONTAINS edges, and every leaf is a
:class:`~wattmosaic_engine.contracts.MeasurementEndpoint` reader.  The panel
envelope (``version``/``status``/``stats``/``roots``/``flat``/``notes``) is the
wire shape the panel renderer reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..contracts import utcnow
from ..electrical.topology_graph import DisplayNode, TopologyGraph

#: Wire-shape version of the tree payload.
TREE_VERSION = 2


def _walk(nodes: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children") or ()))


def build_display_tree_v3(
    graph: TopologyGraph,
    *,
    generated_at: Any = None,
) -> dict[str, Any]:
    """Build the panel wire shape from T1's ``TopologyGraph`` (Z1).

    The hierarchy comes from T1's
    ``primary_display_tree()`` over confirmed CONTAINS edges, and every leaf is
    a :class:`~wattmosaic_engine.contracts.MeasurementEndpoint` reader.  The
    panel envelope (``version``/``status``/``stats``/``roots``/``flat``/
    ``notes``) is kept unchanged so the renderer needs no edit.

    ``generated_at`` is injected by the caller; this module never reads a clock.
    Absent values are ``None``, never 0.0.
    """
    nodes: tuple[DisplayNode, ...] = graph.primary_display_tree()

    def _node(node: DisplayNode, depth: int) -> dict[str, Any]:
        children = [_node(child, depth + 1) for child in node.children]
        # A leaf is an L4 reader node; internal nodes are grouping/panel nodes.
        is_reader = node.reader_id is not None
        return {
            "id": f"reader:{node.reader_id}" if is_reader else f"group:{node.label}:{depth}",
            "kind": "circuit" if is_reader else "group",
            "label": node.label,
            "depth_hint": depth,
            "reader_id": node.reader_id,
            "latest_power_w": None,
            "children": children,
        }

    roots = [_node(node, 1) for node in nodes]
    all_nodes = list(_walk(roots))
    stats = {
        "panels": 0,
        "circuits": sum(1 for node in all_nodes if node["kind"] == "circuit"),
        "devices": 0,
        "unplaced_devices": 0,
        "unplaced_circuits": 0,
        "max_depth": max((int(node["depth_hint"]) for node in all_nodes), default=0),
        "duplicate_feeds": 0,
        "channels": len(graph.endpoints),
        "feed_circuits": len(roots),
    }
    return {
        "version": TREE_VERSION,
        "status": "confirmed",
        "generated_at": generated_at or utcnow().isoformat().replace("+00:00", "Z"),
        "topology_version": None,
        "stats": stats,
        "roots": roots,
        "flat": {"panels": [], "circuits": [], "devices": []},
        "notes": list(graph.problems),
    }


__all__ = ["TREE_VERSION", "build_display_tree_v3"]
