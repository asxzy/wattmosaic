"""Projecting a fixture onto a CT row: materialisation, ownership, dimming.

A device that only a configuration defines -- a light behind a shared circuit --
has no channel of its own, so discovery never emits it.  These tests pin the two
things that make creating it safe:

* it is placed on the row it declares, with a phase inherited from that row, and
  it disappears when the declaration is removed;
* it is **never** placed on a row another device already owns as a dedicated
  measurement, because that would count the same circuit twice.

The dimmer cases pin the other half of the requirement: a measured
level→watts curve changes the reported watts with the measured level, while a
missing level means "full on" with a distinct reason rather than a silent guess.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceTopology,
    ExpectedPower,
    HaConnectivity,
    LightConfiguration,
    MonitoringLevel,
    Phase,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.configuration import (
    cts_claimed_by_dedicated,
    effective_configs,
    interpolate_curve,
    materialisation_blockers,
    project_configuration,
)
from wattmosaic_engine.disaggregation.engine import DisaggregationEngine
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.electrical.window import ObservationWindow

AT = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
MATERIALISED = "materialised_from_config"


def _topology(*, branch: bool = True) -> Topology:
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
                ct_type=CTType.BRANCH if branch else CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                device_id=None if branch else "dev_lights",
            ),
        ],
        devices=[] if branch else [
            DeviceTopology(
                device_id="dev_lights",
                name="lights",
                phase=Phase.L1,
                dedicated_ct_ids=["ct1"],
            )
        ],
    )


def _fixture(device_id: str = "bar", ct: str = "ct1") -> DeviceConfiguration:
    return DeviceConfiguration(
        device_id=device_id,
        connectivity=HaConnectivity.CONNECTED,
        monitoring=MonitoringLevel.SHARED,
        state_entity_ids=[f"light.{device_id}"],
        monitored_by_ct_ids=[ct],
        configured_by="auto",
        expected=ExpectedPower(on_w=19.57),
    )


# --------------------------------------------------------------------------- #
# Materialisation
# --------------------------------------------------------------------------- #


def test_a_configured_device_absent_from_topology_is_materialised_on_its_row() -> None:
    """Discovery cannot emit a fixture behind a circuit; the declaration can."""
    projected = project_configuration(_topology(), [_fixture()])
    device = projected.device_map()["bar"]

    assert device.branch_ct_ids == ["ct1"]
    # Phase is inherited from the row, never invented.
    assert device.phase == Phase.L1
    assert device.metadata[MATERIALISED] == "auto"
    assert build_graph(projected).problems == []


def test_removing_the_configuration_removes_the_materialised_device() -> None:
    """A declaration is the only thing that created it, so removing it removes it."""
    with_device = project_configuration(_topology(), [_fixture()])
    assert "bar" in with_device.device_map()

    removed = project_configuration(with_device, [])
    assert "bar" not in removed.device_map()


def test_two_auto_devices_on_one_row_are_both_materialised() -> None:
    """The normal shared-circuit case: several fixtures, one CT row."""
    projected = project_configuration(_topology(), [_fixture("bar"), _fixture("laundry")])
    assert {"bar", "laundry"} <= set(projected.device_map())
    assert build_graph(projected).problems == []


# --------------------------------------------------------------------------- #
# Ownership: never count one circuit twice
# --------------------------------------------------------------------------- #


def test_a_dedicated_row_reports_its_claimant() -> None:
    owner = cts_claimed_by_dedicated(_topology(branch=False), ["ct1"])
    assert owner == ("dev_lights", "ct1")


def test_a_branch_row_has_no_claimant() -> None:
    assert cts_claimed_by_dedicated(_topology(), ["ct1"]) is None


def test_a_device_owning_its_own_dedicated_row_is_not_a_conflict() -> None:
    """The direct-measurement path must keep working for the device that owns it."""
    config = DeviceConfiguration(
        device_id="dev_lights",
        monitoring=MonitoringLevel.DEDICATED,
        monitored_by_ct_ids=["ct1"],
    )
    assert materialisation_blockers(_topology(branch=False), [config]) == {}


def test_placing_a_second_device_on_an_owned_row_is_refused() -> None:
    """Adding a fixture to a dedicated row would report the row twice."""
    topology = _topology(branch=False)
    projected = project_configuration(topology, [_fixture()])

    assert "bar" not in projected.device_map()
    blockers = materialisation_blockers(topology, [_fixture()])
    assert "bar" in blockers
    assert "convert that channel to a shared circuit" in blockers["bar"]


def test_the_owned_row_is_still_credited_exactly_once() -> None:
    """The refusal exists so the circuit has one claimant, not two."""
    topology = _topology(branch=False)
    projected = project_configuration(topology, [_fixture()])
    window = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 28.0})

    results = DisaggregationEngine().infer(
        window,
        device_states={"dev_lights": "on", "bar": "on"},
        models=None,
        topology=projected,
    ).results
    by_id = {result.device_id: result for result in results}

    assert "bar" not in by_id
    assert by_id["dev_lights"].power_w == pytest.approx(28.0)
    assert by_id["dev_lights"].source == Source.DIRECT_MEASURED.value


def test_converting_the_row_to_a_branch_unblocks_the_placement() -> None:
    """The reported remedy actually works."""
    branch = _topology(branch=True)
    projected = project_configuration(branch, [_fixture()])

    assert materialisation_blockers(branch, [_fixture()]) == {}
    assert projected.device_map()["bar"].branch_ct_ids == ["ct1"]
    assert build_graph(projected).problems == []


# --------------------------------------------------------------------------- #
# Dimming: measured level -> measured watts
# --------------------------------------------------------------------------- #


def _dimmer(curve: list[tuple[float, float]]) -> DeviceConfiguration:
    return DeviceConfiguration(
        device_id="bar",
        connectivity=HaConnectivity.CONNECTED,
        monitoring=MonitoringLevel.SHARED,
        state_entity_ids=["light.bar"],
        monitored_by_ct_ids=["ct1"],
        configured_by="auto",
        light=LightConfiguration(
            bulb_count=1,
            watts_per_bulb_on=20.0,
            dimming_curve=curve,
        ),
    )


def _infer(config: DeviceConfiguration, level: float | None):
    topology = project_configuration(_topology(), [config])
    base = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 27.92})
    metadata = {} if level is None else {"device_levels": {"bar": level}}
    window = ObservationWindow(
        at=base.at, readings=base.readings, metadata=metadata
    )
    return DisaggregationEngine().infer(
        window, device_states={"bar": "on"}, models=None, topology=topology
    ).results[0]


def test_a_measured_curve_is_scaled_from_fraction_to_watts() -> None:
    """The contract stores fractions; attribution needs watts."""
    configs = effective_configs(
        project_configuration(_topology(), [_dimmer([(0.4, 0.4), (1.0, 1.0)])])
    )
    assert configs["bar"].dimming_curve == ((0.4, 8.0), (1.0, 20.0))


def test_a_dimmer_reports_watts_at_the_measured_level() -> None:
    """At 40 % the fixture draws 8 W, not its full-on 20 W."""
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])

    at_40 = _infer(config, 0.4)
    assert at_40.power_w == pytest.approx(8.0)
    assert "measured_dimming_curve" in at_40.explanation


def test_a_level_between_measured_points_is_interpolated() -> None:
    """A steady dimmer position between two measurements uses the measured shape."""
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])

    at_70 = _infer(config, 0.7)
    assert at_70.power_w == pytest.approx(14.0)


def test_without_a_reported_level_the_full_on_value_is_used_and_labelled() -> None:
    """No brightness is "not reported", never 100 % -- and the reason says which."""
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])

    no_level = _infer(config, None)
    assert no_level.power_w == pytest.approx(20.0)
    assert "configured_running_power" in no_level.explanation
    assert "measured_dimming_curve" not in no_level.explanation


def test_a_level_is_clamped_to_the_measured_range() -> None:
    """Below the lowest measured point is clamped, not extrapolated to zero."""
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])

    at_5 = _infer(config, 0.05)
    assert at_5.power_w == pytest.approx(8.0)


def test_interpolate_curve_refuses_an_empty_curve() -> None:
    assert interpolate_curve([], 0.5) is None


def test_a_curve_without_full_on_watts_is_not_used() -> None:
    """Fractions cannot be turned into watts without the fixture's full output."""
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])
    config = config.model_copy(
        update={"light": config.light.model_copy(update={"watts_per_bulb_on": None})}
    )
    configs = effective_configs(project_configuration(_topology(), [config]))
    assert configs["bar"].dimming_curve == ()


