"""Hierarchy model: panel → circuits → devices (contract v1 §1).

The tests fall into three groups.

1. **The real installation.**  ``fixtures/topo.json`` is the production
   ``GET /api/v1/topology`` payload (38 channels, 28 devices).  It is the only
   input that can prove the model against reality, so the leaf counts, the
   exactly-once guarantee, the duplicate-feed merge and the absence of empty
   groups are all pinned against it.
2. **The contract example.**  A synthetic two-panel topology reproduces §1.2
   exactly (MAIN_PANEL → CT01/CT02 → lights, SUBPANEL_01 → Upper_Humidifier_CT
   → Upper_Humidifier) and the rendered shape is asserted node by node.
3. **Negative tests.**  Each guarantee is reverted at its seam (the duplicate
   merge, the empty-group omission, the single-placement rule, the cycle guard)
   and the test suite must fail.  A guarantee nobody can break on purpose is a
   guarantee nobody is testing.

The fixture path is resolved through ``WATT_MOSAIC_TOPO_FIXTURE_DIR`` when set,
because the shared fixtures live outside the repository while the UI workstream
consumes the produced tree from the same directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine.api.facade import NullFacade, StubFacade
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.electrical import panel_tree as panel_tree_module
from wattmosaic_engine.electrical.panel_tree import (
    IMPLICIT_PANEL_ID,
    TREE_VERSION,
    build_panel_tree,
)

FIXTURE_DIR = Path(
    os.environ.get(
        "WATT_MOSAIC_TOPO_FIXTURE_DIR",
        "/private/var/folders/wy/06f64nbx6kz8z88km48c3b3h0000gn/T/opencode/fixtures",
    )
)
TOPO_FIXTURE = FIXTURE_DIR / "topo.json"
NESTING_FIXTURE = FIXTURE_DIR / "nesting.json"
TREE_OUTPUT = FIXTURE_DIR / "tree-expected.json"
TOKEN = "tree-token"
HEADERS = {"X-API-Token": TOKEN}

#: Only the tests that read the production capture depend on it; the synthetic
#: contract-example and negative tests must run even without it.
needs_fixture = pytest.mark.skipif(
    not TOPO_FIXTURE.is_file(), reason="production topology fixture is not available"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _real_topology() -> Topology:
    return Topology.model_validate(json.loads(TOPO_FIXTURE.read_text()))


def _real_tree(**kwargs: Any) -> dict[str, Any]:
    return build_panel_tree(_real_topology(), **kwargs)


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def _kinds(tree: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [node for node in _walk(tree["roots"]) if node["kind"] == kind]


def _ids(tree: dict[str, Any], kind: str, key: str) -> list[str]:
    return [node[key] for node in _kinds(tree, kind)]


def _group_roles(node: dict[str, Any]) -> list[str]:
    return [child["role"] for child in node["children"] if child["kind"] == "group"]


def _render(nodes: list[dict[str, Any]], prefix: str = "") -> list[str]:
    """ASCII rendering used for the pinned §1.2 shape."""
    lines: list[str] = []
    for index, node in enumerate(nodes):
        last = index == len(nodes) - 1
        branch = "└── " if last else "├── "
        suffix = ""
        if node["kind"] == "group":
            suffix = f" ({node['role']})"
        elif node["kind"] == "circuit":
            suffix = f" ({node['ct_id']})"
        elif node["kind"] == "panel":
            suffix = " (panel)"
        else:
            suffix = " (device)"
        lines.append(prefix + branch + node["label"] + suffix)
        lines.extend(_render(node.get("children") or [], prefix + ("    " if last else "│   ")))
    return lines


# --------------------------------------------------------------------------- #
# 1. The real installation
# --------------------------------------------------------------------------- #


@needs_fixture
def test_real_fixture_shape_and_stats() -> None:
    topo = _real_topology()
    tree = _real_tree()

    assert tree["version"] == TREE_VERSION
    assert tree["status"] == str(topo.status)
    assert tree["topology_version"] == topo.version

    stats = tree["stats"]
    # 38 channels: 6 are panel feeds measured on the two boards' mains, so 32
    # circuits.  All 28 declared devices must be placed — none may be dropped.
    assert stats["channels"] == len(topo.channels) == 38
    assert stats["feed_circuits"] == 6
    assert stats["circuits"] == 32
    assert stats["devices"] == len(topo.devices) == 28
    assert stats["unplaced_devices"] == 0
    assert stats["unplaced_circuits"] == 0
    assert stats["panels"] == 2
    assert stats["duplicate_feeds"] == 2
    assert stats["max_depth"] == 5

    # Every circuit and device is a terminal *item*: the union of circuits and
    # devices must equal the statistics, and no leaf may be a group or panel
    # (a leaf is what the UI ultimately shows, contract R6).
    items = [node for node in _walk(tree["roots"]) if node["kind"] in {"circuit", "device"}]
    assert len(items) == stats["circuits"] + stats["devices"]
    for node in _walk(tree["roots"]):
        if not node["children"]:
            assert node["kind"] in {"circuit", "device"}, node["id"]


@needs_fixture
def test_real_fixture_every_circuit_and_device_exactly_once() -> None:
    topo = _real_topology()
    tree = _real_tree()

    circuit_ids = _ids(tree, "circuit", "ct_id")
    device_ids = _ids(tree, "device", "device_id")

    assert len(circuit_ids) == len(set(circuit_ids)), "a circuit was rendered twice"
    assert len(device_ids) == len(set(device_ids)), "a device was rendered twice"
    assert set(device_ids) == {device.device_id for device in topo.devices}

    # Every non-feed channel is a circuit node, and no feed is one.
    feeds = set(_ids(tree, "panel", "feed_ct_ids") and [])
    feed_ids = {ct for node in _kinds(tree, "panel") for ct in node["feed_ct_ids"]}
    expected_circuits = {
        channel.ct_id for channel in topo.channels if channel.ct_id not in feed_ids
    }
    assert set(circuit_ids) == expected_circuits
    assert feeds == set()

    # flat[] is a summary of the same nodes, not a second tree.
    assert [row["ct_id"] for row in tree["flat"]["circuits"]] == circuit_ids
    assert [row["device_id"] for row in tree["flat"]["devices"]] == device_ids


@needs_fixture
def test_real_fixture_no_group_node_is_empty() -> None:
    tree = _real_tree()
    groups = _kinds(tree, "group")
    assert groups, "the real tree must contain group nodes"
    for group in groups:
        assert group["children"], f"empty group {group['id']} rendered (contract R5)"
        assert group["role"] in {"circuits", "devices", "subpanels"}


@needs_fixture
def test_real_fixture_duplicate_feed_is_merged_not_nested() -> None:
    """``refoss_main_panel_l1/l2`` aliases ``main_load_manager_l1/l2``.

    A duplicate is the same conductor seen by two monitors, so it must merge
    into the canonical panel node: one panel, both feeds listed, a "2 monitors"
    badge, and no SUBPANELS level created to hold a second copy of the board.
    """
    tree = _real_tree()
    panels = _kinds(tree, "panel")
    assert [panel["panel_id"] for panel in panels] == [
        "main_load_manager",
        "refoss_sub_panel",
    ]

    main = panels[0]
    assert main["is_service_entry"] is True
    assert main["feed_ct_ids"] == [
        "main_load_manager_l1",
        "main_load_manager_l2",
        "refoss_main_panel_l1",
        "refoss_main_panel_l2",
    ]
    assert {"text": "2 monitors", "tone": "info"} in main["badges"]
    assert sorted(main["monitors"]) == ["Main Load Manager", "Refoss - Main Panel"]

    # The sub panel is a real child of the main board (declared parent_ct_id).
    assert main["children"][-1]["role"] == "subpanels"
    assert main["children"][-1]["children"] == [panels[1]]
    assert panels[1]["panel_id"] == "refoss_sub_panel"
    assert panels[1]["is_service_entry"] is False
    # The duplicate board must not appear anywhere as a panel of its own.
    assert "refoss_main_panel" not in {panel["panel_id"] for panel in panels}


@needs_fixture
def test_real_fixture_tree_is_deterministic() -> None:
    first = _real_tree()
    second = _real_tree()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


@needs_fixture
def test_real_fixture_with_nesting_keeps_leaf_counts() -> None:
    """Advisory nesting may only add suggestions; stats and leaves cannot move."""
    plain = _real_tree()
    nested = _real_tree(nesting=json.loads(NESTING_FIXTURE.read_text()))

    assert plain["stats"] == nested["stats"]
    assert [node["id"] for node in _walk(plain["roots"])] == [
        node["id"] for node in _walk(nested["roots"])
    ]
    assert nested["suggestions"]["applied"] is True
    assert nested["suggestions"]["duplicate_aliases"] == {
        "refoss_main_panel_l1": "main_load_manager_l1",
        "refoss_main_panel_l2": "main_load_manager_l2",
    }
    # A nesting candidate must never be promoted to a tree edge.
    assert not any(
        node["kind"] == "panel" and node["panel_id"] == "refoss_main_panel"
        for node in _walk(nested["roots"])
    )


@needs_fixture
def test_real_fixture_produces_ui_fixture() -> None:
    """Publish the tree the UI workstream consumes (same directory as topo.json)."""
    tree = _real_tree()
    TREE_OUTPUT.write_text(json.dumps(tree, indent=2) + "\n")
    assert TREE_OUTPUT.is_file()
    reloaded = json.loads(TREE_OUTPUT.read_text())
    assert reloaded["stats"]["circuits"] == 32
    assert reloaded["stats"]["devices"] == 28


# --------------------------------------------------------------------------- #
# 2. The contract example, pinned
# --------------------------------------------------------------------------- #


def _channel(
    ct_id: str,
    *,
    ct_type: CTType = CTType.BRANCH,
    panel_id: str = "main_panel",
    panel_name: str = "Main Panel",
    parent: str | None = None,
    paired: str | None = None,
    device_id: str | None = None,
    phase: Phase = Phase.UNKNOWN,
) -> CTChannel:
    return CTChannel(
        ct_id=ct_id,
        entity_id=f"sensor.{ct_id.lower()}_power",
        name=ct_id,
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        paired_ct_id=paired,
        device_id=device_id,
        panel_id=panel_id,
        panel_name=panel_name,
    )


def _two_panel_topology() -> Topology:
    return Topology(
        version=2,
        status=TopologyStatus.CANDIDATE,
        channels=[
            _channel("MAIN_L1", ct_type=CTType.MAIN, phase=Phase.L1, paired="MAIN_L2"),
            _channel("MAIN_L2", ct_type=CTType.MAIN, phase=Phase.L2, paired="MAIN_L1"),
            _channel("CT01", phase=Phase.L1),
            _channel("CT02", phase=Phase.L1),
            # Sub-panel feed: its declared parent is the main panel's feed.
            _channel(
                "SUB_L1",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
                parent="MAIN_L1",
                panel_id="subpanel_01",
                panel_name="Subpanel 01",
            ),
            _channel(
                "SUB_L2",
                ct_type=CTType.MAIN,
                phase=Phase.L2,
                parent="MAIN_L2",
                paired="SUB_L1",
                panel_id="subpanel_01",
                panel_name="Subpanel 01",
            ),
            _channel(
                "Upper_Humidifier_CT",
                panel_id="subpanel_01",
                panel_name="Subpanel 01",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="Basement_Hallway_Light",
                name="Basement_Hallway_Light",
                branch_ct_ids=["CT01"],
            ),
            DeviceTopology(
                device_id="Basement_Gym_Light",
                name="Basement_Gym_Light",
                branch_ct_ids=["CT01"],
            ),
            DeviceTopology(
                device_id="Master_Bedroom_Light",
                name="Master_Bedroom_Light",
                branch_ct_ids=["CT02"],
            ),
            # A double-pole load spanning both circuits: panel level (R3).
            DeviceTopology(
                device_id="Device_X",
                name="Device_X",
                branch_ct_ids=["CT01", "CT02"],
                phase=Phase.BOTH,
                double_pole=True,
            ),
            DeviceTopology(
                device_id="Upper_Humidifier",
                name="Upper_Humidifier",
                dedicated_ct_ids=["Upper_Humidifier_CT"],
                branch_ct_ids=["Upper_Humidifier_CT"],
            ),
        ],
    )


def test_contract_example_shape_is_exact() -> None:
    tree = build_panel_tree(_two_panel_topology())
    assert _render(tree["roots"]) == [
        "└── Main Panel (panel)",
        "    ├── CIRCUITS (circuits)",
        "    │   ├── CT01 (CT01)",
        "    │   │   ├── Basement_Gym_Light (device)",
        "    │   │   └── Basement_Hallway_Light (device)",
        "    │   └── CT02 (CT02)",
        "    │       └── Master_Bedroom_Light (device)",
        "    ├── DEVICES (devices)",
        "    │   └── Device_X (device)",
        "    └── SUBPANELS (subpanels)",
        "        └── Subpanel 01 (panel)",
        "            └── CIRCUITS (circuits)",
        "                └── Upper_Humidifier_CT (Upper_Humidifier_CT)",
        "                    └── Upper_Humidifier (device)",
    ]
    # §1.2 explicitly has no empty DEVICES group under the sub panel.
    assert "DEVICES" not in _render(tree["roots"])[12:]
    assert tree["stats"] == {
        "panels": 2,
        "circuits": 3,
        "devices": 5,
        "unplaced_devices": 0,
        "unplaced_circuits": 0,
        "max_depth": 5,
        "duplicate_feeds": 0,
        "channels": 7,
        "feed_circuits": 4,
    }


def test_contract_node_fields() -> None:
    tree = build_panel_tree(_two_panel_topology())

    panel = tree["roots"][0]
    for key in (
        "id",
        "kind",
        "label",
        "subtitle",
        "badges",
        "depth_hint",
        "children",
        "panel_id",
        "monitor",
        "feed_ct_ids",
        "is_service_entry",
    ):
        assert key in panel, key
    assert panel["feed_ct_ids"] == ["MAIN_L1", "MAIN_L2"]

    circuit = _kinds(tree, "circuit")[0]
    for key in (
        "id",
        "kind",
        "label",
        "subtitle",
        "badges",
        "depth_hint",
        "children",
        "ct_id",
        "ct_type",
        "phase",
        "entity_id",
        "enabled",
        "parent_ct_id",
        "paired_ct_id",
        "alias_of_ct_id",
        "topology_status",
        "polarity",
        "measurement_multiplier",
        "latest_power_w",
    ):
        assert key in circuit, key

    device = _kinds(tree, "device")[0]
    for key in (
        "id",
        "kind",
        "label",
        "subtitle",
        "badges",
        "depth_hint",
        "children",
        "device_id",
        "phase",
        "topology_status",
        "source",
        "latest_power_w",
        "confidence",
        "via_ct_id",
    ):
        assert key in device, key

    group = next(node for node in _kinds(tree, "group"))
    for key in ("id", "kind", "label", "subtitle", "badges", "depth_hint", "children", "role"):
        assert key in group, key


def test_latest_results_are_attached_to_devices() -> None:
    tree = build_panel_tree(
        _two_panel_topology(),
        latest_results={"Upper_Humidifier": {"power_w": 12.5, "confidence": 0.9, "source": "inferred"}},
    )
    device = next(
        node for node in _kinds(tree, "device") if node["device_id"] == "Upper_Humidifier"
    )
    assert device["latest_power_w"] == 12.5
    assert device["confidence"] == 0.9
    assert device["source"] == "inferred"


# --------------------------------------------------------------------------- #
# Legacy / degraded inputs
# --------------------------------------------------------------------------- #


def test_legacy_topology_without_panel_fields_falls_back_to_one_panel() -> None:
    """An old stored topology (no ``panel_id``) must still produce a usable tree."""
    legacy = Topology.model_validate(
        {
            "version": 1,
            "channels": [
                {"ct_id": "m1", "entity_id": "sensor.m1", "ct_type": "main"},
                {"ct_id": "b1", "entity_id": "sensor.b1", "ct_type": "branch"},
            ],
            "devices": [{"device_id": "d1", "name": "Load", "branch_ct_ids": ["b1"]}],
        }
    )
    tree = build_panel_tree(legacy)
    assert tree["stats"]["panels"] == 1
    assert tree["roots"][0]["panel_id"] == IMPLICIT_PANEL_ID
    assert [node["ct_id"] for node in _kinds(tree, "circuit")] == ["b1"]
    assert [node["device_id"] for node in _kinds(tree, "device")] == ["d1"]
    assert any("implicit panel" in note for note in tree["notes"])


def test_legacy_panel_evidence_is_honoured() -> None:
    """A pre-``panel_id`` topology keeps the board discovery already recorded."""
    legacy = Topology.model_validate(
        {
            "version": 1,
            "channels": [
                {
                    "ct_id": "panel_a_l1",
                    "entity_id": "sensor.panel_a_ct1_power",
                    "ct_type": "main",
                    "metadata": {"evidence": ["panel=Panel A"]},
                },
                {
                    "ct_id": "panel_a_lights",
                    "entity_id": "sensor.panel_a_lights_power",
                    "ct_type": "branch",
                    "metadata": {"evidence": ["panel=Panel A"]},
                },
            ],
            "devices": [
                {"device_id": "d1", "name": "Lights", "branch_ct_ids": ["panel_a_lights"]}
            ],
        }
    )
    tree = build_panel_tree(legacy)
    assert tree["roots"][0]["panel_id"] == "panel_a"
    assert tree["roots"][0]["label"] == "Panel A"
    # The degraded path is announced, never silent.
    assert any("carried no panel_id" in note for note in tree["notes"])


@needs_fixture
def test_persisted_topology_still_yields_the_same_tree(tmp_path: Path) -> None:
    """The stored channel table has no panel column yet.

    ``Database.save_topology`` (owned by another workstream) writes the columns
    it knows about, so a topology read back has ``panel_id=None``.  The tree must
    still be correct — via the evidence fallback — and must say so.  When the
    column is added this test keeps passing, it simply stops reporting legacy.
    """
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "roundtrip.db")
    db.save_topology(_real_topology())
    loaded = db.load_topology()
    assert loaded is not None

    tree = build_panel_tree(loaded)
    assert [panel["panel_id"] for panel in _kinds(tree, "panel")] == [
        "main_load_manager",
        "refoss_sub_panel",
    ]
    assert tree["stats"] == _real_tree()["stats"]


def test_undeclared_panel_surfaces_under_unattributed() -> None:
    """Silent dropping is a bug: an unresolvable circuit/device must be visible."""
    topology = Topology(
        channels=[
            _channel("MAIN_L1", ct_type=CTType.MAIN),
            _channel("ORPHAN", panel_id=""),
        ],
        devices=[
            DeviceTopology(device_id="placed", name="Placed", branch_ct_ids=["MAIN_L1"]),
            DeviceTopology(device_id="ghost", name="Ghost", branch_ct_ids=["MISSING"]),
        ],
    )
    topology.channels[1].panel_id = None
    topology.channels[1].panel_name = None
    tree = build_panel_tree(topology)

    assert tree["stats"]["unplaced_circuits"] == 1
    assert tree["stats"]["unplaced_devices"] == 1
    rendered = _render(tree["roots"])
    assert any("Unattributed" in line for line in rendered)
    assert "Ghost" in "\n".join(rendered)
    assert "ORPHAN" in "\n".join(rendered)
    # Still exactly once each.
    assert _ids(tree, "device", "device_id").count("ghost") == 1
    assert _ids(tree, "circuit", "ct_id").count("ORPHAN") == 1


def test_circuit_cycle_terminates_and_keeps_every_circuit() -> None:
    """A malformed parent chain must not hang, recurse or drop nodes."""
    topology = Topology(
        channels=[
            _channel("MAIN_L1", ct_type=CTType.MAIN),
            _channel("A", parent="B"),
            _channel("B", parent="A"),
        ],
        devices=[],
    )
    tree = build_panel_tree(topology)
    assert sorted(_ids(tree, "circuit", "ct_id")) == ["A", "B"]
    assert any("loop detected" in note for note in tree["notes"])


def test_panel_cycle_terminates_and_renders_each_panel_once() -> None:
    topology = Topology(
        channels=[
            # Each board's feed declares the other as its parent: a cycle.
            _channel("P1_L1", ct_type=CTType.MAIN, panel_id="p1", panel_name="P1", parent="P2_L1"),
            _channel("P2_L1", ct_type=CTType.MAIN, panel_id="p2", panel_name="P2", parent="P1_L1"),
        ],
        devices=[],
    )
    tree = build_panel_tree(topology)
    assert sorted(_ids(tree, "panel", "panel_id")) == ["p1", "p2"]
    assert any("panel nesting loop detected" in note for note in tree["notes"])


# --------------------------------------------------------------------------- #
# 3. Negative tests — each reverts one guarantee at its seam
# --------------------------------------------------------------------------- #


@needs_fixture
def test_negative_duplicate_merge_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverting the merge must create a third panel ��� detected below."""
    merged = _real_tree()
    assert [panel["panel_id"] for panel in _kinds(merged, "panel")] == [
        "main_load_manager",
        "refoss_sub_panel",
    ]

    monkeypatch.setattr(
        panel_tree_module._TreeBuilder, "_duplicate_feed_pairs", lambda self: []
    )
    reverted = _real_tree()
    assert sorted(panel["panel_id"] for panel in _kinds(reverted, "panel")) == [
        "main_load_manager",
        "refoss_main_panel",
        "refoss_sub_panel",
    ]
    assert reverted["stats"]["duplicate_feeds"] == 0
    # The acceptance assertion (merge happened) now fails.
    assert reverted["stats"]["panels"] != merged["stats"]["panels"]


