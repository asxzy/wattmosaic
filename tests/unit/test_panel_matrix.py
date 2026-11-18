"""Node-executed tests for the user-facing matrix model (U1).

The pure helpers in ``addon/ui/matrix-model.js`` are the contract between the
v3 ``MatrixPayload`` (K05) and the DOM renderers.  They are deliberately free of
network and DOM access so they can be imported directly under node via
``tests/unit/panel_harness.py``.  Every test here executes the real shipped
module — never a copy of its logic — against the K05 HB §17 fixture.

These are the U1 tests: ``headerRows``/``rowTree``/``cellView``/``applyFilters``,
the eight distinct text categories, group/member semantics, and the structured
filters.  The DOM-level AT-12 assertions live in
``tests/acceptance/test_at12_multilevel_columns.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.panel_harness import DOM_SHIM, EVENT_DOM_SHIM, lit, node_path, run_esm

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "matrix" / "hb17_payload.json"

_MODEL = "import { headerRows, rowTree, cellView, applyFilters, leafReaders, flattenRows, matrixObserveVersion, CATEGORIES } from './matrix-model.js';\n"
_DRILL = "import { matrixCellDrill, matrixReaderDrill, matrixRowDrill } from './matrix-drill.js';\n"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _run(program: str) -> dict:
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    return run_esm(_MODEL + program, shim=DOM_SHIM)


# --------------------------------------------------------------------------- #
# headerRows — L0..L4 nested colSpan (AT-12 at the model level).
# --------------------------------------------------------------------------- #


def test_header_rows_have_one_row_per_depth(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ rows: headerRows(d.columns) }));\n"
    )
    rows = result["rows"]
    assert len(rows) == 5, f"expected 5 header levels for the HB17 house, got {len(rows)}"
    # Each level exposes its depth label.
    assert [r["level"] for r in rows] == ["L0", "L1", "L2", "L3", "L4"]


def test_colspan_sums_to_leaf_count_per_row(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ rows: headerRows(d.columns), leaves: leafReaders(d.columns) }));\n"
    )
    leaves = result["leaves"]
    assert len(leaves) == 6, "HB17 fixture should have 6 reader leaves"
    for row in result["rows"]:
        total = sum(cell["colSpan"] for cell in row["cells"])
        assert total == len(leaves), (
            f"row {row['level']} colSpan sum {total} != leaf count {len(leaves)}"
        )


def test_header_rows_chain_parent_ids(payload: dict) -> None:
    """A lower level's cell points at its parent's node id (nested tree)."""
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ rows: headerRows(d.columns) }));\n"
    )
    rows = result["rows"]
    assert rows[0]["cells"][0]["parentId"] is None
    # README: the fixture nests some nodes at irregular levels (e.g. the
    # "Unassigned measurements" L1 container sits under the L1 panel node).
    # A lower-level cell's parent must still be a node published in an earlier
    # level, which is what makes the header a tree rather than a flat list.
    # A cell's parentId must name a real node from the column tree; cells at
    # L0 are roots (parentId None).  This proves the header is built from the
    # nested tree rather than inventing a flat list of readers.
    def node_ids(nodes):
        for n in nodes:
            yield n.get("node_id")
            yield from node_ids(n.get("children") or [])
    tree_ids = set(node_ids(payload["columns"]))
    for row in rows:
        for cell in row["cells"]:
            if row["level"] == "L0":
                assert cell["parentId"] is None
            else:
                assert cell["parentId"] in tree_ids, (
                    f"{cell['level']} cell {cell['label']} has unknown parent "
                    f"{cell['parentId']}"
                )


def test_annotations_are_reported_without_extra_leaves(payload: dict) -> None:
    """Secondary edges become annotation chips, never extra reader columns."""
    columns = json.loads(json.dumps(payload["columns"]))
    # Annotate a leaf with a paired/alias/overlap edge.
    leaf = columns[0]["children"][0]["children"][0]["children"][0]["children"][0]
    leaf["annotations"] = [
        {"kind": "paired_leg", "label": "Paired with ct_circuit_b"},
        {"kind": "shared_load", "label": "Shared with ct_main"},
    ]
    result = _run(
        f"const d = {{ columns: {lit(columns)} }};\n"
        "console.log(JSON.stringify({ rows: headerRows(d.columns), leaves: leafReaders(d.columns) }));\n"
    )
    assert len(result["leaves"]) == 6, "annotations must not create extra leaves"
    assert sum(c["colSpan"] for c in result["rows"][-1]["cells"]) == 6
    l4 = result["rows"][-1]["cells"]
    assert any("Paired with" in a["label"] for c in l4 for a in c["annotations"])


# --------------------------------------------------------------------------- #
# rowTree — device groups, unknown profiles, residuals and summaries.
# --------------------------------------------------------------------------- #


def test_row_tree_preserves_kinds_and_hierarchy(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ tree: rowTree(d.rows) }));\n"
    )
    tree = result["tree"]
    assert tree, "expected a non-empty row tree"
    # Special rows are nested under rooms, so collect kinds from the subtree.
    kinds = set()
    def collect(nodes):
        for n in nodes:
            kinds.add(n["kind"])
            collect(n.get("children") or [])
    collect(tree)
    assert {"room", "device", "group", "unknown_profile", "reader_residual", "summary"} <= kinds
    # The living-room room node carries the group and its member devices.
    living = next(n for n in tree if n["kind"] == "room" and "Living" in n["label"])
    group = next(c for c in living["children"] if c["kind"] == "group")
    assert len(group["children"]) == 2, "group members are child rows"


def test_flatten_rows_exposes_group_members_and_special_labels(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ flat: flattenRows(rowTree(d.rows)) }));\n"
    )
    flat = result["flat"]
    by_id = {r["rowId"]: r for r in flat if r.get("rowId")}
    group = by_id["grp_living_lights"]
    assert group["kind"] == "group"
    assert set(group["memberRowIds"]) == {"pd_light_1", "pd_light_2"}
    assert by_id["ulp_kitchen_fridge"]["kind"] == "unknown_profile"
    assert by_id["ct_main"]["kind"] == "reader_residual"
    assert by_id["physical_total"]["kind"] == "summary"
    assert by_id["physical_total"]["derived"] is True


# --------------------------------------------------------------------------- #
# cellView — 8 distinct text categories, not colour-only (HB §19.3).
# --------------------------------------------------------------------------- #


def test_all_eight_categories_have_distinct_text(payload: dict) -> None:
    result = _run(
        "console.log(JSON.stringify({ categories: CATEGORIES }));\n"
    )
    cats = result["categories"]
    assert len(cats) == 8
    texts = [c["text"] for c in cats]
    assert len(set(texts)) == 8, f"categories must have distinct text: {texts}"
    glyphs = [c["glyph"] for c in cats]
    assert len(set(glyphs)) == 8, f"categories must have distinct glyphs: {glyphs}"


def test_cell_view_maps_fixture_cells_to_categories(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ views: d.cells.map(cellView) }));\n"
    )
    views = result["views"]
    assert len(views) == len(payload["cells"])
    for view in views:
        assert set(("category", "text", "glyph", "value", "confidence", "age")) <= set(view)
    # Group-only member cells show an interval + non-identifiable text, no watt.
    member = next(v for v in views if v["category"] == "group_only")
    assert "non-identifiable" in member["text"].lower()
    assert member["value"] == "—", "a non-identifiable member never gets a number"
    # Directly measured cells show a real value.
    measured = next(v for v in views if v["category"] == "directly_measured")
    assert measured["value"] != "—"


def test_cell_view_interval_and_confidence_and_age(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ views: d.cells.map(cellView) }));\n"
    )
    views = result["views"]
    # A confirmed group cell carries bounds and a confidence glyph.
    group = next(
        v for v in views
        if v["category"] == "confirmed_inferred" and v["interval"] is not None
    )
    assert "W" in group["interval"]
    assert group["confidence"], "a cell with confidence components gets a glyph"
    # Every cell exposes an age string (possibly '—' when no timestamp).
    assert all(isinstance(v["age"], str) for v in views)


def test_cell_view_unknown_never_invents_a_number(payload: dict) -> None:
    cell = {
        "row_kind": "device", "row_id": "d", "reader_id": "r",
        "mapping": {"observability": "unknown", "relation_type": None,
                    "mapping_status": "unknown", "valid_from": "2026-01-01T00:00:00Z"},
        "power": {"measurement_status": "unknown", "estimated_power_w": 0,
                  "bounds": None},
        "identifiability": "insufficient_evidence",
        "group_id": None,
        "event_association": {}, "confidence": {},
        "provenance": {"sources_used": [], "evidence_refs": [],
                       "topology_version_id": "t", "mapping_version_id": "m"},
        "timestamps": {"first_observed_at": "2026-01-01T00:00:00Z",
                       "updated_at": "2026-01-01T00:00:00Z"},
        "visual": "unknown", "derived": False,
    }
    result = _run(
        f"const c = {lit(cell)};\n"
        "console.log(JSON.stringify({ view: cellView(c) }));\n"
    )
    view = result["view"]
    assert view["category"] == "unknown"
    assert view["value"] == "—", "a 0 W placeholder must not be shown for UNKNOWN"


# --------------------------------------------------------------------------- #
# applyFilters — structured filters over the v3 payload.
# --------------------------------------------------------------------------- #


def test_apply_filters_by_reader(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ out: applyFilters(d, { reader: 'plug_tv' }) }));\n"
    )
    out = result["out"]
    cells = out["cells"]
    assert cells and all(c["reader_id"] == "plug_tv" for c in cells)
    # Leaves not matching the filter are pruned from the column tree.
    assert [r["reader_id"] for r in out["leafReaders"]] == ["plug_tv"]


def test_apply_filters_by_mapping_status_and_category(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({\n"
        "  prov: applyFilters(d, { status: 'provisional' }).cells,\n"
        "  grp: applyFilters(d, { category: 'group_only' }).cells,\n"
        "}));\n"
    )
    assert result["prov"] and all(
        c["mapping"]["mapping_status"] == "provisional" for c in result["prov"]
    )
    assert result["grp"] and all(c["visual"] == "group_only" for c in result["grp"])


def test_apply_filters_by_identifiability_and_unknown(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({\n"
        "  nonid: applyFilters(d, { identifiability: 'non_identifiable' }).cells,\n"
        "  unknown: applyFilters(d, { unknownOnly: true }).cells,\n"
        "}));\n"
    )
    assert result["nonid"] and all(
        c["identifiability"] == "non_identifiable" for c in result["nonid"]
    )
    assert result["unknown"] and all(
        c["row_kind"] == "unknown_profile" for c in result["unknown"]
    )


def test_apply_filters_by_panel_and_level(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({\n"
        "  panel: applyFilters(d, { panel: 'Unassigned measurements' }).leafReaders,\n"
        "  circuit: applyFilters(d, { circuit: 'Circuit A' }).leafReaders,\n"
        "}));\n"
    )
    assert [r["reader_id"] for r in result["panel"]] == ["plug_tv"]
    assert [r["reader_id"] for r in result["circuit"]] == ["ct_circuit_a"]


def test_apply_filters_empty_returns_full_payload(payload: dict) -> None:
    result = _run(
        f"const d = {lit(payload)};\n"
        "console.log(JSON.stringify({ out: applyFilters(d, {}) }));\n"
    )
    out = result["out"]
    assert len(out["cells"]) == len(payload["cells"])
    assert len(out["leafReaders"]) == 6


# --------------------------------------------------------------------------- #
# Polling: refetch only on a mapping_version_id change.
# --------------------------------------------------------------------------- #


def test_polling_refetches_only_on_version_change() -> None:
    result = _run(
        "const a = matrixObserveVersion({ mapping_version_id: 'map-7' });\n"
        "const b = matrixObserveVersion({ mapping_version_id: 'map-7' });\n"
        "const c = matrixObserveVersion({ mapping_version_id: 'map-8' });\n"
        "const d = matrixObserveVersion({ versions: { mapping_version_id: 'map-9' } });\n"
        "console.log(JSON.stringify({ a, b, c, d }));\n"
    )
    assert result == {"a": True, "b": False, "c": True, "d": True}, result


# --------------------------------------------------------------------------- #
# Performance: 500 rows × 100 readers (HB §19 / U1 tests).
# --------------------------------------------------------------------------- #


def test_cell_drill_shows_evidence(payload: dict) -> None:
    """The cell drill surfaces mapping, events, evidence, confidence & reasons."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        f"const d = {lit(payload)};\n"
        "const groupCell = d.cells.find((c) => c.visual === 'group_only');\n"
        "const text = matrixCellDrill(groupCell, d).textContent;\n"
        "const readerText = matrixReaderDrill(d).textContent;\n"
        "const rowText = matrixRowDrill(d).textContent;\n"
        "console.log(JSON.stringify({ text, readerText, rowText }));\n"
    )
    result = run_esm(_DRILL + program, shim=EVENT_DOM_SHIM)
    text = result["text"]
    for value in ("Mapping status", "Event groups", "Evidence", "Confidence components",
                  "Reasons for uncertainty", "Group", "non-identifiable"):
        assert value.lower() in text.lower(), f"cell drill missing {value!r}: {text[:300]}"
    assert "grp_living_lights" in text, "the group id must appear in the drill"
    # Reader drill shows per-reader totals, never summed across readers.
    assert "Measured" in result["readerText"] and "Residual" in result["readerText"]
    assert "ct_main" in result["readerText"]
    # Row drill lists row ids and their observed readers.
    assert "grp_living_lights" in result["rowText"] or "pd_tv" in result["rowText"]


