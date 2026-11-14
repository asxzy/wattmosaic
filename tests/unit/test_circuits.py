"""WP6 — circuit classification (Groups D and E).

What this file proves
---------------------
A circuit's *electrical scope* is its own fact, with its own evidence and its
own confidence.  ``CTType`` describes a sensor and ``MonitoringLevel``
describes a device's visibility; neither can say "one device, and only that
device" or "two known devices *and* load we cannot name".

Every rule is pinned by a fixture where the wrong answer is attractive:

* ``D1`` the EV charger is dedicated and its classification is *confirmed* only
  once the residual has actually been checked;
* ``D2`` the mandatory Hot Tub with a declared ×2 factor is **one** dedicated
  appliance -- not two loads, and not two circuits;
* ``D3`` the A/C compressor duty cycle is dedicated;
* ``D4`` a device with standby power stays dedicated (standby is a property of
  the device, never a second load);
* ``D5`` a continuously varying (dimmable) load stays dedicated while its
  response tracks the CT;
* ``D6`` a circuit that *appears* dedicated but carries a second load is not
  dedicated, and a single known device plus unexplained load is not silently
  promoted to ``PARTIALLY_UNDERSTOOD`` (that class requires two known devices);
* ``D7`` one isolated event is not evidence of a dedicated circuit -- the answer
  is ``UNKNOWN``, which is therefore reachable;
* ``E1``/``E2``/``E3``/``E4`` several loads on one breaker are ``SHARED`` because
  each has its own isolated evidence;
* ``E5`` two loads that only ever switch together are ambiguous, so ``SHARED``
  is refused rather than asserted;
* ``E6``/``E7`` two known devices plus unattributable load is
  ``PARTIALLY_UNDERSTOOD`` with the residual recorded, not ``SHARED``.

Fixtures come from WP2 (``tools.simulator.hot_tub`` and
``tools.simulator.labhouse.build_scenario``).  ``truth_view`` / ``ground_truth``
is used **only** in assertions; ``classify`` never receives it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    Circuit,
    CircuitClass,
    ConfidenceVector,
    CTChannel,
    CTType,
    IsolationStatus,
    MeasurementBasis,
    MeasurementFactor,
    Phase,
    ResidualPower,
    Topology,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.association import (
    AssociationConfig,
    AssociationFinder,
    Observation,
)
from wattmosaic_engine.disaggregation.circuits import (
    CircuitClassifier,
    CircuitConfig,
    explain,
    persist,
)
from wattmosaic_engine.disaggregation.isolation import EventIsolation
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.models.residual_power import ResidualAnalyzer, ResidualScope

from tools.simulator.hot_tub import DEVICE_ID as TUB_ID
from tools.simulator.hot_tub import hot_tub
from tools.simulator.labhouse import build_scenario

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ONE_CT = "ct_branch"
OTHER_CT = "ct_neighbour"


# --------------------------------------------------------------------------- #
# builders: observable samples -> real association relations
# --------------------------------------------------------------------------- #


def sample(ct_id: str | None, at: datetime, value: float) -> dict[str, Any]:
    row: dict[str, Any] = {"state_timestamp": at.isoformat(), "value_w": float(value)}
    if ct_id is not None:
        row["ct_id"] = ct_id
    return row


def isolation(device_id: str, at: datetime) -> EventIsolation:
    return EventIsolation(
        status=IsolationStatus.ISOLATED,
        event_id=f"{device_id}@{at.isoformat()}",
        device_id=device_id,
        entity_id=f"switch.{device_id}",
        occurred_at=at,
        received_at=at + timedelta(seconds=1),
        analysis_offset_s=0.0,
        isolation_window_s=30.0,
        window_start=at,
        window_end=at + timedelta(seconds=30),
        competing_event_ids=[],
        competing_device_ids=[],
        reason="isolated",
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
    before_n: int = 4,
    after_n: int = 6,
) -> list[Observation]:
    """One device event: a flat baseline and a flat response on each CT."""
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    for ct_id, (b, a) in cts.items():
        for i in range(before_n):
            before.append(sample(ct_id, at - timedelta(seconds=15.0 * (before_n - i)), b))
        for i in range(after_n):
            after.append(sample(ct_id, at + timedelta(seconds=15.0 * i), a))
    return finder.observe(
        isolation(device_id, at),
        before,
        after,
        [],
        device_delta_w=device_after - device_before,
    )


def repeated(
    device_id: str,
    *,
    n: int,
    off_w: float,
    on_w: float,
    cts: dict[str, tuple[float, float]],
    step_s: float = 3600.0,
    start: datetime = T0,
) -> list[Observation]:
    """``n`` independent isolated events for one device."""
    finder = AssociationFinder(AssociationConfig(min_events=1))
    out: list[Observation] = []
    for index in range(n):
        out.extend(
            cycle(
                finder,
                device_id,
                start + timedelta(seconds=step_s * index),
                device_before=off_w,
                device_after=on_w,
                cts=cts,
            )
        )
    return out


def jittered(
    device_id: str,
    *,
    n: int,
    off_w: float,
    on_w: float,
    cts: dict[str, tuple[float, float]],
    wobble_frac: float = 0.10,
    step_s: float = 3600.0,
    start: datetime = T0,
) -> list[Observation]:
    """Like :func:`repeated`, but each *event* responds a little differently.

    Stability is ``1 - MAD(share) / median(share)`` across events, so a fixture
    whose response is identical every time scores exactly 1.0 and cannot
    exercise the stability bar.  The offsets spread the per-event shares around
    the device's own step, so the *median* share still says "the device explains
    the CT" while the events genuinely disagree -- a real conductor.
    """
    step = on_w - off_w
    offsets = (0.0, -wobble_frac, -wobble_frac / 2.0, wobble_frac / 2.0)
    finder = AssociationFinder(AssociationConfig(min_events=1))
    out: list[Observation] = []
    for index in range(n):
        offset = step * offsets[index % len(offsets)]
        out.extend(
            cycle(
                finder,
                device_id,
                start + timedelta(seconds=step_s * index),
                device_before=off_w,
                device_after=on_w,
                cts={
                    ct_id: (b, a + offset) for ct_id, (b, a) in cts.items()
                },
            )
        )
    return out


def relations_of(
    observations: list[Observation], *, registry: FactorRegistry | None = None
) -> list[TopologyRelation]:
    return AssociationFinder(AssociationConfig(min_events=1)).analyze(
        observations, registry=registry or FactorRegistry()
    )


def hot_tub_relations(name: str, *, cycles: int) -> tuple[list[TopologyRelation], FactorRegistry]:
    """Replay the WP2 fixture's *observable* table as repeated isolated events."""
    fixture = hot_tub(name)
    published = fixture.published_table()
    ha = fixture.observations()["ha_power"]
    observations: list[Observation] = []
    for index in range(cycles):
        observations.extend(
            cycle(
                AssociationFinder(AssociationConfig(min_events=1)),
                TUB_ID,
                T0 + timedelta(seconds=3600.0 * index),
                device_before=ha["before_w"],
                device_after=ha["after_w"],
                cts={ct: (row["before_w"], row["after_w"]) for ct, row in published.items()},
            )
        )
    rows: list[MeasurementFactor] = []
    for items in fixture.as_measurement_factors().values():
        rows.extend(items)
    registry = FactorRegistry(rows)
    return relations_of(observations, registry=registry), registry