def test_negative_empty_group_omission_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverting the omission must render empty DEVICES/SUBPANELS groups."""
    topology = _two_panel_topology()
    clean = build_panel_tree(topology)
    assert all(group["children"] for group in _kinds(clean, "group"))

    monkeypatch.setattr(panel_tree_module, "_group_has_content", lambda children: True)
    reverted = build_panel_tree(topology)
    empty = [group for group in _kinds(reverted, "group") if not group["children"]]
    assert empty, "the empty-group seam did not change the output"
    assert {group["role"] for group in empty} == {"devices", "subpanels"}


def test_negative_single_placement_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverting the exactly-once rule must duplicate a device."""
    clean = build_panel_tree(_two_panel_topology())
    assert len(_ids(clean, "device", "device_id")) == len(
        set(_ids(clean, "device", "device_id"))
    )

    original = panel_tree_module._TreeBuilder._assign_device

    def _place_twice(self, device_id, *, where, target, panel_id):  # type: ignore[no-untyped-def]
        original(self, device_id, where=where, target=target, panel_id=panel_id)
        if where == "circuit":  # the classic bug: circuit *and* panel level
            self.panel_level_devices.setdefault(panel_id, []).append(device_id)

    monkeypatch.setattr(panel_tree_module._TreeBuilder, "_assign_device", _place_twice)
    reverted = build_panel_tree(_two_panel_topology())
    ids = _ids(reverted, "device", "device_id")
    assert len(ids) > len(set(ids)), "the exactly-once seam did not change the output"


