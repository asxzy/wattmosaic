"""Attaching a WattMosaic measurement to an existing load.

Two user-visible cases drive every test here:

**One double-pole load with its own CT.**  A hot tub on 240 V has two hot legs
and one CT that the monitor reports twice.  Declaring "this CT measures that
device" must produce a *direct measurement*, not a metadata note the solver
never reads.

**Many loads sharing one circuit.**  A bank of dimmers on one circuit has
several CTs (or one CT and several switches), and the switch state is the only
per-lamp evidence available.  A device that is positively OFF must consume no
power, so the single device that is ON owns the row.  Every ambiguous
combination must stay unknown: `off` is evidence, `unavailable` is not.

Every scenario is synthetic.  The engine sees only the window, states, models
and topology; ground truth is constructed first and measurements are derived
from it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    ConfidenceTier,
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceTopology,
    ExpectedPower,
    HaConnectivity,
    MonitoringLevel,
    Phase,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.configuration import (
    PROJECTED_CTS_KEY,
    project_configuration,
)
from wattmosaic_engine.disaggregation.engine import DisaggregationEngine
from wattmosaic_engine.electrical.direct import resolve_direct
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.electrical.window import ObservationWindow

AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _window(values: dict[str, float]) -> ObservationWindow:
    return ObservationWindow.from_values(AT, values)


def _infer(topology: Topology, values: dict[str, float], states: dict[str, str]):
    return DisaggregationEngine().infer(
        _window(values), device_states=states, models=None, topology=topology
    )


# --------------------------------------------------------------------------- #
# Declared CT reaches attribution
# --------------------------------------------------------------------------- #


def _declared_topology(role: CTType) -> Topology:
    """One main, one CT whose role is being tested, one device."""
    return Topology(
        status=TopologyStatus.CONFIRMED,
        channels=[
            CTChannel(
                ct_id="ct_main",
                entity_id="sensor.main",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct_load",
                entity_id="sensor.load_power",
                ct_type=role,
                phase=Phase.BOTH,
                parent_ct_id="ct_main",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="hot_tub",
                name="Hot tub",
                phase=Phase.BOTH,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
    )


def _declare(topology: Topology) -> Topology:
    return project_configuration(
        topology,
        [
            DeviceConfiguration(
                device_id="hot_tub",
                monitoring=MonitoringLevel.DEDICATED,
                monitored_by_ct_ids=["ct_load"],
            )
        ],
    )


def test_declared_dedicated_ct_becomes_a_direct_measurement() -> None:
    """A declaration is not a note: the graph must expose it as an attribution."""
    projected = _declare(_declared_topology(CTType.DEDICATED_DEVICE))
    device = projected.device_map()["hot_tub"]
    assert device.dedicated_ct_ids == ["ct_load"]

    graph = build_graph(projected)
    assert graph.attribution_ct_ids("hot_tub") == ("ct_load",)
    reading = resolve_direct(
        {"ct_load": 1200.0}, graph, max_age_s=120, at=AT
    ).get("hot_tub")
    assert reading is not None
    assert reading.usable is True
    assert reading.power_w == pytest.approx(1200.0)


def test_declared_branch_ct_becomes_an_inference_row_not_a_measurement() -> None:
    """Only a dedicated role may claim a direct measurement."""
    projected = _declare(_declared_topology(CTType.BRANCH))
    device = projected.device_map()["hot_tub"]
    assert device.dedicated_ct_ids == []
    assert device.branch_ct_ids == ["ct_load"]

    graph = build_graph(projected)
    assert graph.attribution_ct_ids("hot_tub") == ("ct_load",)
    assert resolve_direct({"ct_load": 1200.0}, graph, max_age_s=120, at=AT) == {}


def test_declared_unclassified_ct_is_attributed_as_a_branch() -> None:
    """A user who declares a CT without extra classification still gets a row."""
    projected = _declare(_declared_topology(CTType.UNKNOWN))
    assert projected.device_map()["hot_tub"].branch_ct_ids == ["ct_load"]
    assert build_graph(projected).attribution_ct_ids("hot_tub") == ("ct_load",)


def test_removing_the_configuration_retracts_the_projected_ct() -> None:
    """A removed declaration must not leave a phantom attribution behind.

    The retraction is limited to CTs this projection added, so a CT the user
    assigned through the channel wizard survives.
    """
    projected = _declare(_declared_topology(CTType.DEDICATED_DEVICE))
    assert projected.device_map()["hot_tub"].metadata.get(PROJECTED_CTS_KEY)

    removed = project_configuration(
        projected.model_copy(update={"version": projected.version + 1}), []
    )
    device = removed.device_map()["hot_tub"]
    assert device.dedicated_ct_ids == []
    assert device.branch_ct_ids == []
    assert PROJECTED_CTS_KEY not in device.metadata
    assert build_graph(removed).attribution_ct_ids("hot_tub") == ()


def test_retraction_leaves_a_wizard_assigned_ct_alone() -> None:
    """Only what the projection added is removed."""
    topology = _declared_topology(CTType.DEDICATED_DEVICE)
    topology = topology.model_copy(
        update={
            "devices": [
                topology.devices[0].model_copy(
                    update={"dedicated_ct_ids": ["ct_load"]}
                )
            ]
        }
    )
    # No configuration at all: nothing is projected, nothing is retracted.
    left_alone = project_configuration(topology, [])
    assert left_alone.device_map()["hot_tub"].dedicated_ct_ids == ["ct_load"]


def test_disabled_channel_is_not_attributed() -> None:
    """A disabled CT proves nothing; never invent a measurement from it."""
    topology = _declared_topology(CTType.DEDICATED_DEVICE)
    topology = topology.model_copy(
        update={
            "channels": [
                topology.channels[0],
                topology.channels[1].model_copy(update={"enabled": False}),
            ]
        }
    )
    projected = _declare(topology)
    assert projected.device_map()["hot_tub"].dedicated_ct_ids == []
    assert build_graph(projected).attribution_ct_ids("hot_tub") == ()


# --------------------------------------------------------------------------- #
# Shared circuit: one CT, several switches
# --------------------------------------------------------------------------- #


def _shared_topology(*, devs: tuple[str, ...] = ("lamp_a", "lamp_b")) -> Topology:
    """One branch CT that carries every listed lamp; each lamp has a switch."""
    topology = Topology(
        status=TopologyStatus.CONFIRMED,
        channels=[
            CTChannel(
                ct_id="ct_main",
                entity_id="sensor.main",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct_lights",
                entity_id="sensor.lights_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="ct_main",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id=device_id,
                name=device_id,
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
            for device_id in devs
        ],
    )
    return project_configuration(
        topology,
        [
            DeviceConfiguration(
                device_id=device_id,
                monitoring=MonitoringLevel.SHARED,
                connectivity=HaConnectivity.CONNECTED,
                monitored_by_ct_ids=["ct_lights"],
                state_entity_ids=[f"light.{device_id}"],
            )
            for device_id in devs
        ],
    )


def _by_device(batch) -> dict[str, object]:
    return {result.device_id: result for result in batch.results}


def test_single_on_claimant_owns_the_whole_row() -> None:
    """One lamp ON, the other positively OFF: the CT measures the ON lamp."""
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    assert results["lamp_a"].power_w == pytest.approx(200.0)
    # ``source`` is what the UI labels "Measurement source", and it must say
    # ``inferred``: a shared CT never measured this lamp individually, however
    # certain the switch state is.  The *evidence tier* is a separate ordering of
    # how strong the evidence is, and the contract already promotes a
    # corroborated shared-channel estimate to tier 3 (``tiers.py``:
    # "a declared load signal corroborates a shared-channel estimate").
    assert results["lamp_a"].source == Source.INFERRED
    assert results["lamp_a"].confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert results["lamp_b"].power_w == pytest.approx(0.0)


def test_the_off_device_is_never_credited_a_share_of_the_row() -> None:
    """A declared-OFF device must not absorb the solver's least-norm tie-break.

    This is the fabrication the pin exists to prevent: two lamps on one branch
    CT previously split 190/10 and credited 10 W to a lamp the user said was
    off.  A device with no stated standby cannot draw anything.
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    assert results["lamp_b"].power_w < 1e-6


