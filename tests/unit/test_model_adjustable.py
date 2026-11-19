"""L2: AdjustableModel fitted on device-attributed delta power (analysis F2).

What is pinned:

* the control variable is declared, not a hard-coded vocabulary (no brightness
  table, no ``_MAX_RPM`` normalisation);
* a non-linear P(x) curve is recovered without assuming P = k·x;
* PAVA is applied when monotonicity is declared or validated, and only then;
* ``x_coverage`` is reported and a query outside the declared control value
  returns ``None`` / ``UNKNOWN``;
* standby is a LevelStat on the same spec, never 0 W;
* declared priors seed the spec and are never overwritten; a >3σ discrepancy
  is surfaced as a diagnostic;
* the model class comes from the declared ``LoadShape`` (ADJUSTABLE here),
  and learning never silently switches a FIXED device.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    EvidenceRef,
    LearningObservation,
    LevelStat,
    LoadShape,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PredictionStatus,
)
from wattmosaic_engine.models.adjustable import AdjustableModel, model_class_for_load_shape
from wattmosaic_engine.models.spec import decode_spec, encode_spec

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _obs(
    x: float,
    delta_w: float,
    *,
    variable: str = "position",
    seq: int = 0,
    subject: str = "damper",
    basis: ModelBasis = ModelBasis.DELTA,
    level_w: float | None = None,
) -> LearningObservation:
    return LearningObservation(
        observation_id=f"obs-{subject}-{seq}",
        subject_kind="device",
        subject_id=subject,
        endpoint_id="ct_shared",
        event_group_ref=EvidenceRef(kind="event_group", id=f"grp-{seq}"),
        transition="level_change",
        attributes={variable: x},
        delta_w=delta_w,
        level_w=level_w,
        status="confirmed",
        identifiability="identifiable",
        basis=basis,
        created_at=T0,
    )


def test_class_from_declared_load_shape() -> None:
    assert model_class_for_load_shape(LoadShape.ADJUSTABLE) == ModelClass.ADJUSTABLE
    assert model_class_for_load_shape(LoadShape.FIXED) == ModelClass.FIXED
    assert model_class_for_load_shape(LoadShape.UNKNOWN) == ModelClass.FIXED
    assert AdjustableModel.model_class == ModelClass.ADJUSTABLE


def test_nonlinear_curve_recovered_without_linear_assumption() -> None:
    # Quadratic power: a straight P=kx fit would be badly wrong at the ends.
    points = [(0, 0), (10, 5), (20, 20), (30, 45), (40, 80)]
    observations = [_obs(x, y, seq=i, variable="position") for i, (x, y) in enumerate(points)]
    model = AdjustableModel().fit(
        observations,
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert model.spec.control_variable == "position"
    assert model.spec.knots_x == pytest.approx([0, 10, 20, 30, 40])
    assert model.spec.knots_w == pytest.approx([0, 5, 20, 45, 80])
    # Interpolation between knots works, no P=kx assumption.
    pred = model.predict(level=PowerLevelV3.ACTIVE, attributes={"position": 25})
    assert pred.status == PredictionStatus.KNOWN
    assert pred.mean_w == pytest.approx(32.5, abs=1e-6)


def test_declared_control_variable_has_no_hardcoded_vocabulary() -> None:
    observations = [_obs(x, x * x, seq=i, variable="valve_opening") for i, x in enumerate([0, 1, 2, 3])]
    model = AdjustableModel().fit(
        observations,
        control_variable="valve_opening",
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert model.spec.control_variable == "valve_opening"
    # A different declared name is not silently mixed in.
    other = AdjustableModel().fit(
        observations,
        control_variable="brightness",
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert other.spec.knots_x == []


def test_no_max_rpm_normalisation() -> None:
    observations = [_obs(x, float(x), seq=i, variable="rpm") for i, x in enumerate([0, 500, 1500, 2500])]
    model = AdjustableModel().fit(
        observations,
        control_variable="rpm",
        monotonic=True,
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert model.spec.knots_x == pytest.approx([0, 500, 1500, 2500])


def test_pava_only_when_monotonic_declared_or_validated() -> None:
    # Declared non-monotonic curve keeps its shape.
    data = [(0, 10), (10, 40), (20, 25), (30, 60)]
    observations = [_obs(x, y, seq=i) for i, (x, y) in enumerate(data)]
    free = AdjustableModel().fit(
        observations,
        control_variable="position",
        monotonic=False,
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert free.spec.monotone is False
    assert free.spec.knots_w == pytest.approx([10, 40, 25, 60])

    # Declared monotonic enforces PAVA (the 40 > 25 violation is pooled).
    mono = AdjustableModel().fit(
        observations,
        control_variable="position",
        monotonic=True,
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert mono.spec.monotone is True
    assert mono.spec.knots_w == sorted(mono.spec.knots_w)


def test_x_coverage_reported_and_unknown_returns_none() -> None:
    observations = [_obs(x, float(x) * 2, seq=i) for i, x in enumerate([10, 20, 30])]
    model = AdjustableModel().fit(
        observations,
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert model.spec.x_coverage == pytest.approx((10.0, 30.0))
    known = model.predict(level=PowerLevelV3.ACTIVE, attributes={"position": 20})
    assert known.status == PredictionStatus.KNOWN
    assert known.mean_w == pytest.approx(40.0)
    unknown = model.predict(level=PowerLevelV3.ACTIVE, attributes={"position": 999})
    assert unknown.status == PredictionStatus.UNKNOWN
    assert unknown.mean_w is None
    missing = model.predict(level=PowerLevelV3.ACTIVE, attributes={})
    assert missing.status == PredictionStatus.UNKNOWN
    assert missing.mean_w is None


def test_standby_never_zero_when_absent() -> None:
    model = AdjustableModel().fit(
        [_obs(10, 20), _obs(20, 40)],
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
    )
    assert model.spec.standby.median_w is None
    pred = model.predict(level=PowerLevelV3.STANDBY, attributes={"position": 10})
    assert pred.status == PredictionStatus.UNKNOWN
    assert pred.mean_w is None


def test_declared_prior_not_overwritten_and_discrepancy_diagnosed() -> None:
    prior = LevelStat(median_w=500.0, mad_w=1.0, n=10)
    model = AdjustableModel().fit(
        [_obs(10, 20), _obs(20, 40)],
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
        declared_priors={PowerLevelV3.STANDBY: prior},
    )
    assert model.spec.standby.median_w == 500.0
    assert model.diagnostics()


def test_spec_round_trip_is_lossless() -> None:
    model = AdjustableModel().fit(
        [_obs(10, 20), _obs(20, 40)],
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
    )
    decoded = decode_spec(encode_spec(model.spec))
    assert isinstance(decoded, AdjustableSpec)
    assert decoded == model.spec
    assert AdjustableModel(decoded, control_variable="position").predict(
        level=PowerLevelV3.ACTIVE, attributes={"position": 20}
    ) == model.predict(level=PowerLevelV3.ACTIVE, attributes={"position": 20})


def test_build_version_uses_adjustable_shape() -> None:
    model = AdjustableModel().fit(
        [_obs(10, 20), _obs(20, 40)],
        control_variable="position",
        load_shape=LoadShape.ADJUSTABLE,
    )
    version = model.build_version(subject_id="damper", number=1, trainer_version="L2-test")
    assert version.model_class == ModelClass.ADJUSTABLE
    assert version.subject_id == "damper"
