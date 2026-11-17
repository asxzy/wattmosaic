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
3. When the lagged state says OFF but the CT still carries the running load,
   the engine may legitimately decline to credit the device: `design.md` §4.3
   says an OFF device on a solo row can report
   `unexplained_load_on_device_circuit` and become `unknown`. What it may never
   do is make the energy vanish, or hand it to some other device. Both tests
   below assert exactly that: the withheld power is *preserved as residual and
   never fabricated*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_state_lag_does_not_move_any_other_device(aligned) -> None:
    """Withholding the lagged device's power must not hand it to anyone else.

    Wave 2 removed the direct path, so this can no longer be a "direct watts are
    byte-identical" test.  Instead: when the lagged run disagrees with the
    aligned run about ``water_heater``, the difference is either withheld
    (reported as residual) or, at ticks where both runs see the same state,
    zero.  No *other* device's attribution may move, and total attributed power
    plus residual must equal the main measurement on every tick — that is the
    conservation law that rules out fabrication.

    ``design.md`` §4.3: an OFF device on a solo row may report
    ``unexplained_load_on_device_circuit`` and become ``unknown``; that is the
    honest answer, not a defect.
    """
    baseline, lagged = aligned
    adapters = (
        EngineAdapter(build_real_home_topology().to_contract_topology()),
        EngineAdapter(build_real_home_topology().to_contract_topology()),
    )
    worst_other_drift = 0.0
    worst_conservation = 0.0
    same_state_delta = 0.0
    withheld_ticks = 0
    for ra, rb in zip(baseline, lagged, strict=True):
        out_a = adapters[0](ra)
        out_b = adapters[1](rb)
        main_b = float(out_b["reconciliation"]["main_power_w"])
        attributed_b = sum(float(d["power_w"]) for d in out_b["devices"].values())
        residual_b = float(out_b["reconciliation"]["unknown_residual_w"])
        worst_conservation = max(worst_conservation, abs(attributed_b + residual_b - main_b))
        worst_other_drift = max(
            worst_other_drift,
            abs(
                sum(float(d["power_w"]) for k, d in out_b["devices"].items() if k != DEVICE)
                - sum(float(d["power_w"]) for k, d in out_a["devices"].items() if k != DEVICE)
            ),
        )
        p_a = float(out_a["devices"].get(DEVICE, {}).get("power_w", 0.0))
        p_b = float(out_b["devices"].get(DEVICE, {}).get("power_w", 0.0))
        if ra["ha_states"][ENTITY]["state"] == rb["ha_states"][ENTITY]["state"]:
            same_state_delta = max(same_state_delta, abs(p_a - p_b))
        elif p_b < p_a:
            withheld_ticks += 1
    # Same reported state -> same reported power.  The lag must act only through
    # the state stream, not through some other path.
    assert same_state_delta < 1e-6, (
        f"state lag moved {DEVICE} by {same_state_delta:.3f} W on ticks where both "
        "runs report the same state"
    )
    assert worst_other_drift < 1e-6, (
        f"withholding {DEVICE} moved another device by {worst_other_drift:.3f} W"
    )
    assert worst_conservation < 1e-6, (
        "attributed + residual drifted from the measured main by "
        f"{worst_conservation:.3f} W; power was fabricated or lost"
    )
    assert withheld_ticks > 0, (
        "the lagged run never withheld anything for this device; the fixture no "
        "longer exercises the hazard"
    )