def lab_relations(
    stratum: str,
    *,
    devices: tuple[str, ...],
    cts: tuple[str, ...],
    minutes: float = 60.0,
    interval: float = 15.0,
) -> list[TopologyRelation]:
    """Turn a LabHouse observation stream into association relations.

    Only ``observations()`` is read, and contamination is *real*: every event is
    classified by ``EventIsolator`` against the other events in the same stream.
    """
    from wattmosaic_engine.disaggregation.isolation import EventIsolator, SampleCoverage

    house = build_scenario(stratum, 42, minutes=minutes, interval=interval)
    view = house.observations()
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
    finder = AssociationFinder(AssociationConfig(min_events=1))
    last_power: dict[str, float] = {}
    observations: list[Observation] = []
    for event in events:
        device = str(event["device_id"])
        at = event["occurred_at"]
        before = [
            sample(ct_id, ts, watts)
            for ct_id, rows in series.items()
            for ts, watts in rows
            if at - timedelta(seconds=60) <= ts < at
        ]
        after = [
            sample(ct_id, ts, watts)
            for ct_id, rows in series.items()
            for ts, watts in rows
            if at <= ts < at + timedelta(seconds=120)
        ]
        if not before or not after:
            continue
        coverage = SampleCoverage(baseline_samples=len(before), event_samples=len(after))
        declared = None
        for raw in view:
            for candidate in raw["events"]:
                if (
                    candidate.get("device_id") == device
                    and candidate.get("occurred_at") == at.isoformat()
                ):
                    declared = float(candidate.get("power_w", 0.0))
        if declared is None:
            continue
        delta = declared - last_power.get(device, 0.0)
        last_power[device] = declared
        verdict = isolator.classify(
            event, events, now=at + timedelta(seconds=420), coverage=coverage
        )
        observations.extend(finder.observe(verdict, before, after, [], device_delta_w=delta))
    return finder.analyze(observations, registry=FactorRegistry())


def residual_for(
    ct_id: str,
    circuit_power_w: float,
    known: dict[str, float],
    *,
    basis: MeasurementBasis = MeasurementBasis.CT_MEASURED,
) -> ResidualPower:
    """A real WP7 residual: ``circuit_power - sum(known)``, never clamped."""
    scope = ResidualScope(
        scope_id=f"scope:{ct_id}",
        ct_id=ct_id,
        basis=basis,
        device_contributions=dict(known),
    )
    return ResidualAnalyzer().compute(scope, circuit_power_w)


def circuits_of(
    relations: list[TopologyRelation],
    *,
    registry: FactorRegistry | None = None,
    topology: TopologyGraph | None = None,
) -> list[Circuit]:
    return CircuitClassifier().candidates(relations, registry=registry, topology=topology)


def classify(
    relations: list[TopologyRelation],
    *,
    registry: FactorRegistry | None = None,
    residual: ResidualPower | None = None,
    observations: list[Observation] | None = None,
) -> Circuit:
    classifier = CircuitClassifier()
    candidates = classifier.candidates(relations, registry=registry)
    assert len(candidates) == 1, f"expected one scope, got {[c.circuit_id for c in candidates]}"
    return classifier.classify(candidates[0], relations, residual=residual, observations=observations)