# --------------------------------------------------------------------------- #
# Shared-circuit behaviour with materialised siblings
# --------------------------------------------------------------------------- #


def test_modelled_siblings_make_the_row_separable() -> None:
    """Two declared fixtures on one row are each bounded by their own prior."""
    topology = project_configuration(
        _topology(), [_fixture("bar"), _fixture("laundry")]
    )
    window = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 27.92})

    both_on = DisaggregationEngine().infer(
        window,
        device_states={"bar": "on", "laundry": "on"},
        models=None,
        topology=topology,
    ).results
    by_id = {result.device_id: result for result in both_on}

    # Both priors are reported, and neither claims the whole row.
    assert by_id["bar"].power_w == pytest.approx(19.57)
    assert by_id["laundry"].power_w == pytest.approx(19.57)
    assert by_id["bar"].source == Source.CONFIGURED_PRIOR.value


def test_both_off_leaves_the_baseline_unattributed() -> None:
    """The standing load on the row is not invented onto a fixture."""
    topology = project_configuration(
        _topology(), [_fixture("bar"), _fixture("laundry")]
    )
    window = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 8.35})

    results = DisaggregationEngine().infer(
        window,
        device_states={"bar": "off", "laundry": "off"},
        models=None,
        topology=topology,
    ).results

    assert all(result.power_w == 0.0 for result in results)
    assert all(result.source == Source.UNKNOWN.value for result in results)


