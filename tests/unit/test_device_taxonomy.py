"""Device taxonomy axes, per-device configuration and evidence tiers.

These tests pin the behaviour that matters most: that configuration can only
*add* honest evidence, never fabricate attribution.  Every case here is derived
from a real observation in this household's 7-day recorder history:

* a lighting circuit carries several unmodelled fixtures, so one lamp must not
  inherit the whole row;
* a complex controller reports a setpoint mode while the CT tracks real heating,
  so the mode entity must never be treated as runtime;
* a dedicated CT is a measurement whether or not any state entity exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    TIER_RANK,
    ConfidenceTier,
    ControllerConfiguration,
    ControllerStateMapping,
    ControllerStateRole,
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceTopology,
    DeviceType,
    ExpectedPower,
    HaConnectivity,
    LightConfiguration,
    LightType,
    MonitoringLevel,
    Phase,
    PowerBehavior,
    Source,
    Topology,
)
from wattmosaic_engine.disaggregation.configuration import (
    configured_power,
    derive_monitoring,
    effective_config,
    effective_configs,
    project_configuration,
)
from wattmosaic_engine.disaggregation.engine import DisaggregationEngine
from wattmosaic_engine.disaggregation.tiers import (
    TIER_DESCRIPTION,
    TIER_TYPICAL_SCORE,
    TierInputs,
    evidence_tier,
    grade,
    tier_rank,
)
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.electrical.window import ObservationWindow
from wattmosaic_engine.entity_evidence import classify_entity, select_runtime_entities

AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _topology() -> Topology:
    """One main, one dedicated CT, one shared branch carrying an unmodelled load."""
    return Topology(
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ct_water_heater",
                entity_id="sensor.water_heater_power",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="main_l1",
            ),
            CTChannel(
                ct_id="br_lights",
                entity_id="sensor.lights_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="main_l1",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="water_heater",
                name="Water Heater",
                dedicated_ct_ids=["ct_water_heater"],
                phase=Phase.L1,
                metadata={"state_entity_ids": ["binary_sensor.water_heater_zone_1_heating"]},
            ),
            DeviceTopology(device_id="lamp", name="Kitchen lamp", branch_ct_ids=["br_lights"], phase=Phase.L1),
        ],
    )


def _window(values: dict[str, float], **kwargs):
    return ObservationWindow.from_values(AT, values, **kwargs)


def _infer(topology: Topology, values: dict[str, float], states: dict[str, str]):
    return DisaggregationEngine().infer(
        _window(values), device_states=states, models=None, topology=topology
    )


def _with(devices: list[DeviceConfiguration]) -> Topology:
    return project_configuration(_topology(), DeviceConfigurationSet(devices=devices))


# --------------------------------------------------------------------------- #
# Contract validation — the axes must not contradict each other
# --------------------------------------------------------------------------- #


def test_not_connected_device_cannot_declare_state_entities() -> None:
    with pytest.raises(ValueError, match="not connected"):
        DeviceConfiguration(
            device_id="d",
            connectivity=HaConnectivity.NOT_CONNECTED,
            state_entity_ids=["switch.x"],
        )


def test_complex_behavior_requires_a_controller() -> None:
    with pytest.raises(ValueError, match="behavior=complex"):
        DeviceConfiguration(device_id="d", behavior=PowerBehavior.COMPLEX)


def test_light_configuration_rejected_for_non_light() -> None:
    with pytest.raises(ValueError, match="not valid for device_type"):
        DeviceConfiguration(
            device_id="d",
            device_type=DeviceType.REFRIGERATION,
            light=LightConfiguration(bulb_count=2),
        )


def test_light_group_watts_is_per_bulb_times_count() -> None:
    light = LightConfiguration(light_type=LightType.LED, bulb_count=6, watts_per_bulb_on=9.0)
    assert light.full_on_w() == pytest.approx(54.0)


def test_dimming_curve_must_ascend() -> None:
    with pytest.raises(ValueError, match="ascending"):
        LightConfiguration(dimming_curve=[(0.5, 0.4), (0.2, 0.1)])


def test_expected_power_tolerance_defaults_to_ten_percent_floor() -> None:
    assert ExpectedPower(on_w=100.0).tolerance() == pytest.approx(10.0)
    assert ExpectedPower(on_w=100.0, tolerance_fraction=0.05).tolerance() == pytest.approx(5.0)
    assert ExpectedPower(on_w=5.0).tolerance() == pytest.approx(2.0)  # floor wins


def test_duplicate_device_configuration_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate device configuration"):
        DeviceConfigurationSet(
            devices=[
                DeviceConfiguration(device_id="d"),
                DeviceConfiguration(device_id="d"),
            ]
        )


def test_controller_load_states_requires_confirmation() -> None:
    controller = ControllerConfiguration(
        states=[
            ControllerStateMapping(label="Heating", role=ControllerStateRole.LOAD, power_w=4500.0),
            ControllerStateMapping(
                label="Aux", role=ControllerStateRole.LOAD, power_w=900.0, confirmed=True
            ),
            ControllerStateMapping(label="Idle", role=ControllerStateRole.OFF),
        ]
    )
    # An unconfirmed mapping is never allowed to drive a power estimate.
    assert controller.load_states() == {"aux": 900.0}


# --------------------------------------------------------------------------- #
# Entity evidence — modes are never runtime (ADR-0012)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entity_id",
    [
        "climate.hallway_thermostat",  # setpoint mode dominates the rows
        "water_heater.water_heater_tank",  # a controller mode, not runtime
        "water_heater.spa_heater",
    ],
)
def test_mode_domains_are_never_load_evidence(entity_id: str) -> None:
    evidence = classify_entity(entity_id)
    assert evidence.role == "mode"
    assert evidence.is_load is False


@pytest.mark.parametrize(
    "entity_id",
    [
        "binary_sensor.water_heater_zone_1_heating",
        "light.bedroom_main_lights",
        "switch.landscape_socket_1",
        "fan.spa_pump_1",
    ],
)
def test_runtime_domains_are_load_evidence(entity_id: str) -> None:
    assert classify_entity(entity_id).role == "load"


def test_explicit_declaration_overrides_domain_rule() -> None:
    # A user may legitimately know that a climate entity *is* the runtime signal
    # for their integration; the declaration wins and the reason says so.
    evidence = classify_entity("climate.odd_integration", declared_load=True)
    assert evidence.role == "load"
    assert evidence.reason == "declared_by_user"


def test_select_runtime_entities_splits_loads_from_modes() -> None:
    loads, modes = select_runtime_entities(
        ["climate.hallway_thermostat", "binary_sensor.water_heater_zone_1_heating"]
    )
    assert [e.entity_id for e in loads] == ["binary_sensor.water_heater_zone_1_heating"]
    assert [e.entity_id for e in modes] == ["climate.hallway_thermostat"]


def test_unrecognised_domain_is_neither_load_nor_mode() -> None:
    assert classify_entity("sensor.something").role == "unknown"


# --------------------------------------------------------------------------- #
# Effective configuration and projection
# --------------------------------------------------------------------------- #


def test_monitoring_is_derived_from_cooccupancy_not_ct_type() -> None:
    """A branch CT a device owns alone is dedicated in fact (hot tub, heater…)."""
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="br_solo",
                entity_id="sensor.solo",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            ),
        ],
        devices=[DeviceTopology(device_id="heater", branch_ct_ids=["br_solo"], phase=Phase.L1)],
    )
    graph = build_graph(topology)
    assert derive_monitoring(topology.devices[0], graph, topology=topology) == MonitoringLevel.DEDICATED


def test_branch_shared_by_two_devices_is_shared() -> None:
    topology = Topology(
        channels=[
            CTChannel(ct_id="br", entity_id="sensor.br", ct_type=CTType.BRANCH, phase=Phase.L1),
        ],
        devices=[
            DeviceTopology(device_id="a", branch_ct_ids=["br"], phase=Phase.L1),
            DeviceTopology(device_id="b", branch_ct_ids=["br"], phase=Phase.L1),
        ],
    )
    graph = build_graph(topology)
    assert derive_monitoring(topology.devices[0], graph, topology=topology) == MonitoringLevel.SHARED


def test_device_with_no_attribution_ct_is_home_total_only() -> None:
    topology = Topology(
        channels=[CTChannel(ct_id="main", entity_id="s.m", ct_type=CTType.MAIN, phase=Phase.L1)],
        devices=[DeviceTopology(device_id="ghost", phase=Phase.L1)],
    )
    graph = build_graph(topology)
    assert (
        derive_monitoring(topology.devices[0], graph, topology=topology)
        == MonitoringLevel.HOME_TOTAL_ONLY
    )


def test_declared_monitoring_overrides_the_graph() -> None:
    topology = _topology()
    graph = build_graph(topology)
    declared = MonitoringLevel.SHARED
    projected = project_configuration(
        topology,
        DeviceConfigurationSet(
            devices=[DeviceConfiguration(device_id="water_heater", monitoring=declared)]
        ),
    )
    effective = effective_config("water_heater", topology=projected, graph=graph)
    assert effective.monitoring == MonitoringLevel.SHARED


def test_projection_does_not_rewrite_evidence_derived_state_entities() -> None:
    """User configuration must not overwrite the mapping tool's provenance."""
    original = ["binary_sensor.water_heater_zone_1_heating"]
    projected = _with(
        [
            DeviceConfiguration(
                device_id="water_heater",
                state_entity_ids=["binary_sensor.user_typed"],
            )
        ]
    )
    assert projected.device_map()["water_heater"].metadata["state_entity_ids"] == original
    # ... but the user's declaration still takes effect.
    effective = effective_configs(projected)["water_heater"]
    assert "binary_sensor.user_typed" in effective.load_entity_ids