def only(relations: list[TopologyRelation], subject: str) -> TopologyRelation:
    matches = [r for r in relations if r.subject_id == subject]
    assert matches, f"no relation for {subject}"
    return matches[0]


def _value(item: Any) -> str:
    """Contract enums are stored as their plain value (``use_enum_values=True``)."""
    return str(getattr(item, "value", item))


# --------------------------------------------------------------------------- #
# Group D — dedicated circuits
# --------------------------------------------------------------------------- #


def test_d1_ev_charger_is_dedicated_and_confirmed_only_once_the_residual_is_checked() -> None:
    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    relation = only(relations, "vehicle_charger")
    assert relation.evidence["verdict"] == "supported"
    assert relation.evidence["stats"]["n_supporting"] >= 3

    unchecked = classify(relations)
    assert CircuitClass(unchecked.classification) is CircuitClass.DEDICATED
    # Nothing contradicted it, but no residual was measured, so it is a proposal.
    assert TopologyStatus(unchecked.classification_status) is TopologyStatus.CANDIDATE

    checked = classify(
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )
    assert CircuitClass(checked.classification) is CircuitClass.DEDICATED
    assert TopologyStatus(checked.classification_status) is TopologyStatus.CONFIRMED
    assert checked.metadata["algorithm_version"] == CircuitConfig().algorithm_version


def test_d2_hot_tub_with_x2_factor_is_one_dedicated_appliance_not_two_loads() -> None:
    relations, registry = hot_tub_relations("factor_two", cycles=3)
    # Both legs really do observe the tub; the factor already claims the whole
    # appliance, so they are one scope, not two circuits and not two loads.
    candidates = circuits_of(relations, registry=registry)
    assert len(candidates) == 1
    circuit = candidates[0]
    assert circuit.device_ids == [TUB_ID]
    assert set(circuit.ct_ids) == {"ct_hot_tub_l1", "ct_hot_tub_l2"}
    assert circuit.measurement_basis == MeasurementBasis.FACTOR_ADJUSTED

    classified = CircuitClassifier().classify(
        circuit,
        relations,
        residual=residual_for(
            "ct_hot_tub_l1",
            500.0,
            {TUB_ID: 500.0},
            basis=MeasurementBasis.FACTOR_ADJUSTED,
        ),
    )
    assert CircuitClass(classified.classification) is CircuitClass.DEDICATED
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CONFIRMED
    # The 250 W sibling leg is a share of the same appliance, never a second load.
    evidence = classified.metadata["evidence"]
    assert evidence["stats"]["device_count"] == 1
    assert evidence["stats"]["explained_fraction"] == pytest.approx(1.0, abs=0.05)
    # Three cycles seen on two legs are still three events, not six.
    assert evidence["stats"]["isolated_events"] == 3
    assert evidence["bars"]["min_isolated_events"] is True


def test_d3_air_conditioner_duty_cycle_is_dedicated() -> None:
    relations = lab_relations(
        "hvac_modulation",
        devices=("air_conditioner",),
        cts=("ct_main_air_conditioner",),
        minutes=135.0,
    )
    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.DEDICATED
    assert classified.metadata["evidence"]["stats"]["isolated_events"] >= 3


def test_d4_dedicated_circuit_with_standby_power_is_still_dedicated() -> None:
    """Standby is a property of the device, not a second load on the circuit."""
    observations = repeated(
        "network_switch",
        n=4,
        off_w=6.0,  # the device draws standby while "off"
        on_w=506.0,
        cts={ONE_CT: (6.0, 506.0)},
    )
    relations = relations_of(observations)
    relation = only(relations, "network_switch")
    # A non-zero standing level on the CT must not be read as another load.
    assert relation.evidence["stats"]["ct_level_w"] == pytest.approx(506.0)

    classified = classify(
        relations,
        residual=residual_for(ONE_CT, 506.0, {"network_switch": 506.0}),
    )
    assert CircuitClass(classified.classification) is CircuitClass.DEDICATED
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CONFIRMED
    assert classified.metadata["evidence"]["stats"]["standby_explained"] is True


def test_d5_dedicated_circuit_with_dynamic_power_is_still_dedicated() -> None:
    relations = lab_relations(
        "dynamic", devices=("lighting_1",), cts=("ct_main_lighting_ct1",)
    )
    relation = only(relations, "lighting_1")
    assert relation.evidence["stats"]["stability"] >= CircuitConfig().min_stability

    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.DEDICATED
    assert classified.metadata["evidence"]["stats"]["isolated_events"] >= 3


def test_d6a_appears_dedicated_but_carries_a_second_load_is_not_dedicated() -> None:
    observations = repeated(
        "space_heater", n=4, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 1500.0)}
    )
    observations += repeated(
        "dehumidifier",
        n=4,
        off_w=0.0,
        on_w=400.0,
        cts={ONE_CT: (0.0, 400.0)},
        start=T0 + timedelta(minutes=30),
    )
    relations = relations_of(observations)
    classified = classify(relations)
    assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED
    assert CircuitClass(classified.classification) is CircuitClass.SHARED
    assert set(classified.device_ids) == {"space_heater", "dehumidifier"}


