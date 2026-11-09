"""Correlating a switch with the CT row it sits on (ADR-0024).

Every case here is built from an *observation*: a switch transition, the row
watts immediately before and after, and (for a dimmer) the output level that was
reported.  Ground truth is chosen first and the observations are derived from it,
so a passing test means the inference recovered the load that was constructed --
not that it matched a fixture written to satisfy it.

The refusal cases matter more than the happy path.  A correlation that is wrong
creates a device and attributes real household power to the wrong appliance, and
nothing downstream can tell.  So each guard is exercised by constructing the
situation that should defeat it and asserting that *no* candidate is produced,
with a reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceTopology,
    HaConnectivity,
    MonitoringLevel,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.auto_placement import (
    auto_device_id,
    merge_placements,
    plan_placements,
)
from wattmosaic_engine.ingestion.correlation import (
    CorrelationReport,
    CtCandidate,
    EntityObservation,
    correlate,
    estimate_row_noise,
    level_from_attributes,
    observations_from_history,
    parse_history_payload,
)

T0 = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)

#: The measured basement baseline: CT1 carries this with every fixture off.
BASELINE_W = 8.35
#: Measured steps, from the live switching experiment.
BAR_POTS_W = 19.57
LAUNDRY_W = 13.39
#: Measured quiet-period jitter on CT1.
CT1_NOISE_W = 0.039


def _cycle(
    entity_id: str,
    *,
    watts: float,
    minute: float,
    level: float | None = None,
    baseline: float = BASELINE_W,
    ct_id: str = "ct1",
) -> list[EntityObservation]:
    """One on/off cycle: the row rises by ``watts`` and returns to baseline."""
    at = T0 + timedelta(minutes=minute)
    off_row = {ct_id: baseline}
    on_row = {ct_id: baseline + watts}
    return [
        EntityObservation(entity_id, at, True, off_row, on_row, level=level),
        EntityObservation(
            entity_id,
            at + timedelta(minutes=1),
            False,
            on_row,
            off_row,
            previous_level=level,
        ),
    ]


def _correlate(observations, **kwargs):
    kwargs.setdefault("now", T0 + timedelta(hours=4))
    kwargs.setdefault("row_noise_w", {"ct1": CT1_NOISE_W})
    return correlate(observations, **kwargs)


# --------------------------------------------------------------------------- #
# Happy path -- real measured values
# --------------------------------------------------------------------------- #


def test_two_fixtures_on_one_row_are_both_placed_with_measured_watts() -> None:
    """Bar pots and laundry lights share CT1; both steps are recovered."""
    observations = []
    for index in range(3):
        observations += _cycle(
            "light.basement_basement_bar_pot_lights",
            watts=BAR_POTS_W,
            minute=10 * index,
        )
        observations += _cycle(
            "light.basement_laundry_basement_laundry_main_lights",
            watts=LAUNDRY_W,
            minute=10 * index + 3,
        )

    report = _correlate(observations)
    by_entity = {candidate.entity_id: candidate for candidate in report.candidates}

    assert len(report.candidates) == 2
    bar = by_entity["light.basement_basement_bar_pot_lights"]
    laundry = by_entity["light.basement_laundry_basement_laundry_main_lights"]
    assert bar.ct_id == "ct1"
    assert laundry.ct_id == "ct1"
    assert bar.corroborations == 3
    assert bar.agreement == 1.0
    # The measured step, not the row total: the baseline is never attributed.
    assert bar.typical_w == pytest.approx(BAR_POTS_W, abs=0.01)
    assert laundry.typical_w == pytest.approx(LAUNDRY_W, abs=0.01)
    assert bar.is_dimmable is False
    assert bar.dimming_curve == ()


def test_a_single_ct_load_needs_no_siblings_to_be_placed() -> None:
    """A fixture alone on its row is the same evidence as one of several."""
    observations = []
    for index in range(3):
        observations += _cycle("switch.pump", watts=850.0, minute=10 * index)

    report = _correlate(observations)
    assert [c.entity_id for c in report.candidates] == ["switch.pump"]
    assert report.candidates[0].typical_w == pytest.approx(850.0)


# --------------------------------------------------------------------------- #
# Refusal case 1: one coincidence is not evidence
# --------------------------------------------------------------------------- #


def test_a_single_coincidence_is_refused() -> None:
    """The postman opening the gate while the fridge cycles is not a correlation."""
    observations = _cycle("light.hall", watts=40.0, minute=0)

    report = _correlate(observations)

    assert report.candidates == ()
    assert report.unresolved["light.hall"] == "too_few_corroborations"


def test_two_corroborations_are_below_the_default_but_usable_on_request() -> None:
    """The threshold is explicit: two is not enough unless the caller says so.

    The live recorder held only two pairs per basement fixture, so this is the
    real situation.  It stays ``unknown`` by default; a caller who has decided to
    accept weaker evidence can lower the bar, and the answer is then reported with
    its true corroboration count.
    """
    observations = []
    for index in range(2):
        observations += _cycle("light.hall", watts=40.0, minute=10 * index)

    assert _correlate(observations).candidates == ()
    relaxed = _correlate(observations, min_corroborations=2)
    assert len(relaxed.candidates) == 1
    assert relaxed.candidates[0].corroborations == 2


# --------------------------------------------------------------------------- #
# Refusal case 2: the row must actually move
# --------------------------------------------------------------------------- #


def test_a_step_inside_the_noise_floor_is_refused() -> None:
    """A flicker that a CT cannot distinguish from jitter proves nothing."""
    observations = []
    for index in range(6):
        observations += _cycle("light.tiny", watts=0.2, minute=10 * index)

    report = _correlate(observations, row_noise_w={"ct1": 0.5})

    assert report.candidates == ()
    assert report.unresolved["light.tiny"] == "no_row_responded"


def test_a_steadier_row_needs_less_absolute_movement() -> None:
    """The threshold scales with the row's own measured noise."""
    observations = []
    for index in range(6):
        observations += _cycle("light.small", watts=2.0, minute=10 * index)

    # 2 W over a 0.05 W floor is 40 sigma, so this is a real response.
    report = _correlate(observations, row_noise_w={"ct1": 0.05})
    assert len(report.candidates) == 1


