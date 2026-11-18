"""AT-03 — stable non-zero standby consumption is modeled (HB §21).

Closes: L2. The TV plug reports 3 W standby → MEASURED 3 W; a shared-circuit
device learns a 7 W standby level from ΔP and reports level STANDBY, not 0.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by L2"),
]


def test_plug_standby_measured_and_learned_standby_level(hb17) -> None:
    """A stable 3 W standby is reported as MEASURED, never collapsed to zero."""
    from wattmosaic_engine.contracts import EstimateStatus, PowerLevelV3
    from wattmosaic_engine.models import learn_power_model  # target surface (L2)

    house = hb17["house"]
    plug_samples = [s for s in house.reader_samples if s["reader_id"] == "tv_plug"]
    assert plug_samples, "the plug must emit samples for the 3 W standby to be observable"
    standby_step = next(
        e for e in house.truth["steps"] if e["name"] == "tv_standby_reported"
    )
    assert standby_step.step == 8  # HB §17 step 8

    estimates = learn_power_model(hb17["observation"])
    tv_est = next(e for e in estimates if e.subject_id == "tv")
    assert tv_est.point_w == pytest.approx(3.0, abs=1.0)
    assert tv_est.status == EstimateStatus.MEASURED
    assert tv_est.level == PowerLevelV3.STANDBY
    assert tv_est.point_w is not None and tv_est.point_w > 0

    # A device sharing a circuit with no dedicated endpoint learns from ΔP.
    shared = next(e for e in estimates if e.subject_id == "shared_circuit_device")
    assert shared.point_w == pytest.approx(7.0, abs=2.0)
    assert shared.level == PowerLevelV3.STANDBY
