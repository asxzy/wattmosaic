"""AT-11 — mapping cells change as evidence accumulates (HB §21).

Closes: M2. Cell independent-event counts and confidence rise only with distinct
event groups; 100 samples from one event change the count by exactly 1.
"""

from __future__ import annotations

CLOSED_BY = "M2"


def test_cell_counts_distinct_event_groups_only() -> None:
    """Repeated samples of one event do not inflate the independent count."""
    from wattmosaic_engine.mapping import MatrixStore  # target surface (M2)

    store = MatrixStore()
    cell = ("device", "light_1", "ct_a")
    store.apply_samples(cell, n=100, event_group_id="evt-1")
    after_one = store.cell(cell).event_association.independent_event_count
    assert after_one == 1

    for eid in ("evt-2", "evt-3"):
        store.apply_samples(cell, n=10, event_group_id=eid)
    after_three = store.cell(cell).event_association.independent_event_count
    assert after_three == 3

    conf = store.cell(cell).confidence.event_association
    assert conf > 0.0
