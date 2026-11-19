"""U2 — overview, device and topology views aligned to v3.

The panel ships flat ES modules; these tests execute the *real* shipped view
modules under node through ``tests/unit/panel_harness.py``.  They pin the U2
contract:

* every rendered number carries status, identifiability, bounds and a
  provenance drill (AT-20);
* UNKNOWN / non-identifiable renders an em dash, never "0 W";
* the overview first render never calls the legacy ``/mapping`` route;
* a PROVISIONAL edge accept calls the T4 pin route (and reject calls reject);
* the device detail shows status / identifiability / bounds / group /
  provisional plus the "Observed by readers" drill and history cards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.panel_harness import DOM_SHIM, EVENT_DOM_SHIM, lit, node_path, run_esm

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "matrix" / "hb17_payload.json"
UI_DIR = REPO_ROOT / "addon" / "ui"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _require_node() -> None:
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")


def _view_model(payload: dict) -> dict:
    """Run v3ViewModel from the real view-overview.js under node."""
    _require_node()
    program = (
        "import { v3ViewModel } from './view-overview.js';\n"
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify(v3ViewModel(d)));\n"
    )
    return run_esm(program, shim=DOM_SHIM)


def test_every_rendered_number_has_status_identifiability_bounds_and_drill(payload: dict) -> None:
    """AT-20 semantics: no naked number, each value explained and drillable."""
    model = _view_model(payload)
    rendered = model["rendered_numbers"]
    assert rendered, "the overview rendered no numbers"
    for item in rendered:
        assert item["status_text"], f"missing status for {item['id']}"
        assert item["identifiability"], f"missing identifiability for {item['id']}"
        assert item["bounds"] is not None, f"missing bounds for {item['id']}"
        assert "lower_w" in item["bounds"] and "upper_w" in item["bounds"]
        assert item["provenance_drill"], f"missing provenance drill for {item['id']}"


def test_ha_attributes_carry_status_bounds_and_versions(payload: dict) -> None:
    """HA attributes carry status, identifiability, bounds and the version triple."""
    model = _view_model(payload)
    ha = model["ha_attributes"]
    assert ha, "no device HA attributes were built"
    for attrs in ha.values():
        assert "status" in attrs and "identifiability" in attrs
        assert "lower_w" in attrs and "upper_w" in attrs
        for key in ("topology_version_id", "mapping_version_id", "model_version_id"):
            assert key in attrs


def test_unknown_and_group_members_render_dash_not_zero(payload: dict) -> None:
    """An UNKNOWN / non-identifiable cell shows "—", never "0 W"."""
    model = _view_model(payload)
    by_id = {n["id"]: n for n in model["rendered_numbers"]}
    # The living-room lights are a non-identifiable group: member cells carry
    # no point value and must render a dash with a non-identifiable status.
    member = by_id["pd_light_1@ct_circuit_a"]
    assert member["value"] == "—"
    assert member["watts"] is None
    assert member["identifiability"] == "non_identifiable"
    # No rendered number anywhere is a fabricated zero watt.
    for item in model["rendered_numbers"]:
        if item["watts"] is None:
            assert item["value"] == "—"
    # Confirmed estimated group sum does show its published value.
    group = by_id["grp_living_lights@ct_circuit_a"]
    assert group["watts"] == 45.0
    assert group["value"] != "—"


def test_overview_first_render_never_calls_mapping() -> None:
    """The flat /mapping card and its first-render fetch are gone."""
    source = (UI_DIR / "view-overview.js").read_text(encoding="utf-8")
    # The renderer uses the local v3 fetch, never the legacy fetchOverview
    # (which still calls /mapping in core.js, owned by another task).
    body = source.split("async function renderOverview(", 1)[1]
    body = body.split("\n}", 1)[0]
    assert "fetchOverviewV3(" in body
    assert "fetchOverview(" not in body
    assert "'/mapping'" not in body and '"/mapping"' not in body
    assert "mappingMatrixCard" not in body
    assert "overviewMatrixPayload" in body


def test_matrix_summary_group_residual_unknown_cards_render(payload: dict) -> None:
    """The v3 cards are wired into the overview render path."""
    source = (UI_DIR / "view-overview.js").read_text(encoding="utf-8")
    for needle in ("matrixSummaryCard", "Group estimates", "Residual by reader", "Unknown loads"):
        assert needle in source
    _require_node()
    program = (
        "import { matrixSummaryCard, v3ViewModel } from './view-overview.js';\n"
        f"const m = v3ViewModel({lit(payload)});\n"
        "const card = matrixSummaryCard(m);\n"
        "console.log(JSON.stringify({ text: card.textContent, numbers: m.rendered_numbers.length }));\n"
    )
    result = run_esm(program, shim=DOM_SHIM)
    assert "Matrix summary" in result["text"]
    assert "Mapping version" in result["text"]


def test_provisional_edge_accept_calls_pin_route() -> None:
    """A PROVISIONAL edge accept POSTs /topology/edges/{id}/pin."""
    _require_node()
    program = (
        "import { acceptProvisionalEdge, rejectProvisionalEdge, v3TopologyModel } from './view-topology.js';\n"
        "const calls = [];\n"
        "const http = async (path, body) => { calls.push([path, body]); return {ok:true}; };\n"
        "await acceptProvisionalEdge('edge-42', http);\n"
        "await rejectProvisionalEdge('edge-42', http);\n"
        "const m = v3TopologyModel({edges:[{edge_id:'edge-42', edge_type:'contains', lifecycle:'provisional', reader_id:'ct_a'}]});\n"
        "console.log(JSON.stringify({calls, provisional: m.provisional.length, lifecycle: m.edges[0].lifecycle, drill: m.edges[0].matrix_drill}));\n"
    )
    result = run_esm(program, shim=DOM_SHIM)
    assert result["calls"] == [
        ["/topology/edges/edge-42/pin", {}],
        ["/topology/edges/edge-42/reject", {}],
    ]
    assert result["provisional"] == 1
    assert result["lifecycle"] == "provisional"
    assert result["drill"] == "/matrix/readers/ct_a"


def test_typed_edges_group_by_type_and_render_actions() -> None:
    """Typed edges are grouped by type and PROVISIONAL edges get accept/reject."""
    _require_node()
    program = (
        "import { v3TopologyModel, v3EdgesCard } from './view-topology.js';\n"
        "const payload = {edges:[\n"
        "  {edge_id:'e1', edge_type:'contains', lifecycle:'provisional', parent_id:'A', child_id:'B', reader_id:'ct_b'},\n"
        "  {edge_id:'e2', type:'peer', status:'confirmed', parent_id:'C', child_id:'D'},\n"
        "]};\n"
        "const m = v3TopologyModel(payload);\n"
        "const card = v3EdgesCard(m);\n"
        "console.log(JSON.stringify({types:Object.keys(m.byType), text:card.textContent}));\n"
    )
    result = run_esm(program, shim=EVENT_DOM_SHIM)
    assert set(result["types"]) == {"contains", "peer"}
    assert "Typed topology edges (v3)" in result["text"]
    assert "Accept" in result["text"] and "Reject" in result["text"]
    assert "Matrix columns" in result["text"]


def test_device_detail_model_exposes_status_bounds_group_and_observers(payload: dict) -> None:
    """Device detail carries status, identifiability, bounds, group, provisional."""
    _require_node()
    program = (
        "import { v3DeviceModel } from './view-devices.js';\n"
        f"const d = {lit(payload)};\n"
        "const m = v3DeviceModel(\n"
        "  {device_id:'pd_light_1', estimate:{status:'estimated', identifiability:'non_identifiable', group_id:'grp_living_lights'}},\n"
        "  d.cells.slice(0,2), [], null, []);\n"
        "console.log(JSON.stringify(m));\n"
    )
    model = run_esm(program, shim=DOM_SHIM)
    assert model["status"] == "estimated"
    assert model["identifiability"] == "non_identifiable"
    assert model["point_w"] is None
    assert model["group_id"] == "grp_living_lights"
    assert set(model["bounds"]) == {"lower_w", "upper_w"}
    assert len(model["observed_by"]) == 2
    assert all(r["drill"].startswith("/matrix/cells/") for r in model["observed_by"])


def test_endpoint_scope_confirm_reject_routes() -> None:
    """Endpoint scope review calls D2's scope route only on user action."""
    _require_node()
    program = (
        "import { confirmEndpointScope, rejectEndpointScope } from './view-devices.js';\n"
        "const calls = [];\n"
        "const http = async (path, body) => { calls.push([path, body]); return {}; };\n"
        "await confirmEndpointScope('plug_1', http);\n"
        "await rejectEndpointScope('plug_1', http);\n"
        "console.log(JSON.stringify({calls}));\n"
    )
    result = run_esm(program, shim=DOM_SHIM)
    assert result["calls"] == [
        ["/endpoints/plug_1/scope", {"decision": "confirm"}],
        ["/endpoints/plug_1/scope", {"decision": "reject"}],
    ]
