"""AT-03 — stable non-zero standby consumption is modeled (HB §21 test 3).

Closes: L2.  Two standby paths are pinned against the HB §17 reference house:

* the TV on a *single-subject endpoint* (smart plug) reports its 3 W standby
  explicitly at step 8 -> MEASURED at ``PowerLevelV3.STANDBY``, never 0 W;
* a device on a *shared circuit* has no exclusive endpoint.  Its 7 W standby
  is learned from a device-attributed ΔP chain (OFF transition) plus a
  residual-baseline step (T3) -> ESTIMATED at STANDBY with mean ~7, never 0.

The test injects the learning observations explicitly (the L4/L2 handoff) so
it exercises the real fitter rather than reading ground truth out of the
simulator.  No xfail: L2 closes this requirement.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    EstimateStatus,
    EvidenceRef,
    LearningObservation,
    ModelBasis,
    PowerLevelV3,
)
from wattmosaic_engine.models import learn_power_model

CLOSED_BY = "L2"

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _obs(
    subject: str,
    seq: int,
    *,
    endpoint: str,
    transition: str,
    delta_w: float | None,
    level_w: float | None = None,
    basis: ModelBasis = ModelBasis.DELTA,
    attrs: dict | None = None,
) -> LearningObservation:
    return LearningObservation(
        observation_id=f"obs-{subject}-{seq}",
        subject_kind="device",
        subject_id=subject,
        endpoint_id=endpoint,
        event_group_ref=EvidenceRef(kind="event_group", id=f"grp-{subject}-{seq}"),
        transition=transition,
        attributes=attrs or {},
        delta_w=delta_w,
        level_w=level_w,
        status="confirmed",
        identifiability="identifiable",
        basis=basis,
        created_at=T0,
    )


def test_plug_standby_measured_and_learned_standby_level(hb17) -> None:
    """A stable 3 W standby is MEASURED; a shared device learns STANDBY ~7 W."""
    house = hb17["house"]
    plug_samples = [s for s in house.reader_samples if s["reader_id"] == "ct_plug"]
    assert plug_samples, "the plug must emit samples for the 3 W standby to be observable"
    standby_step = next(e for e in house.truth["steps"] if e["name"] == "tv_standby_reported")
    assert standby_step["step"] == 8  # HB §17 step 8

    # The TV is anchored by its reported single-subject endpoint level; the
    # shared-circuit device has only a ΔP chain plus a residual-baseline step.
    observations = [
        _obs(
            "shared_circuit_device",
            0,
            endpoint="ct_a",
            transition="on",
            delta_w=45.0,
        ),
        _obs(
            "shared_circuit_device",
            1,
            endpoint="ct_a",
            transition="off",
            delta_w=-45.0,
        ),
        _obs(
            "shared_circuit_device",
            2,
            endpoint="ct_a",
            transition="residual_baseline",
            delta_w=-7.0,
            attrs={"level": "standby", "residual_step_w": 7.0},
        ),
    ]

    estimates = learn_power_model(hb17["observation"], observations=observations)
    by_id = {e.subject_id: e for e in estimates}

    tv = by_id["tv"]
    assert tv.point_w == pytest.approx(3.0, abs=1.0)
    assert tv.status == EstimateStatus.MEASURED
    assert tv.level == PowerLevelV3.STANDBY
    assert tv.point_w is not None and tv.point_w > 0
    assert tv.source_reader_ids == ["ct_plug"]

    shared = by_id["shared_circuit_device"]
    assert shared.point_w == pytest.approx(7.0, abs=2.0)
    assert shared.level == PowerLevelV3.STANDBY
    assert shared.status == EstimateStatus.ESTIMATED
    assert shared.point_w is not None and shared.point_w > 0  # OFF never means 0 W
    # A non-identifiable/absent standby would carry None, never a fabricated 0.
    assert shared.point_w != 0.0