# --------------------------------------------------------------------------- #
# Refusal case 3: direction must agree
# --------------------------------------------------------------------------- #


def test_a_row_that_falls_when_the_light_comes_on_is_refused() -> None:
    """A nearby load switching off is not this fixture drawing power."""
    observations = []
    for index in range(6):
        at = T0 + timedelta(minutes=10 * index)
        observations.append(
            EntityObservation(
                "light.backwards",
                at,
                True,
                {"ct1": BASELINE_W},
                {"ct1": BASELINE_W - 25.0},
            )
        )

    report = _correlate(observations)

    assert report.candidates == ()
    assert report.unresolved["light.backwards"] == "no_row_responded"


# --------------------------------------------------------------------------- #
# Refusal case 4: it must be discriminating
# --------------------------------------------------------------------------- #


def test_two_rows_responding_equally_leaves_the_answer_unknown() -> None:
    """A scene that drives several circuits cannot be attributed to one."""
    observations = []
    for index in range(6):
        at = T0 + timedelta(minutes=10 * index)
        observations.append(
            EntityObservation(
                "light.scene",
                at,
                True,
                {"ct1": 0.0, "ct2": 0.0},
                {"ct1": 50.0, "ct2": 50.0},
            )
        )

    report = _correlate(observations, row_noise_w={"ct1": 0.1, "ct2": 0.1})

    assert report.candidates == ()
    # Both rows responded the same number of times, so there is no basis to
    # prefer one.  Refusing is the point; the exact reason names which guard.
    assert report.unresolved["light.scene"] == "ambiguous_rows"


