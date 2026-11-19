"""AT-14 — one physical load visible to many Readers counted once (HB §21).

Closes: I5. The observable TV (a single-subject plug reporting 3 W standby in
HB §17 step 8) is seen by the plug, Circuit A, Subpanel and Main.  The snapshot
publishes the TV exactly once with ``MEASURED`` status, and the reconciliation
counts that one physical load once in ``identified_w`` — not four times from
the four rows that observe it.

The test asserts the semantics on the contract objects from I5's pure
``build_snapshot`` (no HTTP surface exists yet; M1 owns the matrix projection
and U1/U2 own the panel).  The observation frame is the only input: no ground
truth is read, so the same assertions hold under replay (AT-19).
"""

from __future__ import annotations

import pytest
from wattmosaic_engine.contracts import EstimateStatus, InferenceMethod

CLOSED_BY = "I5"


def test_tv_counted_once_in_snapshot_reconciliation(hb17) -> None:
    """Four readers observing one TV still yield one physical contribution."""
    from wattmosaic_engine.inference.estimates import build_snapshot

    snapshot = build_snapshot(hb17["observation"])
    tv_estimates = [e for e in snapshot.device_estimates if e.subject_id == "tv"]
    assert len(tv_estimates) == 1

    tv = tv_estimates[0]
    # MEASURED comes only from a confirmed single-subject endpoint (DEC-05):
    # the plug's 3 W standby, never collapsed to 0 (HB §17 step 8).
    assert tv.status == EstimateStatus.MEASURED
    assert tv.method == InferenceMethod.DIRECT_READING
    assert tv.point_w == pytest.approx(3.0, abs=0.01)
    assert tv.point_w > 0.0
    assert tv.source_reader_ids, "the TV must list at least one contributing reader"

    # The nested CTs observe the same physical load as EXPECTED entries, but
    # provenance never duplicates one reader and the reconciliation counts the
    # TV once, not once per observing reader.
    assert len(tv.source_reader_ids) == len(set(tv.source_reader_ids))
    recon_tv = snapshot.reconciliation.identified_w
    assert recon_tv is not None
    assert recon_tv == pytest.approx(3.0, abs=0.01)
    # Not 4× the plug reading (the DEF-04 / AT-14 double-count defect).
    assert recon_tv != pytest.approx(12.0)


def test_reconciliation_terms_use_null_semantics(hb17) -> None:
    """Absent terms are None, never 0.0; the identity is never fabricated."""
    from wattmosaic_engine.inference.estimates import build_snapshot

    snapshot = build_snapshot(hb17["observation"])
    recon = snapshot.reconciliation
    # main and identified are real; group/unknown may be absent (HB §18).
    assert recon.main_w is not None
    assert recon.identified_w is not None
    if recon.group_w is None or recon.unknown_w is None:
        assert recon.balance_error_w is None
