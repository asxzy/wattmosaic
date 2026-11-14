"""WP5 — device→CT association discovery (Groups C and I).

What this file proves
---------------------
An association is a *typed relation with evidence*, not a field.  Every relation
emitted here must be re-derivable from the numbers in its own ``evidence`` dict,
and every rule that withholds a relation is asserted by a fixture where the
wrong answer is attractively plausible:

* ``C1`` 1 device / 1 CT -- the baseline;
* ``C2`` 1 device / many CTs -- the mandatory Hot Tub: two CTs, one appliance,
  and it must **not** become two independent loads;
* ``C3`` many devices / 1 CT -- both directions of a many-to-many relation;
* ``C4`` many CTs on the same load -- the sibling leg is an overlap, not a sum;
* ``C5`` main/sub overlap -- a nested CT tracks the device, but it is not a
  second independent observation of it;
* ``C6`` independent CTs with similar responses -- no false association;
* ``C7`` negative CT values -- sign is preserved, and a vanishing device is not
  an association;
* ``C8`` missing samples -- absence of data is never support;
* ``C9`` reversed orientation -- explicit, never silently clipped;
* ``I1`` two devices always switch together -- ``AMBIGUOUS``;
* ``I2`` strongly correlated but magnitude-mismatched -- no relation;
* ``I3`` one device never isolated -- insufficient evidence, no relation;
* ``I4`` multiple devices explain one CT change -- ambiguous or split;
* ``I5`` insufficient independent evidence -- a single event is capped;
* ``I6`` conflicting observations -- confidence falls;
* ``I7`` unobserved loads -- nothing invented;
* ``I8`` topology undeterminable -- no relation emitted.

Fixtures come from WP2 (``tools.simulator.hot_tub`` and
``tools.simulator.labhouse.build_scenario``).  ``truth_view`` / ``ground_truth``
is used **only** in assertions; ``analyze`` never receives it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    IsolationStatus,
    MeasurementBasis,
    MeasurementFactor,
    RelationKind,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.association import (
    AssociationConfig,
    AssociationFinder,
    Observation,
    persist,
)
from wattmosaic_engine.disaggregation.isolation import EventIsolation
from wattmosaic_engine.electrical.factors import FactorRegistry

from tools.simulator.hot_tub import CT_L1, CT_L2, hot_tub
from tools.simulator.hot_tub import DEVICE_ID as TUB_ID
from tools.simulator.labhouse import build_scenario

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CT = "ct_branch"
OTHER = "ct_neighbour"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def sample(ct_id: str | None, at: datetime, value: float, *, device_id: str | None = None) -> dict[str, Any]:
    """One observable sample.  ``ct_id`` absent means a device power sample."""
    row: dict[str, Any] = {"state_timestamp": at.isoformat(), "value_w": float(value)}
    if ct_id is not None:
        row["ct_id"] = ct_id
    if device_id is not None:
        row["device_id"] = device_id
    return row


def isolation(
    device_id: str,
    at: datetime,
    *,
    status: IsolationStatus = IsolationStatus.ISOLATED,
    event_id: str | None = None,
) -> EventIsolation:
    competing = ["sibling@x"] if status is IsolationStatus.CONTAMINATED else []
    return EventIsolation(
        status=status,
        event_id=event_id or f"{device_id}@{at.isoformat()}",
        device_id=device_id,
        entity_id=f"switch.{device_id}",
        occurred_at=at,
        received_at=at + timedelta(seconds=1),
        analysis_offset_s=0.0,
        isolation_window_s=30.0,
        window_start=at,
        window_end=at + timedelta(seconds=30),
        competing_event_ids=competing,
        competing_device_ids=[c.split("@")[0] for c in competing],
        reason="isolated" if status is IsolationStatus.ISOLATED else "concurrent_device_change",
        evidence={},
    )


def cycle(
    finder: AssociationFinder,
    device_id: str,
    at: datetime,
    *,
    device_before: float,
    device_after: float,
    cts: dict[str, tuple[float, float]],
    status: IsolationStatus = IsolationStatus.ISOLATED,
    before_n: int = 4,
    after_n: int = 6,
    after_delay_s: float = 0.0,
    before_delay_s: float = 0.0,
) -> list[Observation]:
    """One device event with a flat baseline and a flat response on each CT.

    ``after_delay_s`` shifts *when the response appears* (a delayed CT); the
    samples themselves keep their true timestamps, which is what makes the lag
    search meaningful rather than a relabelling.
    """
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    for ct_id, (b, a) in cts.items():
        for i in range(before_n):
            ts = at - timedelta(seconds=15.0 * (before_n - i)) - timedelta(seconds=before_delay_s)
            before.append(sample(ct_id, ts, b))
        for i in range(after_n):
            ts = at + timedelta(seconds=15.0 * i + after_delay_s)
            after.append(sample(ct_id, ts, a))
    return finder.observe(
        isolation(device_id, at, status=status),
        before,
        after,
        [],
        device_delta_w=device_after - device_before,
    )


def observe_n(
    finder: AssociationFinder,
    device_id: str,
    *,
    n: int,
    device_delta: float,
    ct_deltas: dict[str, float],
    step_s: float = 3600.0,
    start: datetime = T0,
    status: IsolationStatus = IsolationStatus.ISOLATED,
    noise: dict[str, float] | None = None,
    **kwargs: Any,
) -> list[Observation]:
    """``n`` repeated cycles; ``noise`` adds a fixed offset to each CT's response."""
    out: list[Observation] = []
    for index in range(n):
        at = start + timedelta(seconds=step_s * index)
        cts = {}
        for ct_id, delta in ct_deltas.items():
            offset = (noise or {}).get(ct_id, 0.0)
            cts[ct_id] = (1000.0 + offset, 1000.0 + offset + delta)
        out.extend(
            cycle(
                finder,
                device_id,
                at,
                device_before=1000.0,
                device_after=1000.0 + device_delta,
                cts=cts,
                status=status,
                **kwargs,
            )
        )
    return out


