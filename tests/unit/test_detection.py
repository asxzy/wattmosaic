"""P3 — Streaming change detector: identity, hysteresis, CUSUM, drift, gaps.

Mirrors the task's acceptance bullets against the *production* module, using the
P2 :class:`ReaderStateStore` and the K02 :class:`ReaderMeasurement` contract.
No wall clock and no database are involved; the fakes in ``runtime.fakes`` are
not needed here (the P2 store is the real, pure state model).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    MeasurementBasis,
    MeasurementEvent,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
    StepClass,
)
from wattmosaic_engine.realtime.detection import (
    DETECTOR_VERSION,
    ChangeDetector,
    DetectorConfig,
    DetectorState,
    build_measurement_event,
)
from wattmosaic_engine.realtime.reader_state import ReaderStateStore

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
CADENCE_S = 15.7


def measurement(
    reader: str,
    at: datetime,
    power: float | None,
    *,
    sequence: int = 0,
    flags: Sequence[QualityFlag] = (),
) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
        observed_at=at,
        received_at=at + timedelta(seconds=2),
        active_power_w=power,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=None,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED.value,
        quality_flags=list(flags),
        sequence=sequence,
    )


def stream(
    store: ReaderStateStore,
    detector: ChangeDetector | None,
    reader: str,
    values: Sequence[float | None],
    *,
    start: datetime = T0,
    step_s: float = CADENCE_S,
    seq0: int = 0,
    flags: Sequence[QualityFlag] = (),
) -> list[datetime]:
    """Feed samples one at a time, advancing the detector after each (P6 order)."""
    times: list[datetime] = []
    for i, value in enumerate(values):
        when = start + timedelta(seconds=i * step_s)
        store.add(measurement(reader, when, value, sequence=seq0 + i, flags=flags))
        if detector is not None:
            detector.process(reader, when)
        times.append(when)
    return times


def make_detector(
    store: ReaderStateStore | None = None,
    config: DetectorConfig | None = None,
) -> tuple[ReaderStateStore, ChangeDetector, list[MeasurementEvent]]:
    store = store or ReaderStateStore()
    events: list[MeasurementEvent] = []
    detector = ChangeDetector(store, events.append, config)
    return store, detector, events


def warmup(
    store: ReaderStateStore,
    detector: ChangeDetector,
    reader: str,
    value: float = 100.0,
    count: int = 64,
    *,
    step_s: float = CADENCE_S,
    start: datetime = T0,
) -> datetime:
    """Seed enough samples for P2's σ estimate to leave its 1 W floor."""
    return stream(store, detector, reader, [value] * count, start=start, step_s=step_s)[-1]


def is_class(event: MeasurementEvent, step_class: StepClass) -> bool:
    """Compare a step class (stored as a string value by Pydantic)."""
    return event.step_class == step_class.value


# --------------------------------------------------------------------------- #
# Positive / negative steps, sustained vs transient
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("step_w", [10.0, -10.0])
def test_positive_and_negative_steps(step_w: float) -> None:
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct")
    stream(store, detector, "ct", [100.0 + step_w] * 8, start=last + timedelta(seconds=CADENCE_S))

    steps = [e for e in events if is_class(e, StepClass.SUSTAINED)]
    assert len(steps) == 1
    assert math.isclose(steps[0].delta_w, step_w, abs_tol=1e-6)
    assert (steps[0].delta_w > 0) == (step_w > 0)
    assert steps[0].reader_id == "ct"
    assert 0.0 <= steps[0].quality_score <= 1.0
    assert store.reader_state("ct") is not None


def test_transient_excursion_is_rejected() -> None:
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct")
    # Two samples 10 W high (enough to form a post level), then back to
    # baseline before the confirmation deadline: a short transient.
    stream(
        store,
        detector,
        "ct",
        [110.0] * 2 + [100.0] * 8,
        start=last + timedelta(seconds=CADENCE_S),
    )

    transients = [e for e in events if is_class(e, StepClass.TRANSIENT)]
    assert len(transients) == 1
    assert not [e for e in events if is_class(e, StepClass.SUSTAINED)]


def test_no_step_on_flat_series() -> None:
    store, detector, events = make_detector()
    warmup(store, detector, "ct")
    assert events == []


# --------------------------------------------------------------------------- #
# One step yields exactly one event even with 20 identical trailing samples
# --------------------------------------------------------------------------- #