def test_d6b_single_known_device_plus_unexplained_load_is_not_promoted_to_partial() -> None:
    relations = repeated(
        "space_heater", n=4, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 1500.0)}
    )
    relations = relations_of(relations)
    classified = classify(
        relations,
        residual=residual_for(ONE_CT, 1900.0, {"space_heater": 1500.0}),
    )
    # 400 W is unexplained, so this is certainly not DEDICATED ...
    assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED
    # ... and PARTIALLY_UNDERSTOOD requires two known devices, so the honest
    # answer is UNKNOWN rather than an over-claimed class.
    assert CircuitClass(classified.classification) is CircuitClass.UNKNOWN
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CANDIDATE
    assert classified.metadata["evidence"]["stats"]["residual_contradiction_w"] == pytest.approx(
        400.0
    )


def test_d6c_a_co_toggling_second_device_blocks_dedicated_even_without_its_own_share() -> None:
    """One device with solo events plus one that only ever moves with it.

    The second device has no independent contribution, so it fails the "no
    independent contributions" bar *by absence*.  It is still an identified
    device on the wire, so calling the circuit dedicated to the first one would
    hide a real load.
    """
    observations = repeated(
        "space_heater", n=4, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 1500.0)}
    )
    # A second load that only ever switches on *with* the heater: it never gets
    # an isolated event of its own.
    finder = AssociationFinder(AssociationConfig(min_events=1))
    for index in range(2):
        at = T0 + timedelta(seconds=3600.0 * (4 + index))
        observations.extend(
            cycle(
                finder,
                "mystery_load",
                at,
                device_before=0.0,
                device_after=400.0,
                cts={ONE_CT: (0.0, 1900.0)},
            )
        )
        observations.extend(
            cycle(
                finder,
                "space_heater",
                at,
                device_before=0.0,
                device_after=1500.0,
                cts={ONE_CT: (0.0, 1900.0)},
            )
        )
    relations = relations_of(observations)
    verdicts = {r.subject_id: r.evidence["verdict"] for r in relations}
    # The heater has solo events of its own; the mystery load never does.
    assert verdicts["space_heater"] in {"supported", "split"}
    assert verdicts["mystery_load"] == "ambiguous"

    classified = classify(
        relations, residual=residual_for(ONE_CT, 1900.0, {"space_heater": 1500.0})
    )
    assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED
    assert classified.metadata["evidence"]["bars"]["no_other_identified_devices"] is False


def test_d7_insufficient_evidence_is_unknown_not_a_dedicated_fallback() -> None:
    observations = repeated("space_heater", n=1, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 1500.0)})
    relations = relations_of(observations)
    assert only(relations, "space_heater").evidence["verdict"] == "single_event"

    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.UNKNOWN
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CANDIDATE
    assert classified.metadata["classification_confidence"] < 0.5


def test_d7b_no_relations_at_all_is_unknown() -> None:
    circuit = Circuit(circuit_id="c-empty", ct_ids=[ONE_CT])
    classified = CircuitClassifier().classify(circuit, [])
    assert CircuitClass(classified.classification) is CircuitClass.UNKNOWN
    assert classified.metadata["classification_confidence"] == 0.0


def test_a_never_confirmed_start_is_downgraded_when_evidence_is_thin() -> None:
    relations = repeated(
        "space_heater", n=4, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 1500.0)}
    )
    relations = relations_of(relations)
    # A caller hands in a circuit already marked CONFIRMED with no support.
    pre_confirmed = Circuit(
        circuit_id="c-pre",
        classification=CircuitClass.DEDICATED,
        classification_status=TopologyStatus.CONFIRMED,
        ct_ids=[ONE_CT],
        device_ids=["space_heater"],
    )
    classified = CircuitClassifier().classify(
        pre_confirmed,
        relations,
        residual=residual_for(ONE_CT, 3000.0, {"space_heater": 1500.0}),
    )
    assert TopologyStatus(classified.classification_status) is not TopologyStatus.CONFIRMED
    assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED


# --------------------------------------------------------------------------- #
# Group E — shared circuits
# --------------------------------------------------------------------------- #


def test_e1_multiple_lighting_loads_on_one_breaker_are_shared() -> None:
    observations = repeated("lamp_a", n=4, off_w=0.0, on_w=120.0, cts={ONE_CT: (0.0, 120.0)})
    observations += repeated(
        "lamp_b",
        n=4,
        off_w=0.0,
        on_w=380.0,
        cts={ONE_CT: (0.0, 380.0)},
        start=T0 + timedelta(minutes=45),
    )
    relations = relations_of(observations)
    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.SHARED
    assert set(classified.device_ids) == {"lamp_a", "lamp_b"}
    evidence = classified.metadata["evidence"]
    assert evidence["stats"]["independent_devices"] == 2