def test_removing_configuration_restores_original_metadata() -> None:
    before = _topology().device_map()["water_heater"].metadata
    configured = _with([DeviceConfiguration(device_id="water_heater", behavior=PowerBehavior.FIXED)])
    removed = project_configuration(configured, DeviceConfigurationSet(devices=[]))
    assert removed.device_map()["water_heater"].metadata == before


def test_mode_entity_listed_as_state_entity_is_reclassified() -> None:
    """The natural mistake is handled deterministically, not silently honoured."""
    projected = _with(
        [
            DeviceConfiguration(
                device_id="water_heater",
                device_type=DeviceType.WATER_HEATER,
                behavior=PowerBehavior.COMPLEX,
                state_entity_ids=["water_heater.water_heater_tank"],
                controller=ControllerConfiguration(),
            )
        ]
    )
    effective = effective_configs(projected)["water_heater"]
    assert "water_heater.water_heater_tank" not in effective.load_entity_ids
    assert "water_heater.water_heater_tank" in effective.mode_entity_ids
    assert any(r.startswith("mode_entity_reclassified") for r in effective.notes)


def test_light_group_expected_watts_flows_into_effective_config() -> None:
    projected = _with(
        [
            DeviceConfiguration(
                device_id="lamp",
                device_type=DeviceType.LIGHT,
                monitoring=MonitoringLevel.SHARED,
                light=LightConfiguration(
                    light_type=LightType.LED, bulb_count=4, watts_per_bulb_on=9.0
                ),
            )
        ]
    )
    effective = effective_configs(projected)["lamp"]
    assert effective.expected_on_w == pytest.approx(36.0)
    assert "light_group_of_4" in effective.notes