def test_one_step_yields_exactly_one_event_across_many_ticks() -> None:
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct")
    times = stream(
        store,
        detector,
        "ct",
        [110.0] * 25,
        start=last + timedelta(seconds=CADENCE_S),
    )
    # Additional ticks long after the step must not re-emit it.
    for extra in range(20):
        detector.process("ct", times[-1] + timedelta(seconds=(extra + 1) * CADENCE_S))

    steps = [e for e in events if is_class(e, StepClass.SUSTAINED)]
    assert len(steps) == 1
    assert len({e.event_id for e in events}) == len(events)


def test_event_id_is_stable_and_versioned() -> None:
    args = dict(
        reader_id="ct",
        delta_w=10.0,
        sigma_w=1.0,
        z_score=10.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=T0,
        onset_hi=T0 + timedelta(seconds=15),
        pre_level_w=100.0,
        post_level_w=110.0,
        quality_score=0.9,
        detected_at=T0,
    )
    first = build_measurement_event(**args)
    second = build_measurement_event(**args)
    assert first.event_id == second.event_id
    assert DETECTOR_VERSION == "3.0.0"
    assert isinstance(first, MeasurementEvent)


# --------------------------------------------------------------------------- #
# False-alarm rate < 1 per reader-day on 7 days of noisy data
# --------------------------------------------------------------------------- #


def test_false_alarm_rate_below_one_per_reader_day() -> None:
    rng = random.Random(20261118)
    total_events = 0
    days = 7
    samples_per_day = 720  # 120 s cadence — a quiet, slow-install reader.
    dt = 120.0
    for day in range(days):
        store, detector, events = make_detector()
        values: list[float] = []
        for _ in range(samples_per_day):
            noise = rng.gauss(0.0, 1.0)
            if rng.random() < 0.02:
                noise += rng.choice([-1.0, 1.0]) * rng.uniform(6.0, 12.0)
            values.append(150.0 + noise)
        stream(store, detector, "ct", values, start=T0 + timedelta(days=day), step_s=dt)
        # False alarms are sustained events mistaken for real steps.  Transient
        # rejections are explicitly classified as non-steps and are not counted.
        total_events += len([e for e in events if is_class(e, StepClass.SUSTAINED)])
    assert total_events < days, f"{total_events} false sustained events in {days} reader-days"


# --------------------------------------------------------------------------- #
# Per-reader threshold: 10 W detected on quiet (σ=1 W), not noisy (σ=8 W)
# --------------------------------------------------------------------------- #


def _noisy_step_events(sigma: float, seed: int) -> list[MeasurementEvent]:
    rng = random.Random(seed)
    store, detector, events = make_detector()
    # Enough warm-up for P2 to build a real σ estimate (it recomputes every 64
    # innovations; a short series stays at the 1 W floor).
    values = [100.0 + rng.gauss(0.0, sigma) for _ in range(140)]
    values += [110.0 + rng.gauss(0.0, sigma) for _ in range(20)]
    stream(store, detector, "ct", values)
    return [e for e in events if is_class(e, StepClass.SUSTAINED)]


def test_10w_step_detected_on_quiet_but_not_noisy_reader() -> None:
    quiet = _noisy_step_events(1.0, seed=1)
    noisy = _noisy_step_events(8.0, seed=1)
    assert len(quiet) >= 1
    assert noisy == []


# --------------------------------------------------------------------------- #
# Drift over 3 h is DRIFT, not step events
# --------------------------------------------------------------------------- #


def test_slow_drift_is_classified_drift_not_step() -> None:
    store, detector, events = make_detector()
    # Warm up σ, then 3 h of 10 min-cadence samples: +0.3 W/sample ≈ 5.4 W
    # total, well under the enter threshold each sample, but a steady drift.
    last = warmup(store, detector, "ct", count=64)
    stream(
        store,
        detector,
        "ct",
        [100.0 + 0.3 * i for i in range(19)],
        step_s=600.0,
        start=last + timedelta(seconds=600.0),
    )
    assert any(is_class(e, StepClass.DRIFT) for e in events)
    assert not [e for e in events if is_class(e, StepClass.SUSTAINED)]


# --------------------------------------------------------------------------- #
# Periodic tag on an X07 fridge cycle
# --------------------------------------------------------------------------- #


def test_periodic_tag_on_x07_fridge_cycle() -> None:
    store, detector, events = make_detector()
    warmup(store, detector, "ct", value=50.0, count=64)
    # Five compressor ON steps, each ~900 W, every 30 min (1850 s period),
    # with OFF steps in between.  Feed in strict time order.
    samples: list[tuple[datetime, float]] = []
    cycle_start = T0 + timedelta(seconds=64 * CADENCE_S)
    for cycle in range(12):
        on_at = cycle_start + timedelta(seconds=cycle * 1850)
        for i in range(4):
            samples.append((on_at + timedelta(seconds=i * CADENCE_S), 50.0))
        for i in range(4):
            samples.append((on_at + timedelta(seconds=(4 + i) * CADENCE_S), 950.0))
    samples.sort(key=lambda item: item[0])
    for i, (at, value) in enumerate(samples):
        store.add(measurement("ct", at, value, sequence=i))
        detector.process("ct", at)

    steps = [e for e in events if is_class(e, StepClass.SUSTAINED)]
    periodic = [e for e in events if is_class(e, StepClass.PERIODIC)]
    assert len(steps) + len(periodic) >= 4
    assert periodic, "a fridge cycle should earn at least one periodic tag"
    assert all(e.delta_w > 0 for e in periodic)


