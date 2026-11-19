"""Z10 — a fall inside the ON step's settling window is reported promptly.

After Z7's fix an OFF transition that lands inside the ON step's SETTLING
period was still only detected once SETTLING expired (~120 s later), and its
onset was dated from the settling window rather than the real fall.  These
tests pin the prompt, correctly-dated reversal detection and keep Z7's
single-emission guarantee (no phantom re-emission) as the regression guard.
"""

from __future__ import annotations

import random
from datetime import timedelta

from wattmosaic_engine.contracts import StepClass

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
