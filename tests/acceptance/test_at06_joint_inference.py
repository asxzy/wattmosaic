"""AT-06 — multiple simultaneous devices jointly inferred (HB §21).

Closes: I3. HB §7.7 example: A +45, B +60, Main +110, then lights and TV are
separated, ~5 W goes to ΔU on Main and there is no greedy assignment.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I3"),
]


def test_simultaneous_devices_are_separated_jointly(hb17) -> None:
    """A joint solve separates co-occurring devices and keeps residual as ΔU."""
    from wattmosaic_engine.contracts import JointInferenceResult
    from wattmosaic_engine.inference import infer_jointly  # target surface (I3)

    result: JointInferenceResult = infer_jointly(
        reader_deltas={"ct_a": 45.0, "ct_b": 60.0, "ct_main": 110.0}
    )
    by_id = {e.subject_id: e for e in result.device_estimates}
    assert by_id["light_1"].point_w == pytest.approx(22.5, abs=5.0)
    assert by_id["light_2"].point_w == pytest.approx(22.5, abs=5.0)
    assert by_id["tv"].point_w == pytest.approx(60.0, abs=5.0)

    assert result.residual_w_by_reader.get("ct_main") == pytest.approx(5.0, abs=2.0)
    assert result.greedy_assignment_used is False