def registry(*factors: MeasurementFactor) -> FactorRegistry:
    return FactorRegistry(list(factors))


# --------------------------------------------------------------------------- #
# the Hot Tub fixture, as events
# --------------------------------------------------------------------------- #


def hot_tub_observations(
    name: str,
    *,
    cycles: int = 1,
    finder: AssociationFinder | None = None,
    tolerance_ok: bool = True,
) -> list[Observation]:
    """Replay the WP2 fixture's *observable* table as isolated events.

    The fixture contains one physical cycle (before at 300 s, step at 600 s,
    settled after at 720 s).  Repeated cycles reuse the same numbers at distinct
    timestamps, which is exactly what "the same appliance switching again" looks
    like; nothing is read from the fixture's ground truth.
    """
    fixture = hot_tub(name)
    published = fixture.published_table()
    ha = fixture.observations()["ha_power"]
    finder = finder or AssociationFinder(AssociationConfig(min_events=max(1, cycles)))
    out: list[Observation] = []
    for index in range(cycles):
        at = T0 + timedelta(seconds=3600.0 * index)
        cts = {
            ct_id: (row["before_w"], row["after_w"])
            for ct_id, row in published.items()
        }
        out.extend(
            cycle(
                finder,
                TUB_ID,
                at,
                device_before=ha["before_w"],
                device_after=ha["after_w"],
                cts=cts,
            )
        )
    return out


def hot_tub_registry(name: str) -> FactorRegistry:
    fixture = hot_tub(name)
    rows: list[MeasurementFactor] = []
    for items in fixture.as_measurement_factors().values():
        rows.extend(items)
    return FactorRegistry(rows)


# --------------------------------------------------------------------------- #
# LabHouse fixture -> observations
# --------------------------------------------------------------------------- #


def lab_observations(
    stratum: str,
    *,
    devices: tuple[str, ...],
    cts: tuple[str, ...],
    finder: AssociationFinder | None = None,
    pre_s: float = 60.0,
    post_s: float = 120.0,
    minutes: float = 40.0,
    interval: float = 15.0,
    only_clean: bool = False,
) -> list[Observation]:
    """Turn a LabHouse observation stream into association observations.

    Only ``observations()`` is read: CT watts from ``latest_measurements``,
    device steps from the declared ``events``.  Contamination is *real*: every
    event is classified by ``EventIsolator`` against the other events in the
    same stream, exactly as production does.
    """
    from wattmosaic_engine.disaggregation.isolation import EventIsolator, SampleCoverage

    house = build_scenario(stratum, 42, minutes=minutes, interval=interval)
    view = house.observations()
    finder = finder or AssociationFinder()

    series: dict[str, list[tuple[datetime, float]]] = {}
    events: list[dict[str, Any]] = []
    for record in view:
        for ct_id, reading in record["latest_measurements"].items():
            if ct_id not in cts:
                continue
            series.setdefault(ct_id, []).append(
                (datetime.fromisoformat(reading["state_timestamp"]), float(reading["value_w"]))
            )
        for raw in record["events"]:
            if raw.get("device_id") not in devices:
                continue
            events.append(
                {
                    "event_id": f"{raw['device_id']}@{raw['occurred_at']}",
                    "device_id": raw["device_id"],
                    "entity_id": raw["entity_id"],
                    "occurred_at": datetime.fromisoformat(raw["occurred_at"]),
                    "received_at": datetime.fromisoformat(raw["occurred_at"]),
                    "new_state": raw.get("state"),
                    "event_type": "state_changed",
                }
            )

    events.sort(key=lambda item: item["occurred_at"])
    isolator = EventIsolator()
    last_power: dict[str, float] = {}
    out: list[Observation] = []
    for event in events:
        device = str(event["device_id"])
        at = event["occurred_at"]
        before = [
            sample(ct_id, ts, watts)
            for ct_id, rows in series.items()
            for ts, watts in rows
            if at - timedelta(seconds=pre_s) <= ts < at
        ]
        after = [
            sample(ct_id, ts, watts)
            for ct_id, rows in series.items()
            for ts, watts in rows
            if at <= ts < at + timedelta(seconds=post_s)
        ]
        if not before or not after:
            continue
        coverage = SampleCoverage(baseline_samples=len(before), event_samples=len(after))
        # Declared device step: the event names the new power; the previous
        # value is the last declared power of the same device.
        declared = None
        for raw in view:
            for candidate in raw["events"]:
                if candidate.get("device_id") == device and candidate.get("occurred_at") == event["occurred_at"].isoformat():
                    declared = float(candidate.get("power_w", 0.0))
        if declared is None:
            continue
        delta = declared - last_power.get(device, 0.0)
        last_power[device] = declared
        verdict = isolator.classify(event, events, now=at + timedelta(seconds=post_s + 300), coverage=coverage)
        if only_clean and verdict.status is not IsolationStatus.ISOLATED:
            continue
        out.extend(finder.observe(verdict, before, after, [], device_delta_w=delta))
    return out


# --------------------------------------------------------------------------- #
# C1 — one device, one CT
# --------------------------------------------------------------------------- #


def test_one_device_one_ct_emits_a_candidate_relation_with_evidence() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=4, device_delta=800.0, ct_deltas={CT: 800.0})
    relations = finder.analyze(observations, registry=registry())

    assert len(relations) == 1
    relation = relations[0]
    assert RelationKind(relation.kind) is RelationKind.DEVICE_CT_ASSOCIATION
    assert relation.subject_id == "pump" and relation.object_id == CT
    assert TopologyStatus(relation.status) is TopologyStatus.CANDIDATE
    assert relation.requires_confirmation is True
    assert relation.declared_by == "inference"
    assert relation.algorithm_version == AssociationConfig().algorithm_version
    assert relation.valid_from == T0
    assert relation.valid_to is None

    evidence = relation.evidence
    assert evidence["verdict"] == "supported"
    assert evidence["stats"]["n_events"] == 4
    assert evidence["stats"]["median_delta_w"] == pytest.approx(800.0)
    assert evidence["stats"]["mad_w"] == pytest.approx(0.0)
    assert evidence["stats"]["lag_s"] == 0.0
    assert evidence["stats"]["stability"] == pytest.approx(1.0)
    assert evidence["stats"]["basis"] == MeasurementBasis.CT_MEASURED.value
    assert len(evidence["supporting"]) == 4
    assert evidence["conflicting"] == []
    assert 0.0 < (relation.confidence or 0.0) <= 1.0