def test_a_row_that_did_not_respond_does_not_make_the_answer_ambiguous() -> None:
    """A bystander inside the noise floor was never a response at all."""
    observations = []
    for index in range(6):
        at = T0 + timedelta(minutes=10 * index)
        observations.append(
            EntityObservation(
                "light.dominant",
                at,
                True,
                {"ct1": 0.0, "ct2": 0.0},
                {"ct1": 60.0, "ct2": 0.2},
            )
        )

    # 0.2 W on a 0.1 W floor is 2 sigma, below the 6 sigma response threshold.
    report = _correlate(observations, row_noise_w={"ct1": 0.1, "ct2": 0.1})

    assert len(report.candidates) == 1
    assert report.candidates[0].ct_id == "ct1"


def test_a_second_real_response_leaves_the_answer_unknown() -> None:
    """1.5 W over a 0.1 W floor *is* a response, so the evidence is ambiguous."""
    observations = []
    for index in range(6):
        at = T0 + timedelta(minutes=10 * index)
        observations.append(
            EntityObservation(
                "light.split",
                at,
                True,
                {"ct1": 0.0, "ct2": 0.0},
                {"ct1": 60.0, "ct2": 1.5},
            )
        )

    report = _correlate(observations, row_noise_w={"ct1": 0.1, "ct2": 0.1})

    assert report.candidates == ()
    assert report.unresolved["light.split"] == "ambiguous_rows"


def test_an_inconsistent_switch_is_refused_even_with_enough_agreements() -> None:
    """Half-explained movement is not a correlation, however often it repeats.

    The row answers four of six transitions.  That is still three corroborating
    *pairs*, so the corroboration minimum alone would accept it -- but a row that
    ignores a third of the switch's movement has not been shown to carry the
    fixture.  The agreement floor is the guard that refuses it.
    """
    # The row watts immediately before and after each edge.  Four of the six
    # edges see the expected movement; the other two see the row sit still.
    BASELINE = BASELINE_W
    LIT = BASELINE_W + 40.0
    edges = [
        (0, True, BASELINE, LIT),
        (1, False, LIT, LIT),        # row did not fall: no evidence of this load
        (10, True, LIT, LIT),        # row did not rise
        (11, False, LIT, BASELINE),
        (20, True, BASELINE, LIT),
        (21, False, LIT, BASELINE),
    ]
    observations = [
        EntityObservation(
            "light.inconsistent",
            T0 + timedelta(minutes=minute),
            was_on,
            {"ct1": before},
            {"ct1": after},
        )
        for minute, was_on, before, after in edges
    ]

    report = _correlate(observations)

    assert report.candidates == ()
    assert report.unresolved["light.inconsistent"] == "inconsistent_with_row"


def test_three_agreeing_pairs_alone_are_not_enough_without_agreement() -> None:
    """Keeps the previous test honest: the same pairs *with* full agreement pass.

    Without this, a broken refusal could pass ``test_an_inconsistent_switch...``
    for the wrong reason (too few corroborations) and nobody would notice.
    """
    observations = []
    for index in range(3):
        observations += _cycle("light.consistent", watts=40.0, minute=10 * index)

    report = _correlate(observations)

    assert len(report.candidates) == 1
    assert report.candidates[0].corroborations == 3
    assert report.candidates[0].agreement == 1.0


def test_a_competing_partial_response_withholds_the_answer() -> None:
    """One row winning is not enough if another row also follows the switch.

    This is the scene case without an exact tie: ``ct1`` answers every
    transition, ``ct2`` answers four of six.  ``ct1`` is the better answer, but
    nothing here proves the fixture is on ``ct1`` rather than being part of a
    scene that drives both, so the evidence does not pick one.
    """
    observations = []
    for index in range(6):
        at = T0 + timedelta(minutes=5 * index)
        responds = index < 4
        observations.append(
            EntityObservation(
                "light.scene",
                at,
                True,
                {"ct1": 0.0, "ct2": 0.0},
                {"ct1": 50.0, "ct2": 50.0 if responds else 0.0},
            )
        )

    report = _correlate(observations, row_noise_w={"ct1": 0.1, "ct2": 0.1})

    assert report.candidates == ()
    assert report.unresolved["light.scene"] == "not_discriminating"