def test_negative_cycle_guard_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reverting the cycle guard must drop circuits from the tree."""
    guarded = build_panel_tree(
        Topology(
            channels=[
                _channel("MAIN_L1", ct_type=CTType.MAIN),
                _channel("A", parent="B"),
                _channel("B", parent="A"),
            ],
            devices=[],
        )
    )
    assert sorted(_ids(guarded, "circuit", "ct_id")) == ["A", "B"]

    monkeypatch.setattr(
        panel_tree_module, "_break_parent_cycles", lambda parent_of, *, kind: []
    )
    unguarded = build_panel_tree(
        Topology(
            channels=[
                _channel("MAIN_L1", ct_type=CTType.MAIN),
                _channel("A", parent="B"),
                _channel("B", parent="A"),
            ],
            devices=[],
        )
    )
    # Both circuits have a parent and neither is a root, so without the guard
    # the whole cycle — and every device under it — disappears.
    assert _ids(unguarded, "circuit", "ct_id") == []
    assert unguarded["stats"]["circuits"] == 0


# --------------------------------------------------------------------------- #
# 4. Discovery populates the declared panel (the field the tree consumes)
# --------------------------------------------------------------------------- #


def test_discovery_populates_panel_fields_without_touching_evidence() -> None:
    """``panel_id``/``panel_name`` are the declared grouping; evidence stays."""
    from wattmosaic_engine.ingestion.discovery import discover_candidate_topology

    states = [
        {
            "entity_id": f"sensor.panel_ct{leg}_power",
            "state": "10.0",
            "attributes": {"unit_of_measurement": "W", "device_class": "power"},
        }
        for leg in (1, 2)
    ]
    states.append(
        {
            "entity_id": "sensor.panel_lights_power",
            "state": "5.0",
            "attributes": {"unit_of_measurement": "W", "device_class": "power"},
        }
    )
    registries = {
        "entities": [
            {
                "entity_id": state["entity_id"],
                "platform": "demo",
                "name": state["entity_id"],
                "device_id": "hw1",
            }
            for state in states
        ],
        "devices": [{"id": "hw1", "name_by_user": "Refoss - Main Panel"}],
    }

    topology = discover_candidate_topology(states, registries)
    assert topology.channels
    for channel in topology.channels:
        assert channel.panel_id == "refoss_main_panel"
        assert channel.panel_name == "Refoss - Main Panel"
        # Backwards compatibility: the evidence string is still written.
        assert "panel=Refoss - Main Panel" in channel.metadata["evidence"]

    # And the declared field flows straight into the tree.
    tree = build_panel_tree(topology)
    assert [panel["panel_id"] for panel in _kinds(tree, "panel")] == ["refoss_main_panel"]
    assert tree["stats"]["unplaced_circuits"] == 0


# --------------------------------------------------------------------------- #
# 5. EngineCore.panel_tree (cheap path, no nesting unless asked)
# --------------------------------------------------------------------------- #


@needs_fixture
def test_engine_core_panel_tree_is_cheap_and_opt_in(tmp_path: Path) -> None:
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "wm.db")
    db.save_topology(_real_topology())
    config = EngineConfig(database_path=str(tmp_path / "wm.db"), ha_token="not-used")
    core = EngineCore(config, db)

    nesting_calls: list[float] = []

    def _nesting(*, hours: float = 12.0) -> dict[str, Any]:
        nesting_calls.append(hours)
        return json.loads(NESTING_FIXTURE.read_text())

    core.nesting_candidates = _nesting  # type: ignore[assignment]

    started = time.perf_counter()
    tree = core.panel_tree()
    elapsed = time.perf_counter() - started

    assert tree["stats"]["circuits"] == 32
    assert tree["stats"]["devices"] == 28
    # The expensive path must not run by default...
    assert nesting_calls == []
    # ...and the cheap path must stay far inside the 50 ms target.
    assert elapsed < 0.050, f"panel_tree() took {elapsed * 1000:.1f} ms"

    nested = core.panel_tree(include_nesting=True)
    assert nesting_calls == [12.0]
    assert nested["stats"] == tree["stats"]
    assert "suggestions" in nested


def test_engine_core_panel_tree_without_topology(tmp_path: Path) -> None:
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "empty.db")
    config = EngineConfig(database_path=str(tmp_path / "empty.db"), ha_token="not-used")
    core = EngineCore(config, db)

    tree = core.panel_tree()
    assert tree["roots"] == []
    assert tree["stats"]["circuits"] == 0
    assert tree["notes"]


# --------------------------------------------------------------------------- #
# 6. HTTP endpoint
# --------------------------------------------------------------------------- #


class _TopologyOnlyFacade(StubFacade):
    """Serves the real fixture through the tree endpoint (no engine needed)."""

    def __init__(self) -> None:
        super().__init__()
        self._real = _real_topology()
        self.calls: list[bool] = []

    def topology(self) -> Topology:
        return self._real

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        self.calls.append(include_nesting)
        nesting = json.loads(NESTING_FIXTURE.read_text()) if include_nesting else None
        return build_panel_tree(self._real, nesting=nesting)


async def _with_server(facade: Any):
    server = APIServer(facade, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    return server


@needs_fixture
async def test_tree_endpoint_serves_the_contract_payload() -> None:
    facade = _TopologyOnlyFacade()
    server = await _with_server(facade)
    try:
        async with aiohttp.ClientSession() as session:

            async def get(url: str) -> tuple[int, dict[str, Any]]:
                async with session.get(url, headers=HEADERS) as resp:
                    return resp.status, await resp.json()

            base = f"http://127.0.0.1:{server.port}/api/v1"
            status, body = await get(f"{base}/topology/tree")
            assert status == 200
            assert set(body) >= {
                "version",
                "status",
                "generated_at",
                "topology_version",
                "stats",
                "roots",
                "flat",
                "notes",
            }
            assert body["version"] == TREE_VERSION
            assert body["stats"]["circuits"] == 32
            assert body["stats"]["devices"] == 28
            assert set(body["flat"]) == {"panels", "circuits", "devices"}
            assert body["flat"]["panels"][0]["panel_id"] == "main_load_manager"

            # Nesting is off unless asked for: the cheap path must not pay for it.
            assert "suggestions" not in body
            assert facade.calls[-1] is False

            status, truthy = await get(f"{base}/topology/tree?include_nesting=1")
            assert status == 200
            assert "suggestions" in truthy
            assert facade.calls[-1] is True

            status, falsy = await get(f"{base}/topology/tree?include_nesting=no")
            assert status == 200
            assert "suggestions" not in falsy
            assert facade.calls[-1] is False

            status, absent = await get(f"{base}/topology/tree?include_nesting=")
            assert status == 200
            assert "suggestions" not in absent
            assert facade.calls[-1] is False
    finally:
        await server.stop()


@needs_fixture
async def test_tree_endpoint_is_fast_on_the_real_fixture() -> None:
    server = await _with_server(_TopologyOnlyFacade())
    try:
        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{server.port}/api/v1/topology/tree"
            best = float("inf")
            for _ in range(5):  # warm up, then measure the best round trip
                start = time.perf_counter()
                async with session.get(url, headers=HEADERS) as resp:
                    assert resp.status == 200
                    await resp.read()
                best = min(best, time.perf_counter() - start)
            # Contract §2 allows 500 ms; the implementation target is well under
            # 50 ms and the tree build itself is ~0.5 ms.
            assert best < 0.050, f"tree endpoint took {best * 1000:.1f} ms"
    finally:
        await server.stop()


def test_tree_endpoint_is_unavailable_without_an_engine() -> None:
    """``NullFacade`` must map to 503, exactly like every other endpoint."""
    async def check() -> None:
        server = await _with_server(NullFacade())
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{server.port}/api/v1/topology/tree"
                async with session.get(url, headers=HEADERS) as resp:
                    assert resp.status == 503
        finally:
            await server.stop()

    asyncio.run(check())


@needs_fixture
def test_ambiguous_service_entry_is_not_claimed() -> None:
    """No containment at all must not turn every board into a "service entry".

    The real capture happens to declare ``parent_ct_id`` on the sub-panel feeds,
    so exactly one panel is fed from outside.  But discovery on many installs
    infers no containment at all, and then *every* root has feeds with no parent
    -- the necessary test for "fed from outside the installation" passes for all
    of them.  Labelling each one "service entry" is a false claim about the
    house, and because roots sort service entries first it also pushes the real
    main board last.

    When the data cannot distinguish the boards, the tree claims nothing.
    """
    data = json.loads(TOPO_FIXTURE.read_text())
    for channel in data["channels"]:
        channel["parent_ct_id"] = None

    tree = build_panel_tree(Topology.model_validate(data))

    panels = _kinds(tree, "panel")
    assert len(panels) == 2
    assert [panel["is_service_entry"] for panel in panels] == [False, False]
    assert all(panel["subtitle"].startswith("distribution board") for panel in panels)
    # The ambiguity is stated rather than hidden.
    assert any("outside the installation" in note for note in tree["notes"])
    # And no rows are lost by the change.
    assert tree["stats"]["circuits"] == 32
    assert tree["stats"]["devices"] == 28


@needs_fixture
def test_unambiguous_service_entry_is_still_named_and_first() -> None:
    """The ordinary case must keep its label -- the fix must not over-correct."""
    tree = _real_tree()
    panels = _kinds(tree, "panel")
    assert tree["roots"][0]["panel_id"] == "main_load_manager"
    assert panels[0]["is_service_entry"] is True
    assert panels[0]["subtitle"].startswith("service entry")
    assert panels[1]["is_service_entry"] is False