# --------------------------------------------------------------------------- #
# C2 — one device, many CTs (the mandatory Hot Tub)
# --------------------------------------------------------------------------- #


def test_hot_tub_equal_deltas_yield_two_associations_and_no_independent_loads() -> None:
    """The brief's regression: CT1 +250, CT2 +250, HA +500."""
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = hot_tub_observations("factor_two", finder=finder)
    relations = finder.analyze(observations, registry=hot_tub_registry("factor_two"))

    assert {r.object_id for r in relations} == {CT_L1, CT_L2}
    assert {r.subject_id for r in relations} == {TUB_ID}
    # One appliance, two conductors: the pair is announced as an overlap so no
    # consumer can add 500 W + 250 W as two loads.
    for relation in relations:
        assert RelationKind(relation.kind) is RelationKind.DEVICE_CT_ASSOCIATION
        assert relation.evidence["overlap"]["ct_ids"] == [CT_L1, CT_L2]
    # Nothing here is parenthood or an independent second load.
    assert all(RelationKind(r.kind) is not RelationKind.ELECTRICAL_PARENT_CHILD for r in relations)
    assert all(RelationKind(r.kind) is not RelationKind.CT_MEASUREMENT_OVERLAP for r in relations)


def test_hot_tub_factor_adjusted_leg_claims_the_whole_appliance() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = hot_tub_observations("factor_two", finder=finder)
    relations = finder.analyze(observations, registry=hot_tub_registry("factor_two"))
    by_ct = {r.object_id: r for r in relations}

    adjusted = by_ct[CT_L1]
    assert adjusted.evidence["stats"]["basis"] == MeasurementBasis.FACTOR_ADJUSTED.value
    assert adjusted.evidence["stats"]["applied_multiplier"] == pytest.approx(2.0)
    # 250 W published x 2 = 500 W = the declared appliance step.
    assert adjusted.evidence["stats"]["median_delta_w"] == pytest.approx(500.0)
    assert adjusted.evidence["stats"]["contribution"] == pytest.approx(1.0)
    # The sibling leg is a physical conductor: 250 W of the same 500 W.
    sibling = by_ct[CT_L2]
    assert sibling.evidence["stats"]["basis"] == MeasurementBasis.CT_MEASURED.value
    assert sibling.evidence["stats"]["contribution"] == pytest.approx(0.5)
    # The factor already claims the appliance, so the two must not be summed.
    assert adjusted.evidence["overlap"]["summing_allowed"] is False
    assert sibling.evidence["overlap"]["summing_allowed"] is False


def test_hot_tub_single_cycle_can_never_be_more_than_low_confidence() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = hot_tub_observations("factor_one", finder=finder)
    relations = finder.analyze(observations, registry=hot_tub_registry("factor_one"))
    assert len(relations) == 2
    for relation in relations:
        assert relation.evidence["verdict"] == "single_event"
        assert (relation.confidence or 0.0) <= 0.5


def test_hot_tub_repeated_cycles_raise_confidence_above_a_single_event() -> None:
    single = AssociationFinder(AssociationConfig(min_events=1))
    one = single.analyze(
        hot_tub_observations("factor_one", cycles=1, finder=single),
        registry=hot_tub_registry("factor_one"),
    )
    repeated = AssociationFinder(AssociationConfig(min_events=1))
    many = repeated.analyze(
        hot_tub_observations("factor_one", cycles=3, finder=repeated),
        registry=hot_tub_registry("factor_one"),
    )
    assert len(many) == 2
    floor = max((r.confidence or 0.0) for r in one)
    for relation in many:
        assert relation.evidence["stats"]["n_events"] == 3
        assert (relation.confidence or 0.0) > floor


# --------------------------------------------------------------------------- #
# C3 — many devices, one CT; C4 — many CTs, one load
# --------------------------------------------------------------------------- #


def test_one_ct_many_devices_is_not_a_one_to_one_assumption() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "lamp_a", n=3, device_delta=120.0, ct_deltas={CT: 120.0})
    observations += observe_n(finder, "lamp_b", n=3, device_delta=380.0, ct_deltas={CT: 380.0}, start=T0 + timedelta(minutes=30))
    relations = finder.analyze(observations, registry=registry())
    assert {r.subject_id for r in relations} == {"lamp_a", "lamp_b"}
    assert {r.object_id for r in relations} == {CT}


def test_many_cts_on_the_same_load_are_an_overlap_not_a_second_load() -> None:
    finder = AssociationFinder()
    observations = observe_n(
        finder, "dryer", n=3, device_delta=2000.0, ct_deltas={"ct_main_l1": 2000.0, "ct_main_l2": 0.0}
    )
    relations = finder.analyze(observations, registry=registry())
    assert [r.object_id for r in relations] == ["ct_main_l1"]
    # The zero-response CT is not a relation, and nothing was invented for it.
    assert all(RelationKind(r.kind) is RelationKind.DEVICE_CT_ASSOCIATION for r in relations)


def test_the_same_wire_seen_by_two_monitors_is_one_load_not_two() -> None:
    """C4: two clamps on one conductor report identical watts and identical levels."""
    finder = AssociationFinder()
    observations = observe_n(
        finder,
        "dryer",
        n=3,
        device_delta=2000.0,
        ct_deltas={"ct_monitor_a": 2000.0, "ct_monitor_b": 2000.0},
    )
    relations = finder.analyze(observations, registry=registry())
    assert {r.object_id for r in relations} == {"ct_monitor_a", "ct_monitor_b"}
    for relation in relations:
        overlap = relation.evidence["overlap"]
        assert overlap["summing_allowed"] is False
        assert overlap["reason"] == "duplicate_measurement_of_one_device"
        # One device, one appliance: 2000 W total, never 2000 + 2000.
        assert sum(overlap["contributions"].values()) == pytest.approx(2.0)