def test_observations_outside_the_age_horizon_are_ignored() -> None:
    """Circuit wiring changes and bulbs get replaced; old evidence is not current."""
    observations = []
    for index in range(6):
        # Fourteen days plus an hour: past ``MAX_AGE_S`` for "now".
        observations += _cycle(
            "light.stale", watts=40.0, minute=-60 * 24 * 14 - 60 + 10 * index
        )

    report = correlate(observations, now=T0, row_noise_w={"ct1": CT1_NOISE_W})

    assert report.candidates == ()
    assert report.skipped["too_old"] == len(observations)


def test_transitions_inside_the_dedup_gap_are_one_event() -> None:
    """A burst of repeated state reports is one physical toggle, not several.

    Some integrations re-publish the same state several times within a second.
    If each were counted, the burst would look like several corroborating events
    and the evidence would overstate itself.  The count is asserted directly
    because that is the quantity the guard protects.
    """
    burst = [
        EntityObservation(
            "light.burst",
            T0 + timedelta(seconds=offset),
            True,
            {"ct1": BASELINE_W},
            {"ct1": BASELINE_W + 40.0},
        )
        for offset in (0.0, 0.4, 0.9, 1.4)
    ]
    observations = burst
    for index in range(3):
        observations += _cycle("light.burst", watts=40.0, minute=10 * (index + 1))

    report = _correlate(observations)

    assert len(report.candidates) == 1
    # Four burst reports plus three real cycles is ten raw transitions.  With the
    # gap applied the burst is one event, so seven independent transitions remain
    # -- and the count is asserted directly because that is what the guard
    # protects.  Without it this would read "10 of 10".
    assert report.candidates[0].evidence[0] == "7 of 7 independent transitions agreed"


# --------------------------------------------------------------------------- #
# Refusal case 5: only load domains, and only real transitions
# --------------------------------------------------------------------------- #


def test_a_mode_only_domain_is_never_a_candidate() -> None:
    """A climate/mode entity does not prove load (ADR-0012)."""
    observations = []
    for index in range(6):
        observations += _cycle("climate.thermostat", watts=900.0, minute=10 * index)

    report = _correlate(observations)

    assert report.candidates == ()
    assert report.skipped["external_state"] == len(observations)


def test_unavailable_and_no_op_states_are_not_transitions() -> None:
    """A bridge blip must not manufacture two phantom transitions (ADR-0015)."""
    states = {
        "light.flaky": [
            (T0, "off"),
            (T0 + timedelta(minutes=1), "unavailable"),
            (T0 + timedelta(minutes=2), "off"),
            (T0 + timedelta(minutes=3), "on"),
            (T0 + timedelta(minutes=4), "off"),
            (T0 + timedelta(minutes=5), "off"),
        ]
    }
    readings = {
        "ct1": [
            (T0, BASELINE_W),
            (T0 + timedelta(minutes=1), BASELINE_W),
            (T0 + timedelta(minutes=2), BASELINE_W),
            (T0 + timedelta(minutes=3), BASELINE_W + BAR_POTS_W),
            (T0 + timedelta(minutes=4), BASELINE_W),
            (T0 + timedelta(minutes=5), BASELINE_W),
        ]
    }

    observations = observations_from_history(
        entity_states=states, row_readings=readings, window_s=120
    )

    # ``off -> unavailable -> off`` yields no transition at all; only the real
    # on/off pair survives.  A repeated ``off -> off`` is not a transition.
    assert [(o.was_on, o.at) for o in observations] == [
        (True, T0 + timedelta(minutes=3)),
        (False, T0 + timedelta(minutes=4)),
    ]


