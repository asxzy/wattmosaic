"""The Topology tab renders from the contract 3.0 routes.

Regression: the tab fetched the legacy ``GET /topology`` (removed at the 3.0
cutover, 404), treated the 404 as "No topology configured" and rendered nothing
else, so a fresh install with 46 measurement endpoints showed an empty tab.

These tests drive the real ``renderTopology`` under node with a stubbed
``fetch`` that serves only 3.0 payloads, and pin:

* an empty 3.0 topology (endpoints, no edges) renders the endpoints and a
  "No confirmed edges yet" state, never "No topology configured";
* "No topology configured" appears only when ``/health`` says
  ``details.topology.configured`` is false (no measurement endpoint);
* status is "confirmed" iff an edge is CONFIRMED or pinned, else "candidate";
* the hierarchy fallback nests by confirmed CONTAINS edges only;
* rendering never writes and never requests a removed legacy route.
"""

from __future__ import annotations

import pytest

from tests.unit.panel_harness import EVENT_DOM_SHIM, lit, node_path, run_esm

T0 = "2026-01-01T00:00:00+00:00"


def _require_node() -> None:
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")


def _endpoint(endpoint_id: str, name: str, *, scope: str = "circuit") -> dict:
    return {
        "endpoint_id": endpoint_id,
        "kind": "ct",
        "entity_id": f"sensor.{endpoint_id}_power",
        "energy_entity_id": None,
        "display_name": name,
        "scope": scope,
        "scope_status": "candidate",
        "scope_subject_id": None,
        "panel_id": None,
        "enabled": True,
        "created_at": T0,
        "revisions": [
            {
                "endpoint_id": endpoint_id,
                "revision": 1,
                "valid_from": T0,
                "phase": "l1",
                "polarity": "normal",
                "multiplier": 1.0,
                "calibration_version": "discovery",
                "measurement_type": "instantaneous",
                "declared_by": "discovery",
            }
        ],
    }


def _edge(edge_id: str, parent: str, child: str, lifecycle: str, *, pinned: bool = False) -> dict:
    return {
        "edge_id": edge_id,
        "relation": "contains",
        "source_endpoint_id": parent,
        "target_endpoint_id": child,
        "lifecycle": lifecycle,
        "declared_by": "inference",
        "beta": 1.0,
        "beta_basis": "unity",
        "evidence": {},
        "valid_from": T0,
        "valid_until": None,
        "pinned": pinned,
    }


ENDPOINTS = [
    _endpoint("ct_feed", "Main feed", scope="panel_feed"),
    _endpoint("ct_sub", "Sub panel"),
    _endpoint("ct_oven", "Oven"),
]

#: The engine's /topology/tree for a topology with no edges: one
#: "Unassigned measurements" group holding every reader.
EMPTY_TREE = {
    "version": 2,
    "status": "confirmed",
    "generated_at": T0,
    "topology_version": "topo-1",
    "stats": {"panels": 0, "circuits": 3, "devices": 0, "unplaced_devices": 0, "unplaced_circuits": 0},
    "roots": [
        {
            "id": "group:Unassigned measurements:1",
            "kind": "group",
            "label": "Unassigned measurements",
            "reader_id": None,
            "children": [
                {"id": f"reader:{e['endpoint_id']}", "kind": "circuit", "label": e["display_name"],
                 "reader_id": e["endpoint_id"], "children": []}
                for e in ENDPOINTS
            ],
        }
    ],
    "flat": {"panels": [], "circuits": [], "devices": []},
    "notes": [],
}


def _health(configured: bool, *, endpoints: int = 3, edges: int = 0) -> dict:
    topology = (
        {"configured": True, "status": "candidate", "version": "topo-1",
         "endpoints": endpoints, "edges": edges, "devices": 0}
        if configured
        else {"configured": False, "reason": "no measurement endpoints configured"}
    )
    return {"status": "ok", "contract_version": "3.0.0", "details": {"topology": topology}}


def _api(*, edges: list[dict] | None = None, endpoints: list[dict] | None = None,
         configured: bool = True, tree: dict | None = EMPTY_TREE) -> dict:
    edges = edges or []
    api = {
        "/topology/v3": {"version_id": "topo-1", "version": 1, "status": "active",
                         "edges": edges, "primary_display_tree": []},
        "/endpoints": {"endpoints": ENDPOINTS if endpoints is None else endpoints},
        "/status": {"learning": {}},
        "/health": _health(configured, edges=len(edges)),
    }
    if tree is not None:
        api["/topology/tree"] = tree
    return api


_FETCH_STUB = r"""
const __requests = [];
globalThis.__api = {};
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || 'GET';
  const path = String(url).replace(/^.*\/api\/v1/, '');
  __requests.push([method, path]);
  const key = path.split('?')[0];
  if (method === 'GET' && key in globalThis.__api) {
    const body = JSON.stringify(globalThis.__api[key]);
    return { ok: true, status: 200, text: async () => body };
  }
  return { ok: false, status: 404, text: async () => '{"error":"not_found"}' };
};
Node.prototype.scrollIntoView = function () {};
Node.prototype.focus = function () {};
Node.prototype.querySelectorAll = function () { return []; };
"""