def test_a_shared_event_never_becomes_an_independent_power_estimate() -> None:
    """Two devices switching together: the CT is observed, the share is not."""
    finder = AssociationFinder()
    observations = observe_n(finder, "twin_a", n=4, device_delta=900.0, ct_deltas={CT: 900.0})
    observations += observe_n(finder, "twin_b", n=4, device_delta=900.0, ct_deltas={CT: 900.0})
    relations = finder.analyze(observations, registry=registry())

    for relation in relations:
        stats = relation.evidence["stats"]
        # No solo event was ever seen, so no per-device share exists ...
        assert stats["n_supporting"] == 0
        assert stats["n_joint_events"] == 4
        assert stats["median_delta_w"] == 0.0
        # ... yet the CT's real response is still recorded, labelled as shared.
        assert stats["median_observed_delta_w"] == pytest.approx(900.0)
        assert stats["median_observed_ratio"] == pytest.approx(1.0)
        assert relation.evidence["verdict"] == "ambiguous"


# --------------------------------------------------------------------------- #
# C5 — main/sub overlap: tracking is not independent observation
# --------------------------------------------------------------------------- #


def test_a_nested_feed_tracks_a_device_but_is_not_an_independent_observation() -> None:
    finder = AssociationFinder()
    observations = observe_n(
        finder,
        "water_heater",
        n=3,
        device_delta=1000.0,
        ct_deltas={"ct_sub_branch": 1000.0, "ct_main_l1": 1000.0},
    )
    relations = finder.analyze(observations, registry=registry())
    # Both CTs legitimately observe the device ...
    assert {r.object_id for r in relations} == {"ct_sub_branch", "ct_main_l1"}
    # ... but summing two CTs that see the *same* watts would double count.
    for relation in relations:
        assert relation.evidence["overlap"]["summing_allowed"] is False
        assert relation.evidence["overlap"]["reason"] == "duplicate_measurement_of_one_device"


# --------------------------------------------------------------------------- #
# C6 / I2 — correlation alone is insufficient
# --------------------------------------------------------------------------- #


def test_independent_cts_with_similar_responses_do_not_both_associate() -> None:
    """Two unrelated circuits each rise by the device's whole step."""
    finder = AssociationFinder()
    observations = observe_n(
        finder,
        "vacuum",
        n=3,
        device_delta=900.0,
        ct_deltas={"ct_a": 900.0, "ct_b": 900.0},
        noise={"ct_a": 0.0, "ct_b": 4000.0},
    )
    relations = finder.analyze(observations, registry=registry())
    assert relations == []
    withheld = finder.withheld(observations)
    assert withheld["vacuum"]["budget_exceeded"] == 1


def test_strongly_correlated_but_magnitude_mismatched_ct_never_associates() -> None:
    finder = AssociationFinder()
    # Same shape, every event, but the CT moves four times as much.
    observations = observe_n(finder, "kettle", n=5, device_delta=300.0, ct_deltas={OTHER: 1200.0})
    assert finder.analyze(observations, registry=registry()) == []
    assert finder.withheld(observations)["kettle"]["magnitude_overshoot"] == 5


def test_noise_does_not_let_a_spike_dominate_the_median() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "fan", n=5, device_delta=200.0, ct_deltas={CT: 200.0})
    # One in five events reports a 5000 W spike.  Median/MAD must ignore it.
    spiked = list(observations)
    spiked[0] = Observation(
        **{
            **{f: getattr(observations[0], f) for f in observations[0].__slots__},
            "ct_delta_w": 5000.0,
        }
    )
    relations = finder.analyze(spiked, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["stats"]["median_delta_w"] == pytest.approx(200.0)
    # The spike is retained as conflicting evidence, not silently dropped.
    assert any(item["reason"] == "magnitude_overshoot" for item in relations[0].evidence["conflicting"])


def test_a_single_spiky_sample_does_not_move_the_measured_step() -> None:
    """The median is why one bad sample cannot invent a step at measurement time."""
    finder = AssociationFinder()
    at = T0
    before = [sample(CT, at - timedelta(seconds=15 * (4 - i)), 1000.0) for i in range(4)]
    # One of six "after" samples is a 40 kW transient from a neighbouring load.
    after = [sample(CT, at + timedelta(seconds=15 * i), 1200.0) for i in range(6)]
    after[2] = sample(CT, at + timedelta(seconds=30), 40000.0)
    observations = finder.observe(
        isolation("pump", at), before, after, [], device_delta_w=200.0
    )

    assert len(observations) == 1
    # Median of the settled window is 1200 W, so the step is the real 200 W.
    assert observations[0].ct_delta_w == pytest.approx(200.0)
    assert observations[0].ct_noise_w == pytest.approx(0.0)
    # And the spike never reaches the evidence as a 39 kW response.
    relations = finder.analyze(observations, registry=registry())
    assert relations == []  # one event, below min_events: no candidate yet
    assert finder.withheld(observations)["pump"]["too_few_events"] == 1


# --------------------------------------------------------------------------- #
# C7 — negative values
# --------------------------------------------------------------------------- #


def test_a_negative_ct_step_is_rejected_when_the_device_step_is_positive() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "heater", n=3, device_delta=1500.0, ct_deltas={CT: -1500.0})
    assert finder.analyze(observations, registry=registry()) == []
    assert finder.withheld(observations)["heater"]["opposite_direction"] == 3


