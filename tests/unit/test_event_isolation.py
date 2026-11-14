"""WP4 — event isolation and physical-device event identity.

What is proven here (``docs/test_scenario_matrix.md`` Group G, plus the
dead-guard repair from ``docs/assessment_gap_analysis.md`` G5/G7):

* one device changing is ``ISOLATED``; a second *physical* device changing in
  either analysis window is ``CONTAMINATED`` and is retained, not dropped;
* three or more devices switched by one automation name every competing
  device, one name per appliance;
* several HA entities of one appliance collapse to **one** device event, and
  the entity roles decide what counts as load evidence at all
  (``AVAILABILITY`` / ``MODE_ONLY`` / ``ENERGY_COUNTER`` never do);
* a change during the *power-analysis* window specifically is distinguished
  from a change inside the tight isolation window;
* no CT samples, an un-elapsed window, or a naive timestamp is
  ``INSUFFICIENT_DATA`` — absence of data never defaults to ``ISOLATED``;
* ``occurred_at`` is byte-identical before and after classification, while
  ``received_at`` is preserved untouched and ordering is by event time;
* a late/out-of-order event retroactively contaminates an ``ISOLATED`` verdict
  (``reclassify``);
* offset and per-device/per-class window configuration change the verdict;
* the learning pipeline now constructs ``SampleEvidence`` on the production
  path, so ``SampleRejection.CONCURRENT_EVENTS`` can actually fire.

No wall-clock: every call injects ``now`` and every timestamp is explicit.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    DeviceEvent,
    HaEntityBinding,
    HaEntityRole,
    IsolationStatus,
    SampleRejection,
    utcnow,
)
from wattmosaic_engine.disaggregation.isolation import (
    EventIsolation,
    EventIsolator,
    IsolationConfig,
    IsolationWindow,
    PhysicalDeviceEvent,
    SampleCoverage,
    clean_attribution_inputs,
    physical_device_events,
    split_attribution,
)
from wattmosaic_engine.learning import (
    LearningPipeline,
    LearningThresholds,
    SampleEvidence,
    SampleFilter,
)
from wattmosaic_engine.storage import Database

from tests.unit.conftest import BASE_TIME, windows_over

#: A verdict may only be ``ISOLATED`` when coverage was measured and enough.
COVERED = SampleCoverage(baseline_samples=6, event_samples=6)
#: Long enough that ``BASE_TIME`` events are always mature and never "future".
MATURE = BASE_TIME + timedelta(hours=2)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def event(
    event_id: str,
    device_id: str,
    entity_id: str,
    at: datetime,
    *,
    state: str = "on",
    previous_state: str | None = "off",
    event_type: str = "state_changed",
    received_at: datetime | None = None,
) -> DeviceEvent:
    return DeviceEvent(
        event_id=event_id,
        device_id=device_id,
        entity_id=entity_id,
        event_type=event_type,  # type: ignore[arg-type]
        previous_state=previous_state,
        new_state=state,
        occurred_at=at,
        received_at=received_at if received_at is not None else at + timedelta(seconds=1),
    )


def binding(
    entity_id: str,
    device_id: str,
    role: HaEntityRole,
    *,
    load_evidence: bool | None = None,
    contributes_power: bool = False,
) -> HaEntityBinding:
    if load_evidence is None:
        load_evidence = role in (HaEntityRole.LOAD_STATE, HaEntityRole.POWER_MEASUREMENT)
    return HaEntityBinding(
        entity_id=entity_id,
        device_id=device_id,
        role=role,
        is_load_evidence=load_evidence,
        contributes_power=contributes_power,
    )


def isolator(
    *,
    offset: float = 0.0,
    window: float = 30.0,
    power_window: float | None = 60.0,
    bindings: list[HaEntityBinding] | None = None,
    config: IsolationConfig | None = None,
    device_classes: dict[str, tuple[str, ...]] | None = None,
) -> EventIsolator:
    cfg = config or IsolationConfig(
        analysis_offset_s=offset,
        isolation_window_s=window,
        power_window_s=power_window,
    )
    return EventIsolator(cfg, bindings=bindings or [], device_classes=device_classes or {})


# --------------------------------------------------------------------------- #
# G1 — one device changes
# --------------------------------------------------------------------------- #


def test_one_device_change_is_isolated_with_recorded_windows() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)

    result = iso.classify(own, [own], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert result.reason == "isolated"
    assert result.device_id == "lamp"
    assert result.entity_id == "light.lamp"
    assert result.window_start == BASE_TIME
    assert result.window_end == BASE_TIME + timedelta(seconds=30)
    assert result.isolation_window_s == pytest.approx(30.0)
    assert result.analysis_offset_s == pytest.approx(0.0)
    assert result.competing_event_ids == []
    assert result.competing_device_ids == []
    assert result.power_window_contaminated is False
    assert result.evidence["coverage"]["sufficient"] is True


# --------------------------------------------------------------------------- #
# G2 — two devices inside the isolation window
# --------------------------------------------------------------------------- #


def test_two_devices_within_the_isolation_window_is_contaminated() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=5))

    result = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.reason == "concurrent_device_change"
    assert result.competing_event_ids == ["e2"]
    assert result.competing_device_ids == ["heater"]
    assert result.power_window_contaminated is True
    assert result.evidence["competing_groups"][0]["device_id"] == "heater"


def test_events_of_the_same_device_are_never_competing() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    same_device = event("e1b", "lamp", "switch.lamp", BASE_TIME + timedelta(seconds=2))

    result = iso.classify(own, [own, same_device], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert result.competing_device_ids == []


# --------------------------------------------------------------------------- #
# G3 — an automation switching several devices
# --------------------------------------------------------------------------- #


def test_automation_with_three_devices_names_every_competing_device() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    other_a = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=2))
    other_b = event("e3", "pump", "switch.pump", BASE_TIME + timedelta(seconds=3))

    result = iso.classify(own, [own, other_a, other_b], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.competing_device_ids == ["heater", "pump"]
    assert result.evidence["competing_device_count"] == 2


# --------------------------------------------------------------------------- #
# G4 — one physical device, many HA entities
# --------------------------------------------------------------------------- #


def test_multiple_entities_of_one_device_collapse_to_one_device_event() -> None:
    bindings = [
        binding("light.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("switch.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("sensor.tub_power", "tub", HaEntityRole.POWER_MEASUREMENT, contributes_power=True),
    ]
    events = [
        event("e1", "tub", "light.tub", BASE_TIME, state="on"),
        event("e2", "tub", "switch.tub", BASE_TIME + timedelta(seconds=1), state="on"),
        event("e3", "tub", "sensor.tub_power", BASE_TIME + timedelta(seconds=2), state="2100"),
    ]

    groups = physical_device_events(events, bindings)

    assert len(groups) == 1
    group = groups[0]
    assert isinstance(group, PhysicalDeviceEvent)
    assert group.device_id == "tub"
    assert group.entity_ids == ("light.tub", "sensor.tub_power", "switch.tub")
    assert group.event_ids == ("e1", "e2", "e3")
    # One observation per entity, none discarded.
    assert set(group.states) == {"light.tub", "switch.tub", "sensor.tub_power"}
    assert group.power_contributor == "sensor.tub_power"
    assert group.conflicting is False


def test_three_entities_of_one_device_do_not_become_three_competing_events() -> None:
    bindings = [
        binding("light.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("switch.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("sensor.tub_power", "tub", HaEntityRole.POWER_MEASUREMENT),
    ]
    iso = isolator(bindings=bindings)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    tub_events = [
        event("e2", "tub", "light.tub", BASE_TIME + timedelta(seconds=2)),
        event("e3", "tub", "switch.tub", BASE_TIME + timedelta(seconds=2)),
        event("e4", "tub", "sensor.tub_power", BASE_TIME + timedelta(seconds=2)),
    ]

    result = iso.classify(own, [own, *tub_events], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    # One physical device, not three; one representative event id.
    assert result.competing_device_ids == ["tub"]
    assert result.competing_event_ids == ["e2"]
    assert result.evidence["competing_groups"][0]["event_ids"] == ["e2", "e3", "e4"]


def test_two_entities_of_one_device_that_disagree_stay_one_marked_event() -> None:
    bindings = [
        binding("light.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("switch.tub", "tub", HaEntityRole.LOAD_STATE),
    ]
    events = [
        event("e1", "tub", "light.tub", BASE_TIME, state="on"),
        event("e2", "tub", "switch.tub", BASE_TIME + timedelta(seconds=1), state="off"),
    ]

    groups = physical_device_events(events, bindings)

    assert len(groups) == 1
    assert groups[0].conflicting is True
    assert "conflicting_observations" in groups[0].notes
    assert groups[0].states == {"light.tub": "on", "switch.tub": "off"}


def test_conflicting_entities_still_isolate_but_mark_the_evidence() -> None:
    bindings = [
        binding("light.tub", "tub", HaEntityRole.LOAD_STATE),
        binding("switch.tub", "tub", HaEntityRole.LOAD_STATE),
    ]
    iso = isolator(bindings=bindings)
    own = event("e1", "tub", "light.tub", BASE_TIME, state="on")
    disagreeing = event("e2", "tub", "switch.tub", BASE_TIME + timedelta(seconds=1), state="off")

    result = iso.classify(own, [own, disagreeing], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert result.evidence["own_device_conflicting"] is True


def test_at_most_one_power_contributor_is_counted_per_device() -> None:
    # The contract forbids two contributors; the isolator must not double-count
    # or silently add the second one to the device total either.
    bindings = [
        binding("sensor.a", "tub", HaEntityRole.POWER_MEASUREMENT, contributes_power=True),
        binding("sensor.b", "tub", HaEntityRole.POWER_MEASUREMENT, contributes_power=True),
    ]
    groups = physical_device_events(
        [
            event("e1", "tub", "sensor.a", BASE_TIME, state="2100"),
            event("e2", "tub", "sensor.b", BASE_TIME + timedelta(seconds=1), state="2100"),
        ],
        bindings,
    )

    assert len(groups) == 1
    assert groups[0].power_contributor == "sensor.a"
    assert "power_contributor_conflict" in groups[0].notes


# --------------------------------------------------------------------------- #
# Group G — roles decide what is a load event
# --------------------------------------------------------------------------- #


def test_availability_only_change_does_not_contaminate() -> None:
    bindings = [
        binding("light.lamp", "lamp", HaEntityRole.LOAD_STATE),
        binding("binary_sensor.heater", "heater", HaEntityRole.AVAILABILITY),
    ]
    iso = isolator(bindings=bindings)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    unavailable = event(
        "e2",
        "heater",
        "binary_sensor.heater",
        BASE_TIME + timedelta(seconds=5),
        state="off",
        event_type="availability",
    )

    assert physical_device_events([unavailable], bindings) == []
    result = iso.classify(own, [own, unavailable], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert result.competing_device_ids == []
    assert result.power_window_contaminated is False


@pytest.mark.parametrize(
    "role",
    [HaEntityRole.MODE_ONLY, HaEntityRole.ENERGY_COUNTER, HaEntityRole.UNKNOWN],
)
def test_non_load_roles_never_create_a_load_event(role: HaEntityRole) -> None:
    bindings = [binding("sensor.thing", "dev", role, load_evidence=False)]

    groups = physical_device_events(
        [event("e1", "dev", "sensor.thing", BASE_TIME, state="on")], bindings
    )

    assert groups == []


def test_an_availability_event_of_the_subject_is_insufficient_not_isolated() -> None:
    bindings = [binding("binary_sensor.tub", "tub", HaEntityRole.AVAILABILITY)]
    iso = isolator(bindings=bindings)
    availability = event(
        "e1",
        "tub",
        "binary_sensor.tub",
        BASE_TIME,
        state="off",
        event_type="availability",
    )

    result = iso.classify(availability, [availability], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.INSUFFICIENT_DATA
    assert result.reason == "not_load_evidence"


def test_an_availability_flip_without_an_event_type_still_is_not_load_evidence() -> None:
    """The production bridge keeps only ``new_state``, not ``event_type``.

    ``integration._bridge_event`` converts an HA event into the learning dict
    ``{device_id, entity_id, new_state, attributes, occurred_at}`` and drops the
    event type, so an ``unavailable`` label is the only surviving evidence that
    this was a reachability flip rather than the appliance switching.  Counting
    it would make every bridge blip a competing device.
    """
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    bridged = {
        "device_id": "heater",
        "entity_id": "binary_sensor.heater",
        "new_state": "unavailable",
        "attributes": {},
        "occurred_at": BASE_TIME + timedelta(seconds=4),
    }

    assert physical_device_events([bridged]) == []
    result = iso.classify(own, [own, bridged], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert result.competing_device_ids == []


# --------------------------------------------------------------------------- #
# G5 — another device changes during the power-analysis window specifically
# --------------------------------------------------------------------------- #


def test_a_change_in_the_power_analysis_window_only_contaminates() -> None:
    # isolation window 10 s, power window 60 s: the sibling moves at +45 s.
    iso = isolator(window=10.0, power_window=60.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=45))

    result = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.reason == "power_window_device_change"
    assert result.competing_device_ids == ["heater"]
    assert result.power_window_contaminated is True
    assert result.evidence["in_isolation_window"] is False
    assert result.evidence["in_power_window"] is True


def test_a_change_after_the_power_window_is_not_contamination() -> None:
    iso = isolator(window=10.0, power_window=60.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=61))

    result = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED


# --------------------------------------------------------------------------- #
# G9 — insufficient data is never silently "isolated"
# --------------------------------------------------------------------------- #


def test_missing_ct_samples_are_insufficient_data() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    empty = SampleCoverage(baseline_samples=0, event_samples=0)

    result = iso.classify(own, [own], now=MATURE, coverage=empty)

    assert result.status == IsolationStatus.INSUFFICIENT_DATA
    assert result.reason == "insufficient_samples"
    assert "baseline_samples" in result.evidence["coverage"]


def test_unmeasured_coverage_is_insufficient_data_not_isolated() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)

    result = iso.classify(own, [own], now=MATURE, coverage=None)

    assert result.status == IsolationStatus.INSUFFICIENT_DATA
    assert result.reason == "coverage_unknown"


def test_a_window_that_has_not_elapsed_is_insufficient_data() -> None:
    iso = isolator(window=30.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)

    result = iso.classify(own, [own], now=BASE_TIME + timedelta(seconds=10), coverage=COVERED)

    assert result.status == IsolationStatus.INSUFFICIENT_DATA
    assert result.reason == "window_not_elapsed"
    assert result.evidence["window_elapsed"] is False


def test_production_shaped_dict_events_without_ids_are_still_classifiable() -> None:
    """``integration._bridge_event`` emits dicts with no ``event_id``.

    A contaminated verdict must name its competitors, so a missing id is
    synthesised deterministically instead of raising a validation error on the
    real production path.
    """
    iso = isolator()
    own = {
        "device_id": "lamp",
        "entity_id": "light.lamp",
        "new_state": "on",
        "occurred_at": BASE_TIME,
        "attributes": {"supporting_ct_ids": ["ct_lamp"]},
    }
    sibling = {
        "device_id": "heater",
        "entity_id": "switch.heater",
        "new_state": "on",
        "occurred_at": BASE_TIME + timedelta(seconds=4),
        "attributes": {"supporting_ct_ids": ["ct_heater"]},
    }

    result = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.competing_device_ids == ["heater"]
    assert result.competing_event_ids == [f"heater@{sibling['occurred_at'].isoformat()}"]
    # Deterministic, not a random id.
    again = isolator().classify(own, [own, sibling], now=MATURE, coverage=COVERED)
    assert again.competing_event_ids == result.competing_event_ids


def test_contamination_is_still_provable_before_the_window_elapses() -> None:
    iso = isolator(window=30.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=5))

    result = iso.classify(
        own, [own, sibling], now=BASE_TIME + timedelta(seconds=10), coverage=COVERED
    )

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.reason == "concurrent_device_change"


def test_a_naive_event_timestamp_is_insufficient_data() -> None:
    iso = isolator()
    naive = DeviceEvent(
        event_id="e1",
        device_id="lamp",
        entity_id="light.lamp",
        event_type="state_changed",
        new_state="on",
        occurred_at=datetime(2026, 3, 1, 9, 0, 0),  # no tzinfo
    )

    result = iso.classify(naive, [naive], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.INSUFFICIENT_DATA
    assert result.reason == "event_time_naive"


# --------------------------------------------------------------------------- #
# G6/G7 — timestamps: original preserved, receive time intact, order by time
# --------------------------------------------------------------------------- #


def test_occurred_at_is_byte_identical_before_and_after_classification() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=2))
    before = own.occurred_at.isoformat()

    contaminated = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)
    isolated = isolator().classify(own, [own], now=MATURE, coverage=COVERED)
    insufficient = isolator().classify(own, [own], now=BASE_TIME, coverage=COVERED)

    for result in (contaminated, isolated, insufficient):
        assert result.occurred_at.isoformat() == before
        assert result.model_dump()["occurred_at"] == own.occurred_at
    # The event object itself is never mutated.
    assert own.occurred_at.isoformat() == before


def test_receive_time_is_preserved_and_ordering_uses_event_time() -> None:
    iso = isolator()
    early = event(
        "e1",
        "lamp",
        "light.lamp",
        BASE_TIME,
        received_at=BASE_TIME + timedelta(seconds=50),
    )
    late = event(
        "e2",
        "heater",
        "switch.heater",
        BASE_TIME + timedelta(seconds=5),
        received_at=BASE_TIME + timedelta(seconds=90),
    )

    groups = iso.physical_events([late, early])

    assert [g.device_id for g in groups] == ["lamp", "heater"]
    assert groups[0].occurred_at == BASE_TIME
    assert groups[0].received_at == BASE_TIME + timedelta(seconds=50)
    result = iso.classify(early, [late, early], now=MATURE, coverage=COVERED)
    assert result.received_at == BASE_TIME + timedelta(seconds=50)
    assert result.occurred_at == BASE_TIME


def test_out_of_order_arrival_is_classified_by_event_time() -> None:
    iso = isolator(window=30.0)
    early = event("e1", "lamp", "light.lamp", BASE_TIME, received_at=BASE_TIME + timedelta(seconds=120))
    late_in_time = event(
        "e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=3),
        received_at=BASE_TIME + timedelta(seconds=10),
    )

    # The arrival order is (late_in_time, early); event time decides.
    result = iso.classify(early, [late_in_time, early], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.CONTAMINATED
    assert result.competing_device_ids == ["heater"]


# --------------------------------------------------------------------------- #
# G8 — overlapping (not identical) event windows
# --------------------------------------------------------------------------- #


def test_overlapping_windows_contaminate_the_earlier_event_only() -> None:
    iso = isolator(window=30.0, power_window=30.0)
    first = event("e1", "lamp", "light.lamp", BASE_TIME)
    second = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=20))

    first_result = iso.classify(first, [first, second], now=MATURE, coverage=COVERED)
    # The later event's own forward windows start after the earlier step, so its
    # measured delta is its own; that asymmetry is deliberate and documented.
    second_result = iso.classify(second, [first, second], now=MATURE, coverage=COVERED)

    assert first_result.status == IsolationStatus.CONTAMINATED
    assert first_result.competing_device_ids == ["heater"]
    assert second_result.status == IsolationStatus.ISOLATED


# --------------------------------------------------------------------------- #
# G10 — configurable offsets and per-device/per-class windows
# --------------------------------------------------------------------------- #


def test_offset_config_change_alters_the_verdict() -> None:
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=5))

    immediate = isolator(offset=0.0).classify(
        own, [own, sibling], now=MATURE, coverage=COVERED
    )
    shifted = isolator(offset=60.0).classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert immediate.status == IsolationStatus.CONTAMINATED
    assert shifted.status == IsolationStatus.ISOLATED
    assert shifted.analysis_offset_s == pytest.approx(60.0)
    assert shifted.window_start == BASE_TIME + timedelta(seconds=60)


def test_per_device_override_beats_the_class_default_beats_the_global_default() -> None:
    config = IsolationConfig(
        isolation_window_s=30.0,
        power_window_s=30.0,
        defaults={"hot_tub": IsolationWindow(0.0, 120.0, 120.0)},
        per_device={"tub_fast": IsolationWindow(0.0, 5.0, 5.0)},
    )
    iso = EventIsolator(
        config,
        device_classes={
            "tub_fast": ("hot_tub",),
            "tub_slow": ("hot_tub",),
            "lamp": (),
        },
    )
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=20))

    fast = iso.classify(
        event("e1", "tub_fast", "switch.tub", BASE_TIME),
        [event("e1", "tub_fast", "switch.tub", BASE_TIME), sibling],
        now=MATURE,
        coverage=COVERED,
    )
    slow = iso.classify(
        event("e3", "tub_slow", "switch.tub2", BASE_TIME),
        [event("e3", "tub_slow", "switch.tub2", BASE_TIME), sibling],
        now=MATURE,
        coverage=COVERED,
    )
    plain = iso.classify(
        event("e4", "lamp", "light.lamp", BASE_TIME),
        [event("e4", "lamp", "light.lamp", BASE_TIME), sibling],
        now=MATURE,
        coverage=COVERED,
    )

    # 5 s override: the +20 s sibling is outside both windows.
    assert fast.status == IsolationStatus.ISOLATED
    assert fast.isolation_window_s == pytest.approx(5.0)
    # 120 s class default: the +20 s sibling is inside.
    assert slow.status == IsolationStatus.CONTAMINATED
    assert slow.isolation_window_s == pytest.approx(120.0)
    # 30 s global default: the +20 s sibling is inside.
    assert plain.status == IsolationStatus.CONTAMINATED
    assert plain.isolation_window_s == pytest.approx(30.0)


def test_isolation_config_rejects_impossible_timings() -> None:
    with pytest.raises(ValueError):
        IsolationConfig(analysis_offset_s=-1.0)
    with pytest.raises(ValueError):
        IsolationConfig(isolation_window_s=-1.0)
    with pytest.raises(ValueError):
        IsolationConfig(isolation_window_s=math.inf)
    with pytest.raises(ValueError):
        IsolationConfig(isolation_window_s=math.nan)
    with pytest.raises(ValueError):
        IsolationConfig(power_window_s=-5.0)
    with pytest.raises(ValueError):
        IsolationWindow(0.0, -3.0, 10.0)
    with pytest.raises(ValueError):
        IsolationConfig(per_device={"x": IsolationWindow(0.0, -1.0, 1.0)})


# --------------------------------------------------------------------------- #
# G6 — delayed / out-of-order reclassification
# --------------------------------------------------------------------------- #


def test_a_late_event_retroactively_contaminates_an_isolated_verdict() -> None:
    iso = isolator(window=30.0, power_window=60.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    isolated = iso.classify(own, [own], now=MATURE, coverage=COVERED)
    assert isolated.status == IsolationStatus.ISOLATED

    late = event(
        "e2",
        "heater",
        "switch.heater",
        BASE_TIME + timedelta(seconds=10),
        received_at=MATURE + timedelta(minutes=5),
    )
    revised = iso.reclassify(isolated, late)

    assert revised.status == IsolationStatus.CONTAMINATED
    assert revised.reason == "late_competing_device"
    assert revised.competing_device_ids == ["heater"]
    assert revised.competing_event_ids == ["e2"]
    assert revised.occurred_at == isolated.occurred_at
    assert revised.occurred_at.isoformat() == own.occurred_at.isoformat()
    assert revised.window_start == isolated.window_start
    assert revised.window_end == isolated.window_end
    assert revised.evidence["reclassified"] is True
    assert revised.evidence["previous_status"] == "isolated"
    assert revised.received_at == own.received_at


def test_reclassify_leaves_unrelated_late_events_alone() -> None:
    iso = isolator(window=30.0, power_window=60.0)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    isolated = iso.classify(own, [own], now=MATURE, coverage=COVERED)

    same_device = event("e9", "lamp", "switch.lamp", BASE_TIME + timedelta(seconds=5))
    after_window = event("e10", "heater", "switch.heater", BASE_TIME + timedelta(seconds=61))
    availability = event(
        "e11", "pump", "binary_sensor.pump", BASE_TIME + timedelta(seconds=5),
        event_type="availability", state="off",
    )

    for candidate in (same_device, after_window, availability):
        assert iso.reclassify(isolated, candidate) is isolated


def test_reclassify_is_idempotent_once_contaminated() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=2))
    contaminated = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    third = event("e3", "pump", "switch.pump", BASE_TIME + timedelta(seconds=3))

    assert iso.reclassify(contaminated, third) is contaminated


# --------------------------------------------------------------------------- #
# Retention: contaminated events are kept, only excluded from clean inputs
# --------------------------------------------------------------------------- #


def test_contaminated_events_are_retained_and_only_excluded_from_clean_inputs() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=2))
    clean = iso.classify(own, [own], now=MATURE, coverage=COVERED)
    dirty = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    isolations = [clean, dirty]
    accept, withheld = split_attribution(isolations)

    assert [i.event_id for i in accept] == ["e1"]
    assert [i.event_id for i in withheld] == ["e1"]
    assert withheld[0].status == IsolationStatus.CONTAMINATED
    # Nothing is dropped: the union is the input.
    assert len(accept) + len(withheld) == len(isolations)
    assert clean_attribution_inputs(isolations) == accept


def test_insufficient_data_is_withheld_from_clean_attribution_too() -> None:
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    unknown = iso.classify(own, [own], now=MATURE, coverage=None)

    accept, withheld = split_attribution([unknown])

    assert accept == []
    assert withheld == [unknown]


# --------------------------------------------------------------------------- #
# Dead concurrency guard: before / after
# --------------------------------------------------------------------------- #


NEVER_STALE = LearningThresholds(stale_after_s=10**9)


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _concurrent_cycle(
    devices: list[str], *, event_at: datetime, shared_ct: str | None = None
) -> tuple[list[DeviceEvent], list]:
    """Feed history, then return simultaneous ON transitions + the last window.

    ``shared_ct`` puts every device on one branch circuit, which is the
    genuinely unresolvable case: one measured step with three possible causes.
    Left unset, each device keeps its own CT (the dedicated case) so a test can
    prove that *separability*, not simultaneity, is what decides attribution.
    """
    cts = [shared_ct] if shared_ct else [f"ct_{device}" for device in devices]
    values = {ct: (lambda at: 10.0 if at < event_at else 110.0) for ct in cts}
    windows = windows_over(BASE_TIME, event_at + timedelta(minutes=2), 15.7, values)
    events = [
        event(
            f"e_{device}",
            device,
            f"light.{device}",
            event_at,
            state="on",
        )
        for device in devices
    ]
    # ``supporting_ct_ids`` is how the collector learns which CT to measure.
    for index, ev in enumerate(events):
        ev.attributes["supporting_ct_ids"] = [cts[0] if shared_ct else cts[index]]
    return events, windows


def test_concurrency_guard_fires_on_the_production_path(db: Database) -> None:
    """Three devices switched together are rejected for CONCURRENT_EVENTS.

    Before the WP4 repair ``LearningPipeline.consider`` called
    ``self.filter.evaluate(sample)`` (``learning/pipeline.py`` line 72 at
    ``0.2.1-dev.24``).  ``SampleFilter.evaluate`` then defaulted to
    ``SampleEvidence()`` (``sample_filter.py:194``), whose ``concurrent_events``
    is 0, so ``checks[CONCURRENT_EVENTS]`` was always ``0 > 1 == False`` and
    ``SampleRejection.CONCURRENT_EVENTS`` was unreachable in production --
    only the hand-built evidence in ``test_learning.py`` could trigger it.
    Now the pipeline derives the evidence from the event isolator, so the
    guard fires for a genuinely contaminated event.
    """
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(
        ["lamp", "heater", "pump"], event_at=event_at, shared_ct="ct_branch"
    )
    for window in windows[:-1]:
        pipeline.collector.observe(window)

    accepted = pipeline.consider(events, windows[-1])

    assert accepted == []
    assert len(pipeline.last_rejected) == 3
    assert {sample.rejection_reason for sample in pipeline.last_rejected} == {
        SampleRejection.CONCURRENT_EVENTS
    }
    assert all("concurrent_events=2>1" in sample.notes[-1] for sample in pipeline.last_rejected)


def test_the_old_call_shape_could_never_reach_the_concurrency_guard(db: Database) -> None:
    """Same samples, old call shape: no evidence, no rejection."""
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(
        ["lamp", "heater", "pump"], event_at=event_at, shared_ct="ct_branch"
    )
    for window in windows[:-1]:
        pipeline.collector.observe(window)
    samples = pipeline.collector.consider(events, windows[-1])
    assert len(samples) == 3

    # This is exactly ``self.filter.evaluate(sample)`` -- the pre-WP4 call.
    legacy_filter = SampleFilter(NEVER_STALE)
    for sample in samples:
        decision = legacy_filter.evaluate(sample)
        assert decision.accepted is True
        assert decision.metrics["concurrent_events"] == 0.0
        assert decision.reason is None


def test_pipeline_evidence_is_built_from_the_isolator(db: Database) -> None:
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(
        ["lamp", "heater", "pump"], event_at=event_at, shared_ct="ct_branch"
    )
    for window in windows[:-1]:
        pipeline.collector.observe(window)

    seen: list[SampleEvidence | None] = []
    original = pipeline.filter.evaluate

    def spy(sample, evidence=None, **kwargs):
        seen.append(evidence)
        return original(sample, evidence, **kwargs)

    pipeline.filter.evaluate = spy  # type: ignore[method-assign]
    pipeline.consider(events, windows[-1])

    assert len(seen) == 3
    assert all(isinstance(evidence, SampleEvidence) for evidence in seen)
    concurrent = sorted(evidence.concurrent_events for evidence in seen if evidence is not None)
    assert concurrent == [2, 2, 2]
    # ``concurrency_window_s`` used to be referenced nowhere outside its
    # definition; it now travels with the evidence as the isolation window.
    assert all(
        evidence.concurrency_window_s == pytest.approx(NEVER_STALE.concurrency_window_s)
        for evidence in seen
        if evidence is not None
    )


def test_window_transitions_join_the_isolation_neighbourhood(db: Database) -> None:
    """A sibling in the current cycle's ``state_transitions`` is not invisible.

    ``main.py`` attaches the cycle's transitions to the aligned window; a
    sibling that has not matured for learning is still a real competitor, so the
    pipeline merges both sources and deduplicates by event identity.
    """
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(["lamp"], event_at=event_at)
    for window in windows[:-1]:
        pipeline.collector.observe(window)
    sibling_at = event_at + timedelta(seconds=3)
    sibling = event(
        "e_sibling", "heater", "switch.heater", sibling_at, state="on"
    )
    window = windows[-1]

    class _WithTransitions:
        def __init__(self, inner, transitions):
            self._inner = inner
            self.at = inner.at
            self.state_transitions = transitions

        def __getattr__(self, name):
            return getattr(self._inner, name)

    pipeline.consider(events, _WithTransitions(window, [sibling]))

    isolation = next(iter(pipeline.last_isolations.values()))
    assert isolation.competing_device_ids == ["heater"]
    assert isolation.status == IsolationStatus.CONTAMINATED


def test_repeated_event_identity_is_counted_once(db: Database) -> None:
    """The same event seen twice (batch + window) is one competitor."""
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(["lamp"], event_at=event_at, shared_ct="ct_branch")
    for window in windows[:-1]:
        pipeline.collector.observe(window)
    sibling = event("e_sib", "heater", "switch.heater", event_at + timedelta(seconds=2))
    sibling.attributes["supporting_ct_ids"] = ["ct_branch"]

    evidence = pipeline.isolation_evidence(
        pipeline.collector.build(events[0]), [events[0], sibling], windows[-1]
    )
    assert evidence.concurrent_events == 1
    assert evidence.competing_device_ids == ["heater"]


def test_a_single_sibling_change_is_tolerated_by_the_documented_threshold(db: Database) -> None:
    """``max_concurrent_events = 1`` keeps one sibling; two or more reject.

    This is the documented threshold (``learning/thresholds.py``), not a WP4
    change: the repair makes the *count* real, it does not tighten the limit.
    """
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(
        ["lamp", "heater"], event_at=event_at, shared_ct="ct_branch"
    )
    for window in windows[:-1]:
        pipeline.collector.observe(window)

    accepted = pipeline.consider(events, windows[-1])

    assert len(accepted) == 2
    assert pipeline.last_rejected == []


def test_a_dedicated_ct_device_is_not_guarded_out_by_an_unrelated_scene(db: Database) -> None:
    """A competitor on a *disjoint* CT cannot have polluted the measured delta.

    This is the difference between the verdict and the guard.  ``classify`` still
    reports ``CONTAMINATED`` -- something else *was* changing in the window, and
    that is what the contract records.  The guard asks the narrower question the
    rejection is actually about: could that change have moved this sample's CT?
    Three devices with their own CTs switch together; each step is still
    measurable, so rejecting all three would discard the only clean samples a
    dedicated-CT appliance ever produces.
    """
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(["lamp", "heater", "pump"], event_at=event_at)
    for window in windows[:-1]:
        pipeline.collector.observe(window)

    accepted = pipeline.consider(events, windows[-1])

    assert len(accepted) == 3
    assert pipeline.last_rejected == []
    # The verdict itself is unchanged: three devices did change together.
    assert all(
        isolation.status == IsolationStatus.CONTAMINATED
        for isolation in pipeline.last_isolations.values()
    )
    assert all(
        isolation.evidence["ct_separated_device_ids"] for isolation in pipeline.last_isolations.values()
    )


def test_unknown_ct_scope_falls_back_to_the_conservative_guard(db: Database) -> None:
    """No declared CT means no separability: the competitor still counts.

    An unbound entity (or a hand-built event) carries no ``supporting_ct_ids``.
    Absence of evidence is not evidence of separation, so the guard keeps its
    original behaviour instead of optimistically excusing the sibling.
    """
    pipeline = LearningPipeline(db, thresholds=NEVER_STALE)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(["lamp"], event_at=event_at)
    for window in windows[:-1]:
        pipeline.collector.observe(window)
    sibling = event("e_sib", "heater", "switch.heater", event_at + timedelta(seconds=2))
    # The sibling declares no CT, so its scope is unknown.  (The subject keeps
    # its own CT: without one the collector cannot measure the step at all.)
    assert "supporting_ct_ids" not in sibling.attributes

    evidence = pipeline.isolation_evidence(
        pipeline.collector.build(events[0]), [events[0], sibling], windows[-1]
    )

    assert evidence.concurrent_events == 1
    assert evidence.competing_device_ids == ["heater"]
    isolation = next(iter(pipeline.last_isolations.values()))
    assert isolation.evidence["ct_separated_device_ids"] == []


def test_ct_separability_is_a_positive_statement_only() -> None:
    """``could_share_ct`` is conservative: unknown scope always interferes."""
    from wattmosaic_engine.disaggregation.isolation import could_share_ct

    assert could_share_ct(["ct_a"], {"ct_ids": ["ct_a"]}) is True
    assert could_share_ct(["ct_a"], {"ct_ids": ["ct_b"]}) is False
    # Unknown on the competitor's side.
    assert could_share_ct(["ct_a"], {"ct_ids": []}) is True
    # Unknown on the subject's side.
    assert could_share_ct([], {"ct_ids": ["ct_b"]}) is True
    # And a physical device event exposes the same scope as a dict.
    group = PhysicalDeviceEvent(
        device_id="lamp",
        occurred_at=BASE_TIME,
        received_at=BASE_TIME,
        event_ids=("e1",),
        entity_ids=("light.lamp",),
        states={"light.lamp": "on"},
        previous_states={},
        events=(),
        ct_ids=frozenset({"ct_a"}),
    )
    assert could_share_ct(["ct_a"], group) is True
    assert could_share_ct(["ct_z"], group) is False


def test_per_device_isolation_config_reaches_the_learning_evidence(db: Database) -> None:
    thresholds = LearningThresholds(
        stale_after_s=10**9,
        concurrency_window_s=15.0,
        isolation_by_device={"lamp": {"isolation_window_s": 90.0}},
    )
    pipeline = LearningPipeline(db, thresholds=thresholds)
    event_at = BASE_TIME + timedelta(minutes=5)
    events, windows = _concurrent_cycle(["lamp"], event_at=event_at)
    for window in windows[:-1]:
        pipeline.collector.observe(window)

    seen: list[SampleEvidence | None] = []
    original = pipeline.filter.evaluate

    def spy(sample, evidence=None, **kwargs):
        seen.append(evidence)
        return original(sample, evidence, **kwargs)

    pipeline.filter.evaluate = spy  # type: ignore[method-assign]
    pipeline.consider(events, windows[-1])

    assert seen and seen[0] is not None
    assert seen[0].concurrency_window_s == pytest.approx(90.0)


def test_isolator_result_is_reproducible_without_a_wall_clock() -> None:
    """Two calls with the same injected ``now`` are identical."""
    iso = isolator()
    own = event("e1", "lamp", "light.lamp", BASE_TIME)
    sibling = event("e2", "heater", "switch.heater", BASE_TIME + timedelta(seconds=5))

    first = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)
    second = iso.classify(own, [own, sibling], now=MATURE, coverage=COVERED)

    assert first == second
    # And a fresh isolator with the same config agrees.
    assert isolator().classify(own, [own, sibling], now=MATURE, coverage=COVERED) == first


def test_classify_never_needs_the_real_clock(monkeypatch) -> None:
    import wattmosaic_engine.disaggregation.isolation as isolation_module

    def boom() -> datetime:
        raise AssertionError("the wall clock must not be read when now= is given")

    monkeypatch.setattr(isolation_module, "utcnow", boom)
    own = event("e1", "lamp", "light.lamp", BASE_TIME)

    result = isolator().classify(own, [own], now=MATURE, coverage=COVERED)

    assert result.status == IsolationStatus.ISOLATED
    assert isinstance(result, EventIsolation)
    assert utcnow()  # sanity: the real helper still exists elsewhere