def test_a_transition_without_a_reading_on_both_sides_is_dropped() -> None:
    """A stale CT has not measured this transition; interpolation would invent it."""
    states = {"light.x": [(T0, "off"), (T0 + timedelta(minutes=30), "on")]}
    readings = {
        "ct1": [
            (T0, BASELINE_W),
            (T0 + timedelta(minutes=1), BASELINE_W),
            (T0 + timedelta(minutes=40), BASELINE_W + 10.0),
        ]
    }

    observations = observations_from_history(
        entity_states=states, row_readings=readings, window_s=30
    )
    assert observations == []


# --------------------------------------------------------------------------- #
# Dimming: watts per level, not one full-on number
# --------------------------------------------------------------------------- #


def test_a_dimmer_records_watts_at_each_measured_level() -> None:
    """A light dimmed to 40 % draws what the row said at 40 %, and the curve says so."""
    observations = []
    for index, (level, watts) in enumerate(
        [(0.4, 8.0), (0.4, 8.2), (1.0, 20.0), (1.0, 19.8)]
    ):
        observations += _cycle("light.dimmer", watts=watts, minute=10 * index, level=level)

    report = _correlate(observations)
    candidate = report.candidates[0]

    assert candidate.is_dimmable is True
    levels = [level for level, _ in candidate.dimming_curve]
    assert levels == pytest.approx([0.4, 1.0])
    # Median of the repeats at each level -- never a single noisy sample.
    watts_by_level = dict(candidate.dimming_curve)
    assert watts_by_level[0.4] == pytest.approx(8.1, abs=0.01)
    assert watts_by_level[1.0] == pytest.approx(19.9, abs=0.01)
    # Full output is the dimmer's ``on_w``; a median across levels would report a
    # wattage the fixture never actually held.
    assert candidate.typical_w == pytest.approx(19.9, abs=0.01)


def test_a_light_that_never_reports_brightness_stays_a_switch() -> None:
    """``on`` with no brightness is not 100 %: it is no dimming evidence."""
    observations = []
    for index in range(3):
        observations += _cycle("light.plain", watts=25.0, minute=10 * index)

    candidate = _correlate(observations).candidates[0]

    assert candidate.is_dimmable is False
    assert candidate.dimming_curve == ()
    assert candidate.typical_w == pytest.approx(25.0)


def test_a_single_level_cannot_claim_a_curve() -> None:
    """One level is a measurement; a curve needs two distinct levels."""
    observations = []
    for index in range(3):
        observations += _cycle("light.once", watts=8.0, minute=10 * index, level=0.4)

    candidate = _correlate(observations).candidates[0]

    assert candidate.dimming_curve == ((0.4, pytest.approx(8.0)),)
    assert candidate.is_dimmable is False


def test_one_cycle_is_one_corroboration_not_two() -> None:
    """Rising and falling edges of one toggle are the same event.

    Counting them separately would let a single switch cycle satisfy the
    corroboration minimum by itself, which is the coincidence the minimum exists
    to reject.
    """
    observations = _cycle("light.dimmer", watts=8.0, minute=0, level=0.4)

    report = _correlate(observations)

    assert report.candidates == ()
    assert report.unresolved["light.dimmer"] == "too_few_corroborations"

    three = _correlate(
        _cycle("light.dimmer", watts=8.0, minute=0, level=0.4)
        + _cycle("light.dimmer", watts=8.0, minute=10, level=0.4)
        + _cycle("light.dimmer", watts=8.0, minute=20, level=0.4)
    )
    assert three.candidates[0].corroborations == 3
    # Only the off edge contributed, so one cycle is one point.
    assert three.candidates[0].dimming_curve == ((0.4, pytest.approx(8.0)),)