def _render(api: dict) -> dict:
    _require_node()
    program = (
        "import { renderTopology } from './view-topology.js';\n"
        "import { slots } from './slots.js';\n"
        "import { topologyLearningBanner, topologyDiscoverSlot, discoverWindowSelect } from './view-discover.js';\n"
        "Object.assign(slots, { topologyLearningBanner, topologyDiscoverSlot, discoverWindowSelect,\n"
        "  runDiscovery: () => {}, reviewTopologyEdges: () => {} });\n"
        f"globalThis.__api = {lit(api)};\n"
        "const root = document.createElement('div');\n"
        "document.body.append(root);\n"
        "const ctl = new AbortController();\n"
        "await renderTopology(root, ctl.signal);\n"
        "const layout = (document.getElementById('topology-hierarchy') || {dataset: {}}).dataset.layout || null;\n"
        "console.log(JSON.stringify({ text: root.textContent, requests: __requests, layout }));\n"
    )
    return run_esm(program, shim=EVENT_DOM_SHIM + _FETCH_STUB)


REMOVED_ROUTES = ("/topology/nesting", "/topology/confirm", "/mapping", "/confidence_tiers", "/isolation")


def _assert_only_3_0_reads(requests: list[list[str]]) -> None:
    assert all(method == "GET" for method, _ in requests), f"render wrote: {requests}"
    paths = [path.split("?")[0] for _, path in requests]
    assert "/topology" not in paths, "the legacy GET /topology was requested"
    for removed in REMOVED_ROUTES:
        assert not any(p.startswith(removed) for p in paths), (removed, paths)


def test_empty_3_0_topology_renders_endpoints_and_no_confirmed_edges_state() -> None:
    result = _render(_api())
    text = result["text"]
    assert "No topology configured" not in text
    assert "No confirmed edges yet" in text
    # The endpoints are on screen: in the hierarchy and the endpoints card.
    for name in ("Main feed", "Sub panel", "Oven"):
        assert name in text
    assert "Measurement endpoints" in text
    assert "3 total" in text
    assert "Candidate" in text  # status with no confirmed or pinned edge
    assert "No typed edges published by the topology service yet." in text
    assert result["layout"] == "tree"
    _assert_only_3_0_reads(result["requests"])
    assert {p for _, p in result["requests"]} >= {"/topology/v3", "/endpoints", "/topology/tree", "/health"}


def test_no_topology_configured_only_when_engine_has_no_endpoints() -> None:
    result = _render(_api(endpoints=[], configured=False))
    assert "No topology configured" in result["text"]
    assert "No confirmed edges yet" not in result["text"]
    _assert_only_3_0_reads(result["requests"])


def test_health_configured_wins_over_an_empty_endpoint_list() -> None:
    """``configured`` comes from /health; endpoints failing to load is not 'unconfigured'."""
    api = _api()
    del api["/endpoints"]
    result = _render(api)
    assert "No topology configured" not in result["text"]
    assert "Endpoints unavailable" in result["text"]


def test_status_is_confirmed_when_an_edge_is_confirmed_or_pinned() -> None:
    _require_node()
    program = (
        "import { topologyStatusFromEdges } from './view-topology.js';\n"
        f"const confirmed = {lit([_edge('a', 'ct_feed', 'ct_sub', 'confirmed')])};\n"
        f"const pinned = {lit([_edge('b', 'ct_feed', 'ct_sub', 'candidate', pinned=True)])};\n"
        f"const soft = {lit([_edge('c', 'ct_feed', 'ct_sub', 'provisional'), _edge('d', 'ct_sub', 'ct_oven', 'candidate')])};\n"
        f"const rejected = {lit([dict(_edge('e', 'ct_feed', 'ct_sub', 'rejected'), valid_until=T0)])};\n"
        "console.log(JSON.stringify({\n"
        "  confirmed: topologyStatusFromEdges(confirmed),\n"
        "  pinned: topologyStatusFromEdges(pinned),\n"
        "  soft: topologyStatusFromEdges(soft),\n"
        "  rejected: topologyStatusFromEdges(rejected),\n"
        "  none: topologyStatusFromEdges([]),\n"
        "}));\n"
    )
    out = run_esm(program, shim=EVENT_DOM_SHIM)
    assert out == {
        "confirmed": "confirmed",
        "pinned": "confirmed",
        "soft": "candidate",
        "rejected": "candidate",
        "none": "candidate",
    }