def test_a_negative_device_step_associates_with_a_negative_ct_step() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "heater", n=3, device_delta=-1500.0, ct_deltas={CT: -1500.0})
    relations = finder.analyze(observations, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["stats"]["contribution"] == pytest.approx(1.0)
    assert relations[0].evidence["stats"]["median_delta_w"] == pytest.approx(-1500.0)


# --------------------------------------------------------------------------- #
# C8 — missing samples
# --------------------------------------------------------------------------- #


def test_missing_samples_are_insufficient_data_never_support() -> None:
    finder = AssociationFinder()
    clean = observe_n(finder, "tv", n=3, device_delta=150.0, ct_deltas={CT: 150.0})
    blind = finder.observe(
        isolation("tv", T0 + timedelta(minutes=10), status=IsolationStatus.INSUFFICIENT_DATA),
        [],
        [],
        [],
        device_delta_w=150.0,
    )
    assert blind == []
    relations = finder.analyze(clean + blind, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["stats"]["n_events"] == 3


def test_a_device_with_no_usable_ct_sample_emits_nothing() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "ghost", n=3, device_delta=500.0, ct_deltas={CT: 0.0})
    assert finder.analyze(observations, registry=registry()) == []


# --------------------------------------------------------------------------- #
# C9 — reversed orientation
# --------------------------------------------------------------------------- #


def test_a_reversed_ct_still_associates_once_orientation_is_accounted_for() -> None:
    """A CT clamped backwards reports ``-delta``; the sign is evidence, not noise."""
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=650.0, ct_deltas={CT: -650.0})
    assert finder.analyze(observations, registry=registry()) == []

    corrected = [
        Observation(**{**{f: getattr(o, f) for f in o.__slots__}, "ct_delta_w": -o.ct_delta_w})
        for o in observations
    ]
    relations = finder.analyze(corrected, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["verdict"] != "ambiguous"
    # The reversed observations remain withheld, with the reason stated: the
    # relation is only emitted once orientation is accounted for, never by
    # clipping the sign away.
    reversed_only = finder.analyze(observations, registry=registry())
    assert reversed_only == []
    assert finder.withheld(observations)["pump"]["opposite_direction"] == 3


def test_a_declared_inverted_polarity_is_applied_exactly_once_in_observe() -> None:
    """The wiring fix is a declared fact, so it is corrected upstream, not guessed."""
    polarity: dict[str, Any] = {CT: "inverted"}
    finder = AssociationFinder()
    observations: list[Observation] = []
    for index in range(3):
        at = T0 + timedelta(hours=index)
        # The clamp is backwards: the raw CT falls while the pump rises.
        observations.extend(
            finder.observe(
                isolation("pump", at),
                [sample(CT, at - timedelta(seconds=15 * (4 - i)), 1000.0) for i in range(4)],
                [sample(CT, at + timedelta(seconds=15 * i), 350.0) for i in range(6)],
                [],
                device_delta_w=650.0,
                polarity=polarity,
            )
        )

    # Every observation carries the declared correction, and the delta is right.
    assert {item.orientation for item in observations} == {"inverted_declared"}
    assert all(item.ct_delta_w == pytest.approx(650.0) for item in observations)
    relations = finder.analyze(observations, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["stats"]["median_delta_w"] == pytest.approx(650.0)
    # Applied once, not twice: the share is 1.0, not 2.0.
    assert relations[0].evidence["stats"]["contribution"] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# I1 / I4 — ambiguity
# --------------------------------------------------------------------------- #


def test_two_devices_that_always_switch_together_are_ambiguous() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "twin_a", n=4, device_delta=900.0, ct_deltas={CT: 900.0})
    observations += observe_n(finder, "twin_b", n=4, device_delta=900.0, ct_deltas={CT: 900.0})
    relations = finder.analyze(observations, registry=registry())

    assert {r.subject_id for r in relations} == {"twin_a", "twin_b"}
    for relation in relations:
        assert relation.evidence["verdict"] == "ambiguous"
        assert relation.evidence["ambiguous_with"] == sorted(
            {"twin_a", "twin_b"} - {relation.subject_id}
        )
        # Ambiguity is represented in evidence because TopologyStatus has no
        # AMBIGUOUS member; confidence is lowered accordingly.
        assert TopologyStatus(relation.status) is TopologyStatus.CANDIDATE
        assert (relation.confidence or 0.0) < 0.5 * _supported_confidence(4)


def test_multiple_devices_explaining_one_ct_change_split_when_solo_events_discriminate() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "shared_a", n=2, device_delta=100.0, ct_deltas={CT: 100.0})
    observations += observe_n(finder, "shared_b", n=2, device_delta=400.0, ct_deltas={CT: 400.0}, start=T0 + timedelta(minutes=30))
    observations += observe_n(finder, "shared_a", n=2, device_delta=100.0, ct_deltas={CT: 500.0}, start=T0 + timedelta(minutes=60))
    observations += observe_n(finder, "shared_b", n=2, device_delta=400.0, ct_deltas={CT: 500.0}, start=T0 + timedelta(minutes=60))
    relations = finder.analyze(observations, registry=registry())

    assert {r.subject_id for r in relations} == {"shared_a", "shared_b"}
    for relation in relations:
        assert relation.evidence["verdict"] == "split"
        assert relation.evidence["split_with"]
        assert relation.evidence["split_with"][0]["device_ids"] == ["shared_a", "shared_b"]


def _supported_confidence(n: int) -> float:
    return 1.0 - 0.5**n


# --------------------------------------------------------------------------- #
# I3 / I5 — insufficient evidence
# --------------------------------------------------------------------------- #


def test_one_device_never_isolated_emits_no_relation() -> None:
    finder = AssociationFinder()
    observations = observe_n(
        finder,
        "oven",
        n=5,
        device_delta=2000.0,
        ct_deltas={CT: 2000.0},
        status=IsolationStatus.CONTAMINATED,
    )
    assert finder.analyze(observations, registry=registry()) == []
    assert finder.withheld(observations)["oven"]["contaminated_event"] == 5


def test_below_min_events_no_relation_is_emitted() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=3))
    observations = observe_n(finder, "kettle", n=2, device_delta=900.0, ct_deltas={CT: 900.0})
    assert finder.analyze(observations, registry=registry()) == []
    assert finder.withheld(observations)["kettle"]["too_few_events"] == 1


