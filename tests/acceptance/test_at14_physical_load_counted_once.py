"""AT-14 — one physical load visible to many readers counted once (HB §21).

Closes: I5. Both the snapshot reconciliation and the matrix physical total
count the TV once, even with plug + circuit + subpanel + main observing it.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I5"),
]


def test_tv_counted_once_in_snapshot_and_matrix(hb17) -> None:
    """Four readers observing one TV still yield one physical contribution."""
    from wattmosaic_engine.inference import build_snapshot  # target surface (I5)
    from wattmosaic_engine.mapping import project_matrix_payload  # target surface (M1)

    snapshot = build_snapshot(hb17["observation"])
    tv_estimates = [e for e in snapshot.device_estimates if e.subject_id == "tv"]
    assert len(tv_estimates) == 1
    assert set(tv_estimates[0].source_reader_ids) >= {
        "tv_plug", "ct_a", "ct_sub", "ct_main"
    }

    recon_tv = snapshot.reconciliation.identified_w
    assert recon_tv == pytest.approx(120.0, abs=2.0)

    matrix = project_matrix_payload(hb17["observation"])
    assert matrix.physical_total.identified_w == pytest.approx(120.0, abs=2.0)
    assert matrix.physical_total.total_w == pytest.approx(
        sum(
            v for v in (
                matrix.physical_total.measured_w,
                matrix.physical_total.identified_w,
                matrix.physical_total.group_w,
                matrix.physical_total.unknown_w,
                matrix.physical_total.residual_w,
            ) if v is not None
        ),
        abs=0.01,
    )