def test_e2_multiple_plug_loads_on_one_breaker_are_shared() -> None:
    relations = lab_relations(
        "shared",
        devices=("utility_1", "utility_2"),
        cts=("ct_main_utility_plug_ct1",),
        minutes=120.0,
    )
    classified = classify(
        relations,
        residual=residual_for("ct_main_utility_plug_ct1", 2100.0, {"utility_1": 900.0, "utility_2": 1200.0}),
    )
    assert CircuitClass(classified.classification) is CircuitClass.SHARED
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CONFIRMED
    assert classified.metadata["evidence"]["stats"]["residual_contradiction_w"] == pytest.approx(0.0)
    for device in ("utility_1", "utility_2"):
        stats = only(relations, device).evidence["stats"]
        assert stats["n_supporting"] >= 1, "each device must have its own isolated evidence"


def test_e3_multiple_appliances_on_one_breaker_are_shared() -> None:
    observations = repeated("fridge", n=4, off_w=0.0, on_w=180.0, cts={ONE_CT: (0.0, 180.0)})
    observations += repeated(
        "kettle",
        n=4,
        off_w=0.0,
        on_w=2000.0,
        cts={ONE_CT: (0.0, 2000.0)},
        start=T0 + timedelta(minutes=90),
    )
    relations = relations_of(observations)
    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.SHARED
    assert len(classified.device_ids) == 2


def test_e4_devices_with_independent_changes_are_independent_not_ambiguous() -> None:
    observations = repeated("pump", n=4, off_w=0.0, on_w=700.0, cts={ONE_CT: (0.0, 700.0)})
    observations += repeated(
        "fan",
        n=4,
        off_w=0.0,
        on_w=90.0,
        cts={ONE_CT: (0.0, 90.0)},
        start=T0 + timedelta(minutes=20),
    )
    relations = relations_of(observations)
    verdicts = {r.subject_id: r.evidence["verdict"] for r in relations}
    assert verdicts == {"pump": "supported", "fan": "supported"}
    classified = classify(relations)
    assert CircuitClass(classified.classification) is CircuitClass.SHARED


def test_e5_overlapping_operating_schedules_are_not_shared_with_high_confidence() -> None:
    relations = lab_relations(
        "ambiguous_pair",
        devices=("amb_a", "amb_b"),
        cts=("ct_main_utility_plug_ct1",),
    )
    assert relations
    for relation in relations:
        # They never switch separately, so neither has independent evidence.
        assert relation.evidence["verdict"] == "ambiguous"
        assert relation.evidence["stats"]["n_supporting"] == 0

    classified = classify(relations)
    assert CircuitClass(classified.classification) is not CircuitClass.SHARED
    assert CircuitClass(classified.classification) is CircuitClass.UNKNOWN
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CANDIDATE
    assert classified.metadata["classification_confidence"] < 0.5


def test_e6_unmonitored_load_present_is_partially_understood() -> None:
    relations = lab_relations(
        "shared",
        devices=("utility_1", "utility_2"),
        cts=("ct_main_utility_plug_ct1",),
        minutes=120.0,
    )
    residual = residual_for(
        "ct_main_utility_plug_ct1", 3300.0, {"utility_1": 900.0, "utility_2": 1200.0}
    )
    assert residual.residual_w == pytest.approx(1200.0)

    classified = classify(relations, residual=residual)
    assert CircuitClass(classified.classification) is CircuitClass.PARTIALLY_UNDERSTOOD
    assert classified.metadata["unattributed_load_w"] == pytest.approx(1200.0)
    assert classified.metadata["unattributed_note"]
    # The residual *is* the class here, so noticing it must not make the honest
    # verdict look less credible than the "shared" over-claim it replaces.
    assert classified.metadata["classification_confidence"] > 0.5
    assert classified.metadata["classification_confidence"] < 0.99


def test_e7_two_known_devices_plus_residual_records_the_residual() -> None:
    relations = lab_relations(
        "shared",
        devices=("utility_1", "utility_2"),
        cts=("ct_main_utility_plug_ct1",),
        minutes=120.0,
    )
    residual = residual_for(
        "ct_main_utility_plug_ct1", 2400.0, {"utility_1": 900.0, "utility_2": 1200.0}
    )
    classified = classify(relations, residual=residual)
    assert CircuitClass(classified.classification) is CircuitClass.PARTIALLY_UNDERSTOOD
    recorded = classified.metadata["evidence"]["residual"]
    assert recorded["residual_w"] == pytest.approx(300.0)
    assert recorded["kind"] == _value(residual.kind)
    assert recorded["resolved"] is False
    assert classified.metadata["unattributed_load_w"] == pytest.approx(300.0)