def test_a_single_event_can_never_exceed_low_confidence() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = observe_n(finder, "kettle", n=1, device_delta=900.0, ct_deltas={CT: 900.0})
    relations = finder.analyze(observations, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["verdict"] == "single_event"
    assert (relations[0].confidence or 0.0) <= 0.5


# --------------------------------------------------------------------------- #
# I6 — conflicting observations lower confidence
# --------------------------------------------------------------------------- #


def test_conflicting_evidence_lowers_confidence() -> None:
    finder = AssociationFinder()
    clean = observe_n(finder, "washer", n=4, device_delta=600.0, ct_deltas={CT: 600.0})
    conflicted = clean + observe_n(
        finder,
        "washer",
        n=4,
        device_delta=600.0,
        ct_deltas={OTHER: -600.0},
    )
    plain = finder.analyze(clean, registry=registry())
    mixed = finder.analyze(conflicted, registry=registry())
    by_pair = {(r.subject_id, r.object_id): r for r in mixed}
    assert by_pair[("washer", CT)]
    # A CT moving the wrong way is never a second association ...
    assert ("washer", OTHER) not in by_pair
    # ... but it is recorded against the healthy relation and lowers confidence.
    assert by_pair[("washer", CT)].evidence["conflicting"]
    assert (by_pair[("washer", CT)].confidence or 0.0) < (plain[0].confidence or 0.0)


def test_a_ct_that_only_responds_sometimes_is_not_support() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "fridge", n=6, device_delta=150.0, ct_deltas={CT: 150.0})
    # Half the events show no CT response at all (unrelated coincidence).
    observations += observe_n(
        finder, "fridge", n=6, device_delta=150.0, ct_deltas={OTHER: 0.0}, start=T0 + timedelta(hours=1)
    )
    relations = finder.analyze(observations, registry=registry())
    by_ct = {r.object_id: r for r in relations}
    assert CT in by_ct
    assert OTHER not in by_ct


# --------------------------------------------------------------------------- #
# I7 / I8 — unobserved loads, undeterminable topology
# --------------------------------------------------------------------------- #


def test_an_unobserved_load_never_produces_a_relation() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "unknown_load_l1", n=4, device_delta=500.0, ct_deltas={})
    assert finder.analyze(observations, registry=registry()) == []
    # The device is still reported as withheld, with a reason, not as absent.
    assert finder.withheld(observations)["unknown_load_l1"]["no_ct_evidence"] == 1


def test_topology_undeterminable_emits_nothing() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "mystery", n=5, device_delta=400.0, ct_deltas={CT: 200.0})
    # Every response is within the CT's own noise: the step is not established.
    noisy = []
    for item in observations:
        noisy.append(
            Observation(
                **{
                    **{f: getattr(item, f) for f in item.__slots__},
                    "ct_noise_w": 200.0,
                }
            )
        )
    assert finder.analyze(noisy, registry=registry()) == []
    assert finder.withheld(noisy)["mystery"]["response_within_noise"] >= 1


# --------------------------------------------------------------------------- #
# lag
# --------------------------------------------------------------------------- #


def test_a_delayed_ct_response_still_associates_and_reports_its_lag() -> None:
    finder = AssociationFinder()
    observations: list[Observation] = []
    for index in range(3):
        at = T0 + timedelta(hours=index)
        observations.extend(
            cycle(
                finder,
                "pump",
                at,
                device_before=500.0,
                device_after=1500.0,
                cts={CT: (200.0, 1200.0)},
                after_delay_s=45.0,
            )
        )
    relations = finder.analyze(observations, registry=registry())
    assert len(relations) == 1
    assert relations[0].evidence["stats"]["lag_s"] == pytest.approx(45.0, abs=15.0)
    assert relations[0].evidence["stats"]["n_events"] == 3


def test_an_immediate_response_reports_zero_lag() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=1000.0, ct_deltas={CT: 1000.0})
    relations = finder.analyze(observations, registry=registry())
    assert relations[0].evidence["stats"]["lag_s"] == 0.0


def test_lag_is_quantised_to_the_publish_cadence_not_invented() -> None:
    """A +50 s response on a 15 s channel is 45 s, never a fabricated 50 s."""
    finder = AssociationFinder()
    observations: list[Observation] = []
    for index in range(3):
        at = T0 + timedelta(hours=index)
        observations.extend(
            finder.observe(
                isolation("pump", at),
                [sample(CT, at - timedelta(seconds=15 * (4 - i)), 500.0) for i in range(4)],
                [sample(CT, at + timedelta(seconds=15 * i + 50), 1500.0) for i in range(6)],
                [],
                device_delta_w=1000.0,
            )
        )
    relation = finder.analyze(observations, registry=registry())[0]
    assert relation.evidence["stats"]["lag_s"] == pytest.approx(45.0)


def test_a_ct_already_moved_before_the_event_is_not_instant() -> None:
    """A cross *before* the event is not a zero-lag response; it is no evidence."""
    finder = AssociationFinder()
    observations: list[Observation] = []
    for index in range(3):
        at = T0 + timedelta(hours=index)
        observations.extend(
            finder.observe(
                isolation("pump", at),
                # The CT had already risen before the device step: its baseline
                # is 1000 W and the "after" samples start 10 s earlier still high.
                [sample(CT, at - timedelta(seconds=60 + 15 * i), 1000.0) for i in range(3)],
                [sample(CT, at - timedelta(seconds=10) + timedelta(seconds=15 * i), 1500.0)
                 for i in range(6)],
                [],
                device_delta_w=1000.0,
            )
        )
    # A cross before the event is not an instant response, and nothing supports.
    assert all(item.lag_s == pytest.approx(finder.config.max_lag_s) for item in observations)
    assert finder.analyze(observations, registry=registry()) == []
    assert finder.withheld(observations)["pump"]["lag_exceeded"] == 3


# --------------------------------------------------------------------------- #
# basis compatibility
# --------------------------------------------------------------------------- #