def test_configured_power_prefers_confirmed_controller_label() -> None:
    projected = _with(
        [
            DeviceConfiguration(
                device_id="water_heater",
                behavior=PowerBehavior.COMPLEX,
                expected=ExpectedPower(on_w=1000.0),
                controller=ControllerConfiguration(
                    states=[
                        ControllerStateMapping(
                            label="Heating",
                            role=ControllerStateRole.LOAD,
                            power_w=4500.0,
                            confirmed=True,
                        )
                    ]
                ),
            )
        ]
    )
    effective = effective_configs(projected)["water_heater"]
    assert configured_power(effective, "Heating", state_is_on=True) == (
        4500.0,
        "confirmed_controller_state_power",
    )
    # An unknown label falls back to the generic running value, but only when a
    # declared load signal is positively on.
    assert configured_power(effective, "Idle", state_is_on=True) == (
        1000.0,
        "configured_running_power",
    )


def test_unconfigured_device_has_no_prior() -> None:
    assert configured_power(effective_configs(_topology())["water_heater"], "on", state_is_on=True) is None


def test_mode_only_device_never_reports_its_expected_watts() -> None:
    """ADR-0012: a mode signal cannot unlock a watt value.

    A thermostat reports a setpoint mode in nearly every recorded row.  If a
    generic ``expected.on_w`` were unlocked by the mere *presence* of a state
    string,
    a 4.5 kW burner would be reported continuously — and even an ``off`` state
    would report 4.5 kW, because the prior path only checked ``state is not
    None``.  Only a declared **load** entity that is positively on may do that.
    """
    topology = project_configuration(
        Topology(
            channels=[
                CTChannel(
                    ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN, phase=Phase.L1
                )
            ],
            devices=[DeviceTopology(device_id="hallway_hvac", phase=Phase.L1)],
        ),
        DeviceConfigurationSet(
            devices=[
                DeviceConfiguration(
                    device_id="hallway_hvac",
                    device_type=DeviceType.HVAC_HEAT,
                    connectivity=HaConnectivity.CONNECTED,
                    monitoring=MonitoringLevel.HOME_TOTAL_ONLY,
                    behavior=PowerBehavior.COMPLEX,
                    expected=ExpectedPower(on_w=4500.0),
                    controller=ControllerConfiguration(
                        mode_entity_id="climate.hallway_thermostat",
                        states=[
                            ControllerStateMapping(
                                label="heat_cool", role=ControllerStateRole.MODE
                            )
                        ],
                    ),
                )
            ]
        ),
    )
    effective = effective_configs(topology)["hallway_hvac"]
    assert effective.load_entity_ids == ()  # only mode context is declared
    assert effective.mode_entity_ids == ("climate.hallway_thermostat",)
    for state in ("heat_cool", "cooling", "off", "unavailable"):
        result = _infer(topology, {"main_l1": 5000.0}, {"hallway_hvac": state}).get("hallway_hvac")
        assert result.source == Source.UNKNOWN, state
        assert result.power_w == pytest.approx(0.0), state
        assert result.confidence_tier == ConfidenceTier.TIER_0_UNKNOWN, state


