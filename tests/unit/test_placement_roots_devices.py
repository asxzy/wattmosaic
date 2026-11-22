"""ADR-0046: the placement tree carries device nodes and honours user moves.

``EngineCore._placement_roots`` is exercised unbound with simple stubs (it uses
no engine state).  These tests fail before the change: device nodes, the
"Unplaced devices" group, ``user_root`` and the "set by you" chip are new.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from wattmosaic_engine.core.snapshot import SnapshotMixin
from wattmosaic_engine.electrical.topology_placement import Placement

_placement_roots = SnapshotMixin._placement_roots


def _graph(reader_ids: list[str]) -> Any:
    return SimpleNamespace(
        endpoints=[
            SimpleNamespace(endpoint_id=rid, display_name=rid.replace("_", " ").title())
            for rid in reader_ids
        ]
    )


def _report(by_child: dict[str, Placement]) -> Any:
    return SimpleNamespace(by_child=by_child)


def _subjects(reader: str, *device_ids: str) -> dict[str, list[dict[str, Any]]]:
    return {
        reader: [
            {"device_id": did, "label": did.replace("_", " ").title(), "lifecycle": "confirmed"}
            for did in device_ids
        ]
    }


def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes:
        out.append(node)
        out.extend(_walk(node.get("children") or []))
    return out


def _circuit(nodes: list[dict[str, Any]], reader: str) -> dict[str, Any]:
    return next(n for n in _walk(nodes) if n.get("reader_id") == reader)


def test_devices_append_after_subcircuits_of_placed_circuit() -> None:
    graph = _graph(["ct_feed", "ct_sub"])
    report = _report({
        "ct_feed": Placement(child="ct_feed"),
        "ct_sub": Placement(child="ct_sub", parent="ct_feed"),
    })
    roots = _placement_roots(
        None, graph, report, [], {"ct_feed": _subjects("ct_feed", "dev_oven")["ct_feed"]}
    )
    feed = _circuit(roots, "ct_feed")
    kinds = [child["kind"] for child in feed["children"]]
    assert kinds == ["circuit", "device"]
    device = feed["children"][-1]
    assert device["kind"] == "device"
    assert device["device_id"] == "dev_oven"
    assert device["depth_hint"] == 2


def test_devices_under_unplaced_measurement_group() -> None:
    graph = _graph(["ct_feed"])
    report = _report({"ct_feed": Placement(child="ct_feed")})
    roots = _placement_roots(
        None, graph, report, [], {"ct_feed": _subjects("ct_feed", "dev_kettle")["ct_feed"]}
    )
    group = next(n for n in roots if n.get("id") == "group:Unplaced measurements:1")
    feed = group["children"][0]
    assert [c["device_id"] for c in feed["children"] if c["kind"] == "device"] == ["dev_kettle"]


def test_unknown_reader_devices_get_unplaced_devices_group() -> None:
    graph = _graph(["ct_feed"])
    report = _report({"ct_feed": Placement(child="ct_feed")})
    roots = _placement_roots(
        None, graph, report, [], _subjects("ct_unknown", "dev_ghost")
    )
    group = next(n for n in roots if n.get("id") == "group:Unplaced devices:1")
    assert group["label"] == "Unplaced devices"
    assert [c["device_id"] for c in group["children"]] == ["dev_ghost"]


def test_no_unplaced_devices_group_when_empty() -> None:
    roots = _placement_roots(None, _graph(["ct_feed"]), _report({}), [], {})
    assert not any(n.get("id") == "group:Unplaced devices:1" for n in roots)


def test_user_root_child_is_top_level_not_in_unplaced_group() -> None:
    graph = _graph(["ct_feed", "ct_oven"])
    report = _report({
        "ct_feed": Placement(child="ct_feed"),
        "ct_oven": Placement(child="ct_oven", reason="user_root"),
    })
    roots = _placement_roots(None, graph, report, [], {})
    top = [n.get("reader_id") for n in roots if n["kind"] == "circuit"]
    assert "ct_oven" in top
    # A user-root circuit is never listed under "Unplaced measurements".
    unplaced = next((n for n in roots if n.get("id") == "group:Unplaced measurements:1"), None)
    if unplaced:
        assert "ct_oven" not in [c.get("reader_id") for c in unplaced["children"]]
    assert _circuit(roots, "ct_oven")["placement_reason"] == "user_root"


def test_user_reason_child_has_chip_and_is_structural() -> None:
    graph = _graph(["ct_feed", "ct_oven"])
    report = _report({
        "ct_feed": Placement(child="ct_feed"),
        "ct_oven": Placement(child="ct_oven", parent="ct_feed", reason="user",
                             band="high", score=1.0),
    })
    roots = _placement_roots(None, graph, report, [], {})
    oven = _circuit(roots, "ct_oven")
    assert oven["structural"] is True
    assert {"text": "set by you", "tone": "info"} in oven["chips"]
    assert oven["placement_reason"] == "user"


def test_every_circuit_node_carries_placement_reason() -> None:
    graph = _graph(["ct_feed", "ct_sub"])
    report = _report({
        "ct_feed": Placement(child="ct_feed"),
        "ct_sub": Placement(child="ct_sub", parent="ct_feed"),
    })
    roots = _placement_roots(None, graph, report, [], {})
    for node in _walk(roots):
        if node["kind"] == "circuit":
            assert "placement_reason" in node