def test_a_negative_residual_is_a_conflict_never_unattributed_load() -> None:
    """D3: a surplus means the books are wrong, not that a device is missing.

    ``abs()`` used to turn ``-600 W`` into "600 W of unattributed load" and then
    promote two independently evidenced devices to ``PARTIALLY_UNDERSTOOD`` --
    announcing an undiscovered device from a number that says the accounted
    devices already exceed what the conductor measures.
    """
    relations = lab_relations(
        "shared",
        devices=("utility_1", "utility_2"),
        cts=("ct_main_utility_plug_ct1",),
        minutes=120.0,
    )
    residual = residual_for(
        "ct_main_utility_plug_ct1", 1500.0, {"utility_1": 900.0, "utility_2": 1200.0}
    )
    assert residual.residual_w == pytest.approx(-600.0)

    classified = classify(relations, residual=residual)
    stats = classified.metadata["evidence"]["stats"]
    assert stats["residual_w"] == pytest.approx(-600.0)
    assert stats["residual_conflict_w"] == pytest.approx(-600.0)
    # The signed value is kept, but it is never an "extra load".
    assert stats["unattributed_load_w"] is None
    assert "unattributed_load_w" not in classified.metadata
    assert "unattributed_note" not in classified.metadata
    # The contradiction is published as a contradiction, with its sign.
    assert classified.metadata["residual_conflict_w"] == pytest.approx(-600.0)
    assert "surplus" in classified.metadata["residual_conflict_note"]
    # And it never promotes the class: two known devices stay SHARED.
    assert CircuitClass(classified.classification) is CircuitClass.SHARED
    assert CircuitClass(classified.classification) is not CircuitClass.PARTIALLY_UNDERSTOOD
    assert TopologyStatus(classified.classification_status) is TopologyStatus.CANDIDATE
    # The magnitude still trips the contradiction bar, which is its only job.
    assert classified.metadata["evidence"]["bars"]["no_residual_contradiction"] is False


def test_the_sign_of_the_residual_decides_the_class_not_its_magnitude() -> None:
    """Same |residual|, opposite sign, different meaning -- one shared fixture."""
    relations = lab_relations(
        "shared",
        devices=("utility_1", "utility_2"),
        cts=("ct_main_utility_plug_ct1",),
        minutes=120.0,
    )
    known = {"utility_1": 900.0, "utility_2": 1200.0}
    surplus = classify(
        relations,
        residual=residual_for("ct_main_utility_plug_ct1", 1500.0, known),
    )
    shortfall = classify(
        relations,
        residual=residual_for("ct_main_utility_plug_ct1", 2700.0, known),
    )
    assert abs(surplus.metadata["evidence"]["stats"]["residual_contradiction_w"]) == pytest.approx(
        600.0
    )
    assert abs(shortfall.metadata["evidence"]["stats"]["residual_contradiction_w"]) == pytest.approx(
        600.0
    )
    assert CircuitClass(surplus.classification) is CircuitClass.SHARED
    assert CircuitClass(shortfall.classification) is CircuitClass.PARTIALLY_UNDERSTOOD
    assert shortfall.metadata["unattributed_load_w"] == pytest.approx(600.0)
    assert "unattributed_load_w" not in surplus.metadata



# --------------------------------------------------------------------------- #
# candidates() — one circuit per canonical CT scope
# --------------------------------------------------------------------------- #


def test_candidates_collapse_aliases_to_one_circuit() -> None:
    topology = TopologyGraph(
        Topology(
            channels=[
                CTChannel(ct_id="ct_a", entity_id="sensor.a", ct_type=CTType.BRANCH, phase=Phase.L1),
                CTChannel(
                    ct_id="ct_b",
                    entity_id="sensor.b",
                    ct_type=CTType.BRANCH,
                    phase=Phase.L1,
                    alias_of_ct_id="ct_a",
                ),
            ]
        )
    )
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations: list[Observation] = []
    for index in range(3):
        observations.extend(
            cycle(
                finder,
                "dryer",
                T0 + timedelta(seconds=3600.0 * index),
                device_before=0.0,
                device_after=2000.0,
                cts={"ct_a": (0.0, 2000.0), "ct_b": (0.0, 2000.0)},
            )
        )
    relations = relations_of(observations)
    circuits = circuits_of(relations, topology=topology)
    assert len(circuits) == 1
    assert circuits[0].ct_ids == ["ct_a"]
    assert circuits[0].device_ids == ["dryer"]
    assert circuits[0].metadata["ct_alias_map"] == {"ct_b": "ct_a"}


def test_candidates_join_legs_shared_by_a_two_leg_device() -> None:
    """A device on both legs and one on a single leg are still one scope.

    Grouping by an exact conductor tuple would produce two circuits here (one
    per leg set) and then report the single-leg device as its own circuit, which
    is the D2 defect in a different disguise.
    """
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations: list[Observation] = []
    for index in range(3):
        at = T0 + timedelta(seconds=3600.0 * index)
        observations.extend(
            cycle(
                finder,
                "tub",
                at,
                device_before=0.0,
                device_after=1000.0,
                cts={"ct_l1": (0.0, 500.0), "ct_l2": (0.0, 500.0)},
            )
        )
        observations.extend(
            cycle(
                finder,
                "pump",
                at + timedelta(minutes=20),
                device_before=0.0,
                device_after=200.0,
                cts={"ct_l1": (0.0, 200.0)},
            )
        )
    circuits = circuits_of(relations_of(observations))
    assert len(circuits) == 1
    assert circuits[0].ct_ids == ["ct_l1", "ct_l2"]
    assert circuits[0].device_ids == ["pump", "tub"]


def test_candidates_take_the_measurement_basis_from_the_registry() -> None:
    relations, registry = hot_tub_relations("factor_two", cycles=3)
    circuits = circuits_of(relations, registry=registry)
    assert len(circuits) == 1
    assert circuits[0].measurement_basis == MeasurementBasis.FACTOR_ADJUSTED
    plain = circuits_of(relations, registry=FactorRegistry())
    # Without the declaration the same readings cannot claim an appliance total.
    assert plain[0].measurement_basis == MeasurementBasis.CT_MEASURED