def test_topology_model_joins_endpoints_and_contains_edges() -> None:
    _require_node()
    edges = [
        _edge("e-sub", "ct_feed", "ct_sub", "confirmed"),
        _edge("e-oven", "ct_sub", "ct_oven", "provisional"),
    ]
    program = (
        "import { topologyModel, legacyRoots } from './view-topology.js';\n"
        f"const m = topologyModel({{ v3: {{ version_id: 'topo-2', version: 2, edges: {lit(edges)} }},\n"
        f"  endpoints: {{ endpoints: {lit(ENDPOINTS)} }}, tree: null, health: {lit(_health(True, edges=2))} }});\n"
        "const roots = legacyRoots(m);\n"
        "const shape = (n) => ({ id: n.ct_id, kids: n.children.map(shape) });\n"
        "console.log(JSON.stringify({ m, roots: roots.map(shape) }));\n"
    )
    out = run_esm(program, shim=EVENT_DOM_SHIM)
    model = out["m"]
    assert model["configured"] is True
    assert model["status"] == "confirmed"
    assert model["version"] == 2 and model["version_id"] == "topo-2"
    assert model["confirmed_edge_count"] == 1 and model["edge_count"] == 2
    channels = {c["ct_id"]: c for c in model["channels"]}
    assert channels["ct_feed"]["ct_type"] == "main"
    assert channels["ct_sub"]["ct_type"] == "branch"
    assert channels["ct_sub"]["parent_ct_id"] == "ct_feed"
    # PROVISIONAL is a soft suggestion: it never places ct_oven under ct_sub.
    assert channels["ct_oven"]["parent_ct_id"] is None
    assert channels["ct_feed"]["phase"] == "l1"
    assert out["roots"] == [
        {"id": "ct_feed", "kids": [{"id": "ct_sub", "kids": []}]},
        {"id": "ct_oven", "kids": []},
    ]


def _discovery(candidate: int = 0, provisional: int = 0, top_pairs: list | None = None) -> dict:
    return {
        "counts": {
            "candidate": candidate,
            "provisional": provisional,
            "confirmed": 0,
            "conflicted": 0,
        },
        "top_pairs": top_pairs or [],
        "thresholds": {
            "MIN_SUPPORT_PROVISIONAL": 3,
            "MIN_INDEPENDENT_CAUSES": 2,
            "MIN_SUPPORT_CONFIRMED": 8,
        },
    }


def test_edges_card_renders_suggestions_with_in_version_flag() -> None:
    """Suggestions (in_version false) appear as rows with Accept/Pin/Reject."""
    suggestion = _edge("s1", "ct_feed", "ct_sub", "provisional")
    suggestion["in_version"] = False
    version_edge = _edge("v1", "ct_feed", "ct_oven", "confirmed")
    version_edge["in_version"] = True
    api = _api(edges=[version_edge, suggestion])
    api["/topology/v3"]["discovery"] = _discovery(provisional=1)
    result = _render(api)
    text = result["text"]
    assert "Typed topology edges (v3)" in text
    assert "ct_feed → ct_sub" in text
    assert "ct_feed → ct_oven" in text
    assert "Accept" in text and "Reject" in text  # the provisional suggestion
    _assert_only_3_0_reads(result["requests"])


def test_discovery_summary_shown_when_no_provisional_edges() -> None:
    """A candidate-only topology still shows that discovery is working."""
    top_pairs = [
        {
            "edge_id": "inf-1",
            "source_endpoint_id": "ct_feed",
            "target_endpoint_id": "ct_sub",
            "lifecycle": "candidate",
            "support": 2,
            "conflict": 0,
            "independent_causes": 1,
            "needed": {
                "provisional": {"support": 1, "independent_causes": 1},
                "confirmed": {"support": 6, "independent_causes": 1},
            },
        }
    ]
    api = _api(edges=[])  # no PROVISIONAL+ edges
    api["/topology/v3"]["discovery"] = _discovery(candidate=3, top_pairs=top_pairs)
    result = _render(api)
    text = result["text"]
    assert "Discovery:" in text
    assert "candidate pairs observed" in text
    assert "ct_feed→ct_sub" in text
    assert "support 2/3" in text
    assert "1/2 independent causes" in text


def test_discovery_summary_hidden_when_provisional_exists() -> None:
    suggestion = _edge("s1", "ct_feed", "ct_sub", "provisional")
    suggestion["in_version"] = False
    api = _api(edges=[suggestion])
    api["/topology/v3"]["discovery"] = _discovery(candidate=3, provisional=1)
    result = _render(api)
    assert "Discovery:" not in result["text"]


def test_edges_card_offers_pin_and_accept_only_on_click() -> None:
    """Accept (PROVISIONAL) and Pin (CANDIDATE) POST /pin, and only when clicked."""
    edges = [
        _edge("e-sub", "ct_feed", "ct_sub", "candidate"),
        _edge("e-oven", "ct_sub", "ct_oven", "provisional"),
        _edge("e-done", "ct_feed", "ct_oven", "confirmed", pinned=True),
    ]
    result = _render(_api(edges=edges))
    text = result["text"]
    assert "Typed topology edges (v3)" in text
    assert "Accept" in text and "Pin" in text and "Reject" in text
    assert "Pinned" in text
    assert "ct_feed → ct_sub" in text
    assert "Confirm topology edges" in text  # head action: navigation, not a write
    _assert_only_3_0_reads(result["requests"])
