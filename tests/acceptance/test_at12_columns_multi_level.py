"""AT-12 — matrix columns reflect multi-level reader organization (HB §21).

Closes: U1. The reference house's column tree has depth 5 (L0–L4); the DOM
header has one row per level and Σ colSpan per row equals the leaf count.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by U1"),
]


def _tree_depth(columns) -> int:
    return max((1 + _tree_depth(c.children) for c in columns), default=0)


def _leaves(columns):
    for c in columns:
        if not c.children:
            yield c
        else:
            yield from _leaves(c.children)


def test_columns_depth_five_and_dom_header(hb17, panel_model) -> None:
    """Column tree is L0–L4 and the rendered header mirrors it exactly."""
    from wattmosaic_engine.mapping import project_matrix_payload  # target surface (U1/M1)

    payload = project_matrix_payload(hb17["observation"])
    depth = _tree_depth(payload.columns)
    assert depth == 5, f"expected 5 column levels, got {depth}"

    leaves = list(_leaves(payload.columns))
    assert len(leaves) == 5, "one reader leaf per CT in the reference house"

    model = panel_model(payload)
    header_rows = model["column_header_rows"]
    assert len(header_rows) == 5
    for row in header_rows:
        assert sum(cell["colSpan"] for cell in row) == len(leaves)