def test_explicit_off_state_blocks_the_configured_prior() -> None:
    """Only a *positively on* declared load signal (or a confirmed label) counts."""
    topology = _with(
        [
            DeviceConfiguration(
                device_id="lamp",
                monitoring=MonitoringLevel.SHARED,
                state_entity_ids=["light.kitchen"],
                expected=ExpectedPower(on_w=40.0),
            )
        ]
    )
    effective = effective_configs(topology)["lamp"]
    assert configured_power(effective, "off", state_is_on=False) is None
    assert configured_power(effective, "on", state_is_on=True) == (
        40.0,
        "configured_running_power",
    )


# --------------------------------------------------------------------------- #
# Evidence tiers (ADR-0013)
# --------------------------------------------------------------------------- #


def test_tiers_are_strictly_ordered() -> None:
    ranks = [TIER_RANK[t] for t in ConfidenceTier]
    assert ranks == sorted(ranks) == [0, 1, 2, 3, 4]


def test_every_tier_has_a_description_and_typical_band() -> None:
    for tier in ConfidenceTier:
        assert TIER_DESCRIPTION[tier]
        low, high = TIER_TYPICAL_SCORE[tier]
        assert 0.0 <= low <= high <= 1.0


def test_unknown_source_is_tier_zero() -> None:
    tier, reasons = evidence_tier(TierInputs(source=Source.UNKNOWN))
    assert tier == ConfidenceTier.TIER_0_UNKNOWN
    assert "source_unknown" in reasons


def test_model_estimate_is_tier_one() -> None:
    assert evidence_tier(TierInputs(source=Source.MODEL_ESTIMATED))[0] == ConfidenceTier.TIER_1_MODELLED


def test_configured_prior_is_tier_one_with_its_own_reason() -> None:
    tier, reasons = evidence_tier(
        TierInputs(source=Source.CONFIGURED_PRIOR, prior_reason="configured_running_power")
    )
    assert tier == ConfidenceTier.TIER_1_MODELLED
    assert "configured_running_power" in reasons


def test_measured_without_corroboration_is_tier_three() -> None:
    assert evidence_tier(TierInputs(source=Source.DIRECT_MEASURED))[0] == ConfidenceTier.TIER_3_MEASURED


def test_measured_and_corroborated_is_tier_four() -> None:
    tier, _ = evidence_tier(
        TierInputs(
            source=Source.DIRECT_MEASURED,
            state_says_on=True,
            has_declared_load_signal=True,
        )
    )
    assert tier == ConfidenceTier.TIER_4_VERIFIED


def test_undeclared_state_value_is_not_corroboration() -> None:
    """A state text with no declared load entity must never reach Tier 4."""
    tier, _ = evidence_tier(
        TierInputs(
            source=Source.DIRECT_MEASURED,
            state_says_on=True,
            has_declared_load_signal=False,
        )
    )
    assert tier == ConfidenceTier.TIER_3_MEASURED


def test_stale_measurement_cannot_be_verified() -> None:
    tier, reasons = evidence_tier(
        TierInputs(
            source=Source.DIRECT_MEASURED,
            state_says_on=True,
            has_declared_load_signal=True,
            stale=True,
        )
    )
    assert tier == ConfidenceTier.TIER_3_MEASURED
    assert any("not_verifiable" in r for r in reasons)


