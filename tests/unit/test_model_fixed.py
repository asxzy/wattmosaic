"""L2: FixedPowerModel fitted on device-attributed delta power (analysis F2).

What is pinned:

* level stats are median + 1.4826·MAD with counts for standby / idle /
  active / transient, and there is no idle/standby collapse;
* standby is learned from ΔP chains plus a residual-baseline step when the
  endpoint is shared, and from an ABSOLUTE_ANCHOR level when available;
* OFF never means 0 W: after an OFF transition the prediction is the learned
  non-zero standby, at ``PowerLevelV3.STANDBY``;
* one inrush outlier moves the MAD-based variability by < 10 %;
* an unknown level returns ``None`` / ``UNKNOWN``;
* declared priors seed the spec as a prior and are never overwritten; a
  discrepancy beyond 3σ is surfaced as a diagnostic;
* the model class comes from the declared ``LoadShape`` (default FIXED);
  learning may propose a change but never switches silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    EvidenceRef,
    FixedSpec,
    LearningObservation,
    LevelStat,
    LoadShape,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
    PredictionStatus,
)
from wattmosaic_engine.models.fixed import FixedPowerModel
from wattmosaic_engine.models.spec import decode_spec, encode_spec

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _obs(
    subject: str = "dev",
    *,
    transition: str = "on",
    delta_w: float | None = 45.0,
    level_w: float | None = None,
    basis: ModelBasis = ModelBasis.DELTA,
    attrs: dict | None = None,
    seq: int = 0,
) -> LearningObservation:
    return LearningObservation(
        observation_id=f"obs-{subject}-{seq}",
        subject_kind="device",
        subject_id=subject,
        endpoint_id="ct_shared",
        event_group_ref=EvidenceRef(kind="event_group", id=f"grp-{seq}"),
        transition=transition,
        attributes=attrs or {},
        delta_w=delta_w,
        level_w=level_w,
        status="confirmed",
        identifiability="identifiable",
        basis=basis,
        created_at=T0,
    )


def test_default_model_class_is_fixed_and_undeclared_stays_fixed() -> None:
    assert FixedPowerModel.model_class == ModelClass.FIXED
    assert model_class_for_load_shape(LoadShape.FIXED) == ModelClass.FIXED
    assert model_class_for_load_shape(LoadShape.UNKNOWN) == ModelClass.FIXED


def test_model_class_never_switches_silently_from_keywords() -> None:
    # DEC-34: the declared shape wins; a keyword-named device is still FIXED
    # unless the declaration says otherwise.  There is no infer_model_type in
    # the v3 path.
    assert model_class_for_load_shape(LoadShape.FIXED, device_type="dishwasher") == ModelClass.FIXED
    assert model_class_for_load_shape(LoadShape.ADJUSTABLE) == ModelClass.ADJUSTABLE
    assert suggest_model_class(LoadShape.FIXED, observed_nonlinear=True) == ModelClass.ADJUSTABLE
    # A suggestion is reported, never applied to the active spec.
    model = FixedPowerModel().fit([_obs()], load_shape=LoadShape.FIXED)
    assert model.spec.kind == "fixed"
    assert model.suggested_class is None


def test_shared_circuit_standby_from_delta_chain_and_residual_step() -> None:
    # OFF→STANDBY step (-45) and a residual-baseline step of +7 give a 7 W
    # standby.  The old circuit was 52 W, the new one 7 W.
    model = FixedPowerModel().fit(
        [
            _obs(transition="on", delta_w=45.0),
            _obs(transition="off", delta_w=-45.0),
            _obs(transition="residual_baseline", delta_w=-7.0, attrs={"residual_step_w": 7.0}),
        ],
        load_shape=LoadShape.FIXED,
    )
    pred = model.predict(level=PowerLevelV3.STANDBY)
    assert pred.status == PredictionStatus.KNOWN
    assert pred.mean_w == pytest.approx(7.0)
    assert pred.level == PowerLevelV3.STANDBY
    assert pred.mean_w > 0
    assert model.spec.levels[PowerLevelV3.STANDBY].median_w == pytest.approx(7.0)


def test_off_transition_keeps_nonzero_standby() -> None:
    model = FixedPowerModel().fit(
        [
            _obs(transition="on", delta_w=45.0),
            _obs(transition="off", delta_w=-45.0),
            _obs(transition="residual_baseline", delta_w=-7.0, attrs={"residual_step_w": 7.0}),
        ],
        load_shape=LoadShape.FIXED,
    )
    pred = model.predict(level=PowerLevelV3.STANDBY, after_transition="off")
    assert pred.status == PredictionStatus.KNOWN
    assert pred.mean_w == pytest.approx(7.0)
    assert pred.level == PowerLevelV3.STANDBY


def test_absolute_anchor_level_seeds_standby() -> None:
    model = FixedPowerModel().fit(
        [
            _obs(
                transition="standby_reported",
                delta_w=None,
                level_w=3.0,
                basis=ModelBasis.ABSOLUTE_ANCHOR,
                attrs={"endpoint_kind": "smart_plug", "level": "standby"},
            )
        ],
        load_shape=LoadShape.FIXED,
    )
    pred = model.predict(level=PowerLevelV3.STANDBY)
    assert pred.status == PredictionStatus.KNOWN
    assert pred.mean_w == pytest.approx(3.0)
    assert model.basis == ModelBasis.ABSOLUTE_ANCHOR


def test_mad_variability_resists_inrush_outlier() -> None:
    clean = FixedPowerModel().fit(
        [_obs(transition="on", delta_w=60.0 + (i % 3) * 0.2) for i in range(30)],
        load_shape=LoadShape.FIXED,
    )
    spiky = FixedPowerModel().fit(
        [_obs(transition="on", delta_w=60.0 + (i % 3) * 0.2) for i in range(30)]
        + [_obs(transition="on", delta_w=600.0, seq=999)],
        load_shape=LoadShape.FIXED,
    )
    base_sigma = clean.spec.levels[PowerLevelV3.ACTIVE].mad_w
    spike_sigma = spiky.spec.levels[PowerLevelV3.ACTIVE].mad_w
    assert base_sigma is not None and spike_sigma is not None
    assert abs(spike_sigma - base_sigma) <= 0.10 * base_sigma


def test_unknown_level_returns_none_not_zero() -> None:
    model = FixedPowerModel().fit([_obs(transition="on", delta_w=45.0)], load_shape=LoadShape.FIXED)
    pred = model.predict(level=PowerLevelV3.TRANSIENT)
    assert pred.status == PredictionStatus.UNKNOWN
    assert pred.mean_w is None
    assert pred.sigma_w is None


def test_declared_prior_seeds_spec_and_is_never_overwritten() -> None:
    # A declared 50 W with a 5 W uncertainty is consistent with a 45 W learned
    # delta, so it seeds the spec and produces no diagnostic.
    prior = LevelStat(median_w=50.0, mad_w=5.0, n=10)
    model = FixedPowerModel().fit(
        [_obs(transition="on", delta_w=45.0)],
        load_shape=LoadShape.FIXED,
        declared_priors={PowerLevelV3.ACTIVE: prior},
    )
    assert model.spec.levels[PowerLevelV3.ACTIVE].median_w == 50.0
    assert model.spec.levels[PowerLevelV3.ACTIVE].n == 10
    assert model.learned_levels[PowerLevelV3.ACTIVE].median_w == pytest.approx(45.0)
    assert not model.diagnostics()


def test_gross_discrepancy_is_surfaced_as_diagnostic() -> None:
    prior = LevelStat(median_w=500.0, mad_w=1.0, n=10)
    model = FixedPowerModel().fit(
        [_obs(transition="on", delta_w=45.0)],
        load_shape=LoadShape.FIXED,
        declared_priors={PowerLevelV3.ACTIVE: prior},
    )
    diags = model.diagnostics()
    assert any("discrepancy" in d or "3sigma" in d for d in diags)


def test_spec_round_trip_is_lossless() -> None:
    model = FixedPowerModel().fit(
        [
            _obs(transition="on", delta_w=45.0),
            _obs(transition="off", delta_w=-45.0),
        ],
        load_shape=LoadShape.FIXED,
    )
    decoded = decode_spec(encode_spec(model.spec))
    assert isinstance(decoded, FixedSpec)
    assert decoded == model.spec
    assert FixedPowerModel(decoded).predict(level=PowerLevelV3.ACTIVE) == model.predict(
        level=PowerLevelV3.ACTIVE
    )


def test_build_version_uses_declared_shape_and_delta_basis() -> None:
    model = FixedPowerModel().fit(
        [_obs(transition="on", delta_w=45.0)],
        load_shape=LoadShape.FIXED,
    )
    version = model.build_version(subject_id="dev", number=1, trainer_version="L2-test")
    assert isinstance(version, PowerModelVersion)
    assert version.model_class == ModelClass.FIXED
    assert version.basis == ModelBasis.DELTA
    assert version.subject_id == "dev"


# Imported late to keep the test module importable while discussing the API.
from wattmosaic_engine.models.fixed import (  # noqa: E402
    model_class_for_load_shape,
    suggest_model_class,
)
