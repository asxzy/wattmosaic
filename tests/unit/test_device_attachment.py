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

from datetime import UTC, datetime, timedelta

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
CADENCE = 10.0


def _window(values: dict[str, float]) -> ObservationWindow:
    return ObservationWindow.from_values(AT, values)


def _infer(
    topology: Topology,
    values: dict[str, float],
    states: dict[str, str],
    *,
    history: dict[str, str] | None = None,
    step: tuple[float, float] | None = None,
):
    """Infer once, optionally with a measured step on the lighting row.

    ``step=(before_w, after_w)`` writes a history that steps at the window
    instant and marks the devices in ``history`` as having just changed from the
    state named there.  That is the input the delta rule needs: a CT movement
    coinciding with one device's transition and no other device's.
    """
    previous_states = dict(history or {})
    transitions = [
        {
            "device_id": did,
            "previous_state": previous,
            "new_state": states[did],
        }
        for did, previous in previous_states.items()
        if states.get(did) is not None and states[did] != previous
    ]
    histories: dict[str, list[tuple]] = {}
    if step is not None:
        before_w, after_w = step
        rows = [
            (AT - timedelta(seconds=CADENCE * (9 - i)), before_w) for i in range(6)
        ]
        rows += [(AT - timedelta(seconds=CADENCE * (3 - i)), after_w) for i in range(4)]
        histories["ct_lights"] = rows
    return DisaggregationEngine().infer(
        ObservationWindow.from_values(
            AT,
            values,
            histories=histories,
            cadence_s=CADENCE if step is not None else None,
            previous_states=previous_states,
            state_transitions=transitions,
        ),
        device_states=states,
        models=None,
        topology=topology,
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
    """A measured step on the row is the ON lamp's draw (ADR-0026).

    Superseded ADR-0023 asserted 200 W here from the state alone, with no
    wattage stated and no movement seen on the row.  That answer was only right
    when the two declared lamps were the *whole* circuit: it was measured to
    credit 881.72 W to one lamp on a row that carried several undeclared
    fixtures.  The engine cannot tell a complete fixture list from a partial one,
    so the whole row is no longer credited on state alone.

    What remains, and is stronger, is the measured *change*: the row moved by
    200 W at the instant this lamp turned on, and nothing else on the row
    changed.  That is a measurement of the lamp.
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
            history={"lamp_a": "off", "lamp_b": "off"},
            step=(0.0, 200.0),
        )
    )
    assert results["lamp_a"].power_w == pytest.approx(200.0)
    # ``source`` is what the UI labels "Measurement source", and it must say
    # ``inferred``: the row is still shared, so no dedicated CT measured this
    # lamp.  The *evidence tier* says the value is a measurement, because a
    # 200 W step on the row at this lamp's transition, with the sibling steady,
    # measures it.
    assert results["lamp_a"].source == Source.INFERRED
    assert results["lamp_a"].confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert "shared_circuit_step_measured" in results["lamp_a"].explanation
    assert results["lamp_b"].power_w == pytest.approx(0.0)


def test_state_alone_does_not_credit_the_whole_row() -> None:
    """Without a measured step, an un-wattaged claimant gets nothing.

    This is the regression that ADR-0023 could not express.  Two declared
    fixtures, neither stating a wattage, one ON and one OFF: the old rule handed
    the entire row to the ON fixture at ``tier_3_measured``.  Declaring a second
    fixture was what *enabled* that, so more truth made the answer less safe.
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 881.72},
            {"lamp_a": "on", "lamp_b": "off"},
        )
    )
    for result in results.values():
        assert result.power_w == pytest.approx(0.0)
        assert result.source == Source.UNKNOWN
        assert result.confidence_tier == ConfidenceTier.TIER_0_UNKNOWN


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
    """A stated standby draw does not let an un-wattaged row be split.

    The old rule credited the ON lamp 196 W and its OFF sibling 4 W of standby.
    That 196 W was the whole row minus standby, with no wattage stated and no
    movement observed -- the same fabrication that over-credited a lamp with a
    whole lighting circuit.  With the delta rule the measured step is what
    attributes power; a standby declaration bounds an *off* device, it does not
    license the rest of the row.
    """
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
    assert results["lamp_a"].power_w == pytest.approx(0.0)
    assert results["lamp_a"].source == Source.UNKNOWN
    # The OFF lamp is still never credited more than its stated standby.
    assert results["lamp_b"].power_w <= 3.0