def test_brightness_zero_is_not_a_measured_level() -> None:
    """Many integrations publish ``brightness: 0`` while off; that is not output."""
    assert level_from_attributes({"brightness": 0}) is None
    assert level_from_attributes({"brightness": 255}) == pytest.approx(1.0)
    assert level_from_attributes({"brightness": 102}) == pytest.approx(0.4, abs=0.001)
    assert level_from_attributes({"brightness_pct": 50}) == pytest.approx(0.5)
    assert level_from_attributes({}) is None
    assert level_from_attributes(None) is None


def test_a_level_above_full_scale_is_clamped() -> None:
    """An out-of-range attribute is still a fraction; it must not become 3x watts."""
    assert level_from_attributes({"brightness": 1000}) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Row noise and history parsing
# --------------------------------------------------------------------------- #


def test_row_noise_ignores_the_load_steps() -> None:
    """A row that is on half the time is not "noisy"; its *changes* are small."""
    readings = {
        "ct1": [(T0 + timedelta(seconds=30 * i), BASELINE_W + (10.0 if i % 4 == 0 else 0.0))
                for i in range(40)]
    }

    noise = estimate_row_noise(readings)

    # The 10 W steps are a few of many differences, so the median ignores them.
    assert noise["ct1"] < 1.0


def test_history_payload_keeps_states_watts_and_levels_separate() -> None:
    """A CT row's numeric state is watts; a light's state is not."""
    payload = {
        "sensor.ct1_power": [
            {"s": "8.35", "lu": T0.timestamp()},
            {"s": "27.92", "lu": (T0 + timedelta(seconds=30)).timestamp()},
        ],
        "light.dimmer": [
            {"s": "on", "a": {"brightness": 102}, "lu": T0.timestamp()},
            {"s": "off", "lu": (T0 + timedelta(minutes=1)).timestamp()},
        ],
        "light.broken": [{"lu": T0.timestamp()}],
    }

    states, readings, levels = parse_history_payload(payload)

    assert set(readings) == {"sensor.ct1_power"}
    assert readings["sensor.ct1_power"][1][1] == pytest.approx(27.92)
    assert set(states) == {"sensor.ct1_power", "light.dimmer"}
    assert levels["light.dimmer"] == [(T0, pytest.approx(0.4, abs=0.001))]
    # A row with no state is dropped, never invented.
    assert "light.broken" not in states


def test_observations_carry_the_level_at_the_transition() -> None:
    """The level is read from the record, so a dimmer cycle knows its output."""
    states = {
        "light.dimmer": [
            (T0, "off"),
            (T0 + timedelta(minutes=5), "on"),
            (T0 + timedelta(minutes=10), "off"),
        ]
    }
    levels = {
        "light.dimmer": [
            (T0 + timedelta(minutes=5), 0.4),
            (T0 + timedelta(minutes=10), 0.4),
        ]
    }
    # Readings must sit within ``window_s`` of each transition; a poll interval
    # longer than that gap means the CT did not measure this change.
    readings = {
        "ct1": [
            (T0 + timedelta(minutes=4, seconds=50), BASELINE_W),
            (T0 + timedelta(minutes=5, seconds=5), BASELINE_W + 8.0),
            (T0 + timedelta(minutes=9, seconds=50), BASELINE_W + 8.0),
            (T0 + timedelta(minutes=10, seconds=5), BASELINE_W),
        ]
    }

    observations = observations_from_history(
        entity_states=states, row_readings=readings, entity_levels=levels, window_s=120
    )

    assert len(observations) == 2
    # ``on`` carries the level it reached.  The ``off`` edge carries the level it
    # *was* at, which is the curve's measurement: how many watts this fixture
    # drew at 40 % output.
    assert observations[0].was_on is True
    assert observations[0].level == pytest.approx(0.4)
    assert observations[1].was_on is False
    assert observations[1].previous_level == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# Placement: correlation -> configuration
