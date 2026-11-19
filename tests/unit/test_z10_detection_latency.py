"""Z10 — a fall inside the ON step's settling window is reported promptly.

After Z7's fix an OFF transition that lands inside the ON step's SETTLING
period was still only detected once SETTLING expired (~120 s later), and its
onset was dated from the settling window rather than the real fall.  These
tests pin the prompt, correctly-dated reversal detection and keep Z7's
single-emission guarantee (no phantom re-emission) as the regression guard.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta

from wattmosaic_engine.contracts import StepClass
from wattmosaic_engine.realtime.detection import DetectorConfig

from tests.unit.test_detection import is_class, make_detector, stream, warmup

CADENCE = 10.0
PULSE_W = 116.0
STANDBY_W = 3.0
CONFIRM_SAMPLES = 2  # DetectorConfig.confirm_samples default


def _ups(events):
    return [
        e
        for e in events
        if not is_class(e, StepClass.DATA_GAP) and (e.delta_w or 0.0) > 0.0
    ]


def _downs(events):
    return [
        e
        for e in events
        if not is_class(e, StepClass.DATA_GAP) and (e.delta_w or 0.0) < 0.0
    ]


def test_off_fall_inside_settling_is_prompt_and_correctly_dated() -> None:
    """A 45 s ON pulse sampled every 10 s: OFF emitted within 3 cadences.

    The ON step confirms on the 2nd elevated sample; the fall (at ~45 s)
    therefore lands well inside the ON step's 120 s settling window.  The
    detector must notice the reversal there, not ~2 min later.
    """
    store, detector, events = make_detector()
    last = warmup(store, detector, "plug", value=STANDBY_W, count=90, step_s=CADENCE)
    pulse_start = last + timedelta(seconds=CADENCE)
    # 5 ON samples (t=0..40 s), then a long return to standby: the physical
    # fall happens at ~45 s, between the last ON sample and the first standby.
    pulse = [PULSE_W] * 5
    quiet = [STANDBY_W] * 120
    stream(
        store, detector, "plug", [*pulse, *quiet], start=pulse_start, step_s=CADENCE, seq0=1000
    )
    real_fall = pulse_start + timedelta(seconds=45.0)

    ups, downs = _ups(events), _downs(events)
    assert len(ups) == 1, [(e.onset_lo, e.delta_w) for e in ups]
    assert abs(ups[0].delta_w - (PULSE_W - STANDBY_W)) < 2.0

    assert len(downs) == 1, [(e.onset_lo, e.delta_w) for e in downs]
    off = downs[0]
    assert abs(off.delta_w + (PULSE_W - STANDBY_W)) < 2.0
    # Prompt: within (confirmation samples + 1) * cadence of the real fall.
    assert off.detected_at - real_fall <= timedelta(
        seconds=(CONFIRM_SAMPLES + 1) * CADENCE
    ), (off.detected_at, real_fall)
    # Onset is dated from the fall, not the settling window.
    assert off.onset_lo <= real_fall <= off.onset_hi, (off.onset_lo, real_fall, off.onset_hi)
    assert off.onset_hi - off.onset_lo <= timedelta(seconds=CADENCE)

    # No duplicate emission / no phantom OFF from the quiet tail.
    assert len({e.event_id for e in events}) == len(events)


def test_pulse_shorter_than_confirmation_never_double_emits_off() -> None:
    """A 20 s pulse (2 ON samples at 10 s cadence).

    Documented behaviour: a TRANSIENT pair or a single TRANSIENT/sustained
    pair is acceptable; what is forbidden is two phantom OFF steps (Z7).
    """
    store, detector, events = make_detector()
    last = warmup(store, detector, "plug", value=STANDBY_W, count=90, step_s=CADENCE)
    pulse_start = last + timedelta(seconds=CADENCE)
    stream(
        store,
        detector,
        "plug",
        [PULSE_W] * 2 + [STANDBY_W] * 120,
        start=pulse_start,
        step_s=CADENCE,
        seq0=1000,
    )

    ups, downs = _ups(events), _downs(events)
    # Never two phantom OFFs, and never two ONs for the one pulse.
    assert len(downs) <= 1, [(e.onset_lo, e.step_class, e.delta_w) for e in downs]
    assert len(ups) <= 1, [(e.onset_lo, e.step_class, e.delta_w) for e in ups]
    assert len({e.event_id for e in events}) == len(events)
    # Each emitted step has a real magnitude (no fabricated 0 W sentinel).
    for event in events:
        if not is_class(event, StepClass.DATA_GAP):
            assert event.delta_w is not None and abs(event.delta_w) > 0.0


def test_cycling_load_keeps_every_step() -> None:
    """A repeated ON/OFF cycle keeps all steps with correct signs/deltas."""
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct", value=50.0, count=90, step_s=CADENCE)
    at = last + timedelta(seconds=CADENCE)
    seq = 1000
    for _cycle in range(4):
        values = [950.0] * 5 + [50.0] * 40
        stream(store, detector, "ct", values, start=at, step_s=CADENCE, seq0=seq)
        at += timedelta(seconds=CADENCE * len(values))
        seq += len(values)

    ups, downs = _ups(events), _downs(events)
    assert len(ups) == 4, [(e.onset_lo, e.delta_w) for e in ups]
    assert len(downs) == 4, [(e.onset_lo, e.delta_w) for e in downs]
    for up in ups:
        assert abs(up.delta_w - 900.0) < 2.0
    for down in downs:
        assert abs(down.delta_w + 900.0) < 2.0


def test_noisy_steady_load_emits_nothing() -> None:
    """Gaussian noise around a fixed level produces no step events."""
    rng = random.Random(20261119)
    store, detector, events = make_detector()
    values = [50.0 + rng.gauss(0.0, 1.0) for _ in range(240)]
    stream(store, detector, "ct", values, step_s=CADENCE)
    steps = [e for e in events if not is_class(e, StepClass.DATA_GAP)]
    assert steps == [], [(e.step_class, e.onset_lo, e.delta_w) for e in steps]


# --------------------------------------------------------------------------- #
# Z10b — a settling *decay* is not an OFF (Z10 tightened)
# --------------------------------------------------------------------------- #


def test_inrush_decay_is_not_an_off() -> None:
    """A motor's 0 -> 1500 W inrush decaying to 300 W running is one ON.

    The inrush peak is a transient above the settled running level; the fall
    from 1500 to 300 moves toward the pre-step baseline by >1000 W but is the
    motor settling, not an OFF.  With the robust settled level as the reference,
    300 is far enough from the 0 W baseline that the reversal must not fire.
    """
    store, detector, events = make_detector()
    last = warmup(store, detector, "motor", value=0.0, count=90, step_s=CADENCE)
    start = last + timedelta(seconds=CADENCE)
    # Several inrush samples, then a long run at the settled running level.
    values = [0.0] * 20 + [1500.0] * 5 + [300.0] * 60
    stream(store, detector, "motor", values, start=start, step_s=CADENCE, seq0=1000)

    ups, downs = _ups(events), _downs(events)
    assert len(ups) == 1, [(e.onset_lo, e.delta_w) for e in ups]
    # The ON amplitude is either the settled +300 W or the detector's documented
    # confirmed-step amplitude (+1500 W when the peak itself is confirmed); both
    # are acceptable.  What matters is that the decay emits no OFF.
    assert abs(ups[0].delta_w - 300.0) < 2.0 or abs(ups[0].delta_w - 1500.0) < 2.0
    assert not downs, [(e.onset_lo, e.delta_w) for e in downs]
    assert len({e.event_id for e in events}) == len(events)


def test_inrush_peak_never_becomes_the_post_step_reference() -> None:
    """The reversal reference is the settled level, never the inrush peak.

    With ``return_frac`` small (0.01), only a return to within 1 % of the
    pre-step level counts as an OFF.  A decay from the 1500 W inrush peak to
    the 300 W running level is 300 W above the 0 W baseline and must stay
    inside settling even when the whole 0->1500 transition was confirmed.
    """
    store, detector, events = make_detector(config=DetectorConfig(return_frac=0.01))
    last = warmup(store, detector, "motor", value=0.0, count=90, step_s=CADENCE)
    start = last + timedelta(seconds=CADENCE)
    values = [0.0] * 20 + [1500.0] * 5 + [300.0] * 60
    stream(store, detector, "motor", values, start=start, step_s=CADENCE, seq0=1000)

    downs = _downs(events)
    assert not downs, [(e.onset_lo, e.delta_w) for e in downs]


def test_oscillating_load_during_settling_emits_no_extra_steps() -> None:
    """A dimmed/variable load swinging +/-30 % during settling stays one ON.

    Every swing is a > max(enter_z*sigma, min_delta_w) move toward the pre-step
    level, but none returns to that level, so no swing is an OFF and Z10b emits
    no step per swing.
    """
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct", value=100.0, count=90, step_s=CADENCE)
    start = last + timedelta(seconds=CADENCE)
    values = [100.0] * 10
    # Keep every swing inside the 120 s SETTLING window (12 samples at 10 s).
    for i in range(12):
        values.append(1000.0 * (1.0 + 0.3 * math.sin(i)))
    stream(store, detector, "ct", values, start=start, step_s=CADENCE, seq0=1000)

    # No sample returns to within 20 % of the 100 W pre-step baseline, so no
    # swing is an OFF and only the original ON is emitted.
    steps = [e for e in events if not is_class(e, StepClass.DATA_GAP)]
    assert len(steps) == 1, [(e.onset_lo, e.delta_w) for e in steps]
    assert len(_downs(events)) == 0
    assert len({e.event_id for e in events}) == len(events)


def test_return_to_pre_level_still_emits_off_with_true_onset() -> None:
    """A genuine OFF returns to the pre-step level and is still prompt/dated."""
    store, detector, events = make_detector()
    last = warmup(store, detector, "plug", value=STANDBY_W, count=90, step_s=CADENCE)
    pulse_start = last + timedelta(seconds=CADENCE)
    pulse = [PULSE_W] * 5
    quiet = [STANDBY_W] * 120
    stream(
        store, detector, "plug", [*pulse, *quiet], start=pulse_start, step_s=CADENCE, seq0=1000
    )
    real_fall = pulse_start + timedelta(seconds=45.0)

    ups, downs = _ups(events), _downs(events)
    assert len(ups) == 1 and len(downs) == 1
    off = downs[0]
    assert off.onset_lo <= real_fall <= off.onset_hi, (off.onset_lo, real_fall, off.onset_hi)
    assert off.detected_at - real_fall <= timedelta(seconds=(CONFIRM_SAMPLES + 1) * CADENCE)
