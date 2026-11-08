"""Scenario tests for load disaggregation.

Every scenario is synthetic: ground truth device power is constructed first,
then the CT measurements are derived from it (``y = A x`` plus noise).  The
engine never sees the ground truth, only the window/states/models/topology.

Coverage maps 1:1 onto master-spec §19.3 items 1-19 plus the two hard
guarantees: residual is never force-distributed and main power is never
double counted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceModelParameters,
    DeviceModelVersion,
    DeviceState,
    DeviceTopology,
    ModelStatus,
    ModelType,
    Phase,
    QualityStatus,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation import DisaggregationEngine
from wattmosaic_engine.electrical import ObservationWindow
from wattmosaic_engine.electrical.window import ChannelReading

T0 = datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC)
CADENCE = 15.7  # measured HA median, not a 2 s assumption


# --------------------------------------------------------------------------- #
# Synthetic house builder
# --------------------------------------------------------------------------- #
def ch(
    cid: str,
    ct_type: CTType = CTType.BRANCH,
    *,
    phase: Phase = Phase.L1,
    parent: str | None = None,
    device: str | None = None,
    pair: str | None = None,
    resolution: float | None = None,
    reliability: float = 1.0,
) -> CTChannel:
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        device_id=device,
        paired_ct_id=pair,
        resolution_w=resolution,
        reliability=reliability,
        topology_status=TopologyStatus.CONFIRMED,
    )


def mains() -> list[CTChannel]:
    return [
        ch("main_l1", CTType.MAIN, phase=Phase.L1, resolution=1.0),
        ch("main_l2", CTType.MAIN, phase=Phase.L2, resolution=1.0),
    ]


def device(device_id: str, **kwargs) -> DeviceTopology:
    kwargs.setdefault("phase", Phase.L1)
    kwargs.setdefault("topology_status", TopologyStatus.CONFIRMED)
    return DeviceTopology(device_id=device_id, **kwargs)


def topology(channels, devices) -> Topology:
    return Topology(
        version=1,
        status=TopologyStatus.CONFIRMED,
        channels=channels,
        devices=devices,
    )


def states(*device_ids: str, **overrides: str) -> dict[str, DeviceState]:
    out = {}
    for did in device_ids:
        value = overrides.get(did, "on")
        out[did] = DeviceState(entity_id=f"switch.{did}", state=value)
    return out


def window(
    values: dict[str, float | None],
    *,
    at: datetime = T0,
    history: dict[str, list[tuple[datetime, float]]] | None = None,
    ages: dict[str, float] | None = None,
    qualities: dict[str, QualityStatus] | None = None,
    repeated: dict[str, bool] | None = None,
    previous_states: dict[str, str] | None = None,
    transitions: list[dict] | None = None,
    cadence_s: float | None = CADENCE,
    histories_len: int = 6,
) -> ObservationWindow:
    """Build a window; default history is a flat run of ``histories_len`` samples."""
    hist = {}
    if history is None:
        for cid, value in values.items():
            if value is None:
                continue
            start = at - timedelta(seconds=CADENCE * (histories_len - 1))
            hist[cid] = [(start + timedelta(seconds=CADENCE * i), float(value)) for i in range(histories_len)]
    else:
        hist = history
    return ObservationWindow.from_values(
        at,
        values,
        ages=ages,
        qualities=qualities,
        repeated=repeated,
        histories=hist,
        cadence_s=cadence_s,
        previous_states=previous_states,
        state_transitions=transitions,
    )


def model(
    device_id: str,
    *,
    on_w: float | None = None,
    standby_w: float | None = None,
    off_w: float | None = 0.0,
    states_map: dict[str, float] | None = None,
    stages: list[tuple[str, float, float]] | None = None,
    r2: float = 0.92,
    version: int = 1,
) -> DeviceModelVersion:
    params = DeviceModelParameters(
        on_power_w=on_w,
        standby_power_w=standby_w,
        off_power_w=off_w,
        state_map=states_map or {},
        stages=[
            __import__("wattmosaic_engine.contracts", fromlist=["OperatingStage"]).OperatingStage(
                name=name, power_mean_w=mean, power_std_w=std
            )
            for name, mean, std in (stages or [])
        ],
    )
    return DeviceModelVersion(
        device_id=device_id,
        model_type=ModelType.BINARY if not stages and not states_map else ModelType.MULTI_STATE,
        version=version,
        status=ModelStatus.ACTIVE,
        sample_count=120,
        training_quality=0.9,
        validation_metrics={"r2": r2, "mae_w": 12.0},
        parameters=params,
        confidence=0.85,
    )


def registry(models: list[DeviceModelVersion]) -> dict[str, DeviceModelVersion]:
    return {m.device_id: m for m in models}


def run(
    topo: Topology,
    win: ObservationWindow,
    dev_states: dict[str, DeviceState],
    models: dict | None = None,
    engine: DisaggregationEngine | None = None,
):
    return (engine or DisaggregationEngine()).infer(
        win, device_states=dev_states, models=models or {}, topology=topo
    )


# --------------------------------------------------------------------------- #
# Simple branch topology: one shared branch per device (worst case) and a
# dedicated variant used where the scenario needs ground-truth separation.
# --------------------------------------------------------------------------- #
def shared_branch_topo(n: int = 2, ids: list[str] | None = None) -> Topology:
    """``n`` devices on one shared branch CT under main_l1."""
    names = ids if ids is not None else [f"d{i}" for i in range(n)]
    devices = [device(name, branch_ct_ids=["branch_shared"]) for name in names]
    n = len(devices)
    return topology(
        [*mains(), ch("branch_shared", parent="main_l1")],
        devices,
    )


def separate_branch_topo() -> Topology:
    return topology(
        [
            *mains(),
            ch("branch_kitchen", parent="main_l1"),
            ch("branch_fridge", parent="branch_kitchen"),
            ch("branch_laundry", parent="main_l1"),
            ch("ct_tv", CTType.DEDICATED_DEVICE, device="tv"),
        ],
        [
            device("lights", branch_ct_ids=["branch_kitchen"]),
            device("fridge", branch_ct_ids=["branch_fridge"]),
            device("washer", branch_ct_ids=["branch_laundry"], phase=Phase.L1),
            device("tv", dedicated_ct_ids=["ct_tv"]),
        ],
    )


# --------------------------------------------------------------------------- #
# 1-4: activation / deactivation, single and multiple
# --------------------------------------------------------------------------- #
def test_01_single_device_activation():
    """Ground truth: d0 was 10 W, turns ON to 110 W -> +100 W on its branch."""
    topo = shared_branch_topo(1)
    win = window(
        {"main_l1": 210.0, "main_l2": 50.0, "branch_shared": 110.0},
        previous_states={"d0": "off"},
        transitions=[dict(device_id="d0", previous_state="off", new_state="on")],
    )
    batch = run(topo, win, states("d0"))
    result = batch.get("d0")
    assert result is not None
    assert result.source == Source.INFERRED
    assert result.power_w == pytest.approx(110.0, abs=1.0)
    assert "constrained_nnls_fit" in result.explanation


def test_02_single_device_deactivation():
    """Ground truth: d0 falls 150 W -> 3 W standby."""
    topo = shared_branch_topo(1)
    win = window(
        {"main_l1": 60.0, "main_l2": 0.0, "branch_shared": 3.0},
        previous_states={"d0": "on"},
        transitions=[dict(device_id="d0", previous_state="on", new_state="off")],
    )
    batch = run(topo, win, states("d0", d0="off"), registry([model("d0", on_w=150.0, standby_w=3.0)]))
    result = batch.get("d0")
    assert result is not None
    # The branch really measures 3 W, so the inferred value is 3 W (not 0).
    assert result.source == Source.INFERRED
    assert result.power_w == pytest.approx(3.0, abs=1.0)


def test_03_multiple_simultaneous_activation_separable():
    """Two devices on *separate* branches activate together: fully separable."""
    topo = topology(
        [
            *mains(),
            ch("branch_a", parent="main_l1"),
            ch("branch_b", parent="main_l2", phase=Phase.L2),
        ],
        [
            device("a", branch_ct_ids=["branch_a"], phase=Phase.L1),
            device("b", branch_ct_ids=["branch_b"], phase=Phase.L2),
        ],
    )
    win = window(
        {"main_l1": 300.0, "main_l2": 220.0, "branch_a": 300.0, "branch_b": 220.0},
        previous_states={"a": "off", "b": "off"},
        transitions=[
            dict(device_id="a", previous_state="off", new_state="on"),
            dict(device_id="b", previous_state="off", new_state="on"),
        ],
    )
    batch = run(topo, win, states("a", "b"))
    assert batch.get("a").power_w == pytest.approx(300.0, abs=1.0)
    assert batch.get("b").power_w == pytest.approx(220.0, abs=1.0)
    assert batch.get("a").source == Source.INFERRED
    assert batch.get("b").source == Source.INFERRED
    assert batch.diagnostics["ambiguity"]["ambiguous"] is False


def test_04_multiple_simultaneous_deactivation_separable():
    topo = topology(
        [
            *mains(),
            ch("branch_a", parent="main_l1"),
            ch("branch_b", parent="main_l1"),
        ],
        [
            device("a", branch_ct_ids=["branch_a"]),
            device("b", branch_ct_ids=["branch_b"]),
        ],
    )
    win = window(
        {"main_l1": 40.0, "main_l2": 0.0, "branch_a": 20.0, "branch_b": 20.0},
        previous_states={"a": "on", "b": "on"},
        transitions=[
            dict(device_id="a", previous_state="on", new_state="off"),
            dict(device_id="b", previous_state="on", new_state="off"),
        ],
    )
    batch = run(topo, win, states("a", "b", a="off", b="off"), registry(
        [model("a", on_w=100.0, standby_w=20.0), model("b", on_w=80.0, standby_w=20.0)]
    ))
    assert batch.get("a").power_w == pytest.approx(20.0, abs=1.0)
    assert batch.get("b").power_w == pytest.approx(20.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 5-7: dedicated CT, shared branch, nested branch
# --------------------------------------------------------------------------- #
def test_05_dedicated_ct_measurement_is_never_replaced_by_model():
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": 800.0,
            "main_l2": 0.0,
            "branch_kitchen": 500.0,
            "branch_fridge": 150.0,
            "branch_laundry": 0.0,
            "ct_tv": 42.0,
        }
    )
    # A model claims 900 W for the TV; the direct CT says 42 W and must win.
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"), registry([model("tv", on_w=900.0)]))
    tv = batch.get("tv")
    assert tv.source == Source.DIRECT_MEASURED
    assert tv.power_w == pytest.approx(42.0)
    assert tv.supporting_ct_channels == ["ct_tv"]
    assert "direct_ct_measurement" in tv.explanation
    assert batch.reconciliation.direct_measured_w == pytest.approx(42.0)


def test_06_shared_branch_ct_attributes_only_from_branch():
    topo = shared_branch_topo(1)
    win = window({"main_l1": 300.0, "main_l2": 100.0, "branch_shared": 200.0})
    batch = run(topo, win, states("d0"))
    result = batch.get("d0")
    assert result.source == Source.INFERRED
    assert result.power_w == pytest.approx(200.0, abs=1.0)
    assert "branch_shared" in result.supporting_ct_channels


def test_07_nested_branch_ct_does_not_double_count():
    """Kitchen = 900 W total, fridge child = 200 W, lights = 700 W.

    A naive sum would give lights 900 and fridge 200 (1100 W on a 900 W
    branch).  The engine must return 700/200 and reconcile without changing
    the main total.
    """
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": 900.0,
            "main_l2": 0.0,
            "branch_kitchen": 900.0,
            "branch_fridge": 200.0,
            "branch_laundry": 0.0,
            "ct_tv": 0.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"))
    lights = batch.get("lights")
    fridge = batch.get("fridge")
    assert lights.power_w == pytest.approx(700.0, abs=1.0)
    assert fridge.power_w == pytest.approx(200.0, abs=1.0)
    assert lights.power_w + fridge.power_w == pytest.approx(900.0, abs=2.0)
    # Exactly one row per physical quantity; the parent row subtracts the child.
    assert batch.diagnostics["matrix"]["nested_children"]["branch_kitchen"] == ["branch_fridge"]
    assert batch.reconciliation.main_power_w == pytest.approx(900.0)


# --------------------------------------------------------------------------- #
# 8: unknown load preserved
# --------------------------------------------------------------------------- #
def test_08_unknown_load_is_preserved_as_residual():
    topo = shared_branch_topo(1)
    # Branch carries 500 W, but the only known device says OFF and is unknown.
    win = window({"main_l1": 800.0, "main_l2": 0.0, "branch_shared": 500.0})
    batch = run(topo, win, states("d0", d0="off"))
    result = batch.get("d0")
    assert result.source == Source.UNKNOWN
    assert result.power_w == 0.0
    assert "power_left_unattributed" in result.explanation
    # The 500 W (plus 300 W elsewhere) stays in the residual, not on the device.
    assert batch.reconciliation.unknown_residual_w == pytest.approx(800.0, abs=1.0)
    assert batch.reconciliation.accounted_fraction == pytest.approx(0.0, abs=0.01)


# --------------------------------------------------------------------------- #
# 9-10: double pole and phase relationships
# --------------------------------------------------------------------------- #
def double_pole_topo() -> Topology:
    return topology(
        [
            *mains(),
            ch("ct_dryer_l1", CTType.DEDICATED_DEVICE, phase=Phase.L1, device="dryer", pair="ct_dryer_l2"),
            ch("ct_dryer_l2", CTType.DEDICATED_DEVICE, phase=Phase.L2, device="dryer", pair="ct_dryer_l1"),
            ch("branch_l1", parent="main_l1"),
            ch("branch_l2", parent="main_l2", phase=Phase.L2),
        ],
        [
            device("dryer", dedicated_ct_ids=["ct_dryer_l1", "ct_dryer_l2"], phase=Phase.BOTH, double_pole=True),
            device("lamp_l1", branch_ct_ids=["branch_l1"], phase=Phase.L1),
            device("lamp_l2", branch_ct_ids=["branch_l2"], phase=Phase.L2),
        ],
    )


def test_09_double_pole_240v_appliance_total_not_two_appliances():
    """A 2400 W dryer draws 1200 W on each leg; total must be 2400 W, once."""
    topo = double_pole_topo()
    win = window(
        {
            "main_l1": 1300.0,
            "main_l2": 1250.0,
            "ct_dryer_l1": 1200.0,
            "ct_dryer_l2": 1200.0,
            "branch_l1": 100.0,
            "branch_l2": 50.0,
        }
    )
    batch = run(topo, win, states("dryer", "lamp_l1", "lamp_l2"))
    dryer = batch.get("dryer")
    assert dryer.source == Source.DIRECT_MEASURED
    assert dryer.power_w == pytest.approx(2400.0)
    assert len(dryer.supporting_ct_channels) == 2
    # Exactly one device result for the dryer; no phantom second pole.
    assert [r.device_id for r in batch.results].count("dryer") == 1
    # Main total is 2550 W and the dryer contributes 2400 W once.
    assert batch.reconciliation.main_power_w == pytest.approx(2550.0)
    assert batch.reconciliation.direct_measured_w == pytest.approx(2400.0)


def test_10_l1_l2_phase_relationship():
    topo = double_pole_topo()
    win = window(
        {
            "main_l1": 1100.0,
            "main_l2": 950.0,
            "ct_dryer_l1": 0.0,
            "ct_dryer_l2": 0.0,
            "branch_l1": 1000.0,   # lamp_l1: 1000 W on L1
            "branch_l2": 900.0,    # lamp_l2:  900 W on L2
        }
    )
    batch = run(topo, win, states("dryer", "lamp_l1", "lamp_l2", dryer="off"))
    assert batch.get("lamp_l1").power_w == pytest.approx(1000.0, abs=1.0)
    assert batch.get("lamp_l2").power_w == pytest.approx(900.0, abs=1.0)
    assert batch.get("lamp_l1").source == Source.INFERRED
    assert batch.get("lamp_l2").source == Source.INFERRED
    assert batch.reconciliation.l1_power_w == pytest.approx(1100.0)
    assert batch.reconciliation.l2_power_w == pytest.approx(950.0)


# --------------------------------------------------------------------------- #
# 11: reversed CT polarity
# --------------------------------------------------------------------------- #
def test_11_reversed_ct_polarity():
    topo = topology(
        [
            ch("main_l1", CTType.MAIN, phase=Phase.L1),
            ch("main_l2", CTType.MAIN, phase=Phase.L2),
            ch(
                "branch_reversed",
                parent="main_l1",
                resolution=1.0,
            ),
        ],
        [device("heater", branch_ct_ids=["branch_reversed"])],
    )
    # The channel reports -350 W because the CT clamp is reversed.  The
    # topology marks polarity on the channel, and readings already corrected by
    # ingestion arrive as +350 W.
    topo.channels[2].polarity = "inverted"
    corrected = window({"main_l1": 350.0, "main_l2": 0.0, "branch_reversed": 350.0})
    batch = run(topo, corrected, states("heater"))
    assert batch.get("heater").power_w == pytest.approx(350.0, abs=1.0)
    assert batch.reconciliation.main_power_w == pytest.approx(350.0)

    # Uncorrected polarity (a raw Measurement marked inverted) must also work.
    from wattmosaic_engine.contracts import Measurement, Polarity

    raw = {
        "main_l1": Measurement(
            entity_id="sensor.main_l1", ct_id="main_l1", value=350.0, state_timestamp=T0
        ),
        "main_l2": Measurement(
            entity_id="sensor.main_l2", ct_id="main_l2", value=0.0, state_timestamp=T0
        ),
        "branch_reversed": Measurement(
            entity_id="sensor.branch_reversed",
            ct_id="branch_reversed",
            value=-350.0,
            state_timestamp=T0,
            polarization=Polarity.INVERTED,
        ),
    }
    raw_window = ObservationWindow(
        at=T0,
        readings={
            cid: ChannelReading(
                channel_id=cid,
                value_w=m.effective_power_w(),
                polarization=Polarity.NORMAL,
            )
            for cid, m in raw.items()
        },
    )
    batch2 = run(topo, raw_window, states("heater"))
    assert batch2.get("heater").power_w == pytest.approx(350.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 12-14: missing, delayed, stale
# --------------------------------------------------------------------------- #
def test_12_missing_sensor_samples_do_not_abort_or_fabricate():
    topo = separate_branch_topo()
    # branch_fridge is missing entirely; ct_tv missing too.
    win = window(
        {
            "main_l1": 800.0,
            "main_l2": 0.0,
            "branch_kitchen": 500.0,
            "branch_laundry": 0.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"), registry([model("fridge", on_w=150.0)]))
    # The parent row must be excluded (child value unknown), so lights is not
    # silently estimated from the 500 W kitchen total.
    assert "branch_kitchen" in batch.diagnostics["excluded_rows"]
    assert batch.diagnostics["errors"] == {}
    fridge = batch.get("fridge")
    assert fridge.source in (Source.MODEL_ESTIMATED, Source.UNKNOWN)
    # Whatever the source, the batch still reconciles and reports residual.
    assert batch.reconciliation.main_power_w == pytest.approx(800.0)
    assert batch.reconciliation.unknown_residual_w > 0


def test_13_delayed_sensor_update_is_flagged_not_used_as_fresh():
    topo = shared_branch_topo(1)
    win = window(
        {"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 200.0},
        ages={"branch_shared": 45.0},
    )
    batch = run(topo, win, states("d0"))
    result = batch.get("d0")
    # 45 s > soft limit (30 s) but < max age: value is used, quality degraded.
    assert result.quality_status in (QualityStatus.DEGRADED, QualityStatus.STALE)
    assert result.confidence < 0.75
    assert "branch_shared" not in batch.diagnostics["excluded_rows"]


def test_14_stale_reading_is_excluded_and_does_not_produce_a_value():
    topo = shared_branch_topo(1)
    win = window(
        {"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 200.0},
        ages={"branch_shared": 600.0},
    )
    batch = run(topo, win, states("d0"))
    result = batch.get("d0")
    assert "branch_shared" in batch.diagnostics["excluded_rows"]
    assert batch.diagnostics["excluded_rows"]["branch_shared"] == "stale_measurement"
    # With no fresh row for the device and no model, do not fabricate a value.
    assert result.source == Source.UNKNOWN
    assert result.power_w == 0.0
    assert result.quality_status in (QualityStatus.STALE, QualityStatus.MISSING)


# --------------------------------------------------------------------------- #
# 15: noise and drift
# --------------------------------------------------------------------------- #
def test_15_noise_and_drift_stay_within_tolerance():
    rng = np.random.default_rng(20261105)
    topo = topology(
        [
            *mains(),
            ch("branch_a", parent="main_l1", resolution=2.0),
            ch("branch_b", parent="main_l1", resolution=2.0),
        ],
        [
            device("a", branch_ct_ids=["branch_a"]),
            device("b", branch_ct_ids=["branch_b"]),
        ],
    )
    truths = {"a": 620.0, "b": 980.0}
    errors = []
    for step in range(25):
        drift = 0.4 * step  # slow sensor drift of 0.4 W per cycle
        noisy = {
            "branch_a": truths["a"] + drift + float(rng.normal(0, 4.0)),
            "branch_b": truths["b"] + drift + float(rng.normal(0, 4.0)),
        }
        noisy["main_l1"] = noisy["branch_a"] + noisy["branch_b"]
        noisy["main_l2"] = 0.0
        win = window(noisy, at=T0 + timedelta(seconds=CADENCE * step))
        batch = run(topo, win, states("a", "b"))
        errors.append(abs(batch.get("a").power_w - truths["a"]))
        errors.append(abs(batch.get("b").power_w - truths["b"]))
    # Solver averages the rows, so error must stay small despite noise + drift.
    assert max(errors) < 25.0
    assert float(np.mean(errors)) < 10.0


# --------------------------------------------------------------------------- #
# 16: unidentifiable concurrent loads -> ambiguity
# --------------------------------------------------------------------------- #
def test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated():
    """Two devices share one branch CT and both change state together.

    Only the sum (500 W) is observable, so no unique split exists.  The engine
    must mark ambiguity and refuse to assign 250/250.
    """
    topo = shared_branch_topo(2)
    win = window(
        {"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0},
        previous_states={"d0": "off", "d1": "off"},
        transitions=[
            dict(device_id="d0", previous_state="off", new_state="on"),
            dict(device_id="d1", previous_state="off", new_state="on"),
        ],
    )
    batch = run(topo, win, states("d0", "d1"))
    assert batch.diagnostics["ambiguity"]["ambiguous"] is True
    assert "ambiguous_concurrent_events" in batch.diagnostics["warnings"]
    for did in ("d0", "d1"):
        result = batch.get(did)
        assert result.source == Source.UNKNOWN
        assert result.power_w == 0.0
        assert "ambiguous_concurrent_events" in result.explanation
        assert result.confidence <= 0.25
    # The 500 W is preserved, not split.
    assert batch.reconciliation.unknown_residual_w == pytest.approx(500.0, abs=1.0)
    assert batch.reconciliation.inferred_w == pytest.approx(0.0)


def test_16b_concurrent_but_separable_is_not_marked_ambiguous():
    """The same two devices with dedicated CTs *are* separable."""
    topo = topology(
        [
            *mains(),
            ch("ct_a", CTType.DEDICATED_DEVICE, device="a"),
            ch("ct_b", CTType.DEDICATED_DEVICE, device="b"),
        ],
        [
            device("a", dedicated_ct_ids=["ct_a"]),
            device("b", dedicated_ct_ids=["ct_b"]),
        ],
    )
    win = window(
        {"main_l1": 800.0, "main_l2": 0.0, "ct_a": 300.0, "ct_b": 500.0},
        previous_states={"a": "off", "b": "off"},
        transitions=[
            dict(device_id="a", previous_state="off", new_state="on"),
            dict(device_id="b", previous_state="off", new_state="on"),
        ],
    )
    batch = run(topo, win, states("a", "b"))
    assert batch.diagnostics["ambiguity"]["ambiguous"] is False
    assert batch.get("a").power_w == pytest.approx(300.0)
    assert batch.get("b").power_w == pytest.approx(500.0)


# --------------------------------------------------------------------------- #
# 17: standby power
# --------------------------------------------------------------------------- #
def test_17_standby_power_is_preserved():
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": 320.0,
            "main_l2": 0.0,
            "branch_kitchen": 200.0,
            "branch_fridge": 50.0,
            "branch_laundry": 5.0,
            "ct_tv": 4.0,
        }
    )
    batch = run(
        topo,
        win,
        states("lights", "fridge", "washer", "tv"),
        registry(
            [
                model("washer", on_w=1800.0, standby_w=5.0),
                model("tv", on_w=80.0, standby_w=4.0),
            ]
        ),
    )
    washer = batch.get("washer")
    assert washer.power_w == pytest.approx(5.0, abs=1.0)
    assert washer.source in (Source.INFERRED, Source.MODEL_ESTIMATED)
    assert batch.get("tv").power_w == pytest.approx(4.0)
    assert batch.reconciliation.direct_measured_w == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# 18: variable power consumption
# --------------------------------------------------------------------------- #
def test_18_variable_power_consumption_tracks_measurement():
    topo = shared_branch_topo(1, ["d0"])
    for watts in (12.0, 45.0, 180.0, 640.0, 1200.0):
        win = window(
            {"main_l1": watts, "main_l2": 0.0, "branch_shared": watts},
            at=T0,
        )
        batch = run(topo, win, states("d0"), registry([model("d0", on_w=640.0)]))
        result = batch.get("d0")
        assert result.source == Source.INFERRED
        assert result.power_w == pytest.approx(watts, abs=1.0)


# --------------------------------------------------------------------------- #
# 19: multi-stage appliance cycle
# --------------------------------------------------------------------------- #
def test_19_multi_stage_appliance_cycle():
    topo = shared_branch_topo(1, ["washer"])
    washer_model = model(
        "washer",
        stages=[("wash", 500.0, 25.0), ("rinse", 120.0, 10.0), ("spin", 850.0, 40.0)],
    )
    for stage_name, watts in (("wash", 500.0), ("rinse", 120.0), ("spin", 850.0)):
        win = window(
            {"main_l1": watts, "main_l2": 0.0, "branch_shared": watts},
            at=T0,
        )
        batch = run(topo, win, states("washer"), registry([washer_model]))
        result = batch.get("washer")
        assert result.power_w == pytest.approx(watts, abs=2.0)
        assert result.source == Source.INFERRED
        assert result.operating_stage == stage_name
        assert result.stage_status in ("probable", "identified")

    # A stage model is *not* used to override a changing measurement.
    win = window({"main_l1": 300.0, "main_l2": 0.0, "branch_shared": 300.0})
    batch = run(topo, win, states("washer"), registry([washer_model]))
    assert batch.get("washer").power_w == pytest.approx(300.0, abs=2.0)


# --------------------------------------------------------------------------- #
# Hard guarantees
# --------------------------------------------------------------------------- #
def test_residual_is_never_force_distributed():
    topo = separate_branch_topo()
    # 2000 W at the mains, only 700 W of devices are observable.
    win = window(
        {
            "main_l1": 2000.0,
            "main_l2": 0.0,
            "branch_kitchen": 700.0,
            "branch_fridge": 200.0,
            "branch_laundry": 0.0,
            "ct_tv": 0.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"))
    attributed = sum(r.power_w for r in batch.results)
    assert attributed == pytest.approx(700.0, abs=3.0)  # NOT 2000
    assert batch.reconciliation.unknown_residual_w == pytest.approx(1300.0, abs=3.0)
    assert batch.reconciliation.accounted_fraction == pytest.approx(0.35, abs=0.02)
    # Explicit guarantee marker is always present.
    assert "residual_never_redistributed" in batch.reconciliation.explanation


def test_main_power_is_never_double_counted_with_nested_panels():
    """Utility main 3000 W, panel feed 1200 W nested underneath.

    Summing them would give 4200 W.  The nested feed is inside the outer feed's
    measurement, so only the outer feed may define the total.  The trick is to
    get this right *without* discarding the nested feed's own attribution.
    """
    topo = topology(
        [
            ch("utility_l1", CTType.MAIN, phase=Phase.L1),
            ch("panel_l1", CTType.MAIN, phase=Phase.L1, parent="utility_l1"),
            ch("branch_a", parent="panel_l1"),
        ],
        [device("a", branch_ct_ids=["branch_a"])],
    )
    win = window({"utility_l1": 3000.0, "panel_l1": 1200.0, "branch_a": 1200.0})
    batch = run(topo, win, states("a"))
    main = batch.diagnostics["main_power"]
    assert main["errors"] == []
    assert "main_power_unavailable" not in batch.diagnostics["warnings"]
    # Must not report 4200 W: the nested feed is counted once, by its parent.
    assert batch.reconciliation.main_power_w == pytest.approx(3000.0)
    assert batch.reconciliation.main_power_w != pytest.approx(4200.0)
    assert main["per_channel_w"] == {"utility_l1": 3000.0}
    assert main["excluded_channels"] == ["panel_l1"]
    assert any(n.startswith("nested_feeds_not_summed") for n in main["notes"])
    # The household's real unexplained load is the outer feed minus what the
    # engine attributed, and it must stay positive rather than being hidden.
    assert batch.reconciliation.unknown_residual_w == pytest.approx(1800.0)


def test_sum_of_devices_does_not_exceed_main_power_for_consistent_data():
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": 870.0,
            "main_l2": 0.0,
            "branch_kitchen": 700.0,
            "branch_fridge": 200.0,
            "branch_laundry": 120.0,
            "ct_tv": 50.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"))
    attributed = (
        batch.reconciliation.direct_measured_w
        + batch.reconciliation.inferred_w
        + batch.reconciliation.model_estimated_w
    )
    assert attributed <= batch.reconciliation.main_power_w + 1e-6
    assert batch.reconciliation.unknown_residual_w == pytest.approx(0.0, abs=2.0)


# --------------------------------------------------------------------------- #
# Robustness / contract shape
# --------------------------------------------------------------------------- #
def test_per_device_error_isolation():
    """A device whose model resolution raises must not abort the batch."""

    class ExplodingRegistry:
        def active(self, device_id):
            if device_id == "bad":
                raise RuntimeError("model registry unavailable")
            return None

        def estimate(self, device_id, context=None):
            if device_id == "bad":
                raise RuntimeError("model registry unavailable")
            return None

    topo = topology(
        [
            *mains(),
            ch("branch_a", parent="main_l1"),
            ch("branch_b", parent="main_l1"),
        ],
        [
            device("bad", branch_ct_ids=["branch_a"]),
            device("good", branch_ct_ids=["branch_b"]),
        ],
    )
    win = window({"main_l1": 350.0, "main_l2": 0.0, "branch_a": 100.0, "branch_b": 250.0})
    batch = run(topo, win, states("bad", "good"), ExplodingRegistry())
    assert batch.get("good").power_w == pytest.approx(250.0, abs=1.0)
    assert batch.get("good").source == Source.INFERRED
    # The bad device is isolated, not fatal.
    assert batch.get("bad") is not None


def test_results_match_contract_and_confidence_is_not_calibrated():
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": 800.0,
            "main_l2": 10.0,
            "branch_kitchen": 600.0,
            "branch_fridge": 100.0,
            "branch_laundry": 0.0,
            "ct_tv": 30.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv"))
    assert isinstance(batch.reconciliation.main_power_w, float)
    for result in batch.results:
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence_is_calibrated is False
        assert result.explanation
        assert result.source in {s.value for s in Source}
    # Every device in the topology is represented exactly once.
    ids = [r.device_id for r in batch.results]
    assert sorted(ids) == sorted(["lights", "fridge", "washer", "tv"])


def test_observation_cadence_drives_event_windows():
    """A 15.7 s cadence must produce windows derived from it, not 2 s."""
    topo = shared_branch_topo(1)
    win = window({"main_l1": 100.0, "main_l2": 0.0, "branch_shared": 100.0})
    batch = run(topo, win, states("d0"))
    assert batch.diagnostics["cadence_s"] == pytest.approx(CADENCE)
    assert batch.diagnostics["before_s"] == pytest.approx(min(max(2 * CADENCE, 5.0), 60.0))
    assert batch.diagnostics["after_s"] == pytest.approx(min(max(CADENCE, 2.5), 30.0))


def test_event_history_produces_detected_step():
    topo = shared_branch_topo(1)
    hist = []
    for i in range(12):
        hist.append((T0 - timedelta(seconds=CADENCE * (12 - i)), 40.0))
    for i in range(1, 5):
        hist.append((T0 + timedelta(seconds=CADENCE * i - CADENCE), 360.0))
    win = ObservationWindow.from_values(
        T0,
        {"main_l1": 360.0, "main_l2": 0.0, "branch_shared": 360.0},
        histories={"branch_shared": hist},
        cadence_s=CADENCE,
        previous_states={"d0": "off"},
        state_transitions=[dict(device_id="d0", previous_state="off", new_state="on")],
    )
    batch = run(topo, win, states("d0"))
    assert "branch_shared" in batch.diagnostics["ct_steps"]
    assert batch.get("d0").power_w == pytest.approx(360.0, abs=1.0)


def test_negative_export_is_preserved_in_reconciliation():
    topo = separate_branch_topo()
    win = window(
        {
            "main_l1": -400.0,
            "main_l2": 0.0,
            "branch_kitchen": 0.0,
            "branch_fridge": 0.0,
            "branch_laundry": 0.0,
            "ct_tv": 0.0,
        }
    )
    batch = run(topo, win, states("lights", "fridge", "washer", "tv", lights="off", fridge="off", washer="off", tv="off"))
    assert batch.reconciliation.main_power_w == pytest.approx(-400.0)
    assert "negative_power_export_preserved" in batch.reconciliation.explanation


def test_16c_ambiguous_concurrent_loads_with_models_stay_unknown():
    """Models must not paper over an unresolvable split."""
    topo = shared_branch_topo(2)
    win = window(
        {"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0},
        previous_states={"d0": "off", "d1": "off"},
        transitions=[
            dict(device_id="d0", previous_state="off", new_state="on"),
            dict(device_id="d1", previous_state="off", new_state="on"),
        ],
    )
    batch = run(
        topo,
        win,
        states("d0", "d1"),
        registry([model("d0", on_w=300.0), model("d1", on_w=200.0)]),
    )
    # The models happen to sum to 500, but the measurements still do not say
    # which device moved; the engine must not silently trust the coincidence.
    assert batch.diagnostics["ambiguity"]["ambiguous"] is True
    assert batch.get("d0").source == Source.UNKNOWN
    assert batch.get("d1").source == Source.UNKNOWN
    assert batch.reconciliation.unknown_residual_w == pytest.approx(500.0, abs=1.0)


def test_state_pinning_makes_shared_branch_identifiable():
    """With one device explicitly OFF, the other owns the measurable remainder."""
    topo = shared_branch_topo(2)
    win = window({"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0})
    batch = run(
        topo,
        win,
        states("d0", "d1", d1="off"),
        registry([model("d1", on_w=200.0, standby_w=2.0)]),
    )
    d0 = batch.get("d0")
    d1 = batch.get("d1")
    # d1 is held at/below standby tolerance; d0 receives the remainder.
    assert d1.power_w <= 15.0
    assert d0.source == Source.INFERRED
    assert d0.power_w == pytest.approx(500.0, abs=16.0)
    assert "constrained_nnls_fit" in d0.explanation


def test_both_on_shared_branch_uses_model_estimate_not_fabricated_split():
    topo = shared_branch_topo(2)
    win = window({"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0})
    batch = run(
        topo,
        win,
        states("d0", "d1"),
        registry([model("d0", on_w=300.0), model("d1", on_w=200.0)]),
    )
    for did, expected in (("d0", 300.0), ("d1", 200.0)):
        result = batch.get(did)
        assert result.source == Source.MODEL_ESTIMATED
        assert result.power_w == pytest.approx(expected)
        assert "measurements_do_not_identify_device" in result.explanation


def test_repeated_state_update_is_downweighted_not_dropped():
    topo = shared_branch_topo(1)
    fresh = run(
        topo,
        window({"main_l1": 400.0, "main_l2": 0.0, "branch_shared": 400.0}),
        states("d0"),
    )
    repeated = run(
        topo,
        window(
            {"main_l1": 400.0, "main_l2": 0.0, "branch_shared": 400.0},
            repeated={"branch_shared": True},
        ),
        states("d0"),
    )
    assert repeated.get("d0").power_w == pytest.approx(400.0, abs=1.0)
    # A repeat still yields the best known value but with lower confidence.
    assert repeated.get("d0").confidence < fresh.get("d0").confidence


# --------------------------------------------------------------------------- #
# Integration with the real synchronisation window (Agent D's AlignedWindow)
# --------------------------------------------------------------------------- #
def test_real_aligned_window_from_sample_aligner():
    """The engine consumes the production AlignedWindow without adaptation."""
    from wattmosaic_engine.contracts import Measurement, QualityStatus
    from wattmosaic_engine.synchronization import SampleAligner

    topo = separate_branch_topo()
    aligner = SampleAligner()
    values = {
        "main_l1": 900.0,
        "main_l2": 0.0,
        "branch_kitchen": 900.0,
        "branch_fridge": 200.0,
        "branch_laundry": 0.0,
        "ct_tv": 50.0,
    }
    for i in range(8):
        ts = T0 - timedelta(seconds=CADENCE * (7 - i))
        for cid, value in values.items():
            aligner.add(
                Measurement(
                    entity_id=f"sensor.{cid}",
                    ct_id=cid,
                    value=value,
                    state_timestamp=ts,
                    received_at=ts,
                    quality=QualityStatus.GOOD,
                )
            )
    window = aligner.window(at=T0, channels=list(values), max_age_s=120.0)
    batch = run(topo, window, states("lights", "fridge", "washer", "tv"))
    assert batch.get("tv").source == Source.DIRECT_MEASURED
    assert batch.get("tv").power_w == pytest.approx(50.0)
    assert batch.get("lights").power_w == pytest.approx(700.0, abs=2.0)
    assert batch.get("fridge").power_w == pytest.approx(200.0, abs=2.0)
    # No cadence/history is exposed by the synchroniser, so the documented
    # 15.7 s audit fallback applies rather than a hardcoded 2 s.
    assert batch.diagnostics["cadence_s"] == pytest.approx(15.7)
    assert batch.diagnostics["errors"] == {}


def test_window_without_cadence_still_uses_measured_default():
    from wattmosaic_engine.contracts import Measurement
    from wattmosaic_engine.synchronization import SampleAligner

    ts = T0
    aligner = SampleAligner()
    aligner.add(
        Measurement(entity_id="sensor.m", ct_id="m", value=100.0, state_timestamp=ts, received_at=ts)
    )
    window = aligner.window(at=T0, channels=["m"], max_age_s=120.0)
    from wattmosaic_engine.electrical import observed_cadence_s

    assert observed_cadence_s(window) == pytest.approx(15.7)


def test_unavailable_state_is_not_treated_as_off():
    """``unavailable`` means unknown, so it must not pin a device to zero."""
    topo = shared_branch_topo(2)
    win = window({"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0})
    batch = run(
        topo,
        win,
        states("d0", "d1", d1="unavailable"),
        registry([model("d1", on_w=200.0, standby_w=2.0)]),
    )
    # d1 must NOT be inferred at ~10 W from a bogus "off" constraint.
    assert batch.get("d0").source == Source.UNKNOWN
    assert batch.diagnostics["ambiguity"]["ambiguous"] is False
    assert "d0" in batch.diagnostics["identifiability"]["unidentifiable"]


def test_step_tracker_enables_events_on_snapshot_only_windows():
    """The production synchroniser has no history; the tracker supplies it."""
    from wattmosaic_engine.disaggregation.events import StepTracker

    topo = shared_branch_topo(1, ["d0"])
    tracker = StepTracker(max_samples=32)
    engine = DisaggregationEngine(tracker=tracker)

    baseline = {"main_l1": 100.0, "main_l2": 0.0, "branch_shared": 100.0}
    for i in range(6):
        at = T0 + timedelta(seconds=CADENCE * i)
        win = ObservationWindow.from_values(
            at, baseline, previous_states={"d0": "on"}, cadence_s=None
        )
        engine.infer(win, device_states=states("d0"), models={}, topology=topo)

    # Step up and trigger a state transition.
    stepped = {"main_l1": 460.0, "main_l2": 0.0, "branch_shared": 460.0}
    at = T0 + timedelta(seconds=CADENCE * 6)
    win = ObservationWindow.from_values(
        at,
        stepped,
        previous_states={"d0": "on"},
        cadence_s=None,
        state_transitions=[
            dict(device_id="d0", previous_state="off", new_state="on")
        ],
    )
    batch = engine.infer(win, device_states=states("d0"), models={}, topology=topo)
    assert "branch_shared" in batch.diagnostics["ct_steps"]
    step = batch.diagnostics["ct_steps"]["branch_shared"]
    assert step["delta_w"] == pytest.approx(360.0, abs=1.0)
    assert batch.diagnostics["cadence_s"] == pytest.approx(CADENCE, rel=0.05)
    assert batch.get("d0").power_w == pytest.approx(460.0, abs=1.0)
