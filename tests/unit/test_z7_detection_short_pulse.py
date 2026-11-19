"""Z7 — P3 reports a short ON pulse once and never re-emits its OFF step.

The HB §17 relocated TV draws ~120 W for 45 s at a 10 s cadence: its OFF
transition lands inside the ON step's SETTLING period.  The detector used to
keep the ON level as its post-step reference after reporting that OFF, so every
later quiet sample looked ~116 W below "baseline" and the same OFF delta was
re-emitted every confirmation period for as long as P2's slow EWMA stayed away
from the stale reference (45 minutes in the HB §17 house) -- one never-closing
event group, re-inferred forever.
"""

from __future__ import annotations

from datetime import timedelta

from wattmosaic_engine.contracts import StepClass

from tests.unit.test_detection import T0, is_class, make_detector, stream, warmup

CADENCE = 10.0


def test_short_on_pulse_yields_one_on_and_one_off_step_then_silence() -> None:
    store, detector, events = make_detector()
    last = warmup(store, detector, "plug", value=3.0, count=90, step_s=CADENCE)
    start = last + timedelta(seconds=CADENCE)
    # 45 s at ~120 W, then 20 minutes back at the 3 W standby.
    pulse = [120.0] * 5
    quiet = [3.0] * 120
    stream(store, detector, "plug", [*pulse, *quiet], start=start, step_s=CADENCE, seq0=1000)

    real = [e for e in events if not is_class(e, StepClass.DATA_GAP)]
    ups = [e for e in real if (e.delta_w or 0.0) > 0.0]
    downs = [e for e in real if (e.delta_w or 0.0) < 0.0]
    assert len(ups) == 1 and abs(ups[0].delta_w - 117.0) < 2.0
    # Exactly one OFF: the quiet 20 minutes after it produce no further step.
    assert len(downs) == 1, [(e.onset_lo, e.step_class, e.delta_w) for e in downs]
    assert abs(downs[0].delta_w + 117.0) < 2.0
    assert downs[0].onset_lo >= T0


def test_periodic_cycle_keeps_both_halves_after_a_reversal() -> None:
    """After a reported reversal the next ON is measured from the new level.

    A stale post-step reference also hid the next ON step at the old ON level
    (it looked like "the same level as before"), so a cycling load lost every
    other transition.
    """
    store, detector, events = make_detector()
    last = warmup(store, detector, "ct", value=50.0, count=90, step_s=CADENCE)
    at = last + timedelta(seconds=CADENCE)
    seq = 1000
    for _cycle in range(3):
        values = [950.0] * 5 + [50.0] * 40  # 50 s ON, ~7 min OFF
        stream(store, detector, "ct", values, start=at, step_s=CADENCE, seq0=seq)
        at += timedelta(seconds=CADENCE * len(values))
        seq += len(values)

    real = [e for e in events if not is_class(e, StepClass.DATA_GAP)]
    ups = [e for e in real if (e.delta_w or 0.0) > 0.0]
    downs = [e for e in real if (e.delta_w or 0.0) < 0.0]
    assert len(ups) == 3, [(e.onset_lo, e.delta_w) for e in ups]
    assert len(downs) == 3, [(e.onset_lo, e.delta_w) for e in downs]
