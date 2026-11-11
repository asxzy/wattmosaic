"""Anchored CT step detection and StepTracker history semantics.

These tests pin three properties:

1. ``anchor=None`` reproduces the historical (unanchored) result exactly.
2. An anchored query may only use split times near the HA transition, and the
   nearest plausible step beats a far larger distant one.
3. The tracker feeds real CT cadence into the detector: duplicate per-cycle
   readings collapse, and a two-sample baseline is no longer trusted.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.disaggregation.events import (
    DEFAULT_MIN_BEFORE_SAMPLES,
    DEFAULT_NOISE_FLOOR_W,
    StepChange,
    StepTracker,
    detect_changes,
    detect_step,
)
from wattmosaic_engine.electrical.window import ChannelReading

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
CADENCE = 15.7  # measured HA median, not a 2 s assumption


def _series(values: list[float], *, start: datetime = T0, cadence: float = CADENCE):
    return [(start + timedelta(seconds=cadence * i), float(v)) for i, v in enumerate(values)]


def _reading(values: list[float], *, channel_id: str = "ct_kettle", resolution_w: float | None = 1.0):
    history = tuple(_series(values))
    at = history[-1][0]
    return ChannelReading(
        channel_id=channel_id,
        value_w=history[-1][1],
        history=history,
        resolution_w=resolution_w,
    ), at


# --------------------------------------------------------------------------- #
# Backward compatibility: the default path is byte-identical to the old scan
# --------------------------------------------------------------------------- #
def test_default_path_is_unchanged_on_fixed_input():
    """Pins the unanchored result. If this changes, every existing caller changed."""
    reading, at = _reading([200.0] * 33 + [640.0] * 7)
    step = detect_step(reading, at, before_s=80.0, after_s=35.0)
    assert step is not None
    assert step.as_dict() == {
        "channel_id": "ct_kettle",
        "change_time": "2026-01-01T12:09:09.500000+00:00",
        "before_w": 200.0,
        "after_w": 640.0,
        "delta_w": 440.0,
        "noise_w": 0.0,
        "threshold_w": 5.0,
        "detected": True,
        "before_samples": 5,
        "after_samples": 3,
        "source": "history",
    }


def test_default_path_matches_legacy_explicit_arguments():
    """``min_before_samples=2`` is the old explicit call; it must still work."""
    values = [90.0, 110.0, 90.0, 100.0, 100.0, 106.0]
    reading, at = _reading(values)
    legacy = detect_step(
        reading,
        at,
        before_s=40.0,
        after_s=10.0,
        min_before_samples=2,
        noise_floor_w=1.0,
    )
    assert legacy is not None
    assert legacy.detected
    assert legacy.before_samples == 2


def test_default_noise_floor_and_min_samples_constants():
    assert DEFAULT_MIN_BEFORE_SAMPLES == 4
    assert DEFAULT_NOISE_FLOOR_W == 1.0
    params = inspect.signature(detect_step).parameters
    assert params["min_before_samples"].default == 4
    assert params["noise_floor_w"].default is None  # None -> 1 W floor
    assert params["anchor"].default is None


def test_anchor_none_ignores_the_anchor_arguments_entirely():
    """Passing the anchor kwargs with ``anchor=None`` must change nothing."""
    reading, at = _reading([200.0] * 33 + [640.0] * 7)
    plain = detect_step(reading, at, before_s=80.0, after_s=35.0)
    with_kwargs = detect_step(
        reading,
        at,
        before_s=80.0,
        after_s=35.0,
        anchor=None,
        anchor_before_s=1.0,
        anchor_after_s=1.0,
        anchor_grace_s=1.0,
        cadence_s=15.7,
    )
    assert plain is not None and with_kwargs is not None
    assert plain.as_dict() == with_kwargs.as_dict()

    changes_plain = detect_changes(
        {"c": reading}, at, before_s=80.0, after_s=35.0, previous_values={"c": 0.0}
    )
    changes_kwargs = detect_changes(
        {"c": reading},
        at,
        before_s=80.0,
        after_s=35.0,
        previous_values={"c": 0.0},
        anchor=None,
        anchor_before_s=1.0,
        anchor_after_s=1.0,
        anchor_grace_s=1.0,
    )
    assert changes_plain == changes_kwargs


# --------------------------------------------------------------------------- #
# W1: anchored detection
# --------------------------------------------------------------------------- #
def test_anchor_excludes_far_step():
    """A big step 30 s away must not be reported for a transition 15 s away."""
    reading, at = _reading([100.0] * 4 + [600.0] * 4 + [700.0] * 4)
    anchor = T0 + timedelta(seconds=CADENCE * 8)

    near = detect_step(
        reading,
        at,
        before_s=80.0,
        after_s=35.0,
        anchor=anchor,
        anchor_before_s=20.0,
        anchor_after_s=20.0,
    )
    assert near is not None
    assert near.delta_w == pytest.approx(100.0, abs=1.0)
    assert near.change_time == T0 + timedelta(seconds=CADENCE * 8)
    # The 500 W step at index 4 is outside the anchor window and must be unused.
    assert near.change_time >= anchor - timedelta(seconds=20.0)


def test_anchor_prefers_near_step():
    """The nearest plausible step wins over a much larger distant one."""
    reading, at = _reading([100.0] * 4 + [600.0] * 4 + [700.0] * 4)
    anchor = T0 + timedelta(seconds=CADENCE * 8)

    unanchored = detect_step(reading, at, before_s=80.0, after_s=35.0)
    anchored = detect_step(
        reading,
        at,
        before_s=80.0,
        after_s=35.0,
        anchor=anchor,
        anchor_before_s=140.0,  # includes the distant 500 W candidate
        anchor_after_s=40.0,
    )
    assert unanchored is not None and anchored is not None
    assert unanchored.delta_w > anchored.delta_w  # global scan prefers magnitude
    assert anchored.delta_w == pytest.approx(100.0, abs=1.0)
    assert anchored.change_time == T0 + timedelta(seconds=CADENCE * 8)


def test_anchor_outside_window_returns_none():
    """No candidate near the transition means no step -- not a global scan."""
    reading, at = _reading([200.0] * 33 + [640.0] * 7)
    # Half a cadence past the last sample: no split time lives in this window.
    anchor = at + timedelta(seconds=CADENCE / 2)
    assert (
        detect_step(
            reading,
            at,
            before_s=80.0,
            after_s=35.0,
            anchor=anchor,
            anchor_before_s=1.0,
            anchor_after_s=1.0,
        )
        is None
    )


def test_anchor_window_is_used_not_the_observation_horizon():
    """A step slightly older than ``before+after`` is still valid under an anchor."""
    values = [100.0] * 6 + [400.0] * 6 + [400.0] * 20
    reading, at = _reading(values)
    anchor = T0 + timedelta(seconds=CADENCE * 6)
    # before+after = 15 s is far shorter than the 6*15.7 s to ``at``; the
    # unanchored scan skips this candidate entirely.
    assert detect_step(reading, at, before_s=10.0, after_s=5.0) is not None
    step = detect_step(
        reading,
        at,
        before_s=10.0,
        after_s=5.0,
        anchor=anchor,
        anchor_before_s=10.0,
        anchor_after_s=10.0,
    )
    assert step is not None
    assert step.change_time == anchor
    assert step.delta_w == pytest.approx(300.0)


def test_detect_changes_anchor_omits_channels_with_no_nearby_step():
    values = [200.0] * 33 + [640.0] * 7
    reading, at = _reading(values)
    far_reading = ChannelReading(channel_id="far", value_w=640.0, history=tuple(_series(values)))
    anchor = at + timedelta(seconds=CADENCE / 2)

    changes = detect_changes(
        {"near": reading, "far": far_reading},
        at,
        before_s=80.0,
        after_s=35.0,
        previous_values={"near": 0.0, "far": 0.0},
        anchor=anchor,
        anchor_before_s=1.0,
        anchor_after_s=1.0,
    )
    # No snapshot fallback may manufacture a step for a distant transition.
    assert changes == {}


def test_detect_changes_anchor_keeps_matching_channel():
    values = [200.0] * 33 + [640.0] * 7
    reading, at = _reading(values)
    anchor = T0 + timedelta(seconds=CADENCE * 33)
    changes = detect_changes(
        {"ct_kettle": reading},
        at,
        before_s=80.0,
        after_s=35.0,
        anchor=anchor,
        anchor_before_s=60.0,
        anchor_after_s=35.0,
    )
    assert "ct_kettle" in changes
    assert changes["ct_kettle"].detected
    assert changes["ct_kettle"].delta_w == pytest.approx(440.0)


# --------------------------------------------------------------------------- #
# W1: datetime handling and validation
# --------------------------------------------------------------------------- #
def test_naive_anchor_and_naive_history_are_treated_as_utc():
    naive_start = datetime(2026, 1, 1, 12, 0, 0)
    history = tuple(
        (naive_start + timedelta(seconds=CADENCE * i), 200.0 if i < 6 else 400.0) for i in range(12)
    )
    reading = ChannelReading(channel_id="c", value_w=400.0, history=history)
    at = history[-1][0]
    step = detect_step(
        reading,
        at,
        before_s=60.0,
        after_s=20.0,
        anchor=naive_start + timedelta(seconds=CADENCE * 6),
        anchor_before_s=30.0,
        anchor_after_s=30.0,
    )
    assert step is not None
    assert step.change_time.tzinfo is UTC
    assert step.change_time == datetime(2026, 1, 1, 12, 1, 34, 200000, tzinfo=UTC)


def test_aware_anchor_with_offset_is_normalised_to_utc():
    reading, at = _reading([200.0] * 6 + [400.0] * 6)
    offset_anchor = (T0 + timedelta(seconds=CADENCE * 6)).astimezone(datetime(2026, 1, 1).astimezone().tzinfo)
    step = detect_step(
        reading,
        at,
        before_s=60.0,
        after_s=20.0,
        anchor=offset_anchor,
        anchor_before_s=30.0,
        anchor_after_s=30.0,
    )
    assert step is not None
    assert step.change_time.tzinfo is UTC
    assert step.change_time == T0 + timedelta(seconds=CADENCE * 6)


def test_mixed_naive_and_aware_datetimes_raise_value_error():
    reading, at = _reading([200.0] * 33 + [640.0] * 7)
    with pytest.raises(ValueError, match="timezone-aware"):
        detect_step(
            reading,
            at,
            before_s=80.0,
            after_s=35.0,
            anchor=T0.replace(tzinfo=None),
            anchor_before_s=10.0,
            anchor_after_s=10.0,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"anchor_before_s": -1.0}, "anchor_before_s must be non-negative"),
        ({"anchor_after_s": -0.5}, "anchor_after_s must be non-negative"),
        ({"anchor_grace_s": -2.0}, "anchor_grace_s must be non-negative"),
        ({"anchor_before_s": float("nan")}, "anchor_before_s must be finite"),
        ({"anchor_after_s": float("inf")}, "anchor_after_s must be finite"),
    ],
)
def test_negative_or_non_finite_anchor_windows_raise(kwargs, match):
    reading, at = _reading([200.0] * 33 + [640.0] * 7)
    with pytest.raises(ValueError, match=match):
        detect_step(reading, at, before_s=80.0, after_s=35.0, anchor=T0, **kwargs)


def test_grace_widens_the_anchor_window():
    values = [100.0] * 6 + [400.0] * 6
    reading, at = _reading(values)
    anchor = T0 + timedelta(seconds=CADENCE * 6) + timedelta(seconds=5.0)
    base = dict(
        before_s=40.0,
        after_s=20.0,
        anchor=anchor,
        anchor_before_s=2.0,
        anchor_after_s=2.0,
    )
    assert detect_step(reading, at, **base) is None
    step = detect_step(reading, at, **base, anchor_grace_s=10.0)
    assert step is not None
    assert step.change_time == T0 + timedelta(seconds=CADENCE * 6)


# --------------------------------------------------------------------------- #
# W2: StepTracker duplicate suppression and cadence
# --------------------------------------------------------------------------- #
def _observe(tracker: StepTracker, values: list[float], *, step_s: float = 5.0) -> None:
    for i, value in enumerate(values):
        tracker.observe(
            {"ct": ChannelReading(channel_id="ct", value_w=value)},
            T0 + timedelta(seconds=step_s * i),
        )


def test_tracker_collapses_duplicate_cycles_into_one_point():
    tracker = StepTracker()
    _observe(tracker, [100.0, 100.0, 100.0, 100.0, 250.0, 250.0])

    history = tracker.history("ct")
    assert [value for _, value in history] == [100.0, 250.0]
    # The retained timestamp is the last cycle at which the value was current,
    # not the first: the point keeps moving forward while the reading holds.
    assert history[0][0] == T0 + timedelta(seconds=15.0)
    assert history[1][0] == T0 + timedelta(seconds=25.0)
    assert tracker.previous_value("ct") == pytest.approx(250.0)


def test_tracker_cadence_measures_ct_updates_not_engine_cycles():
    """5 s cycles with 15 s CT updates must report ~15.7 s, not 5 s."""
    tracker = StepTracker()
    for cycle in range(30):
        second = 5.0 * cycle
        value = 100.0 * (int(second // CADENCE) + 1)
        tracker.observe(
            {"ct": ChannelReading(channel_id="ct", value_w=value)},
            T0 + timedelta(seconds=second),
        )
    # Without dedupe the median interval is 5 s and before/after collapse.
    assert tracker.cadence_s() == pytest.approx(CADENCE, abs=1.0)


def test_tracker_keeps_a_real_change_as_a_new_point():
    tracker = StepTracker()
    _observe(tracker, [100.0, 100.0, 100.0, 260.0])
    assert len(tracker.history("ct")) == 2
    assert tracker.previous_values() == {"ct": pytest.approx(260.0)}


def test_tracker_never_rewrites_history_for_out_of_order_cycles():
    tracker = StepTracker()
    _observe(tracker, [100.0, 200.0])
    tracker.observe({"ct": ChannelReading(channel_id="ct", value_w=300.0)}, T0 + timedelta(seconds=-5))
    assert [v for _, v in tracker.history("ct")] == [100.0, 200.0]


def test_tracker_dedupe_does_not_shrink_the_retained_horizon():
    tracker = StepTracker(max_samples=8)
    _observe(tracker, [float(c) for c in range(12)])
    assert len(tracker.history("ct")) == 8
    assert [v for _, v in tracker.history("ct")][-1] == 11.0


# --------------------------------------------------------------------------- #
# W3: baseline sample count and per-channel noise floor
# --------------------------------------------------------------------------- #
def test_two_identical_presamples_are_not_a_baseline():
    """MAD of two identical samples is 0, so a 6 W tick used to be a step."""
    history = tuple(_series([100.0, 100.0, 106.0]))
    reading = ChannelReading(channel_id="ct_shop", value_w=106.0, history=history, resolution_w=1.0)
    at = history[-1][0]

    # Old contract, still available explicitly: 2 pre-samples -> threshold 5.
    legacy = detect_step(reading, at, before_s=40.0, after_s=10.0, min_before_samples=2, noise_floor_w=1.0)
    assert legacy is not None and legacy.detected
    assert legacy.threshold_w == pytest.approx(5.0)

    # New default: there is no fourth sample, so there is no baseline at all.
    assert detect_step(reading, at, before_s=40.0, after_s=10.0) is None


def test_a_flat_stretch_of_four_presamples_is_a_baseline():
    """A resolution tick on a genuinely flat channel stays below threshold."""
    history = tuple(_series([100.0, 100.0, 100.0, 100.0, 101.0]))
    reading = ChannelReading(channel_id="ct_shop", value_w=101.0, history=history, resolution_w=1.0)
    at = history[-1][0]
    step = detect_step(reading, at, before_s=40.0, after_s=10.0)
    assert step is not None
    assert step.before_samples == 4
    assert step.noise_w == pytest.approx(0.0)
    assert step.threshold_w == pytest.approx(5.0)  # min_delta_w, not a tick
    assert step.detected is False


def test_noisy_channel_floor_suppresses_borderline_step():
    """A channel whose quality layer reports 8 W MAD must not report 20 W."""
    history = tuple(_series([100.0] * 10))
    reading = ChannelReading(channel_id="ct_bidet", value_w=120.0, history=history, resolution_w=1.0)
    at = T0 + timedelta(seconds=CADENCE * 10)

    default = detect_step(reading, at, before_s=80.0, after_s=15.7)
    assert default is not None and default.detected
    assert default.threshold_w == pytest.approx(5.0)

    floored = detect_step(reading, at, before_s=80.0, after_s=15.7, noise_floor_w=8.0)
    assert floored is not None
    assert floored.threshold_w == pytest.approx(32.0)  # 4 * 8 W
    assert floored.detected is False


def test_noise_floor_still_respects_min_delta_and_resolution():
    history = tuple(_series([100.0] * 10))
    reading = ChannelReading(channel_id="ct_bidet", value_w=104.0, history=history, resolution_w=6.0)
    at = T0 + timedelta(seconds=CADENCE * 10)
    step = detect_step(reading, at, before_s=80.0, after_s=15.7, noise_floor_w=0.0)
    assert step is not None
    # 4 * 0 = 0, so the resolution (6 W) is the binding floor.
    assert step.threshold_w == pytest.approx(6.0)


def test_per_channel_noise_floor_mapping():
    history = tuple(_series([100.0] * 10))
    at = T0 + timedelta(seconds=CADENCE * 10)
    readings = {
        "quiet": ChannelReading(channel_id="quiet", value_w=120.0, history=history),
        "noisy": ChannelReading(channel_id="noisy", value_w=120.0, history=history),
    }
    changes = detect_changes(
        readings,
        at,
        before_s=80.0,
        after_s=15.7,
        noise_floor_w={"noisy": 8.0},
    )
    assert changes["quiet"].detected is True
    assert changes["noisy"].detected is False
    assert changes["noisy"].threshold_w == pytest.approx(32.0)


# --------------------------------------------------------------------------- #
# StepChange contract
# --------------------------------------------------------------------------- #
def test_anchored_result_is_still_a_stepchange():
    reading, at = _reading([100.0] * 6 + [400.0] * 6)
    step = detect_step(
        reading,
        at,
        before_s=60.0,
        after_s=20.0,
        anchor=T0 + timedelta(seconds=CADENCE * 6),
        anchor_before_s=30.0,
        anchor_after_s=30.0,
    )
    assert isinstance(step, StepChange)
    assert step.source == "history"
    assert step.direction == "increase"
    assert step.z_score == pytest.approx(step.delta_w / step.threshold_w)