def test_measured_value_contradicting_state_cannot_be_verified() -> None:
    tier, reasons = evidence_tier(
        TierInputs(
            source=Source.DIRECT_MEASURED,
            state_says_off=True,
            has_declared_load_signal=True,
        )
    )
    assert tier == ConfidenceTier.TIER_3_MEASURED
    assert any("disagrees_with_state" in r for r in reasons)


def test_inferred_with_corroboration_reaches_tier_three() -> None:
    tier, _ = evidence_tier(
        TierInputs(source=Source.INFERRED, state_says_on=True, has_declared_load_signal=True)
    )
    assert tier == ConfidenceTier.TIER_3_MEASURED


def test_inferred_uncorroborated_is_tier_two() -> None:
    assert evidence_tier(TierInputs(source=Source.INFERRED))[0] == ConfidenceTier.TIER_2_INFERRED


def test_grade_never_rewrites_the_score() -> None:
    """A repeated fresh sample must keep its (slightly lower) score."""
    tier, score, _ = grade(TierInputs(source=Source.INFERRED), 0.61)
    assert tier == ConfidenceTier.TIER_2_INFERRED
    assert score == pytest.approx(0.61)


def test_grade_flags_score_divergence_but_does_not_correct_it() -> None:
    tier, score, reasons = grade(TierInputs(source=Source.DIRECT_MEASURED, stale=True), 0.12)
    assert tier == ConfidenceTier.TIER_3_MEASURED
    assert score == pytest.approx(0.12)
    assert any(r.startswith("tier_score_divergence") for r in reasons)


def test_tier_rank_accepts_strings() -> None:
    assert tier_rank("tier_4_verified") == 4
    assert tier_rank("nonsense") == 0


# --------------------------------------------------------------------------- #
# End-to-end: configuration changes the answer, never fabricates it
# --------------------------------------------------------------------------- #


def test_shared_circuit_does_not_credit_one_lamp_with_the_whole_row() -> None:
    """The most damaging possible error: a 300 W row reported as a 40 W lamp."""
    topology = _with(
        [
            DeviceConfiguration(
                device_id="lamp",
                device_type=DeviceType.LIGHT,
                connectivity=HaConnectivity.CONNECTED,
                monitoring=MonitoringLevel.SHARED,
                behavior=PowerBehavior.FIXED,
                state_entity_ids=["light.kitchen"],
                expected=ExpectedPower(on_w=40.0),
            )
        ]
    )
    batch = _infer(topology, {"main_l1": 1000.0, "br_lights": 300.0}, {"lamp": "on"})
    result = batch.get("lamp")
    assert result.power_w == pytest.approx(40.0)
    assert result.source == Source.CONFIGURED_PRIOR
    assert result.confidence_tier == ConfidenceTier.TIER_1_MODELLED
    assert "shared_circuit_not_individually_measured" in result.explanation
    # The unmodelled remainder is held out of the device, not silently dropped.
    assert batch.diagnostics["shared_rows_held_as_upper_bounds"] == ["br_lights"]


def test_dedicated_ct_is_measured_and_graded_measured() -> None:
    """A dedicated CT grades Measured unless a declared signal corroborates it."""
    # The fixture water_heater carries an evidence-derived load entity, so build a
    # device with no declared signal at all to isolate the measurement grade.
    topology = Topology(
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ct_pump",
                entity_id="sensor.pump_power",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="main_l1",
            ),
        ],
        devices=[DeviceTopology(device_id="pump", dedicated_ct_ids=["ct_pump"], phase=Phase.L1)],
    )
    effective = effective_configs(topology)["pump"]
    assert effective.load_entity_ids == ()  # nothing declares runtime
    result = _infer(topology, {"main_l1": 5000.0, "ct_pump": 4500.0}, {"pump": "on"}).get("pump")
    assert result.source == Source.DIRECT_MEASURED
    assert result.power_w == pytest.approx(4500.0)
    assert result.confidence_tier == ConfidenceTier.TIER_3_MEASURED


