"""Unit tests for the learning pipeline.

What is proven here:

* every :class:`SampleRejection` reason is reachable and is the *first*
  diagnosed fault of a deliberately broken sample,
* the collector derives its averaging window from the measured 15.7 s CT
  cadence instead of assuming a sample rate,
* the trainer refuses thin data, picks the right model class from device
  characteristics, and the trained model beats a constant predictor,
* hold-out splitting is deterministic and disjoint, validation metrics are
  correct, and model comparison respects the promotion margin,
* promotion only happens when the candidate genuinely beats the active model,
  freezing blocks it, rollback restores the previous active version, and
  version numbers are never reused,
* stage recognition labels a real three-stage cycle and refuses to claim
  stages in a flat noisy series,
* the end-to-end pipeline persists rejected samples with their reason and
  trains only on accepted ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    DeviceEvent,
    DeviceModelParameters,
    DeviceModelVersion,
    ModelStatus,
    ModelType,
    SampleRejection,
    StageStatus,
)
from wattmosaic_engine.learning import (
    LearningPipeline,
    LearningThresholds,
    PromotionManager,
    SampleCollector,
    SampleEvidence,
    SampleFilter,
    Trainer,
    compare_models,
    current_stage,
    extract_control,
    infer_model_type,
    recognize_stages,
    rejection_summary,
    split_holdout,
    stage_distinguishable,
    validate_model,
)
from wattmosaic_engine.learning.sample_filter import sample_notes
from wattmosaic_engine.learning.trainer import training_quality
from wattmosaic_engine.models import BinaryDeviceModel
from wattmosaic_engine.models.base import context_from_sample
from wattmosaic_engine.models.segmentation import segment_power_series
from wattmosaic_engine.storage import Database

from tests.unit.conftest import BASE_TIME, binary_samples, make_sample, series, windows_over

#: The live filter compares against ``utcnow()``.  Tests pin timestamps around
#: BASE_TIME, so they use a threshold set that cannot expire; freshness is
#: exercised explicitly by passing ``SampleEvidence(now=...)``.
NEVER_STALE = LearningThresholds(stale_after_s=10**9)


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _clean_sample(**overrides):
    defaults = dict(
        device_id="dev",
        index=0,
        baseline=10.0,
        event=110.0,
        state="on",
        captured_at=BASE_TIME,
    )
    defaults.update(overrides)
    return make_sample(**defaults)


def _fitted_binary(on_power: float = 1500.0) -> BinaryDeviceModel:
    samples = [
        make_sample("dev", i, baseline=0.0, event=on_power, state="on")
        if i % 2 == 0
        else make_sample("dev", i, baseline=on_power, event=0.0, state="off")
        for i in range(16)
    ]
    model = BinaryDeviceModel()
    model.fit(samples)
    return model


def _binary_version(
    device_id: str,
    version: int,
    status: ModelStatus,
    *,
    metrics: dict[str, float] | None = None,
) -> DeviceModelVersion:
    model = _fitted_binary()
    return DeviceModelVersion(
        device_id=device_id,
        model_type=ModelType.BINARY,
        version=version,
        status=status,
        created_at=BASE_TIME + timedelta(hours=version),
        trained_from=BASE_TIME,
        trained_to=BASE_TIME + timedelta(hours=1),
        sample_count=16,
        validation_metrics=metrics or {"r2": 0.8, "mae": 5.0, "sample_count": 8.0},
        parameters=model.parameters.model_copy(deep=True),
        confidence=0.8,
    )


# --------------------------------------------------------------------------- #
# Sample filter: every rejection reason is reachable
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "sample_factory", "evidence", "expected"),
    [
        (
            "duplicate",
            lambda f: (
                f.evaluate(_clean_sample(index=0), SampleEvidence(now=BASE_TIME)),
                _clean_sample(index=1, captured_at=BASE_TIME + timedelta(seconds=5)),
                SampleEvidence(now=BASE_TIME),
            ),
            None,
            SampleRejection.DUPLICATE_STATE,
        ),
        (
            "missing",
            lambda f: _clean_sample(supporting_cts=[]),
            SampleEvidence(now=BASE_TIME),
            SampleRejection.MISSING_MEASUREMENT,
        ),
        (
            "timestamp",
            lambda f: _clean_sample(captured_at=BASE_TIME + timedelta(minutes=5)),
            SampleEvidence(now=BASE_TIME),
            SampleRejection.TIMESTAMP_UNRELIABLE,
        ),
        (
            "stale",
            lambda f: _clean_sample(),
            SampleEvidence(now=BASE_TIME, measurement_age_s=500.0),
            SampleRejection.STALE_MEASUREMENT,
        ),
        (
            "concurrent",
            lambda f: _clean_sample(),
            SampleEvidence(now=BASE_TIME, concurrent_events=2),
            SampleRejection.CONCURRENT_EVENTS,
        ),
        (
            "topology",
            lambda f: _clean_sample(),
            SampleEvidence(now=BASE_TIME, topology_confirmed=False),
            SampleRejection.AMBIGUOUS_TOPOLOGY,
        ),
        (
            "association",
            lambda f: _clean_sample(),
            SampleEvidence(now=BASE_TIME, association_confidence=0.3),
            SampleRejection.UNCONFIRMED_ASSOCIATION,
        ),
        (
            "unstable",
            lambda f: _clean_sample(baseline=0.0, event=100.0),
            SampleEvidence(now=BASE_TIME, noise_w=45.0),
            SampleRejection.UNSTABLE_READING,
        ),
        (
            "excessive",
            lambda f: _clean_sample(baseline=0.0, event=1000.0),
            SampleEvidence(now=BASE_TIME, noise_w=70.0),
            SampleRejection.EXCESSIVE_NOISE,
        ),
        (
            "inconsistent",
            lambda f: _clean_sample(delta=50.0),
            SampleEvidence(now=BASE_TIME),
            SampleRejection.INCONSISTENT_CHANGE,
        ),
        (
            "magnitude",
            lambda f: _clean_sample(baseline=100.0, event=104.0),
            SampleEvidence(now=BASE_TIME),
            SampleRejection.INSUFFICIENT_MAGNITUDE,
        ),
    ],
)
def test_each_rejection_reason_is_reachable(
    label: str,
    sample_factory,
    evidence,
    expected: SampleRejection,
) -> None:
    filter_ = SampleFilter(LearningThresholds())

    if label == "duplicate":
        first_decision, second_sample, second_evidence = sample_factory(filter_)
        assert first_decision.accepted is True  # first occurrence is kept
        decision = filter_.evaluate(second_sample, second_evidence)
    else:
        sample = sample_factory(filter_)
        decision = filter_.evaluate(sample, evidence)

    assert decision.accepted is False
    assert decision.reason == expected
    assert decision.quality_score <= 0.25
    assert decision.detail


def test_filter_accepts_clean_sample_and_scores_quality() -> None:
    filter_ = SampleFilter(NEVER_STALE)
    decision = filter_.evaluate(
        _clean_sample(),
        SampleEvidence(now=BASE_TIME, association_confidence=0.95, baseline_samples=6, event_samples=6),
    )

    assert decision.accepted is True
    assert decision.reason is None
    assert decision.quality_score == pytest.approx(0.99)

    marginal = filter_.evaluate(
        make_sample("other", 0, baseline=100.0, event=112.0, state="on", captured_at=BASE_TIME),
        SampleEvidence(now=BASE_TIME),
    )
    assert marginal.accepted is True
    assert marginal.quality_score < decision.quality_score


def test_filter_decision_apply_persists_reason_and_note() -> None:
    filter_ = SampleFilter(LearningThresholds())
    sample = _clean_sample(supporting_cts=[])
    annotated = filter_.evaluate(sample, SampleEvidence(now=BASE_TIME)).apply(sample)

    assert annotated.accepted is False
    assert annotated.rejection_reason == SampleRejection.MISSING_MEASUREMENT
    assert any(note.startswith("rejection_detail:") for note in annotated.notes)
    # The original contract object is never mutated in place.
    assert sample.accepted is True
    assert sample.rejection_reason is None


def test_filter_records_ambiguous_topology_from_notes() -> None:
    sample = _clean_sample(notes=["topology_candidates:2"])
    decision = SampleFilter(NEVER_STALE).evaluate(sample, SampleEvidence(now=BASE_TIME))

    assert decision.reason == SampleRejection.AMBIGUOUS_TOPOLOGY


def test_filter_batch_and_rejection_summary() -> None:
    samples = [
        _clean_sample(index=0, state="on"),
        _clean_sample(index=1, baseline=110.0, event=10.0, state="off"),
        _clean_sample(index=2, device_id="other", supporting_cts=[]),
    ]
    filtered = SampleFilter(NEVER_STALE).filter(samples)
    summary = rejection_summary(filtered)

    assert summary["accepted"] == 2
    assert summary[SampleRejection.MISSING_MEASUREMENT.value] == 1


def test_sample_notes_parse_first_occurrence() -> None:
    sample = _clean_sample(notes=["state:on", "state:off", "noise_w:2.5"])
    notes = sample_notes(sample)

    assert notes["state"] == "on"
    assert notes["noise_w"] == "2.5"
    assert notes["baseline_samples"] == "4"  # written by the shared helper


# --------------------------------------------------------------------------- #
# Sample collector: adaptive windows and control extraction
# --------------------------------------------------------------------------- #


def test_collector_window_tracks_measured_ct_cadence() -> None:
    thresholds = LearningThresholds()
    collector = SampleCollector(thresholds)

    # 15.7 s * 4 = 62.8 s from the audited CT cadence.
    assert collector.cadence_s == pytest.approx(15.7)
    assert collector.window_s == pytest.approx(62.8)

    # A cadence glitch cannot produce degenerate or unbounded windows.
    assert thresholds.window_s(0.5) == pytest.approx(8.0)
    assert thresholds.window_s(100.0) == pytest.approx(180.0)

    observed = collector.observe_cadence(
        [BASE_TIME + timedelta(seconds=offset) for offset in (0, 15, 31, 46, 62)]
    )
    assert observed == pytest.approx(15.5)
    assert collector.window_s == pytest.approx(62.0)


def test_collector_derives_cadence_from_channel_health() -> None:
    collector = SampleCollector(LearningThresholds())

    cadence = collector.cadence_from_health(
        {"ct_a": {"median_interval_s": 16.0}, "ct_b": {"median_interval_s": 14.0}}
    )
    assert cadence == pytest.approx(15.0)

    assert collector.cadence_from_health({"ct_a": {"expected_interval_s": 20.0}}) == pytest.approx(20.0)


def test_collector_builds_sample_from_observed_history() -> None:
    collector = SampleCollector(NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    windows = windows_over(
        BASE_TIME,
        event_at + timedelta(minutes=2),
        15.7,
        {"ct_branch": lambda at: 10.0 if at < event_at else 110.0},
    )
    for window in windows:
        collector.observe(window)

    event = DeviceEvent(
        event_id="e1",
        device_id="lamp",
        entity_id="light.lamp",
        event_type="state_changed",
        new_state="on",
        occurred_at=event_at,
        attributes={"supporting_ct_ids": ["ct_branch"]},
    )
    sample = collector.build(event)

    assert sample is not None
    assert sample.device_id == "lamp"
    assert sample.baseline_power_w == pytest.approx(10.0)
    assert sample.event_power_w == pytest.approx(110.0)
    assert sample.delta_power_w == pytest.approx(100.0)
    assert sample.supporting_ct_ids == ["ct_branch"]
    assert sample.captured_at == event_at
    notes = sample_notes(sample)
    assert notes["state"] == "on"
    assert int(notes["baseline_samples"]) >= 3
    assert int(notes["event_samples"]) >= 3
    assert float(notes["window_s"]) == pytest.approx(62.8)


def test_collector_emits_rejected_shaped_sample_without_measurement() -> None:
    collector = SampleCollector(LearningThresholds())
    event = DeviceEvent(
        event_id="e2",
        device_id="dryer",
        entity_id="switch.dryer",
        event_type="state_changed",
        new_state="on",
        occurred_at=BASE_TIME,
        attributes={},
    )
    sample = collector.build(event)

    assert sample is not None
    assert "no_measurement" in sample.notes
    assert sample.supporting_ct_ids == []
    decision = SampleFilter(LearningThresholds()).evaluate(sample, SampleEvidence(now=BASE_TIME))
    assert decision.reason == SampleRejection.MISSING_MEASUREMENT


def test_extract_control_prefers_numeric_attribute_then_mode_then_state() -> None:
    assert extract_control({"brightness": 128, "speed": 3}, "on") == (128.0, "brightness")
    assert extract_control({"speed": 2}, "on") == (2.0, "speed")
    assert extract_control({"hvac_mode": "heat"}, "heat") == (None, "hvac_mode")
    assert extract_control({}, "running") == (None, "state")
    assert extract_control({}, None) == (None, None)


def test_collector_ignores_malformed_events_without_aborting_batch() -> None:
    collector = SampleCollector(NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=3)
    windows = windows_over(
        BASE_TIME,
        event_at + timedelta(minutes=1),
        15.7,
        {"ct": lambda at: 5.0 if at < event_at else 55.0},
    )
    for window in windows:
        collector.observe(window)

    good = DeviceEvent(
        event_id="ok",
        device_id="dev",
        entity_id="light.dev",
        event_type="state_changed",
        new_state="on",
        occurred_at=event_at,
        attributes={"supporting_ct_ids": ["ct"]},
    )
    class ExplodingEvent:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("malformed event")

    samples = collector.consider([ExplodingEvent(), good], windows[-1])

    assert len(samples) == 1
    assert samples[0].delta_power_w == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# Trainer
# --------------------------------------------------------------------------- #


def test_trainer_refuses_below_minimum_sample_count() -> None:
    trainer = Trainer(LearningThresholds(min_samples_for_model=12))

    ready, reason = trainer.should_train("dev", binary_samples(count=8))
    assert ready is False
    assert reason == "insufficient_samples:8<12"

    ready, reason = trainer.should_train("dev", binary_samples(count=12))
    assert ready is True
    assert reason == "ready"

    result = trainer.train("dev", binary_samples(count=8), model_type=ModelType.BINARY)
    assert result.trained is False
    assert result.version is None
    assert result.reason.startswith("insufficient_samples")


def test_trainer_requires_enough_new_samples_since_active_model() -> None:
    trainer = Trainer(LearningThresholds(min_samples_for_model=12))
    samples = binary_samples(count=24)
    active = _binary_version("dev", 1, ModelStatus.ACTIVE)
    active.trained_to = samples[-1].captured_at

    ready, reason = trainer.should_train("dev", samples, [active])
    assert ready is False
    assert reason.startswith("insufficient_new_samples")

    newer = [
        make_sample(
            "dev",
            i,
            baseline=0.0,
            event=60.0,
            state="on",
            captured_at=samples[-1].captured_at + timedelta(minutes=5 * (i + 1)),
        )
        for i in range(4)
    ]
    ready, _ = trainer.should_train("dev", samples + newer, [active])
    assert ready is True


@pytest.mark.parametrize(
    ("device", "samples", "expected"),
    [
        ({"states": ["on", "off"]}, [], ModelType.BINARY),
        (
            {},
            [make_sample("dev", 0, control_value=10, control_variable="brightness")],
            ModelType.DIMMABLE,
        ),
        (
            {},
            [make_sample("dev", 0, control_value=50, control_variable="speed")],
            ModelType.VARIABLE_SPEED,
        ),
        ({"states": ["heat", "cool", "fan_only"]}, [], ModelType.MULTI_STATE),
        ({"attributes": {"program": "cotton"}}, [], ModelType.TIME_VARYING),
        ({"attributes": {"components": ["fan", "heat"]}}, [], ModelType.COMPOSITE),
        (None, [], ModelType.RESIDUAL),
    ],
)
def test_infer_model_type_from_device_characteristics(device, samples, expected) -> None:
    assert infer_model_type(device, samples) == expected


def test_trainer_produces_candidate_with_validation_metrics() -> None:
    samples = binary_samples(count=32, on_power=1500.0)
    trainer = Trainer(LearningThresholds())
    result = trainer.train("heater", samples, model_type=ModelType.BINARY)

    assert result.trained is True
    assert result.version is not None
    assert result.version.status == ModelStatus.CANDIDATE
    assert result.version.version == 1
    assert result.version.device_id == "heater"
    assert result.version.model_type == ModelType.BINARY
    assert result.version.sample_count == 24  # 3/4 train split of 32
    assert result.metrics["r2"] == pytest.approx(1.0)
    assert result.metrics["mae"] == pytest.approx(0.0)
    assert result.version.training_quality > 0.0


def test_trained_model_beats_constant_predictor_on_held_out_data() -> None:
    samples = binary_samples(count=60, on_power=1500.0)
    trainer = Trainer(LearningThresholds())
    result = trainer.train("heater", samples, model_type=ModelType.BINARY)

    _, holdout = split_holdout(samples, 0.25, 3)
    model = result.model
    assert model is not None

    actual = [sample.event_power_w for sample in holdout]
    model_error = sum(
        abs(sample.event_power_w - model.predict(context_from_sample(sample))) for sample in holdout
    ) / len(holdout)
    constant = sum(actual) / len(actual)
    constant_error = sum(abs(value - constant) for value in actual) / len(actual)

    assert model_error == pytest.approx(0.0)
    # The tail holds 8 OFF (0 W) and 7 ON (1500 W) samples: mean 700 W, and the
    # constant predictor's error is (8*700 + 7*800) / 15.
    assert constant == pytest.approx(700.0)
    assert constant_error == pytest.approx((8 * 700.0 + 7 * 800.0) / 15.0)
    assert model_error < constant_error


def test_trainer_reports_unknown_and_missing_data_reasons() -> None:
    trainer = Trainer(LearningThresholds())

    no_data = trainer.train("dev", [], model_type=ModelType.BINARY)
    assert no_data.trained is False
    assert no_data.reason == "no_accepted_samples"

    unknown = trainer.train("dev", binary_samples(count=12), model_type="not_a_model")
    assert unknown.trained is False
    assert unknown.reason.startswith("unknown_model_type")


def test_training_quality_penalises_low_acceptance_and_coverage() -> None:
    accepted = binary_samples(count=24)
    rejected = [
        make_sample("dev", i + 100, baseline=0.0, event=5.0, accepted=False) for i in range(24)
    ]
    metrics = {"r2": 1.0, "mae": 0.0}
    good, _ = training_quality(accepted, metrics, all_samples=accepted)
    poor, notes = training_quality(accepted, metrics, all_samples=accepted + rejected)

    assert good > poor
    assert any(note.startswith("acceptance=") for note in notes)
    assert training_quality([], metrics) == (0.0, ["no_accepted_samples"])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_split_holdout_is_deterministic_ordered_and_disjoint() -> None:
    samples = binary_samples(count=11)
    train_a, valid_a = split_holdout(samples, 0.25, 3)
    train_b, valid_b = split_holdout(samples, 0.25, 3)

    assert [s.sample_id for s in train_a] == [s.sample_id for s in train_b]
    assert [s.sample_id for s in valid_a] == [s.sample_id for s in valid_b]
    assert (len(train_a), len(valid_a)) == (8, 3)

    train_ids = {s.sample_id for s in train_a}
    valid_ids = {s.sample_id for s in valid_a}
    assert train_ids.isdisjoint(valid_ids)
    assert train_ids | valid_ids == {s.sample_id for s in samples}

    # The hold-out is the *tail* in time, never a random interleaving.
    assert max(s.captured_at for s in train_a) < min(s.captured_at for s in valid_a)

    # Ignored rejected samples never leak into either split.
    rejected = make_sample("dev", 200, accepted=False)
    assert all(s.sample_id != rejected.sample_id for s in (*train_a, *valid_a))


def test_validate_model_reports_correct_metrics() -> None:
    samples = binary_samples(count=24, on_power=1500.0)
    model = _fitted_binary(1500.0)
    report = validate_model(model, samples, LearningThresholds())

    assert report.metrics["sample_count"] == pytest.approx(24.0)
    assert report.metrics["n"] == pytest.approx(24.0)
    assert report.metrics["mae"] == pytest.approx(0.0)
    assert report.metrics["rmse"] == pytest.approx(0.0)
    assert report.metrics["r2"] == pytest.approx(1.0)
    assert report.stable is True
    assert report.fold_r2 == pytest.approx([1.0, 1.0, 1.0])


def test_validate_model_flags_weak_or_empty_data() -> None:
    empty = validate_model(_fitted_binary(), [], LearningThresholds())
    assert empty.stable is False
    assert empty.notes == ["no_validation_samples"]

    # A confidently wrong 500 W model validated on 1500 W / 0 W samples is bad.
    wrong = BinaryDeviceModel(DeviceModelParameters(on_power_w=500.0, off_power_w=0.0))
    report = validate_model(wrong, binary_samples(count=24, on_power=1500.0), LearningThresholds())
    assert report.metrics["mae"] > 0.0
    assert report.metrics["r2"] < 1.0


def test_compare_models_requires_margin_and_respects_mae_guard() -> None:
    improved = compare_models({"r2": 0.90, "mae": 1.0}, {"r2": 0.80, "mae": 1.0})
    assert improved["better"] is True
    assert improved["delta_r2"] == pytest.approx(0.10)

    marginal = compare_models({"r2": 0.82, "mae": 1.0}, {"r2": 0.80, "mae": 1.0})
    assert marginal["better"] is False
    assert marginal["reason"].startswith("r2_improvement_insufficient")

    higher_r2_worse_mae = compare_models({"r2": 0.95, "mae": 2.0}, {"r2": 0.80, "mae": 1.0})
    assert higher_r2_worse_mae["better"] is False
    assert higher_r2_worse_mae["reason"].startswith("mae_worse")

    first_model = compare_models({"r2": 0.60, "mae": 3.0}, None)
    assert first_model["better"] is True
    assert first_model["reason"] == "no_active_model"


# --------------------------------------------------------------------------- #
# Promotion / rollback / freeze
# --------------------------------------------------------------------------- #


def test_candidate_promotes_only_when_it_beats_active_model(db: Database) -> None:
    manager = PromotionManager(db, thresholds=LearningThresholds())
    active = _binary_version("heater", 1, ModelStatus.ACTIVE, metrics={"r2": 0.80, "mae": 5.0})
    db.save_model_version(active, activate=True)

    better = _binary_version("heater", 2, ModelStatus.CANDIDATE, metrics={"r2": 0.60, "mae": 5.0})
    decision = manager.validate_and_promote(better, binary_samples(count=32, on_power=1500.0))

    assert decision.promoted is True
    assert decision.reason == "promoted"
    assert decision.active_version == 2
    assert db.active_model("heater")["version"] == 2

    # A second, equally perfect candidate cannot clear the 0.05 R² margin.
    equal = _binary_version("heater", 3, ModelStatus.CANDIDATE)
    rejected = manager.validate_and_promote(equal, binary_samples(count=32, on_power=1500.0))

    assert rejected.promoted is False
    assert rejected.manual_review is True
    assert rejected.reason.startswith("manual_review_required:r2_improvement_insufficient")
    assert db.active_model("heater")["version"] == 2
    stored = {row["version"]: row["status"] for row in db.model_versions("heater")}
    assert stored[3] == ModelStatus.CANDIDATE.value


def test_candidate_below_absolute_r2_threshold_needs_manual_review(db: Database) -> None:
    manager = PromotionManager(
        db, thresholds=LearningThresholds(min_validation_r2=0.99, promotion_margin=0.05)
    )

    def noisy_samples() -> list:
        return [
            make_sample("heater", i, baseline=0.0, event=1500.0 + ((i % 5) - 2) * 40.0, state="on")
            if i % 2 == 0
            else make_sample("heater", i, baseline=1500.0, event=0.0, state="off")
            for i in range(60)
        ]

    # A 1400 W model fits the 1500 W device reasonably (R² ≈ 0.988) but cannot
    # clear the 0.99 absolute gate.  It is *stable*, so this isolates the
    # threshold check from the stability check.
    candidate = _binary_version("heater", 1, ModelStatus.CANDIDATE)
    candidate.parameters = _fitted_binary(1400.0).parameters.model_copy(deep=True)

    decision = manager.validate_and_promote(candidate, noisy_samples())

    assert decision.promoted is False
    assert decision.reason.startswith("manual_review_required:r2_below_threshold")
    assert db.active_model("heater") is None


def test_frozen_device_never_promotes_a_candidate(db: Database) -> None:
    manager = PromotionManager(db, thresholds=LearningThresholds())
    db.save_model_version(_binary_version("heater", 1, ModelStatus.ACTIVE), activate=True)
    manager.freeze("heater", True)

    candidate = _binary_version("heater", 2, ModelStatus.CANDIDATE)
    decision = manager.validate_and_promote(candidate, binary_samples(count=32, on_power=1500.0))

    assert decision.promoted is False
    assert decision.reason == "frozen"
    assert decision.manual_review is True
    assert db.active_model("heater")["version"] == 1
    assert any("blocked_by_frozen_model" in note for note in candidate.notes)


def test_rollback_restores_previous_active_version_and_keeps_history(db: Database) -> None:
    manager = PromotionManager(db, thresholds=LearningThresholds())
    db.save_model_version(_binary_version("heater", 1, ModelStatus.ACTIVE), activate=True)
    db.save_model_version(_binary_version("heater", 2, ModelStatus.CANDIDATE), activate=True)
    db.set_model_status("heater", "binary", 2, ModelStatus.ACTIVE)

    restored = manager.rollback("heater")

    assert restored == 1
    assert db.active_model("heater")["version"] == 1
    history = {row["version"]: row["status"] for row in db.model_versions("heater")}
    assert history == {1: ModelStatus.ACTIVE.value, 2: ModelStatus.ROLLED_BACK.value}


def test_rollback_ignores_pending_candidate_and_reports_nothing(db: Database) -> None:
    manager = PromotionManager(db, thresholds=LearningThresholds())
    db.save_model_version(_binary_version("heater", 1, ModelStatus.ACTIVE), activate=True)
    # Version 2 was saved but never activated: rolling "back" must not touch it.
    db.save_model_version(_binary_version("heater", 2, ModelStatus.CANDIDATE), activate=False)

    assert manager.rollback("heater") is None
    history = {row["version"]: row["status"] for row in db.model_versions("heater")}
    assert history == {1: ModelStatus.ACTIVE.value, 2: ModelStatus.CANDIDATE.value}

    assert manager.rollback("unknown_device") is None


def test_version_numbers_increment_and_are_never_reused(db: Database) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    samples = binary_samples(count=32, on_power=1500.0)
    accepted, rejected = pipeline.ingest_samples(samples)
    assert not rejected

    first = pipeline.train("heater", accepted, model_type=ModelType.BINARY)
    second = pipeline.train("heater", accepted, model_type=ModelType.BINARY)
    assert (first.version, second.version) == (1, 2)

    pipeline.validate_and_promote(first, accepted)
    assert db.active_model("heater")["version"] == 1

    # A candidate that cannot clear the margin is kept as history — its number
    # is spent and must never be handed out again.
    pipeline.validate_and_promote(second, accepted)
    assert db.active_model("heater")["version"] == 1
    assert {row["version"] for row in db.model_versions("heater")} == {1, 2}

    third = pipeline.train("heater", accepted, model_type=ModelType.BINARY, force=True)
    assert third.version == 3
    versions = sorted(version.version for version in pipeline.registry.versions("heater"))
    assert versions == [1, 2, 3]

    # Rollback refuses a pending candidate: nothing was ever serving version 3.
    assert pipeline.rollback("heater") is None
    assert db.active_model("heater")["version"] == 1


def test_promotion_rejects_already_rejected_candidate(db: Database) -> None:
    manager = PromotionManager(db, thresholds=LearningThresholds())
    candidate = _binary_version("heater", 1, ModelStatus.REJECTED)

    decision = manager.validate_and_promote(candidate, binary_samples(count=32))

    assert decision.promoted is False
    assert decision.reason == "candidate_rejected"
    assert db.active_model("heater") is None


# --------------------------------------------------------------------------- #
# Stage recognition
# --------------------------------------------------------------------------- #


def test_recognize_stages_labels_three_stage_cycle() -> None:
    power_series = series([(20, 100.0), (20, 600.0), (20, 1500.0)])
    stages = recognize_stages(power_series)

    assert len(stages) == 3
    assert [stage.power_mean_w for stage in stages] == pytest.approx([100.0, 600.0, 1500.0])
    assert [stage.name for stage in stages] == ["standby", "medium", "peak"]
    assert all(stage.status == StageStatus.IDENTIFIED for stage in stages)
    assert all(stage.min_duration_s and stage.min_duration_s > 0 for stage in stages)


def test_stage_distinguishable_is_false_on_flat_noisy_series() -> None:
    flat = [(at, 200.0 + (1.0 if i % 2 else -1.0)) for i, (at, _) in enumerate(series([(50, 0.0)]))]
    rng = __import__("random").Random(20261105)
    noisy = [(at, 500.0 + rng.gauss(0.0, 20.0)) for at, _ in series([(100, 0.0)])]

    assert stage_distinguishable(flat) is True  # a true constant level is one resolvable stage
    assert stage_distinguishable(noisy) is False  # noise hides any level boundary
    assert segment_power_series(noisy).ambiguous is True


def test_current_stage_reports_tail_level() -> None:
    power_series = series([(70, 100.0), (10, 600.0)])
    name, status = current_stage(power_series)

    assert name == "peak"
    assert status == StageStatus.IDENTIFIED
    assert current_stage([]) == (None, StageStatus.UNKNOWN)


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #


def _collect_switch_samples(pipeline: LearningPipeline, cycles: int = 16) -> None:
    """Feed alternating 100 W ON / 10 W OFF events at 5-minute spacing."""
    levels: list[tuple[datetime, float]] = []
    events = []
    for k in range(cycles):
        event_at = BASE_TIME + timedelta(seconds=300 * (k + 1))
        level = 110.0 if k % 2 == 0 else 10.0
        levels.append((event_at, level))
        events.append(
            DeviceEvent(
                event_id=f"e{k}",
                device_id="dev",
                entity_id="light.dev",
                event_type="state_changed",
                new_state="on" if k % 2 == 0 else "off",
                occurred_at=event_at,
                attributes={"supporting_ct_ids": ["ct_branch"]},
            )
        )

    def value_at(at: datetime) -> float:
        power = 10.0
        for event_at, level in levels:
            if at >= event_at:
                power = level
        return power

    end = BASE_TIME + timedelta(seconds=300 * (cycles + 1))
    windows = windows_over(BASE_TIME, end, 15.7, {"ct_branch": value_at})
    for window in windows:
        pipeline.collector.observe(window)
    for event in events:
        pipeline.consider([event], windows[-1])


def test_pipeline_end_to_end_persists_and_promotes(db: Database) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    _collect_switch_samples(pipeline)

    # Every sample carries a real baseline/event delta, so all are accepted.
    assert pipeline.last_rejected == []
    accepted = pipeline.stored_samples("dev")
    assert len(accepted) == 16
    assert all(sample.accepted for sample in accepted)

    candidate = pipeline.train("dev", accepted, model_type=ModelType.BINARY)
    assert candidate is not None
    assert candidate.version == 1

    decision = pipeline.validate_and_promote(candidate, accepted)
    assert decision.promoted is True

    active = db.active_model("dev")
    assert active is not None
    assert active["version"] == 1
    assert active["status"] == ModelStatus.ACTIVE.value

    estimate = pipeline.registry.estimate("dev", {"state": "on"})
    assert estimate is not None
    assert estimate.power_w == pytest.approx(110.0)


def test_pipeline_persists_rejected_samples_with_reason_and_never_trains_on_them(
    db: Database,
) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    accepted_samples = binary_samples(device_id="dev", count=32, on_power=1500.0)
    rejected_sample = make_sample(
        "dev", 99, baseline=0.0, event=1500.0, state="on", supporting_cts=[]
    )

    accepted, rejected = pipeline.ingest_samples([*accepted_samples, rejected_sample])
    assert len(accepted) == 32
    assert len(rejected) == 1
    assert rejected[0].rejection_reason == SampleRejection.MISSING_MEASUREMENT

    all_rows = db.learning_samples("dev", accepted_only=False)
    assert len(all_rows) == 33
    persisted_rejected = [row for row in all_rows if not row["accepted"]]
    assert len(persisted_rejected) == 1
    assert persisted_rejected[0]["rejection_reason"] == SampleRejection.MISSING_MEASUREMENT.value

    # Only the 32 accepted samples are visible to the trainer.
    stored = pipeline.stored_samples("dev")
    assert len(stored) == 32
    assert all(sample.accepted for sample in stored)

    candidate = pipeline.train("dev", stored, model_type=ModelType.BINARY)
    assert candidate is not None
    assert candidate.sample_count < 33
    assert candidate.validation_metrics["sample_count"] == pytest.approx(8.0)


def test_pipeline_train_returns_none_when_gating_blocks(db: Database) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    accepted, _ = pipeline.ingest_samples(binary_samples(count=6))

    assert pipeline.train("dev", accepted, model_type=ModelType.BINARY) is None
    # ``force`` bypasses the sample-count gate but not the absolute requirement
    # that there be at least one accepted sample.
    assert pipeline.train("dev", [], model_type=ModelType.BINARY, force=True) is None


def test_pipeline_freeze_blocks_then_unfreeze_allows_and_rollback_restores(
    db: Database,
) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    samples, _ = pipeline.ingest_samples(binary_samples(count=32, on_power=1500.0))

    # A weaker model is already serving, so a perfect candidate can clear the
    # promotion margin once the freeze is lifted.
    active = _binary_version("dev", 1, ModelStatus.ACTIVE, metrics={"r2": 0.50, "mae": 30.0})
    active.device_id = "dev"
    db.save_model_version(active, activate=True)

    pipeline.freeze("dev", True)
    blocked = pipeline.train("dev", samples, model_type=ModelType.BINARY, force=True)
    assert blocked is not None
    decision = pipeline.validate_and_promote(blocked, samples)
    assert decision.promoted is False
    assert decision.reason == "frozen"
    assert db.active_model("dev")["version"] == 1

    pipeline.freeze("dev", False)
    promoted = pipeline.validate_and_promote(blocked, samples)
    assert promoted.promoted is True
    assert db.active_model("dev")["version"] == blocked.version

    assert pipeline.rollback("dev") == 1
    assert db.active_model("dev")["version"] == 1
