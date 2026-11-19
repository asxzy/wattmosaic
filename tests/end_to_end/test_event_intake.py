"""Regression tests for the event-intake path.

The passive real-HA run exposed a cluster of defects in the legacy intake path
(``HASource`` -> ``EngineCore.on_event`` -> learning buffer ->
``SampleCollector``).  That path -- the legacy topology, ``on_event``,
``SampleCollector`` and the ``device_events`` table -- was removed with the
contract-3.0.0 cutover; the v3 intake (``on_ha_event`` -> ``ha_events_v3`` /
event evidence) is covered by the realtime and three-loop runtime suites.

What remains here is the one guarantee that still lives on a shipped type: an
:class:`AlignedWindow` can carry state transitions to inference.

Nothing here talks to Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, datetime


# --------------------------------------------------------------------------- #
# Defect 3: state transitions never reached the disaggregation engine
# --------------------------------------------------------------------------- #
def test_aligned_window_can_carry_transitions() -> None:
    """An ``AlignedWindow`` without transitions disables event inference.

    ``_collect_transitions`` reads ``window.state_transitions`` and falls back
    to ``window.previous_states``.  ``AlignedWindow`` carried neither, so
    ``coerce_window`` produced empty lists and every cycle reported zero events:
    state-transition inference was dead code in production even though the
    engine's own tests exercised it with hand-built windows.
    """
    from wattmosaic_engine.synchronization.aligner import AlignedWindow

    window = AlignedWindow(
        at=datetime(2026, 1, 1, tzinfo=UTC),
        max_age_s=120.0,
        channels={},
        previous_states={"dev_water_heater": "off"},
        state_transitions=(
            {
                "device_id": "dev_water_heater",
                "previous_state": "off",
                "new_state": "on",
                "occurred_at": None,
            },
        ),
    )
    assert window.previous_states == {"dev_water_heater": "off"}
    assert len(window.state_transitions) == 1