def test_declared_load_signal_promotes_to_verified() -> None:
    topology = _with(
        [
            DeviceConfiguration(
                device_id="water_heater",
                device_type=DeviceType.HVAC_HEAT,
                connectivity=HaConnectivity.CONNECTED,
                monitoring=MonitoringLevel.DEDICATED,
                behavior=PowerBehavior.FIXED,
                state_entity_ids=["binary_sensor.water_heater_heating"],
                expected=ExpectedPower(on_w=4500.0),
            )
        ]
    )
    result = _infer(topology, {"main_l1": 5000.0, "ct_water_heater": 4500.0}, {"water_heater": "on"}).get("water_heater")
    assert result.confidence_tier == ConfidenceTier.TIER_4_VERIFIED


def test_verified_downgrades_when_the_load_signal_disagrees() -> None:
    topology = _with(
        [
            DeviceConfiguration(
                device_id="water_heater",
                monitoring=MonitoringLevel.DEDICATED,
                state_entity_ids=["binary_sensor.water_heater_heating"],
            )
        ]
    )
    result = _infer(topology, {"main_l1": 5000.0, "ct_water_heater": 4500.0}, {"water_heater": "off"}).get("water_heater")
    assert result.confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert any("disagrees_with_state" in r for r in result.tier_reasons)


def test_mode_entity_alone_produces_no_watts() -> None:
    """A thermostat saying ``heat_cool`` 99.8 % of the time must not create load."""
    topology = Topology(
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN, phase=Phase.L1),
        ],
        devices=[
            DeviceTopology(
                device_id="hallway_hvac",
                name="Hallway HVAC",
                phase=Phase.L1,
                metadata={},
            )
        ],
    )
    topology = project_configuration(
        topology,
        DeviceConfigurationSet(
            devices=[
                DeviceConfiguration(
                    device_id="hallway_hvac",
                    device_type=DeviceType.HVAC_HEAT,
                    behavior=PowerBehavior.COMPLEX,
                    # The natural mistake: the climate entity named as a state
                    # entity. It must be reclassified as mode context.
                    state_entity_ids=["climate.hallway_thermostat"],
                    controller=ControllerConfiguration(obeys_control=False),
                )
            ]
        ),
    )
    effective = effective_configs(topology)["hallway_hvac"]
    assert effective.load_entity_ids == ()
    result = _infer(
        topology, {"main_l1": 5000.0}, {"hallway_hvac": "heat_cool"}
    ).get("hallway_hvac")
    # Only a main row observes it, so nothing may be attributed to this device.
    assert result.source == Source.UNKNOWN
    assert result.power_w == pytest.approx(0.0)
    assert result.confidence_tier == ConfidenceTier.TIER_0_UNKNOWN


def test_off_device_prior_is_not_applied() -> None:
    """A device whose declared load signal says OFF must not report its on_w."""
    topology = _with(
        [
            DeviceConfiguration(
                device_id="lamp",
                monitoring=MonitoringLevel.SHARED,
                state_entity_ids=["light.kitchen"],
                expected=ExpectedPower(on_w=40.0),
            )
        ]
    )
    result = _infer(topology, {"main_l1": 1000.0, "br_lights": 300.0}, {"lamp": "off"}).get("lamp")
    assert result.source == Source.UNKNOWN


def test_home_total_only_device_reconciles_without_crashing() -> None:
    """Defect #22: a topology with no measurement rows produced a dtype crash.

    This is the user's "not under any sub energy monitor, only under the home
    energy monitor" case: the device has no attribution CT, so the matrix has
    zero rows.  Reconciliation must report an unexplained residual, not raise.
    """
    topology = Topology(
        channels=[CTChannel(ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN, phase=Phase.L1)],
        devices=[DeviceTopology(device_id="fridge", phase=Phase.L1)],
    )
    batch = _infer(topology, {"main_l1": 800.0}, {"fridge": "on"})
    result = batch.get("fridge")
    assert result.source == Source.UNKNOWN
    assert result.confidence_tier == ConfidenceTier.TIER_0_UNKNOWN
    # The whole 800 W stays in the unknown residual; it is never invented.
    assert batch.reconciliation.unknown_residual_w == pytest.approx(800.0)
    assert batch.reconciliation.accounted_fraction == pytest.approx(0.0)


def test_tiers_are_persisted_in_the_result_contract() -> None:
    result = _infer(_topology(), {"main_l1": 5000.0, "ct_water_heater": 4500.0}, {"water_heater": "on"}).get(
        "water_heater"
    )
    payload = result.model_dump(mode="json")
    # The fixture declares a load entity, so the CT measurement is corroborated.
    assert payload["confidence_tier"] == "tier_4_verified"
    assert payload["monitoring_level"] == "dedicated"
    assert payload["tier_reasons"]
    assert payload["confidence_is_calibrated"] is False