def test_a_measured_step_attributes_more_than_standby() -> None:
    """The step, not the standby declaration, is what makes the row separable."""
    projected = project_configuration(
        _shared_topology(),
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
            history={"lamp_a": "off", "lamp_b": "off"},
            step=(0.0, 200.0),
        )
    )
    assert results["lamp_a"].power_w == pytest.approx(200.0)
    assert results["lamp_a"].confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert results["lamp_b"].power_w == pytest.approx(0.0)


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
    unidentifiable.  The state still matters -- it is what tells the engine
    *which* device changed, and therefore whose step a row movement is -- but it
    is paired with a measured step rather than standing in for one (ADR-0026).
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "off"},
            history={"lamp_a": "off", "lamp_b": "off"},
            step=(0.0, 200.0),
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
            history={"lamp_a": "off", "lamp_b": "off"},
            step=(0.0, 200.0),
        )
    )
    assert unlinked["lamp_a"].source == Source.UNKNOWN


def test_a_state_pin_is_never_relaxed_by_the_model_bound_pass() -> None:
    """A stale model bound must never win against a measured step.

    A learnt model's upper bound may be wrong (an old model of a lamp that has
    since been replaced), so evidence from the CT is trusted over it.  A *state*
    pin is also never widened: the OFF lamp keeps its zero pin, and the ON lamp
    takes the measured step rather than the stale 100 W bound.
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
        ObservationWindow.from_values(
            AT,
            {"ct_main": 500.0, "ct_lights": 200.0},
            histories={
                "ct_lights": [
                    (AT - timedelta(seconds=CADENCE * (9 - i)), 0.0) for i in range(6)
                ]
                + [
                    (AT - timedelta(seconds=CADENCE * (3 - i)), 200.0)
                    for i in range(4)
                ]
            },
            cadence_s=CADENCE,
            previous_states={"lamp_a": "off", "lamp_b": "off"},
            state_transitions=[
                {
                    "device_id": "lamp_a",
                    "previous_state": "off",
                    "new_state": "on",
                }
            ],
        ),
        device_states={"lamp_a": "on", "lamp_b": "off"},
        models={"lamp_a": view("lamp_a"), "lamp_b": view("lamp_b")},
        topology=_shared_topology(),
    )
    results = _by_device(batch)
    # The ON lamp is measured at the step, not at its stale 100 W model bound.
    assert results["lamp_a"].power_w == pytest.approx(200.0)
    assert results["lamp_a"].confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert "shared_circuit_step_measured" in results["lamp_a"].explanation
    # The OFF lamp is never credited a model's full running power.
    assert results["lamp_b"].power_w == pytest.approx(0.0)


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
    """A bank of three lamps: the measured step names the lamp that caused it.

    The siblings may be in any state; all that matters is that they held steady
    across the transition, which is the user's rule (ADR-0026).
    """
    results = _by_device(
        _infer(
            _shared_topology(devs=("lamp_a", "lamp_b", "lamp_c")),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "off", "lamp_b": "on", "lamp_c": "off"},
            history={"lamp_b": "off"},
            step=(0.0, 200.0),
        )
    )
    assert results["lamp_b"].power_w == pytest.approx(200.0)
    assert results["lamp_b"].source == Source.INFERRED
    assert results["lamp_b"].confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert results["lamp_a"].power_w == pytest.approx(0.0)
    assert results["lamp_c"].power_w == pytest.approx(0.0)


def test_a_concurrent_sibling_change_blocks_step_attribution() -> None:
    """Two lamps changing at once make the step unattributable.

    This is the guard that keeps the delta rule honest: a 200 W movement on the
    row could belong to either lamp, so neither may claim it.
    """
    results = _by_device(
        _infer(
            _shared_topology(),
            {"ct_main": 500.0, "ct_lights": 200.0},
            {"lamp_a": "on", "lamp_b": "on"},
            history={"lamp_a": "off", "lamp_b": "off"},
            step=(0.0, 200.0),
        )
    )
    for result in results.values():
        assert result.power_w == pytest.approx(0.0)
        assert result.source == Source.UNKNOWN


def test_removing_the_declaration_retracts_the_attribution_row() -> None:
    """Deleting the configuration must not leave a phantom row behind."""
    topology = _shared_topology(devs=("lamp_a", "lamp_b"))
    emptied = project_configuration(topology, [])
    for device in emptied.devices:
        assert not device.metadata.get(PROJECTED_CTS_KEY)
        assert "ct_lights" not in device.branch_ct_ids


# --------------------------------------------------------------------------- #
# A declared wattage bounds what a shared row may credit
# --------------------------------------------------------------------------- #


def _shared_topology_with_watts(
    watts: dict[str, float],
    *,
    on: str | None = None,
) -> Topology:
    """Shared circuit whose lamps state a full-on wattage."""
    topology = _shared_topology(devs=tuple(watts))
    return project_configuration(
        topology,
        [
            DeviceConfiguration(
                device_id=device_id,
                monitoring=MonitoringLevel.SHARED,
                connectivity=HaConnectivity.CONNECTED,
                monitored_by_ct_ids=["ct_lights"],
                state_entity_ids=[f"light.{device_id}"],
                expected=ExpectedPower(on_w=float(on_w)),
            )
            for device_id, on_w in watts.items()
        ],
    )


def test_a_shared_row_cannot_credit_more_than_the_declared_wattage() -> None:
    """The most damaging error: a whole circuit reported as one lamp.

    Measured on the reference installation, ``lights_ct2`` peaked at 1 036 W
    while its three fixtures declared 243.6 / 112.3 / 112.7 W.  With one fixture
    ON and its siblings positively OFF the solver handed that one lamp the
    entire row -- 4.25x what the user said it draws -- and graded it
    ``tier_3_measured``.  The surplus is load nobody declared, and nothing in
    the row says it belongs to the lamp that happens to be on.

    A declared wattage is user evidence, so it bounds the attribution instead of
    being overridden by the measurement.
    """
    topology = _shared_topology_with_watts({"lamp_a": 150.0, "lamp_b": 100.0, "lamp_c": 100.0})
    batch = _infer(
        topology,
        {"ct_main": 1036.0, "ct_lights": 1036.0},
        {"lamp_a": "on", "lamp_b": "off", "lamp_c": "off"},
    )
    results = _by_device(batch)
    assert results["lamp_a"].power_w <= 150.0 * 1.25 + 1e-6, (
        "a 1 036 W row was credited to a lamp declared at 150 W"
    )
    assert "declared_ceiling_applied" in batch.diagnostics


def test_a_shared_row_within_the_declared_wattage_is_still_measured() -> None:
    """The safeguard must not destroy a legitimate single-claimant measurement."""
    topology = _shared_topology_with_watts({"lamp_a": 150.0, "lamp_b": 100.0})
    batch = _infer(
        topology,
        {"ct_main": 150.0, "ct_lights": 150.0},
        {"lamp_a": "on", "lamp_b": "off"},
    )
    results = _by_device(batch)
    assert results["lamp_a"].power_w == pytest.approx(150.0, abs=1.0)
    assert results["lamp_a"].source == Source.INFERRED


def test_a_declared_ceiling_is_never_relaxed() -> None:
    """A stale *model* bound may be widened; a stated wattage may not.

    The relaxation pass exists because a learnt model can be out of date.  A
    declared wattage is not a model: if the row reads more than the fixture
    draws, the difference is undeclared load on the circuit, not a wrong
    declaration.
    """
    from wattmosaic_engine.contracts import DeviceModelParameters, ModelStatus, ModelType
    from wattmosaic_engine.electrical.models import ModelView

    topology = _shared_topology_with_watts({"lamp_a": 100.0, "lamp_b": 100.0})
    view = ModelView(
        device_id="lamp_a",
        model_id="m_a",
        model_type=ModelType.BINARY,
        version=1,
        status=ModelStatus.ACTIVE,
        params=DeviceModelParameters(on_power_w=100.0),
        upper_bound_w=90.0,
        estimated_power_w=100.0,
    )
    batch = DisaggregationEngine().infer(
        _window({"ct_main": 500.0, "ct_lights": 500.0}),
        device_states={"lamp_a": "on", "lamp_b": "off"},
        models={"lamp_a": view},
        topology=topology,
    )
    results = _by_device(batch)
    assert results["lamp_a"].power_w <= 100.0 * 1.25 + 1e-6
    assert "lamp_a" not in (batch.diagnostics.get("relaxed_bounds") or [])


def test_a_capped_value_is_never_graded_as_measured() -> None:
    """A value pinned at the declared wattage is the declaration, not a reading.

    This is the trap that made the earlier safeguard only half-honest.  The
    solver clamps the lamp at its declared ceiling and the numbers then agree
    with the user's own statement, so it is tempting to grade the result
    ``tier_3_measured``.  Nothing measured it: the CT row read *more* than the
    declaration allows, which is exactly why the cap was applied.  The surplus is
    undeclared load, and the device's value is a configured prior.
    """
    topology = _shared_topology_with_watts({"lamp_a": 150.0, "lamp_b": 100.0, "lamp_c": 100.0})
    batch = _infer(
        topology,
        {"ct_main": 1036.0, "ct_lights": 1036.0},
        {"lamp_a": "on", "lamp_b": "off", "lamp_c": "off"},
    )
    result = _by_device(batch)["lamp_a"]
    assert result.source == Source.CONFIGURED_PRIOR
    assert result.confidence_tier == ConfidenceTier.TIER_1_MODELLED
    assert "declared_wattage_bounds_row_attribution" in result.explanation
    assert "constrained_nnls_fit" not in result.explanation


def test_a_value_at_the_ceiling_with_no_surplus_is_still_measured() -> None:
    """Sitting at the declared wattage is only suspicious if power is left over.

    A 150 W lamp on a row reading 165 W reaches its upper bound and the row is
    fully explained.  That is the CT measuring the lamp at its declared draw, and
    demoting it would hide a real measurement.
    """
    topology = _shared_topology_with_watts({"lamp_a": 150.0, "lamp_b": 100.0})
    batch = _infer(
        topology,
        {"ct_main": 165.0, "ct_lights": 165.0},
        {"lamp_a": "on", "lamp_b": "off"},
    )
    result = _by_device(batch)["lamp_a"]
    assert result.source == Source.INFERRED
    assert result.confidence_tier == ConfidenceTier.TIER_3_MEASURED
    assert "declared_ceiling_hit" not in batch.diagnostics


# --------------------------------------------------------------------------- #
# Declared priors must still fit the row they sit on
# --------------------------------------------------------------------------- #


def test_configured_priors_cannot_exceed_the_row_they_share() -> None:
    """Declarations are bounded by the circuit, not only by themselves.

    ADR-0025 caps each claimant at its own declared wattage, and ADR-0026 bounds a
    measured step by the row.  Neither bound applied at Level 3b, so three
    fixtures declaring 60 + 120 + 240 W reported 420 W on a 58 W circuit -- and
    because a ``configured_prior`` is subtracted from the residual as if it had
    been measured, that surplus reduced the unknown load instead of staying in it.

    The measured case came from the reference install: 420 W of declarations on a
    58.35 W circuit, and two 600 W declarations that drove the residual to
    -296.69 W.  A negative residual is physically impossible.
    """
    topology = _shared_topology_with_watts(
        {"lamp_a": 60.0, "lamp_b": 120.0, "lamp_c": 240.0}
    )
    batch = _infer(
        topology,
        {"ct_main": 58.0, "ct_lights": 58.0},
        {"lamp_a": "on", "lamp_b": "on", "lamp_c": "on"},
    )
    results = _by_device(batch)
    reported = sum(results[d].power_w for d in ("lamp_a", "lamp_b", "lamp_c"))
    assert reported <= 58.0 + 1e-6, (
        f"declarations totalling 420 W reported {reported:.1f} W on a 58 W circuit"
    )
    assert batch.reconciliation.unknown_residual_w >= -1e-6, (
        "priors that do not fit the row must not push the residual negative"
    )


def test_a_single_oversized_prior_cannot_exceed_its_row() -> None:
    """One fixture declaring 600 W on a 58 W circuit is bounded by the solve.

    With a single claimant the row is identifiable, so the value comes from the
    constrained fit and the measurement bounds it.  This is the case the engine
    already gets right; it is pinned here so a future change to Level 3b cannot
    silently move the single-claimant case onto the unbounded prior path.
    """
    topology = _shared_topology_with_watts({"lamp_a": 600.0, "lamp_b": 600.0})
    batch = _infer(
        topology,
        {"ct_main": 58.0, "ct_lights": 58.0},
        {"lamp_a": "on", "lamp_b": "on"},
    )
    reported = sum(_by_device(batch)[d].power_w for d in ("lamp_a", "lamp_b"))
    assert reported <= 58.0 + 1e-6


def test_an_over_declared_row_is_shared_in_proportion_to_the_declarations() -> None:
    """The clip follows the declaration ratio, never an even split.

    A 30 W claimant and a 10 W claimant on a 20 W row cannot both be right, but
    their *relative* claims are the only evidence available.  Splitting evenly
    would credit the 10 W fixture double what it said it draws.
    """
    topology = _shared_topology_with_watts({"lamp_a": 30.0, "lamp_b": 10.0})
    batch = _infer(
        topology,
        {"ct_main": 20.0, "ct_lights": 20.0},
        {"lamp_a": "on", "lamp_b": "on"},
    )
    results = _by_device(batch)
    # 20 W row across a 3:1 declaration ratio.
    assert results["lamp_a"].power_w == pytest.approx(15.0)
    assert results["lamp_b"].power_w == pytest.approx(5.0)
    assert results["lamp_a"].power_w + results["lamp_b"].power_w == pytest.approx(20.0)


def test_a_prior_is_not_clipped_below_zero_by_a_reverse_flow_row() -> None:
    """Reverse flow on a device row is not capacity to spend.

    A negative reading means the conductor is exporting, which says nothing
    favourable about the declared draw.  Clipping to zero would invent an "off"
    the state contradicts, so the row is skipped entirely and the declaration
    stands.
    """
    topology = _shared_topology_with_watts({"lamp_a": 60.0, "lamp_b": 120.0})
    batch = _infer(
        topology,
        {"ct_main": 500.0, "ct_lights": -25.0},
        {"lamp_a": "on", "lamp_b": "on"},
    )
    results = _by_device(batch)
    assert "declared_priors_bounded_by_row" not in batch.diagnostics
    assert results["lamp_a"].power_w == pytest.approx(60.0)
    assert results["lamp_b"].power_w == pytest.approx(120.0)


def test_a_prior_within_the_row_is_still_reported() -> None:
    """The guard must not discard an honest declaration that does fit.

    A row carrying 58 W from fixtures declaring 20 + 20 W is explained by them and
    all of it should be reported.  The bound is a ceiling, not a market.
    """
    topology = _shared_topology_with_watts({"lamp_a": 20.0, "lamp_b": 20.0})
    batch = _infer(
        topology,
        {"ct_main": 58.0, "ct_lights": 58.0},
        {"lamp_a": "on", "lamp_b": "on"},
    )
    results = _by_device(batch)
    assert results["lamp_a"].power_w == pytest.approx(20.0, abs=1e-6)
    assert results["lamp_b"].power_w == pytest.approx(20.0, abs=1e-6)


def test_a_prior_spends_only_what_the_row_has_left() -> None:
    """A measurement on another row does not license a bigger prior here.

    The branch row reads 200 W; a device with its own dedicated CT sits on a
    different row, so the priors get the whole 200 W and the residual closes to
    zero when the mains agree with the panel feeds.  Bounding priors by the
    enclosing main (500 W) instead of their own branch would have allowed 500 W
    of declarations here, which is the over-attribution this guard prevents.
    """
    topology = Topology(
        status=TopologyStatus.CONFIRMED,
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="ct_a",
                entity_id="sensor.a",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                device_id="a",
                parent_ct_id="main",
            ),
            CTChannel(
                ct_id="ct_bc",
                entity_id="sensor.bc",
                ct_type=CTType.BRANCH,
                phase=Phase.L2,
                parent_ct_id="main",
            ),
        ],
        devices=[
            DeviceTopology(device_id="a", phase=Phase.L1, dedicated_ct_ids=["ct_a"]),
            DeviceTopology(device_id="b", phase=Phase.L2, branch_ct_ids=["ct_bc"]),
            DeviceTopology(device_id="c", phase=Phase.L2, branch_ct_ids=["ct_bc"]),
        ],
    )
    topology = project_configuration(
        topology,
        [
            DeviceConfiguration(
                device_id=device_id,
                connectivity=HaConnectivity.CONNECTED,
                monitoring=MonitoringLevel.SHARED,
                state_entity_ids=[f"switch.{device_id}"],
                monitored_by_ct_ids=["ct_bc"],
                expected=ExpectedPower(on_w=500.0),
            )
            for device_id in ("b", "c")
        ],
    )
    batch = _infer(
        topology,
        {"main": 500.0, "ct_a": 300.0, "ct_bc": 200.0},
        {"b": "on", "c": "on"},
    )
    results = _by_device(batch)
    assert results["a"].source == Source.DIRECT_MEASURED
    # Two 500 W declarations on a 200 W row share it evenly, not the 500 W main.
    assert results["b"].power_w == pytest.approx(100.0)
    assert results["c"].power_w == pytest.approx(100.0)
    assert batch.reconciliation.unknown_residual_w == pytest.approx(0.0, abs=1e-6)