def test_stated_standby_is_allowed_while_off() -> None:
    """A stated standby draw is real knowledge, and is still not invented."""
    topology = _shared_topology()
    projected = project_configuration(
        topology,
        [
            DeviceConfiguration(
                device_id="lamp_a",
                monitoring=MonitoringLevel.SHARED,
                connectivity=HaConnectivity.CONNECTED,
                monitored_by_ct_ids=["ct_lights"],
                state_entity_ids=["light.lamp_a"],
                expected=ExpectedPower(standby_w=3.0),
            ),
            DeviceConfiguration(
                device_id="lamp_b",
                monitoring=MonitoringLevel.SHARED,
                connectivity=HaConnectivity.CONNECTED,
                monitored_by_ct_ids=["ct_lights"],
                state_entity_ids=["light.lamp_b"],
                expected=ExpectedPower(standby_w=3.0),
            ),
        ],
    )
    results = _by_device(
        _infer(
            projected,
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    assert results["lamp_a"].power_w == pytest.approx(196.0, abs=0.5)
    assert results["lamp_b"].power_w == pytest.approx(4.0, abs=0.5)


def test_two_on_claimants_stay_unknown() -> None:
    """Two lamps ON on one CT are not separable: never split the row."""
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "on"},
        )
    )
    for result in results.values():
        assert result.source == Source.UNKNOWN
        assert result.confidence_tier == ConfidenceTier.TIER_0_UNKNOWN
        assert result.power_w == pytest.approx(0.0)