def test_unknown_basis_is_refused_rather_than_compared() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "tub", n=3, device_delta=500.0, ct_deltas={CT: 500.0})
    unknown_basis = FactorRegistry(
        [MeasurementFactor(ct_id=CT, multiplier=2.0, effective_from=T0, applied_by="unknown")]
    )
    assert finder.analyze(observations, registry=unknown_basis) == []
    assert finder.withheld(observations)["tub"]["basis_unestablished"] >= 1


def test_cross_basis_comparison_is_converted_with_the_recorded_multiplier() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "tub", n=3, device_delta=500.0, ct_deltas={CT: 250.0})
    adjusted = FactorRegistry(
        [MeasurementFactor(ct_id=CT, multiplier=2.0, effective_from=T0, applied_by="user")]
    )
    relations = finder.analyze(observations, registry=adjusted)
    assert len(relations) == 1
    stats = relations[0].evidence["stats"]
    assert stats["basis"] == MeasurementBasis.FACTOR_ADJUSTED.value
    assert stats["applied_multiplier"] == pytest.approx(2.0)
    assert stats["raw_median_delta_w"] == pytest.approx(250.0)
    assert stats["median_delta_w"] == pytest.approx(500.0)


# --------------------------------------------------------------------------- #
# not name-based
# --------------------------------------------------------------------------- #


def test_a_relation_is_unchanged_when_ct_labels_are_permuted() -> None:
    finder = AssociationFinder()
    observers = observe_n(finder, "pump", n=3, device_delta=700.0, ct_deltas={"ct_alpha": 700.0})
    relabelled = [
        Observation(**{**{f: getattr(o, f) for f in o.__slots__}, "ct_id": "zzz_other"})
        for o in observers
    ]
    first = finder.analyze(observers, registry=registry())[0]
    second = finder.analyze(relabelled, registry=registry())[0]

    def normalise(relation: TopologyRelation, ct_name: str) -> dict[str, Any]:
        evidence = dict(relation.evidence)
        evidence.pop("relation_id", None)
        text = repr(evidence).replace(ct_name, "<CT>")
        return {"confidence": relation.confidence, "evidence": text, "kind": relation.kind}

    assert normalise(first, "ct_alpha") == normalise(second, "zzz_other")


def test_relation_id_is_deterministic_and_name_free() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=700.0, ct_deltas={CT: 700.0})
    first = finder.analyze(observations, registry=registry())[0]
    second = finder.analyze(observations, registry=registry())[0]
    assert first.relation_id == second.relation_id
    assert CT in first.relation_id or "dca_" in first.relation_id
    assert "pump" not in first.evidence.get("notes", [])


# --------------------------------------------------------------------------- #
# confidence formula
# --------------------------------------------------------------------------- #


def test_confidence_is_the_documented_product_of_the_evidence_terms() -> None:
    config = AssociationConfig()
    finder = AssociationFinder(config)
    observations = observe_n(finder, "pump", n=4, device_delta=800.0, ct_deltas={CT: 800.0})
    relation = finder.analyze(observations, registry=registry())[0]

    repeatability = 1.0 - 0.5**4
    stability = relation.evidence["stats"]["stability"]
    expected = repeatability * stability * 1.0 * (1.0 / (1.0 + config.conflict_penalty * 0))
    assert relation.confidence == pytest.approx(min(expected, 1.0))


def test_more_events_raise_confidence_monotonically() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    previous = 0.0
    for n in (1, 2, 3, 4, 6):
        observations = observe_n(finder, "pump", n=n, device_delta=800.0, ct_deltas={CT: 800.0})
        relation = finder.analyze(observations, registry=registry())[0]
        assert (relation.confidence or 0.0) > previous
        previous = relation.confidence or 0.0


# --------------------------------------------------------------------------- #
# withdrawal and persistence (G9)
# --------------------------------------------------------------------------- #


def test_withdraw_sets_valid_to_and_keeps_the_history() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=800.0, ct_deltas={CT: 800.0})
    relation = finder.analyze(observations, registry=registry(), now=T0 + timedelta(days=1))[0]
    later = T0 + timedelta(days=2)
    withdrawn = finder.withdraw(relation, "measurement evidence contradicts the association", now=later)

    assert withdrawn.relation_id == relation.relation_id
    assert TopologyStatus(withdrawn.status) is TopologyStatus.REJECTED
    assert withdrawn.valid_to == later
    assert withdrawn.valid_from == relation.valid_from
    assert withdrawn.evidence["withdrawal"]["reason"]
    assert withdrawn.evidence["supporting"] == relation.evidence["supporting"]


def test_persist_is_idempotent_and_keeps_a_withdrawn_validity(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=800.0, ct_deltas={CT: 800.0})
    relation = finder.analyze(observations, registry=registry(), now=T0)[0]
    withdrawn = finder.withdraw(relation, "superseded", now=T0 + timedelta(days=3))

    db = Database(tmp_db_path)
    try:
        assert persist([relation], db) == 1
        assert persist([relation], db) == 1
        assert len(db.relations_of_kind(RelationKind.DEVICE_CT_ASSOCIATION)) == 1

        persist([withdrawn], db)
        rows = db.relations_of_kind(RelationKind.DEVICE_CT_ASSOCIATION)
        assert len(rows) == 1
        assert rows[0]["valid_to"] is not None
        assert rows[0]["status"] == TopologyStatus.REJECTED.value
        assert rows[0]["evidence"]["withdrawal"]["reason"] == "superseded"
    finally:
        db.close()


