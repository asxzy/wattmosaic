"""AT-18 — late-arriving events revise provisional results (HB §21).

Closes: I6. Revision 1 is PROVISIONAL from Main only; a Circuit A sample 20 s
late yields revision 2 REVISED with changed attribution. Revision 1 and its
evidence stay byte-identical.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.acceptance

CLOSED_BY = "I6"


def test_late_sample_revises_without_rewriting_history(hb17) -> None:
    """A late sample appends a new revision; the original record is immutable."""
    from wattmosaic_engine.contracts import EventGroupStatus
    from wattmosaic_engine.inference import records as record_revision  # I6 target surface

    store = record_revision.EventRecordStore()
    rev1 = store.open_provisional(readers=["ct_main"])
    assert rev1.status == EventGroupStatus.PROVISIONAL
    rev1_bytes = rev1.model_dump_json()
    evidence1_bytes = store.evidence_bytes(rev1.record_id)

    rev2 = store.apply_late_sample(
        rev1.group_id, reader="ct_a", delta_w=45.0, late_by_s=20.0
    )
    assert rev2.status == EventGroupStatus.REVISED
    assert rev2.interpretation_revision == rev1.interpretation_revision + 1
    assert rev2.result != rev1.result

    assert store.get(rev1.record_id).model_dump_json() == rev1_bytes
    assert store.evidence_bytes(rev1.record_id) == evidence1_bytes