def test_a_reported_level_without_a_measured_curve_does_not_invent_watts() -> None:
    """A brightness signal alone is not a wattage measurement.

    The level gate exists so that interpolating a curve *requires* a curve.  A
    dimmer whose curve has not been learned still reports its configured full-on
    watts, and says so with the flat reason rather than pretending the level was
    used.
    """
    config = _fixture()  # no ``light`` section at all
    topology = project_configuration(_topology(), [config])
    base = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 27.92})
    window = ObservationWindow(
        at=base.at,
        readings=base.readings,
        metadata={"device_levels": {"bar": 0.4}},
    )

    result = DisaggregationEngine().infer(
        window, device_states={"bar": "on"}, models=None, topology=topology
    ).results[0]

    assert result.power_w == pytest.approx(19.57)
    assert "configured_running_power" in result.explanation
    assert "measured_dimming_curve" not in result.explanation


def test_a_malformed_level_degrades_to_not_reported() -> None:
    """A junk brightness value must not become a wattage from the curve.

    ``NaN`` and a string compare false against every bound, so a naive read would
    fall through to the curve's last point and report those watts as measured.
    """
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])
    topology = project_configuration(_topology(), [config])
    base = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 27.92})

    for junk in ("not-a-number", float("nan"), None):
        window = ObservationWindow(
            at=base.at,
            readings=base.readings,
            metadata={"device_levels": {"bar": junk}},
        )
        result = DisaggregationEngine().infer(
            window, device_states={"bar": "on"}, models=None, topology=topology
        ).results[0]
        assert result.power_w == pytest.approx(20.0), junk
        assert "configured_running_power" in result.explanation, junk


def test_an_out_of_range_level_is_held_at_the_measured_endpoints() -> None:
    """A percentage sent as ``40`` rather than ``0.4`` must not extrapolate.

    The clamp lives in ``interpolate_curve`` so there is exactly one rule; this
    asserts the wiring actually reaches it.
    """
    config = _dimmer([(0.4, 0.4), (1.0, 1.0)])
    topology = project_configuration(_topology(), [config])
    base = ObservationWindow.from_values(AT, {"main": 500.0, "ct1": 27.92})
    window = ObservationWindow(
        at=base.at,
        readings=base.readings,
        metadata={"device_levels": {"bar": 40.0}},
    )

    result = DisaggregationEngine().infer(
        window, device_states={"bar": "on"}, models=None, topology=topology
    ).results[0]

    # Held at the measured maximum, not 40x the curve.
    assert result.power_w == pytest.approx(20.0)