def test_candidates_never_claim_a_classification() -> None:
    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    for circuit in circuits_of(relations):
        assert CircuitClass(circuit.classification) is CircuitClass.UNKNOWN
        assert TopologyStatus(circuit.classification_status) is TopologyStatus.CANDIDATE


# --------------------------------------------------------------------------- #
# confidence, names, explain
# --------------------------------------------------------------------------- #


def test_confidence_is_a_vector_with_only_the_circuit_dimension_populated() -> None:
    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    classified = classify(
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )
    vector = classified.metadata["confidence"]
    assert isinstance(vector, ConfidenceVector)
    assert vector.method == CircuitConfig().algorithm_version
    # Exactly one dimension: no blend of power/topology/residual into the class.
    assert vector.dimensions() == {
        "circuit_classification": pytest.approx(classified.metadata["classification_confidence"])
    }
    assert 0.0 < vector.circuit_classification <= 1.0
    assert classified.metadata["classification_confidence"] == vector.circuit_classification


def test_classification_is_never_name_derived() -> None:
    observations = repeated("lamp_a", n=4, off_w=0.0, on_w=120.0, cts={ONE_CT: (0.0, 120.0)})
    observations += repeated(
        "lamp_b",
        n=4,
        off_w=0.0,
        on_w=380.0,
        cts={ONE_CT: (0.0, 380.0)},
        start=T0 + timedelta(minutes=45),
    )
    baseline = classify(relations_of(observations))

    renamed = {
        "lamp_a": "zebra_9",
        "lamp_b": "kitchen_main_lighting_circuit",
        ONE_CT: "ct_garage_subpanel_lighting",
    }
    renamed_observations = [
        Observation(
            event_id=row.event_id,
            device_id=renamed[row.device_id],
            ct_id=None if row.ct_id is None else renamed[row.ct_id],
            occurred_at=row.occurred_at,
            device_delta_w=row.device_delta_w,
            ct_delta_w=row.ct_delta_w,
            lag_s=row.lag_s,
            ct_noise_w=row.ct_noise_w,
            ct_samples=row.ct_samples,
            ct_level_w=row.ct_level_w,
            isolation_status=row.isolation_status,
            reason=row.reason,
            orientation=row.orientation,
        )
        for row in observations
    ]
    relabelled = classify(relations_of(renamed_observations))

    assert CircuitClass(relabelled.classification) is CircuitClass(baseline.classification)
    assert relabelled.metadata["classification_confidence"] == pytest.approx(
        baseline.metadata["classification_confidence"]
    )
    assert set(relabelled.device_ids) == {"zebra_9", "kitchen_main_lighting_circuit"}


def test_explain_cites_the_evidence_it_used() -> None:
    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    classified = classify(
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )
    text = explain(classified)
    assert "dedicated" in text.lower()
    assert "isolated events" in text
    assert "residual" in text
    assert "vehicle_charger" in text
    # It quotes the numbers, not just the verdict.
    stats = classified.metadata["evidence"]["stats"]
    assert str(stats["isolated_events"]) in text


def test_evidence_carries_supporting_conflicting_and_stats() -> None:
    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    evidence = classify(relations).metadata["evidence"]
    assert evidence["supporting"]
    assert evidence["conflicting"] == []
    assert evidence["stats"]["isolated_events"] >= 3
    assert evidence["algorithm_version"] == CircuitConfig().algorithm_version


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_config_documents_every_threshold_and_rejects_nonsense() -> None:
    config = CircuitConfig()
    assert config.min_isolated_events >= 2
    assert 0.0 < config.min_stability <= 1.0
    assert config.max_residual_contradiction_w > 0.0
    assert config.dedicated_min_observation_s > 0.0
    assert config.algorithm_version

    with pytest.raises(ValueError):
        CircuitConfig(min_isolated_events=0)
    with pytest.raises(ValueError):
        CircuitConfig(min_stability=0.0)
    with pytest.raises(ValueError):
        CircuitConfig(max_residual_contradiction_w=-1.0)
    with pytest.raises(ValueError):
        CircuitConfig(dedicated_min_observation_s=0.0)


def test_the_evidence_bars_are_enforced_individually() -> None:
    """Each dedicated bar, removed on its own, stops a DEDICATED verdict."""
    observations = jittered(
        "space_heater",
        n=4,
        off_w=0.0,
        on_w=1500.0,
        cts={ONE_CT: (0.0, 1500.0)},
        # A real response is never perfectly flat: ~0.92 stability, above the
        # default bar, so a raised bar can still fail on stability alone.
        wobble_frac=0.10,
    )
    relations = relations_of(observations)
    clean = residual_for(ONE_CT, 1500.0, {"space_heater": 1500.0})
    stability = only(relations, "space_heater").evidence["stats"]["stability"]
    assert CircuitConfig().min_stability < stability < 1.0

    assert CircuitClass(classify(relations, residual=clean).classification) is CircuitClass.DEDICATED

    bars = {
        "min_isolated_events": CircuitConfig(min_isolated_events=99),
        "min_stability": CircuitConfig(min_stability=1.0),
        "dedicated_min_observation_s": CircuitConfig(dedicated_min_observation_s=10**9),
    }
    for name, config in bars.items():
        classifier = CircuitClassifier(config)
        circuit = classifier.candidates(relations)[0]
        classified = classifier.classify(circuit, relations, residual=clean)
        assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED, name
        assert classified.metadata["evidence"]["bars"][name] is False, name