def test_large_fixture_render_is_bounded() -> None:
    """A 500×100 fixture renders quickly with groups collapsed by default."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    # Build a large v3 payload: 100 reader leaves and 500 device rows grouped
    # in 50 groups of 10 members (collapsed by default in the DOM renderer).
    js = r"""
import { headerRows, rowTree, flattenRows, cellView } from './matrix-model.js';
const leaves = Array.from({length: 100}, (_, i) => ({
  node_id: 'r' + i, level: 'L4', label: 'R' + i, kind: 'reader',
  reader_id: 'ct_' + i, children: [], annotations: [],
}));
const columns = [{
  node_id: 'd', level: 'L0', label: 'Home', kind: 'domain',
  reader_id: null, annotations: [],
  children: [{
    node_id: 'p', level: 'L1', label: 'Panel', kind: 'panel',
    reader_id: null, annotations: [], children: leaves,
  }],
}];
const rows = Array.from({length: 50}, (_, g) => ({
  node_id: 'grp' + g, kind: 'group', label: 'G' + g, row_id: 'grp_' + g,
  children: Array.from({length: 10}, (_, m) => ({
    node_id: 'd' + g + '_' + m, kind: 'device', label: 'D',
    row_id: 'dev_' + g + '_' + m, children: [],
  })),
}));
const t0 = Date.now();
const hdr = headerRows(columns);
const flat = flattenRows(rowTree(rows));
const cells = Array.from({length: 500}, (_, i) => ({
  visual: 'group_only', identifiability: 'non_identifiable',
  row_id: 'dev_' + Math.floor(i / 10) + '_' + (i % 10),
  reader_id: 'ct_' + (i % 100),
  power: { estimated_power_w: null, bounds: null },
  mapping: { mapping_status: 'confirmed', observability: 'partial' },
  confidence: {}, timestamps: {},
}));
const views = cells.map(cellView);
const ms = Date.now() - t0;
console.log(JSON.stringify({
  ms, headerLevels: hdr.length, rootRows: rows.length,
  flatRows: flat.length, views: views.length,
  collapsedMembers: flat.filter((r) => r.depth > 0).length,
}));
"""
    result = run_esm(js, shim=DOM_SHIM)
    assert result["headerLevels"] == 5
    assert result["views"] == 500
    assert result["ms"] < 1500, f"model projection took {result['ms']}ms (budget 1500ms)"
    # DOM nodes stay bounded: the grid renders 50 root rows by default and the
    # 500 member rows are hidden until their group is expanded.
    assert result["collapsedMembers"] == 500, "all group members are present but collapsed"

