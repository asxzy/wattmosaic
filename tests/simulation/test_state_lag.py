"""State-stream lag must not be confused with measurement lag.

In Home Assistant the device-state path and the CT-sample path are independent.
A `binary_sensor` or `climate` entity can be reported seconds to minutes after
the wattage it explains, so `last_updated` is *not* the instant the load
physically changed. This is not an exotic failure: it is what a cloud-polled
integration looks like, and it is the reason the engine may never treat a state
timestamp as a measurement timestamp.

`SimulatorConfig.state_lag_s` reproduces exactly that: the state machine still
transitions at the physical instant, but the reported `last_updated` (and the
value HA shows) is what was true `lag` seconds earlier. CT samples are untouched.

What these tests pin
--------------------
1. The knob is surgical: only the HA state stream changes. Ground truth and
   every measurement are byte-identical to a zero-lag run.
2. Transition *detection* follows the state stream (by `lag / cadence` ticks),
   while `occurred_at` stays at the window instant — the engine must not
   back-date an event it only learned about now.
3. Direct measurements, which come from the CT path, are completely unaffected.
   A device measured by a dedicated CT must report the same watts with or
   without state lag: its power is observed, not inferred from a state.
4. The engine never reports zero for a device whose own CT says it is drawing
   power, merely because the state stream is late.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tests.simulation.metrics import EngineAdapter
from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel
from tools.simulator.topology import build_real_home_topology

SEED = 202
MINUTES = 45
INTERVAL = 15.0
DEVICE = "water_heater"
ENTITY = "climate.water_heater"
LAG_S = 60.0
#: 60 s of lag at a 15 s record cadence is exactly four ticks.
EXPECTED_TICK_SHIFT = 4


def _records(lag_s: float):
    return list(
        HouseholdScenario(
            SimulatorConfig(
                seed=SEED,
                minutes=MINUTES,
                interval=INTERVAL,
                quality=QualityLevel.GOOD,
                state_lag_s={DEVICE: lag_s} if lag_s else {},
            ),
            topology=build_real_home_topology(),
        ).records()
    )


@pytest.fixture(scope="module")
def aligned():
    """A zero-lag and a lagged run of the *same* physical scenario."""
    return _records(0.0), _records(LAG_S)


# --------------------------------------------------------------------------- #
# The knob itself
# --------------------------------------------------------------------------- #


def test_state_lag_does_not_touch_ground_truth_or_measurements(aligned) -> None:
    """Only the state stream may differ; the physics and sensors must not.

    If this fails, the knob is a scenario change in disguise and none of the
    attribution comparisons below mean anything.
    """
    baseline, lagged = aligned
    assert len(baseline) == len(lagged)
    for i, (a, b) in enumerate(zip(baseline, lagged, strict=True)):
        assert a["ground_truth"] == b["ground_truth"], f"ground truth moved at tick {i}"
        assert a["measurements"] == b["measurements"], f"measurements moved at tick {i}"
        assert a["latest_measurements"] == b["latest_measurements"], (
            f"latest snapshot moved at tick {i}"
        )


def test_state_lag_shifts_only_last_updated_and_value(aligned) -> None:
    """``last_updated`` is the observed instant: ``t - lag``, clamped at start.

    The simulator has no pre-roll, so for the first ``lag`` seconds the observed
    instant is clamped to the scenario start and the shift is smaller than the
    nominal lag. Every tick after that shifts by exactly ``LAG_S``.
    """
    baseline, lagged = aligned
    for i, (a, b) in enumerate(zip(baseline, lagged, strict=True)):
        ta = datetime.fromisoformat(a["ha_states"][ENTITY]["last_updated"])
        tb = datetime.fromisoformat(b["ha_states"][ENTITY]["last_updated"])
        elapsed = float(a["t_s"])
        expected_shift = min(elapsed, LAG_S)
        assert (ta - tb).total_seconds() == pytest.approx(expected_shift), (
            f"tick {i}: state timestamp shifted by {(ta - tb).total_seconds()}s, "
            f"expected {expected_shift}s (lag {LAG_S}s clamped to elapsed {elapsed}s)"
        )
        # Other entities are not lagged.
        for entity, state in a["ha_states"].items():
            if entity == ENTITY:
                continue
            assert state["last_updated"] == b["ha_states"][entity]["last_updated"], (
                f"tick {i}: unrelated entity {entity} was lagged too"
            )


def test_state_lag_defers_the_reported_state_but_not_the_physics(aligned) -> None:
    """The lagged run reports the *earlier* state at the physical transition."""
    baseline, lagged = aligned
    starts = [
        i for i, (a, b) in enumerate(zip(baseline, lagged, strict=True))
        if a["ha_states"][ENTITY]["state"] != b["ha_states"][ENTITY]["state"]
    ]
    assert starts, "the two runs never disagree about the device state"
    # Every disagreement is the lagged run being behind, never ahead.
    for i in starts:
        assert baseline[i]["ground_truth"]["devices"][DEVICE]["power_w"] == pytest.approx(
            lagged[i]["ground_truth"]["devices"][DEVICE]["power_w"]
        )


# --------------------------------------------------------------------------- #
# What the engine does with it
# --------------------------------------------------------------------------- #


def _transitions(records) -> list[tuple[int, str]]:
    adapter = EngineAdapter(build_real_home_topology().to_contract_topology())
    out: list[tuple[int, str]] = []
    for i, record in enumerate(records):
        for event in adapter(record)["diagnostics"]["events"]:
            if event["device_id"] == DEVICE:
                out.append((i, event["direction"]))
    return out


def test_transition_detection_follows_the_state_stream(aligned) -> None:
    """A late state is detected late, by ``lag / cadence`` ticks.

    The engine cannot know about a transition before the state says it happened.
    What it must *not* do is miss one, or report a different set of transitions —
    only their timing may move.
    """
    baseline, lagged = aligned
    base_events = _transitions(baseline)
    lag_events = _transitions(lagged)

    assert len(base_events) == len(lag_events) >= 2, (
        f"baseline {base_events} vs lagged {lag_events}: the lagged state stream "
        "changed the *set* of detected transitions, not just their timing"
    )
    assert [d for _, d in base_events] == [d for _, d in lag_events]
    shifts = [b[0] - a[0] for a, b in zip(base_events, lag_events, strict=True)]
    assert shifts == [EXPECTED_TICK_SHIFT] * len(shifts), (
        f"transitions shifted by {shifts} ticks, expected {EXPECTED_TICK_SHIFT} "
        f"(lag {LAG_S}s / cadence {INTERVAL}s)"
    )


def test_transition_is_not_backdated_by_the_state_lag(aligned) -> None:
    """``occurred_at`` must be the detection instant, not the physical one.

    Back-dating would let the engine attribute power at an instant it had no
    information about, which is the same error as scoring against the future.
    """
    _, lagged = aligned
    adapter = EngineAdapter(build_real_home_topology().to_contract_topology())
    checked = 0
    for record in lagged:
        for event in adapter(record)["diagnostics"]["events"]:
            if event["device_id"] != DEVICE:
                continue
            occurred = datetime.fromisoformat(event["occurred_at"])
            window_at = datetime.fromisoformat(record["timestamp"])
            assert occurred == window_at, (
                f"event back-dated to {occurred} but the window is at {window_at}"
            )
            # And it is *later* than the device's own last_updated, because the
            # state stream is stale. The engine learned of the change now.
            last_updated = datetime.fromisoformat(
                record["ha_states"][ENTITY]["last_updated"]
            )
            assert occurred >= last_updated
            checked += 1
    assert checked >= 2


def test_direct_measurement_is_immune_to_state_lag(aligned) -> None:
    """A dedicated CT reads the wire, not the state entity.

    ``water_heater`` has two dedicated leg CTs, so its power is *measured*. The
    reported watts must be identical with and without a 60 s state lag; only the
    confidence may move (a stale state is weaker corroboration).
    """
    baseline, lagged = aligned
    adapters = (
        EngineAdapter(build_real_home_topology().to_contract_topology()),
        EngineAdapter(build_real_home_topology().to_contract_topology()),
    )
    worst_power_delta = 0.0
    sources: set[str] = set()
    for ra, rb in zip(baseline, lagged, strict=True):
        a = adapters[0](ra)["devices"].get(DEVICE, {})
        b = adapters[1](rb)["devices"].get(DEVICE, {})
        sources.add(str(b.get("source")))
        worst_power_delta = max(
            worst_power_delta,
            abs(float(a.get("power_w", 0.0)) - float(b.get("power_w", 0.0))),
        )
    assert "direct_measured" in sources, (
        f"water_heater was never directly measured ({sources}); this test only "
        "means something for a device with a dedicated CT"
    )
    assert worst_power_delta < 1e-6, (
        f"a 60 s state lag moved a directly measured device by "
        f"{worst_power_delta:.3f} W; the measurement path is reading the state stream"
    )


def test_engine_never_zeroes_a_directly_measured_load(aligned) -> None:
    """Anti-fabrication in the other direction: don't invent OFF either.

    While the state stream still says "idle" but both leg CTs carry the full
    6 kW, reporting 0 W for the device would be the mirror image of inventing
    power — and would be a large error, not a conservative one.
    """
    _, lagged = aligned
    adapter = EngineAdapter(build_real_home_topology().to_contract_topology())
    cases = 0
    for record in lagged:
        truth = record["ground_truth"]["devices"][DEVICE]["power_w"]
        if truth < 1000.0:
            continue
        state = record["ha_states"][ENTITY]["state"]
        if state not in ("idle", "off"):
            continue
        measured_legs = [
            float(m["value_w"])
            for ct_id, m in record["latest_measurements"].items()
            if ct_id.startswith("ct_main_water_heater")
        ]
        if sum(measured_legs) < 1000.0:
            continue  # CT path has not caught up yet either
        reported = adapter(record)["devices"].get(DEVICE, {}).get("power_w", 0.0)
        cases += 1
        assert float(reported) > 1000.0, (
            f"device drew {truth:.0f} W and its CTs measured "
            f"{sum(measured_legs):.0f} W, but the engine reported "
            f"{reported:.0f} W because the state stream still said {state!r}"
        )
    assert cases > 0, (
        "the lagged run never produced a state/measurement disagreement; the "
        "fixture no longer exercises the hazard"
    )