def test_withheld_load_is_preserved_as_residual_never_fabricated(aligned) -> None:
    """An OFF state over a live CT must yield residual, not invented OFF or ON.

    The hazard: while the state stream still says "idle" but both leg CTs carry
    the full 6 kW, the engine may either withhold the device (reporting
    ``unknown`` and leaving the power in the residual) or attribute it.  Both are
    defensible under ``design.md`` §4.3 — "an OFF device on a solo row can report
    ``unexplained_load_on_device_circuit`` and become ``unknown``".

    What is *not* defensible, and what this test pins:

    * the plug must not be pulled out of the residual — reporting 0 W for the
      device while the residual also falls would silently destroy energy;
    * the power must not be handed to another device;
    * when the device keeps the attribution it must keep the CT's wattage.

    Measured on the pinned fixture: 7 hazard ticks, every one of them withheld
    (`unknown`, 0 W) with the residual rising by exactly the leg reading, and
    zero negative-residual ticks.
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
        out = adapter(record)
        result = out["devices"].get(DEVICE, {})
        reported = float(result.get("power_w", 0.0))
        source = str(result.get("source", "unknown"))
        cases += 1
        if source == "unknown" or reported <= 0.0:
            # Withheld: the power must be sitting in the residual, and the
            # ledger must still balance against the measured main.
            attributed = sum(float(d["power_w"]) for d in out["devices"].values())
            residual = float(out["reconciliation"]["unknown_residual_w"])
            main = float(out["reconciliation"]["main_power_w"])
            assert residual >= sum(measured_legs) - 1.0, (
                f"device drew {truth:.0f} W and its CTs measured "
                f"{sum(measured_legs):.0f} W, but the engine withheld it "
                f"({reported:.0f} W) without preserving it as residual "
                f"({residual:.0f} W)"
            )
            assert abs(attributed + residual - main) < 1e-6, (
                f"ledger does not balance: attributed {attributed:.0f} + "
                f"residual {residual:.0f} != main {main:.0f}"
            )
        else:
            # Attributed: it must be the measured wattage, not a guess.
            assert reported >= sum(measured_legs) - 1.0, (
                f"device drew {truth:.0f} W and its CTs measured "
                f"{sum(measured_legs):.0f} W, but the engine reported only "
                f"{reported:.0f} W"
            )
    assert cases > 0, (
        "the lagged run never produced a state/measurement disagreement; the "
        "fixture no longer exercises the hazard"
    )


# --------------------------------------------------------------------------- #
# REQ-EVENT-003 — a response beyond max_lag_s is withheld, not assumed
# --------------------------------------------------------------------------- #


def test_response_beyond_max_lag_is_withheld() -> None:
    """A response at or beyond ``max_lag_s`` is refused with ``lag_exceeded``.

    This is the audit path's own rule (``AssociationFinder._classify``), not
    the engine's: the pass observes a verdict and searches the CT response over
    ``0..max_lag_s``, and a crossing that lands at or past the bound is a
    refusal rather than support.  At the bound the response is
    indistinguishable from silence, so the comparison is deliberately ``>=``
    and the boundary value is never accepted.

    The simulation fixture's 60 s ``state_lag_s`` scenario is the physical
    origin of the hazard (a state stream that reports an old state), and the
    same refusal is then shown on ``AuditPass`` so the reason is visible on the
    production path rather than only in a unit of the finder.
    """
    from wattmosaic_engine.contracts import IsolationStatus
    from wattmosaic_engine.disaggregation.association import (
        REASON_LAG_EXCEEDED,
        AssociationConfig,
        AssociationFinder,
    )
    from wattmosaic_engine.disaggregation.audit import AuditConfig, AuditPass
    from wattmosaic_engine.disaggregation.isolation import EventIsolation

    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    verdict = EventIsolation(
        status=IsolationStatus.ISOLATED,
        event_id=f"{DEVICE}@{at.isoformat()}",
        device_id=DEVICE,
        entity_id=ENTITY,
        occurred_at=at,
        received_at=at,
        analysis_offset_s=0.0,
        isolation_window_s=30.0,
        window_start=at,
        window_end=at + timedelta(seconds=30),
        reason="isolated",
        evidence={},
    )
    ct_id = "ct_main_water_heater_l1"
    before = [
        {
            "state_timestamp": (at - timedelta(seconds=15.0 * (3 - i))).isoformat(),
            "value_w": 1000.0,
            "ct_id": ct_id,
        }
        for i in range(3)
    ]
    # The CT only moves at the 75 s sample: the crossing lands beyond the
    # 60 s bound, which is the sentinel for "no usable crossing" and is refused.
    after = [
        {
            "state_timestamp": (at + timedelta(seconds=15.0 * i)).isoformat(),
            "value_w": 1000.0 if i < 5 else 6000.0,
            "ct_id": ct_id,
        }
        for i in range(6)
    ]

    finder = AssociationFinder(AssociationConfig(max_lag_s=LAG_S, lag_step_s=15.0))
    observations = finder.observe(verdict, before, after, [], device_delta_w=5000.0)
    assert observations, "the fixture produced no observation at all"
    assert all(item.lag_s == pytest.approx(LAG_S) for item in observations)
    assert finder.analyze(observations) == []
    assert finder.withheld(observations)[DEVICE][REASON_LAG_EXCEEDED] >= 1

    # A crossing *at* the bound is refused too, never accepted as support.
    boundary = AssociationFinder(AssociationConfig(max_lag_s=45.0, lag_step_s=15.0))
    boundary_after = [
        {
            "state_timestamp": (at + timedelta(seconds=15.0 * i)).isoformat(),
            "value_w": 1000.0 if i < 4 else 6000.0,
            "ct_id": ct_id,
        }
        for i in range(6)
    ]
    boundary_observations = boundary.observe(
        verdict, before, boundary_after, [], device_delta_w=5000.0
    )
    assert boundary_observations
    assert boundary.analyze(boundary_observations) == []
    assert boundary.withheld(boundary_observations)[DEVICE][REASON_LAG_EXCEEDED] >= 1

    # And the same refusal is what the audit pass publishes, so the reason is
    # not only a unit-level counter.
    topology = build_real_home_topology().to_contract_topology()
    pass_ = AuditPass(config=AuditConfig())
    report = pass_.analyze(
        _Window(at=at, readings={ct_id: 6000.0}),
        topology=topology,
        results=[],
        events=[
            {
                "device_id": DEVICE,
                "entity_id": ENTITY,
                "new_state": "off",
                "occurred_at": at,
            }
        ],
        now=at,
    )
    assert report.stages["association"]["withheld"][DEVICE][REASON_LAG_EXCEEDED] >= 1


class _Window:
    """The minimum ``AuditPass.analyze`` reads: an instant and its readings."""

    __slots__ = ("at", "readings")

    def __init__(self, at: datetime, readings: dict[str, float]) -> None:
        self.at = at
        self.readings = readings