def test_an_unavailable_sibling_blocks_the_single_claimant_rule() -> None:
    """`unavailable` might be drawing; only a *positive* OFF clears the row."""
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "unavailable"},
        )
    )
    for result in results.values():
        assert result.source == Source.UNKNOWN
        assert result.power_w == pytest.approx(0.0)


def test_an_unknown_sibling_blocks_the_single_claimant_rule() -> None:
    """A missing state is not an OFF statement."""
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on"},
        )
    )
    for result in results.values():
        assert result.source == Source.UNKNOWN


def test_everything_off_leaves_the_row_in_the_residual() -> None:
    """A live row with every declared device OFF is unexplained, not attributed."""
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "off", "lamp_b": "off"},
        )
    )
    for result in results.values():
        assert result.source == Source.UNKNOWN
        assert result.power_w == pytest.approx(0.0)


def test_state_pin_does_not_require_a_learnt_model() -> None:
    """A declared load signal is evidence in its own right (ADR-0012).

    Requiring a learnt model made every switch-only shared circuit permanently
    unidentifiable, even though the state said exactly which lamps were dark.
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    assert results["lamp_a"].source == Source.INFERRED
    assert results["lamp_a"].power_w == pytest.approx(200.0)

    # Removing the declared signal removes the evidence: nothing attributes.
    undeclared = _shared_topology().model_copy(
        update={
            "devices": [
                device.model_copy(update={"metadata": {}})
                for device in _shared_topology().devices
            ]
        }
    )
    unlinked = _by_device(
        _infer(
            undeclared,
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    assert unlinked["lamp_a"].source == Source.UNKNOWN


def test_a_state_pin_is_never_relaxed_by_the_model_bound_pass() -> None:
    """The relaxation exists for model priors, never for state evidence.

    A learnt model's upper bound may be wrong (an old model of a lamp that has
    since been replaced), so the CT measurement is trusted over it and the bound
    is widened.  A *state* pin is different: it is a declaration that the device
    is off, and widening it would credit power to a device the user said was
    dark.

    Both devices here carry a model whose bound (100 W) is below the row
    (200 W), so the relaxation pass runs for both.  Only the ON device may be
    relaxed; the OFF one keeps its zero pin and the diagnostics say so.
    """
    from wattmosaic_engine.contracts import (
        DeviceModelParameters,
        ModelStatus,
        ModelType,
    )
    from wattmosaic_engine.electrical.models import ModelView

    def view(device_id: str) -> ModelView:
        return ModelView(
            device_id=device_id,
            model_id=f"m_{device_id}",
            model_type=ModelType.BINARY,
            version=1,
            status=ModelStatus.ACTIVE,
            params=DeviceModelParameters(on_power_w=200.0),
            upper_bound_w=100.0,
            estimated_power_w=200.0,
        )

    batch = DisaggregationEngine().infer(
        _window({"ct_main": 500.0, "ct_lights": 200.0}),
        device_states={"lamp_a": "on", "lamp_b": "off"},
        models={"lamp_a": view("lamp_a"), "lamp_b": view("lamp_b")},
        topology=_shared_topology(),
    )
    results = _by_device(batch)
    # The ON lamp is measured at the full row; its stale model bound is widened.
    assert results["lamp_a"].power_w == pytest.approx(200.0)
    assert "lamp_a" in batch.diagnostics["relaxed_bounds"]
    # The OFF lamp keeps its pin and must not be reported as relaxed.
    assert results["lamp_b"].power_w == pytest.approx(0.0)
    assert "lamp_b" not in batch.diagnostics["relaxed_bounds"]


# --------------------------------------------------------------------------- #
# The product path must be able to reach this behaviour
# --------------------------------------------------------------------------- #


def test_a_branch_channel_carries_exactly_one_device() -> None:
    """Why the device form needs its own CT list: a channel names one device.

    The channel wizard writes ``CTChannel.device_id``, which is a single id.
    Three lamps on one circuit therefore cannot be described by the wizard at
    all -- the second and third assignment would overwrite the first.  The
    engine's ``DeviceConfiguration.monitored_by_ct_ids`` is the mechanism that
    expresses many devices on one row, so the product must send it.
    """
    channel = CTChannel(
        ct_id="ct_lights",
        entity_id="sensor.lights_power",
        ct_type=CTType.BRANCH,
        phase=Phase.L1,
        parent_ct_id="ct_main",
        device_id="lamp_a",
    )
    assert channel.device_id == "lamp_a"
    # There is no second slot to put lamp_b in.
    assert not hasattr(channel, "device_ids")


def test_declaring_a_shared_ct_gives_every_device_an_attribution_row() -> None:
    """The declaration, not the channel, is what puts a lamp on the row."""
    projected = _shared_topology(devs=("lamp_a", "lamp_b", "lamp_c"))
    for device in projected.devices:
        if device.device_id not in ("lamp_a", "lamp_b", "lamp_c"):
            continue
        assert "ct_lights" in device.branch_ct_ids


def test_three_lamps_one_row_still_resolve_when_one_is_on() -> None:
    """A bank of three lamps on one circuit: the ON one owns the whole row."""
    results = _by_device(
        _infer(
            _shared_topology(devs=("lamp_a", "lamp_b", "lamp_c")),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "off", "lamp_b": "on", "lamp_c": "off"},
        )
    )
    assert results["lamp_b"].power_w == pytest.approx(200.0)
    assert results["lamp_b"].source == Source.INFERRED
    assert results["lamp_a"].power_w == pytest.approx(0.0)
    assert results["lamp_c"].power_w == pytest.approx(0.0)


def test_removing_the_declaration_retracts_the_attribution_row() -> None:
    """Deleting the configuration must not leave a phantom row behind."""
    topology = _shared_topology(devs=("lamp_a", "lamp_b"))
    emptied = project_configuration(topology, [])
    for device in emptied.devices:
        assert not device.metadata.get(PROJECTED_CTS_KEY)
        assert "ct_lights" not in device.branch_ct_ids