def test_a_seeded_replay_stamps_the_injected_instant_not_the_wall_clock() -> None:
    """D6: ``created_at`` is analysis output, so it comes from ``now``.

    Two replays of the same observations used to differ by microseconds, which
    makes two seeded runs impossible to compare byte for byte -- and the record
    looked like it had been asserted at replay time rather than at the event.
    """
    at = T0 + timedelta(days=400)
    finder = AssociationFinder()
    observations = observe_n(finder, "pump", n=3, device_delta=800.0, ct_deltas={CT: 800.0})
    first = finder.analyze(observations, registry=registry(), now=at)
    again = AssociationFinder().analyze(observations, registry=registry(), now=at)
    assert first and again
    assert {r.created_at for r in first} == {at}
    assert [r.model_dump() for r in first] == [r.model_dump() for r in again]

    # With no explicit instant and real event history, the earliest supporting
    # event is used -- the clock is never consulted on the pure analysis path.
    inferred = AssociationFinder().analyze(observations, registry=registry())
    assert {r.created_at for r in inferred} == {
        min(
            datetime.fromisoformat(row["occurred_at"])
            for row in relation.evidence["supporting"]
        )
        for relation in inferred
    }



# --------------------------------------------------------------------------- #
# WP2 fixtures: end to end through the real isolation verdict
# --------------------------------------------------------------------------- #


def test_labhouse_dedicated_stratum_associates_the_water_heater_to_its_branch() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = lab_observations(
        "dedicated",
        devices=("water_heater",),
        cts=("ct_main_water_heater_ct1", "ct_main_water_heater_ct2"),
        finder=finder,
        minutes=60.0,
    )
    assert observations, "the dedicated stratum must produce analysable events"
    relations = finder.analyze(observations, registry=registry())
    assert {r.subject_id for r in relations} == {"water_heater"}
    assert relations
    for relation in relations:
        assert relation.object_id.startswith("ct_main_water_heater")


def test_labhouse_ambiguous_pair_is_ambiguous_not_two_loads() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = lab_observations(
        "ambiguous_pair",
        devices=("amb_a", "amb_b"),
        cts=("ct_main_utility_plug_ct1",),
        finder=finder,
        minutes=45.0,
    )
    relations = finder.analyze(observations, registry=registry())
    assert {r.subject_id for r in relations} <= {"amb_a", "amb_b"}
    for relation in relations:
        # Two identical loads on one conductor can never be told apart.
        assert relation.evidence["verdict"] in {"ambiguous", "split"}
        assert TopologyStatus(relation.status) is TopologyStatus.CANDIDATE


def test_labhouse_missing_intervals_never_fabricate_support() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = lab_observations(
        "missing_intervals",
        devices=("lighting_1",),
        cts=("ct_main_lighting_ct1",),
        finder=finder,
        minutes=40.0,
        interval=15.0,
    )
    for observation in observations:
        assert observation.ct_samples >= 1
    relations = finder.analyze(observations, registry=registry())
    for relation in relations:
        for item in relation.evidence["supporting"]:
            assert item["ct_delta_w"] is not None
    # Whatever it concludes, it never claims support from an absent sample.


def test_labhouse_unknown_load_has_no_ct_to_associate_with() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = lab_observations(
        "unknown_load",
        devices=("unknown_load_l1", "unknown_load_l2"),
        cts=("ct_main_lighting_ct1", "ct_sub_l1"),
        finder=finder,
        minutes=40.0,
    )
    relations = finder.analyze(observations, registry=registry())
    assert relations == []


def test_labhouse_negative_ct_stratum_is_handled_by_sign_not_by_clipping() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = lab_observations(
        "negative_ct",
        devices=("refrigerator_1", "refrigerator_2"),
        cts=("ct_sub_l1",),
        finder=finder,
        minutes=60.0,
    )
    relations = finder.analyze(observations, registry=registry())
    for relation in relations:
        stats = relation.evidence["stats"]
        assert isinstance(stats["median_delta_w"], float)
        # Sign is preserved end to end; nothing is clamped to zero.
        assert stats["median_delta_w"] == pytest.approx(stats["raw_median_delta_w"])


def test_labhouse_scenario_observations_never_contain_truth_keys() -> None:
    from tools.simulator.frames import assert_no_ground_truth_keys

    for stratum in ("dedicated", "shared", "main_subpanel_overlap"):
        house = build_scenario(stratum, 42, minutes=12.0, interval=30.0)
        for view in house.observations():
            assert_no_ground_truth_keys(view, where=f"{stratum} observation view")


# --------------------------------------------------------------------------- #
# evidence rules that are pure
# --------------------------------------------------------------------------- #


def test_a_spike_never_produces_a_relation_on_its_own() -> None:
    finder = AssociationFinder()
    observations = observe_n(finder, "lamp", n=3, device_delta=100.0, ct_deltas={CT: 100.0})
    spikes = list(observations)
    spikes[1] = Observation(
        **{
            **{f: getattr(observations[1], f) for f in observations[1].__slots__},
            "ct_delta_w": 9000.0,
        }
    )
    relation = finder.analyze(spikes, registry=registry())[0]
    assert relation.evidence["stats"]["median_delta_w"] == pytest.approx(100.0)
    assert relation.evidence["stats"]["n_events"] == 3


def test_algorithm_version_travels_with_the_relation() -> None:
    finder = AssociationFinder(AssociationConfig(algorithm_version="assoc@test"))
    observations = observe_n(finder, "pump", n=3, device_delta=800.0, ct_deltas={CT: 800.0})
    relation = finder.analyze(observations, registry=registry())[0]
    assert relation.algorithm_version == "assoc@test"


def test_config_rejects_impossible_thresholds() -> None:
    with pytest.raises(ValueError):
        AssociationConfig(min_events=0)
    with pytest.raises(ValueError):
        AssociationConfig(magnitude_tolerance_frac=-0.1)
    with pytest.raises(ValueError):
        AssociationConfig(stability_min=1.5)
    with pytest.raises(ValueError):
        AssociationConfig(max_lag_s=-1.0)
    with pytest.raises(ValueError):
        AssociationConfig(conflict_penalty=-0.5)


def test_confidence_is_bounded_and_never_claims_certainty() -> None:
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations = observe_n(finder, "pump", n=40, device_delta=800.0, ct_deltas={CT: 800.0})
    relation = finder.analyze(observations, registry=registry())[0]
    confidence = relation.confidence or 0.0
    assert 0.0 < confidence < 1.0
    assert not math.isnan(confidence)
