"""AT-08 — direct smart-plug measurements are stronger anchors (HB §21).

Closes: I3. With a noisy circuit, the TV via plug is MEASURED; the joint solve
keeps the TV within plug σ and moves the error to the other candidates or ΔU.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I3"),
]


def test_plug_anchor_constrains_joint_solve(hb17) -> None:
    """A low-σ plug measurement anchors the TV; error moves elsewhere."""
    from wattmosaic_engine.contracts import EstimateStatus
    from wattmosaic_engine.inference import infer_jointly  # target surface (I3)

    result = infer_jointly(
        reader_deltas={"ct_a": 130.0, "tv_plug": 120.0},
        reader_sigmas={"ct_a": 15.0, "tv_plug": 0.5},
    )
    tv = next(e for e in result.device_estimates if e.subject_id == "tv")
    assert tv.status == EstimateStatus.MEASURED
    assert tv.point_w == pytest.approx(120.0, abs=0.5)
    assert abs(tv.point_w - 120.0) <= 3 * 0.5

    others_error = sum(
        abs((e.point_w or 0.0) - 45.0)
        for e in result.device_estimates
        if e.subject_id != "tv"
    )
    assert others_error > 0 or result.residual_w_by_reader.get("ct_a", 0.0) > 0
