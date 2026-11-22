"""Panel display tree (``GET /topology/tree``) from the v3 topology graph.

The hierarchy is T1's ``primary_display_tree()`` over the active version's
confirmed CONTAINS edges, and every leaf is a
:class:`~wattmosaic_engine.contracts.MeasurementEndpoint` reader.  The panel
envelope (``version``/``status``/``stats``/``roots``/``flat``/``notes``) is the
wire shape the panel renderer reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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


def device_node(
    subject: Mapping[str, Any], reader_id: str, depth: int
) -> dict[str, Any]:
    """Build one ``kind: "device"`` display node for a mapped subject.

    Public so read-time tree builders (``SnapshotMixin._placement_roots``) can
    attach the same device rows the classic display tree uses.  The shape is
    deliberately identical to the one ``build_display_tree_v3`` has always
    emitted; this function never appends to a caller's bookkeeping list.
    """
    lifecycle = subject.get("lifecycle")
    return {
        "id": f"device:{subject['device_id']}@{reader_id}",
        "kind": "device",
        "label": subject.get("label") or subject["device_id"],
        "device_id": subject["device_id"],
        "depth_hint": depth,
        "reader_id": reader_id,
        "latest_power_w": None,
        "measurement": None,
        "mapping_lifecycle": lifecycle,
        "declared_by": subject.get("declared_by"),
        "subtitle": f"mapped to this circuit ({lifecycle})" if lifecycle else None,
        "children": [],
    }


def build_display_tree_v3(
    graph: TopologyGraph,
    *,
    generated_at: Any = None,
    devices_by_reader: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the panel wire shape from T1's ``TopologyGraph`` (Z1).

    The hierarchy comes from T1's
    ``primary_display_tree()`` over confirmed CONTAINS edges, and every leaf is
    a :class:`~wattmosaic_engine.contracts.MeasurementEndpoint` reader.  The
    panel envelope (``version``/``status``/``stats``/``roots``/``flat``/
    ``notes``) is kept unchanged so the renderer needs no edit.

    ``generated_at`` is injected by the caller; this module never reads a clock.
    Absent values are ``None``, never 0.0.

    ``devices_by_reader`` maps a reader to the subjects the active *mapping*
    places on it (``device_id``, ``label``, ``lifecycle``, ``declared_by``).
    A mapping edge names the most specific reader and ancestors come from
    topology (contract ``MappingEdge``), so each subject is drawn under its
    reader wherever that reader sits -- under "Unassigned measurements" while
    the reader has no confirmed parent, and with the reader once topology finds
    one.  Placing a device never waits for topology.
    """
    nodes: tuple[DisplayNode, ...] = graph.primary_display_tree()
    devices_by_reader = devices_by_reader or {}
    placed_devices: list[dict[str, Any]] = []

    def _device(subject: Mapping[str, Any], reader_id: str, depth: int) -> dict[str, Any]:
        node = device_node(subject, reader_id, depth)
        placed_devices.append(node)
        return node

    def _node(node: DisplayNode, depth: int) -> dict[str, Any]:
        children = [_node(child, depth + 1) for child in node.children]
        # A leaf is an L4 reader node; internal nodes are grouping/panel nodes.
        is_reader = node.reader_id is not None
        if is_reader:
            children.extend(
                _device(subject, str(node.reader_id), depth + 1)
                for subject in devices_by_reader.get(str(node.reader_id), ())
            )
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
    placed_readers = {str(node["reader_id"]) for node in all_nodes if node["kind"] == "circuit"}
    stats = {
        "panels": 0,
        "circuits": sum(1 for node in all_nodes if node["kind"] == "circuit"),
        "devices": len({node["device_id"] for node in placed_devices}),
        # Mapped onto a reader the topology does not know: never dropped silently.
        "unplaced_devices": len(
            {
                str(subject["device_id"])
                for reader_id, subjects in devices_by_reader.items()
                if reader_id not in placed_readers
                for subject in subjects
            }
        ),
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
        "flat": {
            "panels": [],
            "circuits": [],
            "devices": [
                {"device_id": node["device_id"], "label": node["label"], "reader_id": node["reader_id"]}
                for node in placed_devices
            ],
        },
        "notes": list(graph.problems),
    }


__all__ = ["TREE_VERSION", "build_display_tree_v3", "device_node"]