def test_a_device_whose_power_does_not_explain_the_scope_is_not_dedicated() -> None:
    """Bar 3 on its own: the CT moves by half the device's step."""
    observations = repeated(
        "space_heater", n=4, off_w=0.0, on_w=1500.0, cts={ONE_CT: (0.0, 750.0)}
    )
    relations = relations_of(observations)
    classified = classify(
        relations, residual=residual_for(ONE_CT, 750.0, {"space_heater": 1500.0})
    )
    assert CircuitClass(classified.classification) is not CircuitClass.DEDICATED
    bars = classified.metadata["evidence"]["bars"]
    assert bars["device_power_explains"] is False
    assert classified.metadata["evidence"]["stats"]["explained_fraction"] == pytest.approx(
        0.5, abs=0.05
    )


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_persist_is_idempotent_and_keeps_classification_history(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    candidate = CircuitClassifier().candidates(relations)[0]
    confirmed = CircuitClassifier().classify(
        candidate,
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )

    db = Database(tmp_db_path)
    try:
        assert persist([confirmed], db) == 1
        assert persist([confirmed], db) == 1
        rows = db.circuits_all()
        assert len(rows) == 1
        assert rows[0]["classification"] == CircuitClass.DEDICATED.value
        history = rows[0]["metadata"]["history"]
        # The first write records the transition away from the empty default.
        assert history
        assert history[0]["to"] == CircuitClass.DEDICATED.value

        # A later, weaker re-run must append, never overwrite the earlier claim.
        # No relations at all: the same wire, but the evidence is gone.
        reverted = CircuitClassifier().classify(candidate, [])
        assert CircuitClass(reverted.classification) is CircuitClass.UNKNOWN
        persist([reverted], db)
        rows = db.circuits_all()
        assert len(rows) == 1
        history = rows[0]["metadata"]["history"]
        assert len(history) == 2
        assert history[0]["to"] == CircuitClass.DEDICATED.value
        assert history[1]["to"] == CircuitClass.UNKNOWN.value
        assert history[1]["reason"]
        assert history[1]["at"]
        # Idempotent: writing the same thing again adds no history row.
        persist([reverted], db)
        assert len(db.circuits_all()[0]["metadata"]["history"]) == 2
    finally:
        db.close()


def test_persist_records_a_status_change_even_when_the_class_is_unchanged(
    tmp_db_path: Any,
) -> None:
    from wattmosaic_engine.storage.database import Database

    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    classifier = CircuitClassifier()
    candidate = classifier.candidates(relations)[0]
    proposed = classifier.classify(candidate, relations)
    assert CircuitClass(proposed.classification) is CircuitClass.DEDICATED
    assert TopologyStatus(proposed.classification_status) is TopologyStatus.CANDIDATE
    confirmed = classifier.classify(
        candidate,
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )

    db = Database(tmp_db_path)
    try:
        persist([proposed], db)
        persist([confirmed], db)
        history = db.circuits_all()[0]["metadata"]["history"]
        # The class did not change; the *status* did, and that is a real event.
        assert [row["to"] for row in history] == ["dedicated", "dedicated"]
        assert [row["to_status"] for row in history] == ["candidate", "confirmed"]
        assert history[0]["from_status"] is None
        assert history[1]["from_status"] == "candidate"
        assert history[1]["reason"]
    finally:
        db.close()


def test_persist_stamps_the_injected_instant_so_a_replay_is_byte_identical(
    tmp_db_path: Any,
) -> None:
    """D6: circuit history is analysis output, so its clock is injected.

    Two replays of the same window used to append history rows at
    ``datetime.now()``, so the same evidence produced two different trails and a
    seeded replay could not be compared with the run it replays.
    """
    from wattmosaic_engine.storage.database import Database

    relations = lab_relations(
        "ev_rate_change", devices=("vehicle_charger",), cts=("ct_main_vehicle_charger",)
    )
    circuit = CircuitClassifier().classify(
        CircuitClassifier().candidates(relations)[0],
        relations,
        residual=residual_for("ct_main_vehicle_charger", 3600.0, {"vehicle_charger": 3600.0}),
    )
    at = T0 + timedelta(days=10)

    db = Database(tmp_db_path)
    try:
        persist([circuit], db, now=at)
        first = db.circuits_all()[0]["metadata"]["history"]
        assert [row["at"] for row in first] == [at.isoformat()]

        # Wipe the trail and replay the identical analysis at the identical
        # instant: the stored history must be byte-identical.
        db._conn.execute("DELETE FROM circuits")
        persist([circuit], db, now=at)
        assert db.circuits_all()[0]["metadata"]["history"] == first
        assert first[0]["at"] == at.isoformat()
    finally:
        db.close()