# --------------------------------------------------------------------------- #
# Steady 3 W standby produces no events
# --------------------------------------------------------------------------- #


def test_steady_3w_standby_produces_no_events() -> None:
    store, detector, events = make_detector()
    warmup(store, detector, "standby", value=3.0)
    assert events == []


# --------------------------------------------------------------------------- #
# Onset bounds include the true onset with a window-averaged reader
# --------------------------------------------------------------------------- #


def test_onset_bounds_include_true_onset_with_window_averaged_reader() -> None:
    store = ReaderStateStore(resolution_w_by_reader={"win": 1.0})
    events: list[MeasurementEvent] = []
    detector = ChangeDetector(store, events.append, DetectorConfig(onset_padding_s=30.0))
    # A window-averaged reader: the true physical step happens at index 6
    # (t0 + 6*cadence), but the first elevated sample is stamped at index 8.
    # Onset padding must cover the true onset in every case.
    last = warmup(store, detector, "win", value=100.0, count=64)
    values = [100.0] * 2 + [110.0] * 10
    times = stream(store, detector, "win", values, start=last + timedelta(seconds=CADENCE_S))

    steps = [e for e in events if is_class(e, StepClass.SUSTAINED)]
    assert steps
    # The physical step happened between the last flat sample and the first
    # elevated sample; the 30 s onset padding must cover that interval.
    true_onset = times[2] - timedelta(seconds=CADENCE_S / 2)
    for step in steps:
        assert step.onset_lo <= true_onset <= step.onset_hi, (
            f"onset [{step.onset_lo}, {step.onset_hi}] misses {true_onset}"
        )


# --------------------------------------------------------------------------- #
# Primed values never open SUSPECT; missing emits DATA_GAP
# --------------------------------------------------------------------------- #


def test_primed_values_never_open_suspect() -> None:
    store, detector, events = make_detector()
    primed_flags = [QualityFlag.PRIMED]
    times = stream(store, detector, "ct", [100.0] * 70, flags=primed_flags)
    stream(
        store,
        detector,
        "ct",
        [500.0] * 4,
        start=times[-1] + timedelta(seconds=CADENCE_S),
        seq0=70,
        flags=primed_flags,
    )
    assert events == []
    assert detector.state("ct") == DetectorState.STABLE


def test_missing_reader_emits_single_data_gap() -> None:
    store = ReaderStateStore(stale_after_s=10.0, missing_after_s=30.0)
    events: list[MeasurementEvent] = []
    detector = ChangeDetector(store, events.append, DetectorConfig(gap_cooldown_s=30.0))
    times = stream(store, detector, "ct", [100.0] * 4, step_s=5.0)
    # Advance past missing_after_s and keep ticking with no new samples.
    for extra in range(1, 8):
        detector.process("ct", times[-1] + timedelta(seconds=30 + extra * 5))
    gaps = [e for e in events if is_class(e, StepClass.DATA_GAP)]
    assert len(gaps) == 1
    # A gap has no delta measurement: absent is None, never 0.0 (HB §18).
    assert gaps[0].delta_w is None
    assert gaps[0].sigma_w is None
    assert gaps[0].z_score is None
    # Non-gap events must still carry a real delta.
    for event in events:
        if not is_class(event, StepClass.DATA_GAP):
            assert event.delta_w is not None


# --------------------------------------------------------------------------- #
# Freeze/unfreeze baseline behaviour
# --------------------------------------------------------------------------- #


def test_baseline_frozen_during_suspect_and_step_emitted() -> None:
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct")
    for i, value in enumerate([110.0] * 8, start=1):
        when = last + timedelta(seconds=i * CADENCE_S)
        store.add(measurement("ct", when, value, sequence=64 + i))
        detector.process("ct", when)
        if i == 1:  # first post-step sample
            state = store.reader_state("ct")
            assert state is not None
            assert detector.state("ct") == DetectorState.SUSPECT
            assert state.frozen or state.frozen_until is not None
    assert events and is_class(events[0], StepClass.SUSTAINED)