# --------------------------------------------------------------------------- #


def _branch_topology() -> Topology:
    return Topology(
        status=TopologyStatus.CONFIRMED,
        version=1,
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main_power",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct1",
                entity_id="sensor.lights_ct1_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="dev_bar",
                name="bar",
                phase=Phase.L1,
                metadata={"state_entity_ids": ["light.bar"]},
            )
        ],
    )


def _report(*observations):
    return _correlate(observations)


def test_placement_creates_a_shared_device_on_the_branch_row() -> None:
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)

    plan = plan_placements(
        report=_report(*observations),
        topology=_branch_topology(),
        configurations=DeviceConfigurationSet(version=1),
    )

    assert len(plan.accepted) == 1
    placement = plan.accepted[0]
    assert placement.device_id == auto_device_id("light.bar")
    config = placement.configuration
    assert config.configured_by == "auto"
    # Auto is never user knowledge, so it can never raise a confidence tier.
    assert config.is_user_asserted is False
    assert config.monitoring == MonitoringLevel.SHARED
    assert config.monitored_by_ct_ids == ["ct1"]
    assert config.state_entity_ids == ["light.bar"]
    assert config.expected.on_w == pytest.approx(BAR_POTS_W)


def test_placement_refuses_a_main_row() -> None:
    """The whole-installation row cannot host a fixture."""
    topology = _branch_topology().model_copy(
        update={
            "channels": [
                _branch_topology().channels[0],
                _branch_topology().channels[1].model_copy(
                    update={"ct_type": CTType.MAIN}
                ),
            ]
        }
    )
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)

    plan = plan_placements(
        report=_report(*observations),
        topology=topology,
        configurations=DeviceConfigurationSet(version=1),
    )

    assert plan.accepted == ()
    assert "main row" in plan.refused["light.bar"]


def test_placement_refuses_a_row_owned_by_a_dedicated_measurement() -> None:
    """Creating a second device on a dedicated CT would count the circuit twice."""
    topology = _branch_topology().model_copy(
        update={
            "channels": [
                _branch_topology().channels[0],
                _branch_topology().channels[1].model_copy(
                    update={"ct_type": CTType.DEDICATED_DEVICE, "device_id": "dev_lights"}
                ),
            ],
            "devices": [
                DeviceTopology(
                    device_id="dev_lights",
                    name="lights",
                    phase=Phase.L1,
                    dedicated_ct_ids=["ct1"],
                )
            ],
        }
    )
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)

    plan = plan_placements(
        report=_report(*observations),
        topology=topology,
        configurations=DeviceConfigurationSet(version=1),
    )

    assert plan.accepted == ()
    assert "dedicated measurement" in plan.refused["light.bar"]
    assert "convert that channel to a shared circuit" in plan.refused["light.bar"]


def test_a_user_declaration_is_never_overwritten_by_a_correlation() -> None:
    """A person's assertion outranks an inference, however good it looks."""
    user_config = DeviceConfiguration(
        device_id="my_bar",
        connectivity=HaConnectivity.CONNECTED,
        monitoring=MonitoringLevel.SHARED,
        state_entity_ids=["light.bar"],
        monitored_by_ct_ids=["ct1"],
        configured_by="user",
    )
    configurations = DeviceConfigurationSet(version=1, devices=[user_config])
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)

    plan = plan_placements(
        report=_report(*observations),
        topology=_branch_topology(),
        configurations=configurations,
    )

    assert plan.accepted == ()
    assert "already configured by the user" in plan.refused["light.bar"]


