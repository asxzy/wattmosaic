"""AT-12 — Matrix columns reflect multi-level reader organisation.

HB §21 acceptance 12 (traceability §1): for the HB §17 reference house the
``MatrixPayload.columns`` tree has depth 5 (L0–L4); the DOM header has one row
per level; and for every header row the sum of ``colSpan`` equals the number of
reader leaves, which equals the reader count.  Reader counts are asserted from
the DOM, not from a re-implementation of the tree walk.

This file closes U1, so it carries no xfail marker.  It executes the real
shipped ES modules under node through ``tests/unit/panel_harness.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.panel_harness import DOM_SHIM, lit, node_path, run_esm

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "matrix" / "hb17_payload.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _render_header(payload: dict) -> dict:
    """Render the real header table and inspect the resulting DOM tree."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        "import { matrixHeaderTable } from './matrix-cell.js';\n"
        f"const d = {lit(payload)};\n"
        "const table = matrixHeaderTable(d.columns);\n"
        "const thead = table.children.find((n) => n.tagName === 'thead');\n"
        "const rows = thead.children.filter((n) => n.tagName === 'tr').map((tr) => ({\n"
        "  cells: tr.children.filter((n) => n.tagName === 'th').map((th) => ({\n"
        "    text: th.textContent,\n"
        "    colspan: Number(th.attrs.colspan || th.getAttribute?.('colspan') || 1),\n"
        "    level: th.dataset ? th.dataset.level : null,\n"
        "    kind: th.dataset ? th.dataset.kind : null,\n"
        "  })),\n"
        "}));\n"
        "console.log(JSON.stringify({ rows }));\n"
    )
    return run_esm(program, shim=DOM_SHIM)


def test_at12_header_row_count_equals_tree_depth(payload: dict) -> None:
    result = _render_header(payload)
    rows = result["rows"]
    assert len(rows) == 5, f"expected L0–L4 (5 header rows), got {len(rows)}"
    assert [r["cells"][0]["level"] for r in rows] == ["L0", "L1", "L2", "L3", "L4"]


def test_at12_colspan_sum_equals_leaf_count(payload: dict) -> None:
    result = _render_header(payload)
    rows = result["rows"]
    # The number of reader leaves is asserted from the L4 header row itself.
    readers = [c for c in rows[-1]["cells"] if c["kind"] == "reader"]
    assert len(readers) == 6, "HB17 fixture must publish 6 reader leaves"
    for idx, row in enumerate(rows):
        total = sum(c["colspan"] for c in row["cells"])
        assert total == len(readers), (
            f"header level L{idx}: sum(colSpan)={total} != reader count {len(readers)}"
        )


def test_at12_reader_leaves_show_id_and_name(payload: dict) -> None:
    result = _render_header(payload)
    l4 = result["rows"][-1]["cells"]
    texts = [c["text"] for c in l4 if c["kind"] == "reader"]
    # HB §19.1: show reader ids and readable names in the leaf columns.
    for reader in payload["reader_totals"]:
        assert any(reader in text or text in reader for text in texts), (
            f"reader {reader!r} missing from leaf header: {texts}"
        )
