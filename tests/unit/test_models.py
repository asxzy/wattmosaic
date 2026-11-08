"""Unit tests for the device-model framework.

What is proven here:

* the numeric helpers (PAVA monotonicity, MAE/RMSE/R²/MAPE) are mathematically
  correct against hand-computed values,
* every concrete model learns analytically known synthetic data and predicts
  the known answer back (binary ON/OFF, dimmer curve, fan curve, state map,
  composite sum, residual time bins, appliance stages),
* segmentation finds real step changes, refuses to invent stages in noise, and
  labels level status honestly,
* persisted model versions round-trip through ``serde`` and the database, the
  registry caches by version, invalidates correctly and honours freeze flags.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

import pytest
from wattmosaic_engine.contracts import (
    DeviceModelParameters,
    DeviceModelVersion,
    ModelStatus,
    ModelType,
    OperatingStage,
    StageStatus,
)
from wattmosaic_engine.models import (
    BinaryDeviceModel,
    CompositeDeviceModel,
    DimmableDeviceModel,
    MultiStateDeviceModel,
    ResidualDeviceModel,
    TimeVaryingApplianceModel,
    VariableSpeedDeviceModel,
    build_model,
    context_from_sample,
    enforce_monotonic,
    error_metrics,
    model_class_for,
    segment_power_series,
    trapezoidal_energy_kwh,
)
from wattmosaic_engine.models.registry import ModelRegistry
from wattmosaic_engine.models.serde import parameters_from_json, version_from_row
from wattmosaic_engine.storage import Database

from tests.unit.conftest import BASE_TIME, binary_samples, make_sample, series


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _dimmer_samples() -> list:
    points = [(10, 80), (50, 40), (100, 90), (200, 120), (255, 150)]
    return [
        make_sample(
            "light",
            i,
            baseline=0.0,
            event=power,
            control_value=brightness,
            control_variable="brightness",
            state="on",
        )
        for i, (brightness, power) in enumerate(points)
    ]


def _binary_power_model(on_power: float = 100.0) -> BinaryDeviceModel:
    samples = [
        make_sample("dev", i, baseline=0.0, event=on_power, state="on")
        if i % 2 == 0
        else make_sample("dev", i, baseline=on_power, event=0.0, state="off")
        for i in range(12)
    ]
    model = BinaryDeviceModel()
    model.fit(samples)
    return model


def _active_version(
    device_id: str,
    model: BinaryDeviceModel,
    *,
    version: int = 1,
    metrics: dict[str, float] | None = None,
) -> DeviceModelVersion:
    return DeviceModelVersion(
        device_id=device_id,
        model_type=ModelType.BINARY,
        version=version,
        status=ModelStatus.ACTIVE,
        created_at=BASE_TIME,
        sample_count=12,
        validation_metrics=metrics or {"r2": 0.9, "mae": 1.0},
        parameters=model.parameters.model_copy(deep=True),
        confidence=0.9,
    )


# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #


def test_error_metrics_match_hand_computed_values() -> None:
    metrics = error_metrics([10.0, 20.0, 30.0], [12.0, 17.0, 33.0])

    assert metrics["n"] == pytest.approx(3.0)
    assert metrics["mae"] == pytest.approx(8.0 / 3.0)
    assert metrics["rmse"] == pytest.approx((22.0 / 3.0) ** 0.5)
    # SS_tot = 200, SS_res = 22 -> R² = 1 - 22/200
    assert metrics["r2"] == pytest.approx(0.89)
    assert metrics["mape"] == pytest.approx(15.0)
    assert metrics["max_error"] == pytest.approx(3.0)


def test_error_metrics_perfect_and_empty_predictions() -> None:
    perfect = error_metrics([5.0, 5.0], [5.0, 5.0])
    assert perfect["mae"] == 0.0
    assert perfect["rmse"] == 0.0
    assert perfect["r2"] == 1.0

    empty = error_metrics([], [])
    assert empty["n"] == 0.0
    assert empty["mae"] == 0.0


def test_enforce_monotonic_produces_non_decreasing_curve() -> None:
    curve = enforce_monotonic([(10, 80), (50, 40), (100, 90)])

    assert [x for x, _ in curve] == [10.0, 50.0, 100.0]
    watts = [y for _, y in curve]
    assert watts == sorted(watts)
    # PAVA pools the violating first two points to their mean, leaves 90 alone.
    assert watts == pytest.approx([60.0, 60.0, 90.0])

    unsorted = enforce_monotonic([(5, 100), (1, 100), (3, 50)])
    assert [x for x, _ in unsorted] == [1.0, 3.0, 5.0]
    values = [y for _, y in unsorted]
    assert values == sorted(values)
    assert values[-1] == pytest.approx(100.0)

    assert enforce_monotonic([]) == []


def test_context_from_sample_parses_state_control_and_notes() -> None:
    sample = make_sample(
        "light",
        0,
        baseline=10.0,
        event=110.0,
        control_value=128.0,
        control_variable="brightness",
        state="on",
        notes=["hour:7", "component:main"],
    )
    context = context_from_sample(sample)

    assert context["device_id"] == "light"
    assert context["state"] == "on"
    assert context["attributes"]["brightness"] == pytest.approx(128.0)
    assert context["hour"] == 7
    assert context["component"] == "main"


def test_context_from_sample_infers_state_from_delta_sign() -> None:
    on_context = context_from_sample(make_sample("dev", 0, baseline=0.0, event=60.0, state=None))
    off_context = context_from_sample(make_sample("dev", 1, baseline=60.0, event=0.0, state=None))

    assert on_context["state"] == "on"
    assert off_context["state"] == "off"


def test_build_model_and_model_class_for_cover_every_model_type() -> None:
    expected = {
        ModelType.BINARY: BinaryDeviceModel,
        ModelType.DIMMABLE: DimmableDeviceModel,
        ModelType.VARIABLE_SPEED: VariableSpeedDeviceModel,
        ModelType.MULTI_STATE: MultiStateDeviceModel,
        ModelType.COMPOSITE: CompositeDeviceModel,
        ModelType.TIME_VARYING: TimeVaryingApplianceModel,
        ModelType.RESIDUAL: ResidualDeviceModel,
    }
    for model_type, cls in expected.items():
        assert model_class_for(model_type) is cls
        built = build_model(model_type)
        assert isinstance(built, cls)
        assert built.model_type == model_type

    with pytest.raises(ValueError, match="unknown model type"):
        model_class_for("not_a_model")


# --------------------------------------------------------------------------- #
# Concrete models
# --------------------------------------------------------------------------- #


def test_binary_model_learns_on_and_off_power() -> None:
    samples = [
        make_sample("heater", i, baseline=0.0, event=1500.0, state="on")
        if i % 2 == 0
        else make_sample("heater", i, baseline=1500.0, event=0.0, state="off")
        for i in range(20)
    ]
    model = BinaryDeviceModel()
    params = model.fit(samples)

    assert params.on_power_w == pytest.approx(1500.0)
    assert params.off_power_w == pytest.approx(0.0)
    assert model.predict({"state": "on"}) == pytest.approx(1500.0)
    assert model.predict({"state": "off"}) == pytest.approx(0.0)
    assert model.predict_detailed({"state": "on"})[1:] == (None, StageStatus.UNKNOWN)

    metrics = model.validate(samples)
    assert metrics["mae"] == pytest.approx(0.0)
    assert metrics["r2"] == pytest.approx(1.0)


def test_binary_model_median_ignores_inrush_outlier() -> None:
    samples = binary_samples(count=20, on_power=1500.0)
    samples.append(make_sample("lamp", 99, baseline=0.0, event=9000.0, state="on"))
    model = BinaryDeviceModel()
    model.fit(samples)

    # One 9 kW inrush sample must not drag the steady-state estimate.
    assert model.predict({"state": "on"}) == pytest.approx(1500.0)


def test_binary_confidence_drops_without_state_evidence() -> None:
    model = _binary_power_model()
    with_state = model.confidence({"state": "on"})
    without_state = model.confidence({})

    assert 0.0 < without_state < with_state <= 1.0


def test_dimmable_model_enforces_monotonicity_and_interpolates() -> None:
    model = DimmableDeviceModel()
    params = model.fit(_dimmer_samples())

    watts = [y for _, y in params.curve]
    assert watts == sorted(watts)
    # The 10->80 dip is pooled below the 50->40 observation.
    assert watts[0] == pytest.approx(60.0)

    assert model.predict({"state": "on", "attributes": {"brightness": 50}}) == pytest.approx(60.0)
    assert model.predict({"state": "on", "attributes": {"brightness": 100}}) == pytest.approx(90.0)
    assert model.predict({"state": "on", "attributes": {"brightness": 150}}) == pytest.approx(105.0)
    # Above the learned range it clamps instead of extrapolating.
    assert model.predict({"state": "on", "attributes": {"brightness": 400}}) == pytest.approx(150.0)


def test_dimmable_model_converts_percentage_to_trained_brightness_scale() -> None:
    samples = [
        make_sample(
            "light",
            i,
            baseline=0.0,
            event=power,
            control_value=level,
            control_variable="brightness",
            state="on",
        )
        for i, (level, power) in enumerate([(0, 0), (51, 30), (102, 60), (204, 120), (255, 150)])
    ]
    model = DimmableDeviceModel()
    model.fit(samples)

    # 50 % -> 127.5 on the 0-255 scale -> halfway between 102 W=60 and 204 W=120.
    assert model.predict({"state": "on", "attributes": {"brightness_pct": 50}}) == pytest.approx(75.0)
    assert model.predict({"state": "on", "attributes": {"brightness": 255}}) == pytest.approx(150.0)


def test_dimmable_off_state_uses_learned_off_power() -> None:
    samples = _dimmer_samples()
    samples.append(
        make_sample("light", 99, baseline=150.0, event=3.0, delta=-147.0, state="off")
    )
    model = DimmableDeviceModel()
    model.fit(samples)

    assert model.predict({"state": "off", "attributes": {"brightness": 0}}) == pytest.approx(3.0)


def test_variable_speed_model_learns_curve_and_names_stages() -> None:
    samples = [
        make_sample(
            "fan",
            i,
            baseline=0.0,
            event=power,
            control_value=speed,
            control_variable="speed",
            state="on",
        )
        for i, (speed, power) in enumerate([(20, 25), (40, 100), (60, 225), (80, 400), (100, 625)])
    ]
    model = VariableSpeedDeviceModel()
    params = model.fit(samples)

    assert [y for _, y in params.curve] == pytest.approx([25.0, 100.0, 225.0, 400.0, 625.0])
    assert model.predict({"state": "on", "attributes": {"speed": 50}}) == pytest.approx(162.5)
    # RPM is normalised to percent of a 2000 rpm motor: 1000 rpm == 50 %.
    assert model.predict({"state": "on", "attributes": {"rpm": 1000}}) == pytest.approx(162.5)

    power, stage, status = model.predict_detailed({"state": "on", "attributes": {"speed": 60}})
    assert power == pytest.approx(225.0)
    assert stage == "medium"
    assert status == StageStatus.IDENTIFIED


def test_variable_speed_exponent_recovers_quadratic_law() -> None:
    samples = [
        make_sample(
            "fan",
            i,
            baseline=0.0,
            event=(speed**2) / 16.0,
            control_value=speed,
            control_variable="speed",
            state="on",
        )
        for i, speed in enumerate((20, 40, 60, 80, 100))
    ]
    model = VariableSpeedDeviceModel()
    model.fit(samples)

    assert model.power_law_exponent == pytest.approx(2.0, abs=0.05)


def test_multi_state_model_maps_each_state_and_does_not_guess_unknown() -> None:
    samples = [
        make_sample("hvac", i, baseline=0.0, event=power, state=state)
        for i, (state, power) in enumerate(
            [("heat", 1500), ("cool", 1200), ("fan_only", 300), ("off", 5)] * 2
        )
    ]
    model = MultiStateDeviceModel()
    params = model.fit(samples)

    assert params.state_map == pytest.approx(
        {"heat": 1500.0, "cool": 1200.0, "fan_only": 300.0, "off": 5.0}
    )
    assert model.predict({"state": "heat"}) == pytest.approx(1500.0)
    assert model.predict({"state": "cool"}) == pytest.approx(1200.0)

    # An unseen mode falls back to OFF power and reports UNKNOWN, never 1.5 kW.
    power, stage, status = model.predict_detailed({"state": "dry"})
    assert power == pytest.approx(5.0)
    assert stage is None
    assert status == StageStatus.UNKNOWN
    assert model.confidence({"state": "dry"}) < model.confidence({"state": "heat"})


def test_composite_model_sums_and_isolates_components() -> None:
    samples = []
    for i in range(6):
        samples.append(
            make_sample("bed", i, baseline=0.0, event=60.0, state="on", notes=["component:fan"])
        )
        samples.append(
            make_sample("bed", i, baseline=0.0, event=120.0, state="on", notes=["component:heat"])
        )
    model = CompositeDeviceModel()
    model.fit(samples)

    assert model.predict({"state": "on"}) == pytest.approx(180.0)
    assert model.predict({"state": "on", "component": "fan"}) == pytest.approx(60.0)
    assert model.predict(
        {"components": {"fan": {"state": "on"}, "heat": {"state": "on"}}}
    ) == pytest.approx(180.0)
    assert model.predict(
        {"components": {"fan": {"state": "off"}, "heat": {"state": "on"}}}
    ) == pytest.approx(120.0)


def test_residual_model_uses_time_bins_and_caps_confidence() -> None:
    samples = []
    base_hour = 0
    for hour in range(24):
        power = 100.0 if hour < 6 else 90.0
        samples.append(
            make_sample(
                "fridge",
                hour,
                baseline=0.0,
                event=power,
                state="on",
                captured_at=BASE_TIME.replace(hour=base_hour, minute=0) + timedelta(hours=hour),
            )
        )
    model = ResidualDeviceModel()
    model.fit(samples)

    assert model.predict({"hour": 1}) == pytest.approx(100.0)
    assert model.predict({"hour": 10}) == pytest.approx(90.0)
    # An explicit OFF state is real evidence and must win over the time prior.
    assert model.predict({"state": "off", "hour": 1}) == 0.0
    assert 0.0 < model.confidence() <= ResidualDeviceModel.max_confidence
    assert model.uncertainty_w() >= 0.0

    # The model is deliberately humble: explanations carry the "no state" marker
    # and describe() exposes the uncertainty cap.
    assert "residual_model_no_state" in model.explanation({"hour": 1})
    description = model.describe()
    assert description["max_confidence"] == ResidualDeviceModel.max_confidence
    # Only bin 0 (00:00-04:00) is both populated enough and meaningfully above
    # the global median; the 90 W bins are correctly discarded as uninformative.
    assert set(description["bins"]) == {"0"}

    baseline = model.baseline_from_series(series([(60, 10.0)]))
    assert baseline["baseline_median_w"] == pytest.approx(10.0)
    assert baseline["peak_w"] == pytest.approx(10.0)
    assert baseline["segments"] == 1.0


def test_dimmable_model_rejects_non_control_samples_and_reports_monotonicity() -> None:
    # A switch-only sample for a dimmable device has no brightness and a negative
    # delta, so it teaches the OFF level rather than a curve point.
    samples = _dimmer_samples() + [
        make_sample("light", 90, baseline=150.0, event=2.0, delta=-148.0, state="off")
    ]
    model = DimmableDeviceModel()
    model.fit(samples)

    assert model.notes["curve_points"] == 5
    assert model.notes["monotonic"] is True
    assert model.parameters.off_power_w == pytest.approx(2.0)
    # Unreachable brightness values clamp to the learned endpoints.
    assert model.predict({"state": "on", "attributes": {"brightness_pct": 0}}) == pytest.approx(60.0)
    assert model.predict({"state": "on", "attributes": {"brightness_pct": 100}}) == pytest.approx(150.0)

    # Stages mirror the curve points, and a known brightness reports one.
    assert [stage.power_mean_w for stage in model.parameters.stages] == pytest.approx(
        [60.0, 60.0, 90.0, 120.0, 150.0]
    )
    power, stage, status = model.predict_detailed({"state": "on", "attributes": {"brightness": 102}})
    # 102 sits 2/100 of the way from (100 W -> 90 W) to (200 W -> 120 W).
    assert power == pytest.approx(90.0 + (120.0 - 90.0) * 2 / 100)
    assert stage == "level_100"
    assert status == StageStatus.IDENTIFIED
    assert power == pytest.approx(model.predict({"state": "on", "attributes": {"brightness": 102}}))

    # "brightness_curve_interpolation" appears only when a control value exists.
    assert any("brightness" in line for line in model.explanation({"attributes": {"brightness": 50}}))
    assert "brightness_missing_fallback_mean" in model.explanation({"state": "on"})
    assert model.confidence({"state": "on"}) < model.confidence(
        {"state": "on", "attributes": {"brightness": 102}}
    )


def test_time_varying_model_fits_stage_profile_and_cycle_energy() -> None:
    power_series = series([(20, 100.0), (20, 600.0), (20, 1500.0)])
    model = TimeVaryingApplianceModel()
    params = model.fit_series(power_series)

    assert [stage.power_mean_w for stage in params.stages] == pytest.approx([100.0, 600.0, 1500.0])
    assert [stage.name for stage in params.stages] == ["standby", "medium", "peak"]
    assert all(stage.status == StageStatus.IDENTIFIED for stage in params.stages)

    power, stage, status = model.predict_detailed({"power_series": power_series})
    assert power == pytest.approx(1500.0)
    assert stage == "peak"
    assert status == StageStatus.IDENTIFIED

    summary = model.cycle_summary(power_series)
    assert summary.energy_kwh == pytest.approx(trapezoidal_energy_kwh(power_series))
    assert summary.peak_power_w == pytest.approx(1500.0)
    assert summary.runtime_s == pytest.approx(59 * 15.7)


def test_time_varying_model_reports_unknown_when_stages_are_indistinguishable() -> None:
    rng = random.Random(20261105)
    noisy = [(at, 500.0 + rng.gauss(0.0, 20.0)) for at, _ in series([(100, 0.0)])]
    model = TimeVaryingApplianceModel()
    params = model.fit_series(noisy)

    assert params.stages
    assert all(stage.status == StageStatus.UNKNOWN for stage in params.stages)

    power, stage, status = model.predict_detailed({"power_series": noisy})
    assert stage == "unclassified"
    assert status == StageStatus.UNKNOWN
    assert model.notes["segmentation_ambiguous"] is True


def test_time_varying_model_clusters_sample_levels_into_stages() -> None:
    # Samples (not a series) carry one stable level each; 12 samples give four
    # observations of each of three levels, so every stage can be IDENTIFIED.
    levels = [100.0] * 4 + [600.0] * 4 + [1500.0] * 4
    samples = [
        make_sample("washer", i, baseline=0.0, event=level, state="running", minutes=10)
        for i, level in enumerate(levels)
    ]
    model = TimeVaryingApplianceModel()
    params = model.fit(samples)

    assert [stage.power_mean_w for stage in params.stages] == pytest.approx([100.0, 600.0, 1500.0])
    assert [stage.name for stage in params.stages] == ["standby", "medium", "peak"]
    assert all(stage.status == StageStatus.IDENTIFIED for stage in params.stages)
    assert all(stage.notes == ["samples:4"] for stage in params.stages)
    assert [stage.min_duration_s for stage in params.stages] == pytest.approx([60.0, 60.0, 60.0])

    assert params.on_power_w == pytest.approx(600.0)  # median stage
    assert params.off_power_w == pytest.approx(100.0)  # lowest stage sits at the floor
    assert model.notes["stage_count"] == 3
    assert model.notes["sample_count"] == 12

    # A single observation per level cannot corroborate the level: PROBABLE only.
    sparse = TimeVaryingApplianceModel().fit(
        [
            make_sample("dryer", i, baseline=0.0, event=level, state="running")
            for i, level in enumerate([50.0, 400.0, 2000.0])
        ]
    )
    assert all(stage.status == StageStatus.PROBABLE for stage in sparse.stages)


def test_time_varying_model_predicts_without_a_series_from_learned_stages() -> None:
    samples = [
        make_sample("washer", i, baseline=0.0, event=level, state="running", minutes=10)
        for i, level in enumerate([100.0] * 4 + [600.0] * 4 + [1500.0] * 4)
    ]
    model = TimeVaryingApplianceModel()
    model.fit(samples)

    # No series to observe: OFF uses the learned floor, RUNNING the median stage,
    # and any other state the lowest learned stage.
    assert model.predict({"state": "off"}) == pytest.approx(100.0)
    assert model.predict({"state": "running"}) == pytest.approx(600.0)
    assert model.predict({"state": "unknown"}) == pytest.approx(100.0)
    assert model.predict({}) == pytest.approx(100.0)

    power, stage, status = model.predict_detailed({"state": "off"})
    assert power == pytest.approx(100.0)
    assert stage == "standby"
    assert status == StageStatus.UNKNOWN  # a state alone proves no stage

    # Confidence is capped hard when a stage is not observed.
    assert model.confidence({"state": "on"}) < model.confidence(
        {"power_series": series([(20, 100.0), (20, 600.0), (20, 1500.0)])}
    )


def test_time_varying_model_cycle_summary_reports_stage_names_and_status() -> None:
    model = TimeVaryingApplianceModel()
    model.fit_series(series([(20, 100.0), (20, 600.0), (20, 1500.0)]))
    summary = model.cycle_summary(series([(20, 100.0), (20, 600.0), (20, 1500.0)]))

    assert [stage.name for stage in summary.stages] == ["standby", "medium", "peak"]
    assert all(stage.status == StageStatus.IDENTIFIED for stage in summary.stages)
    assert summary.stage_status == StageStatus.IDENTIFIED
    assert summary.mean_power_w == pytest.approx(
        summary.energy_kwh * 3_600_000.0 / summary.runtime_s
    )
    assert summary.peak_power_w >= summary.mean_power_w

    empty = model.cycle_summary([])
    assert empty.energy_kwh == 0.0
    assert empty.runtime_s == 0.0
    assert empty.notes == ["no_samples"]


# --------------------------------------------------------------------------- #
# Segmentation
# --------------------------------------------------------------------------- #


def test_segment_power_series_finds_each_step_boundary() -> None:
    result = segment_power_series(series([(20, 100.0), (20, 600.0), (20, 1500.0)]))

    assert len(result.segments) == 3
    assert [segment.power_w for segment in result.segments] == pytest.approx([100.0, 600.0, 1500.0])
    assert result.ambiguous is False
    assert len(result.change_points) == 2
    assert result.distinct_levels() == 3


def test_segment_power_series_keeps_constant_and_flags_noisy_envelope() -> None:
    constant = segment_power_series(series([(60, 250.0)]))
    assert len(constant.segments) == 1
    assert constant.ambiguous is False
    assert constant.segments[0].power_w == pytest.approx(250.0)

    rng = random.Random(20261105)
    noisy = [(at, 250.0 + rng.gauss(0.0, 20.0)) for at, _ in series([(100, 0.0)])]
    result = segment_power_series(noisy)
    assert len(result.segments) == 1
    assert result.ambiguous is True
    assert any("unresolved_levels" in note for note in result.notes)


# --------------------------------------------------------------------------- #
# Persistence: serde + registry
# --------------------------------------------------------------------------- #


def test_parameters_and_version_round_trip_through_json() -> None:
    parameters = DeviceModelParameters(
        on_power_w=1500.0,
        off_power_w=4.0,
        standby_power_w=6.0,
        curve=[(0.0, 0.0), (50.0, 300.0), (100.0, 1500.0)],
        state_map={"heat": 1500.0, "off": 4.0},
        stages=[
            OperatingStage(
                name="high",
                status=StageStatus.IDENTIFIED,
                power_mean_w=1500.0,
                power_std_w=12.0,
                min_duration_s=60.0,
                max_duration_s=600.0,
                notes=["samples:5"],
            )
        ],
        residual_std_w=3.0,
    )
    restored = parameters_from_json(json.dumps(parameters.model_dump(mode="json")))

    assert restored is not None
    assert restored.on_power_w == pytest.approx(1500.0)
    assert restored.off_power_w == pytest.approx(4.0)
    assert [list(point) for point in restored.curve] == [
        [0.0, 0.0],
        [50.0, 300.0],
        [100.0, 1500.0],
    ]
    assert restored.state_map == pytest.approx({"heat": 1500.0, "off": 4.0})
    assert restored.stages[0].name == "high"
    assert restored.stages[0].status == StageStatus.IDENTIFIED
    assert restored.residual_std_w == pytest.approx(3.0)

    row = {
        "device_id": "heater",
        "model_type": "binary",
        "version": 3,
        "status": "active",
        "created_at": BASE_TIME.isoformat(),
        "trained_from": BASE_TIME.isoformat(),
        "trained_to": BASE_TIME.isoformat(),
        "sample_count": 42,
        "training_quality": 0.8,
        "validation_metrics_json": json.dumps({"r2": 0.93}),
        "parameters_json": json.dumps(parameters.model_dump(mode="json")),
        "confidence": 0.9,
        "notes_json": json.dumps(["trained_on:30"]),
    }
    version = version_from_row(row)
    assert version is not None
    assert version.model_type == ModelType.BINARY
    assert version.version == 3
    assert version.status == ModelStatus.ACTIVE
    assert version.sample_count == 42
    assert version.validation_metrics["r2"] == pytest.approx(0.93)
    assert version.parameters.on_power_w == pytest.approx(1500.0)


def test_serde_rejects_corrupt_json_instead_of_raising() -> None:
    assert parameters_from_json("{not json") is None
    assert parameters_from_json("[]") is None
    assert version_from_row({"model_type": "binary", "version": 1, "parameters_json": "{bad"}) is None
    assert version_from_row({"model_type": "nonsense", "version": 1}) is None
    assert version_from_row({"model_type": "binary", "version": "not-a-number"}) is None
    # An unknown status degrades to PENDING instead of discarding the model.
    degraded = version_from_row(
        {
            "device_id": "heater",
            "model_type": "binary",
            "version": 4,
            "status": "not_a_status",
            "parameters_json": json.dumps({"on_power_w": 10.0}),
        }
    )
    assert degraded is not None
    assert degraded.status == ModelStatus.PENDING
    assert degraded.notes == []
    assert degraded.sample_count == 0


def test_registry_returns_none_without_active_model(db: Database) -> None:
    registry = ModelRegistry(db)

    assert registry.estimate("missing", {"state": "on"}) is None
    assert registry.model_for("missing") is None
    assert registry.active("missing") is None
    assert registry.status("missing")["has_active_model"] is False


def test_registry_round_trips_persisted_model_and_estimates(db: Database) -> None:
    model = _binary_power_model(1500.0)
    db.save_model_version(_active_version("heater", model), activate=True)
    registry = ModelRegistry(db)

    estimate = registry.estimate("heater", {"state": "on"})
    assert estimate is not None
    assert estimate.power_w == pytest.approx(1500.0)
    assert estimate.confidence == pytest.approx(0.9)
    assert estimate.model_type == ModelType.BINARY
    assert estimate.model_version == 1
    assert estimate.model_id == "heater:binary"

    assert registry.model_for("heater").predict({"state": "off"}) == pytest.approx(0.0)
    assert registry.status("heater")["has_active_model"] is True


def test_registry_caches_by_version_and_invalidates(db: Database) -> None:
    model = _binary_power_model(100.0)
    version = _active_version("heater", model)
    db.save_model_version(version, activate=True)
    registry = ModelRegistry(db)

    first = registry.model_for("heater")
    second = registry.model_for("heater")
    assert first is second
    assert first.predict({"state": "on"}) == pytest.approx(100.0)

    # Rewrite the same version with new parameters: the version-keyed cache must
    # keep serving the parsed object until an explicit invalidation.
    updated = version.model_copy(deep=True)
    updated.parameters = DeviceModelParameters(on_power_w=250.0, off_power_w=0.0)
    db.save_model_version(updated, activate=True)
    assert registry.model_for("heater") is first

    registry.invalidate("heater")
    refreshed = registry.model_for("heater")
    assert refreshed is not first
    assert refreshed.predict({"state": "on"}) == pytest.approx(250.0)


def test_registry_reports_and_survives_frozen_and_corrupt_models(db: Database) -> None:
    model = _binary_power_model(100.0)
    db.save_model_version(_active_version("heater", model), activate=True)
    registry = ModelRegistry(db)

    db.set_model_frozen("heater", True)
    assert registry.is_frozen("heater") is True
    estimate = registry.estimate("heater", {"state": "on"})
    assert estimate is not None
    assert estimate.frozen is True
    assert estimate.power_w == pytest.approx(100.0)  # freezing blocks promotion, not inference

    # Corrupt persisted parameters are ignored, never raised into the caller.
    with db._lock:  # noqa: SLF001 - test-only corruption of the stored blob
        db._conn.execute(
            "UPDATE device_model_versions SET parameters_json=? WHERE device_id=?",
            ("{corrupt", "heater"),
        )
    registry.invalidate("heater")
    assert registry.model_for("heater") is None
    assert registry.estimate("heater", {"state": "on"}) is None


def test_registry_cache_evicts_least_recently_used_and_handles_null_predictions(
    db: Database,
) -> None:
    registry = ModelRegistry(db, cache_size=8)
    for index in range(10):
        model = _binary_power_model(100.0 + index)
        db.save_model_version(
            _active_version(f"device{index}", model, metrics={"r2": 0.9, "mae": 1.0}),
            activate=True,
        )
        assert registry.model_for(f"device{index}") is not None

    assert len(registry._cache) == 8  # noqa: SLF001 - bounded cache is the point
    assert ("device0", "binary", 1, ModelStatus.ACTIVE.value) not in registry._cache  # noqa: SLF001
    assert ("device1", "binary", 1, ModelStatus.ACTIVE.value) not in registry._cache  # noqa: SLF001
    assert ("device9", "binary", 1, ModelStatus.ACTIVE.value) in registry._cache  # noqa: SLF001
    # A nil ON power is a legitimate (if useless) estimate, not a failure.
    nil = _binary_power_model(0.0)
    db.save_model_version(_active_version("nildevice", nil), activate=True)
    estimate = registry.estimate("nildevice", {"state": "on"})
    assert estimate is not None
    assert estimate.power_w == 0.0
    assert estimate.as_dict()["model_type"] == "binary"
    assert estimate.as_dict()["stage_status"] == "unknown"
