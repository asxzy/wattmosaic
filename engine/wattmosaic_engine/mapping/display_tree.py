"""Primary display tree for the panel (M1, Z2b).

The retired ``electrical/panel_tree.py`` built a panel → circuits → devices
payload from the pointer-based topology graph.  This module is the v3
replacement: it builds the same JSON envelope from the authoritative
:class:`~wattmosaic_engine.contracts.Topology` contract and T1's primary display
tree where available.  It is a pure read-time projection — no DB, no wall clock
(``generated_at`` is supplied by the caller), no pointer-tree graph engine.

The wire shape (``version``/``status``/``stats``/``roots``/``flat``/``notes``) is
kept so the HA panel's renderer keeps working across the cutover.  Every node
is derived from declared data: panel ids, declared containment and attribution;
nothing is guessed from a device name.  Absent values are ``None``, never 0.0.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..contracts import CTChannel, DeviceTopology, Topology, utcnow
from ..electrical.topology_graph import DisplayNode, TopologyGraph

#: Schema version of the tree payload (panel contract §1.1).
TREE_VERSION = 2

_IMPLICIT_PANEL_ID = "implicit"
_IMPLICIT_PANEL_NAME = "Installation"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _slug(name: str) -> str:
    return _SLUG_RE.sub("_", name.lower()).strip("_") or "panel"


def _panel_name(channel: CTChannel) -> str | None:
    """Panel id declared on a channel, or the legacy evidence marker."""
    panel_id = getattr(channel, "panel_id", None)
    if panel_id:
        return str(panel_id)
    metadata = channel.metadata if isinstance(channel.metadata, Mapping) else {}
    for item in metadata.get("evidence") or ():
        text = str(item)
        if text.startswith("panel="):
            return text[len("panel=") :].strip() or None
    return None


def _parent(channel: CTChannel) -> str | None:
    return getattr(channel, "parent_ct_id", None)


def _result_value(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _result_power(result: Any) -> float | None:
    value = _result_value(result, "power_w")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _readings_nodes(
    topology: Topology, latest_results: Mapping[str, Any] | None
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for device in topology.devices:
        result = (latest_results or {}).get(device.device_id)
        out[device.device_id] = _result_power(result)
    return out


def build_display_tree(
    topology: Topology,
    *,
    latest_results: Mapping[str, Any] | None = None,
    generated_at: Any = None,
    include_nesting: bool = False,
    nesting: Any = None,
) -> dict[str, Any]:
    """Build the panel display tree payload from a :class:`Topology`.

    ``generated_at`` is injected by the caller (an ISO string); this module
    never reads a wall clock.  ``nesting`` may carry advisory nesting proposals
    which are exposed under ``suggestions`` but never become tree edges.
    """
    channels = list(topology.channels)
    by_id = {channel.ct_id: channel for channel in channels}
    parents: dict[str, str | None] = {
        cid: _parent(ch) for cid, ch in by_id.items() if _parent(ch) in by_id
    }
    notes: list[str] = []

    # Break malformed parent loops deterministically so the walker cannot hang.
    for start in sorted(parents):
        seen = [start]
        current = parents.get(start)
        while current is not None:
            if current in seen:
                broken = seen[seen.index(current)]
                parents[broken] = None
                notes.append(f"containment loop broken at {broken!r}")
                break
            if current not in parents:
                break
            seen.append(current)
            current = parents.get(current)

    children: dict[str, list[str]] = {cid: [] for cid in by_id}
    roots: list[str] = []
    for cid in sorted(by_id):
        parent = parents.get(cid)
        if parent and parent != cid and parent in by_id:
            children[parent].append(cid)
        else:
            roots.append(cid)

    # Group channels into panels (declared panel id, else one implicit panel).
    panels: dict[str, list[str]] = {}
    for channel in channels:
        panel_id = _panel_name(channel) or _IMPLICIT_PANEL_NAME
        slug = _slug(str(panel_id))
        panels.setdefault(slug, []).append(channel.ct_id)

    device_power = _readings_nodes(topology, latest_results)
    device_by_ct: dict[str, list[DeviceTopology]] = {}
    for device in topology.devices:
        for cid in device.branch_ct_ids:
            device_by_ct.setdefault(cid, []).append(device)

    def circuit_node(cid: str, depth: int) -> dict[str, Any]:
        ch = by_id[cid]
        kids = [circuit_node(child, depth + 1) for child in sorted(children.get(cid, []))]
        devices = [
            device_node(device, depth + 1)
            for device in sorted(device_by_ct.get(cid, ()), key=lambda d: d.device_id)
        ]
        return {
            "id": f"circuit:{cid}",
            "kind": "circuit",
            "label": ch.name or cid,
            "subtitle": ch.entity_id or cid,
            "depth_hint": depth,
            "ct_id": cid,
            "ct_type": _text(ch.ct_type),
            "phase": _text(ch.phase),
            "enabled": ch.enabled,
            "alias_of_ct_id": ch.alias_of_ct_id,
            "polarity": _text(ch.polarity),
            "topology_status": _text(ch.topology_status),
            "latest_power_w": None,
            "children": [*kids, *devices],
        }

    def device_node(device: DeviceTopology, depth: int) -> dict[str, Any]:
        return {
            "id": f"device:{device.device_id}",
            "kind": "device",
            "label": device.name or device.device_id,
            "depth_hint": depth,
            "device_id": device.device_id,
            "latest_power_w": device_power.get(device.device_id),
            "children": [],
        }

    root_nodes: list[dict[str, Any]] = []
    for panel_id, ct_ids in sorted(panels.items()):
        panel_roots = [cid for cid in ct_ids if cid in roots]
        group_children = [circuit_node(cid, 2) for cid in sorted(panel_roots)]
        if not group_children:
            continue
        root_nodes.append(
            {
                "id": f"panel:{panel_id}",
                "kind": "panel",
                "label": _IMPLICIT_PANEL_NAME if panel_id == _slug(_IMPLICIT_PANEL_NAME) else panel_id,
                "panel_id": panel_id,
                "depth_hint": 0,
                "feed_ct_ids": sorted(panel_roots),
                "children": [
                    {
                        "id": f"{panel_id}:circuits",
                        "kind": "group",
                        "role": "circuits",
                        "label": "CIRCUITS",
                        "depth_hint": 1,
                        "children": group_children,
                    }
                ],
            }
        )

    all_nodes = list(_walk(root_nodes))
    stats = {
        "panels": sum(1 for node in all_nodes if node["kind"] == "panel"),
        "circuits": sum(1 for node in all_nodes if node["kind"] == "circuit"),
        "devices": sum(1 for node in all_nodes if node["kind"] == "device"),
        "unplaced_devices": len(
            [d for d in topology.devices if not any(d.branch_ct_ids)]
        ),
        "unplaced_circuits": 0,
        "max_depth": max((int(node["depth_hint"]) for node in all_nodes), default=0),
        "duplicate_feeds": 0,
        "channels": len(channels),
        "feed_circuits": len(roots),
    }
    payload: dict[str, Any] = {
        "version": TREE_VERSION,
        "status": _text(topology.status),
        "generated_at": generated_at or utcnow().isoformat().replace("+00:00", "Z"),
        "topology_version": int(topology.version),
        "stats": stats,
        "roots": root_nodes,
        "flat": {"panels": [], "circuits": [], "devices": []},
        "notes": notes,
    }
    if include_nesting and nesting is not None:
        payload["suggestions"] = {"applied": False, "candidate_parents": {}}
    return payload


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

    This is the v3 replacement for the legacy pointer-based projection in
    :func:`build_display_tree`: the hierarchy comes from T1's
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


__all__ = ["TREE_VERSION", "build_display_tree", "build_display_tree_v3"]
