"""AT-13 — reader columns are not duplicated for secondary relationships (HB §21).

Closes: M1. SHARED_LOAD, PAIRED_LEG and SAME_CONDUCTOR edges become annotations,
never extra leaves; leaf count equals endpoint count.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by M1"),
]


def _leaves(columns):
    for c in columns:
        if not c.children:
            yield c
        else:
            yield from _leaves(c.children)


def test_secondary_edges_are_annotations_not_leaves(hb17) -> None:
    """Secondary relations annotate existing readers without minting columns."""
    from wattmosaic_engine.contracts import RelationType
    from wattmosaic_engine.mapping import project_matrix_payload  # target surface (M1)

    payload = project_matrix_payload(
        hb17["observation"],
        secondary_edges=[RelationType.SHARED_LOAD, RelationType.PAIRED_LEG,
                         RelationType.SAME_CONDUCTOR],
    )
    leaves = list(_leaves(payload.columns))
    assert len(leaves) == 5
    assert len({leaf.reader_id for leaf in leaves}) == 5

    annotations = [a for c in payload.columns for a in c.annotations]
    kinds = {a.kind for a in annotations}
    assert kinds >= {"shared_load", "paired_leg", "same_conductor"}