def test_merge_is_idempotent_and_withdraws_only_auto_placements() -> None:
    """Re-running must not duplicate devices, and must be able to withdraw them."""
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)
    user_config = DeviceConfiguration(
        device_id="kept",
        connectivity=HaConnectivity.CONNECTED,
        state_entity_ids=["switch.kept"],
        configured_by="user",
    )
    empty = DeviceConfigurationSet(version=1, devices=[user_config])

    first = merge_placements(
        empty,
        plan_placements(
            report=_report(*observations),
            topology=_branch_topology(),
            configurations=empty,
        ),
    )
    second = merge_placements(
        first,
        plan_placements(
            report=_report(*observations),
            topology=_branch_topology(),
            configurations=first,
        ),
    )

    assert sorted(c.device_id for c in first.devices) == sorted(
        c.device_id for c in second.devices
    )
    assert "kept" in {c.device_id for c in second.devices}

    # A later run with no evidence withdraws the auto device and keeps the user's.
    withdrawn = merge_placements(
        second,
        plan_placements(
            report=_correlate(()),
            topology=_branch_topology(),
            configurations=second,
        ),
    )
    assert [c.device_id for c in withdrawn.devices] == ["kept"]


def test_a_zero_step_never_creates_a_device() -> None:
    """An on/off transition that moved no measured power is not a load."""
    observations = [
        EntityObservation(
            "light.ghost", T0 + timedelta(minutes=10 * i), True, {"ct1": 5.0}, {"ct1": 5.0}
        )
        for i in range(6)
    ]
    plan = plan_placements(
        report=_correlate(observations),
        topology=_branch_topology(),
        configurations=DeviceConfigurationSet(version=1),
    )
    assert plan.accepted == ()


def test_auto_device_id_is_stable_and_distinct_from_discovery() -> None:
    """The same switch must map to the same device across runs."""
    assert auto_device_id("light.bar") == "dev_auto_bar"
    assert auto_device_id("light.basement_bar_pot_lights") == (
        "dev_auto_basement_bar_pot_lights"
    )
    # Discovery placeholders use ``dev_``, so an auto device is never mistaken
    # for a CT-derived one.
    assert not auto_device_id("light.bar").startswith("dev_ct")
    assert auto_device_id("light.bar") == auto_device_id("light.bar")


def test_a_stale_dedicated_mirror_on_a_branch_row_is_still_refused() -> None:
    """A half-applied edit must not licence a second claim on one circuit.

    The channel says ``branch`` while a device still declares the row as its
    dedicated measurement -- exactly the state a failed demotion leaves behind
    (defect #55).  Trusting only the channel role would count the row twice, so
    the device's declaration is consulted as well.
    """
    topology = _branch_topology()
    topology = topology.model_copy(
        update={
            "devices": [
                *topology.devices,
                DeviceTopology(
                    device_id="dev_lights",
                    name="lights",
                    phase=Phase.L1,
                    dedicated_ct_ids=["ct1"],
                ),
            ]
        }
    )
    observations = []
    for index in range(3):
        observations += _cycle("light.bar", watts=BAR_POTS_W, minute=10 * index)

    plan = plan_placements(
        report=_report(*observations),
        topology=topology,
        configurations=DeviceConfigurationSet(version=1),
    )

    assert plan.accepted == ()
    assert "dedicated measurement" in plan.refused["light.bar"]


def test_a_candidate_with_no_positive_step_cannot_be_placed() -> None:
    """Defence in depth: placement refuses a zero-watt claim on its own.

    ``correlate`` cannot currently emit such a candidate, but placement is the
    function that writes a device and an energy entity, so it re-checks rather
    than trusting its caller.  A zero step would create a device that reports
    nothing forever.
    """
    zero = CtCandidate(
        entity_id="light.ghost",
        ct_id="ct1",
        corroborations=3,
        agreement=1.0,
        typical_on_w=0.0,
        typical_off_w=0.0,
    )

    plan = plan_placements(
        report=CorrelationReport(candidates=(zero,)),
        topology=_branch_topology(),
        configurations=DeviceConfigurationSet(version=1),
    )

    assert plan.accepted == ()
    assert "not positive" in plan.refused["light.ghost"]
