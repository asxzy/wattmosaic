"""AT-02 — a parent residual is not evidence against child containment (HB §21).

Closes: T5. Twenty parent-only residual steps leave the CONTAINS edge lifecycle
and support counters unchanged, and the conflict count stays zero.
"""

from __future__ import annotations

import pytest

CLOSED_BY = "T5"

pytestmark = [pytest.mark.acceptance]


def test_parent_only_steps_do_not_weaken_containment(hb17) -> None:
    """Parent-only power changes are neutral to a child's containment edge."""
    from wattmosaic_engine.contracts import TopologyEdgeState
    from wattmosaic_engine.topology import infer_topology  # target surface (T5)

    view = infer_topology(hb17["observation"])
    edge = view.edge("ct_main", "ct_sub", "contains")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL
    before_support = edge.evidence.supporting
    before_conflict = edge.evidence.conflicting

    for _ in range(20):
        view = view.observe_parent_only("ct_main", delta_w=25.0)

    edge = view.edge("ct_main", "ct_sub", "contains")
    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL
    assert edge.evidence.supporting == before_support
    assert edge.evidence.conflicting == before_conflict
    assert edge.evidence.conflicting == 0
