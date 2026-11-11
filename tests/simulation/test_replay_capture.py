"""The replay tool itself, exercised without a real capture.

``tools/audit/replay_capture.py`` is the bridge between the simulator and a real
installation, and its whole value is that it goes through the **production**
path (``Measurement`` -> ``SampleAligner`` -> ``DisaggregationEngine``). If it
were only ever run against the operator's local database, it would be untested
in CI and could rot silently.

So this file replays a *simulated* household through the same code. That gives
the tool a ground truth: the simulator knows the true whole-home power, so a
correct replay must reproduce it. It is the positive control that makes the
real-data gates in ``test_real_topology.py`` interpretable — those can only show
a *difference* between two topologies, this shows the tool is right at all.

Nothing here needs the real database, the network or Home Assistant.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel


def _replay_module():
    """Import ``tools.audit.replay_capture`` (the package has no ``__init__``)."""
    import sys

    from tests.conftest import REPO_ROOT

    audit = str(REPO_ROOT / "tools" / "audit")
    if audit not in sys.path:
        sys.path.insert(0, audit)
    import replay_capture

    return replay_capture


@pytest.fixture(scope="module")
def simulated():
    """A short, deterministic run at the audited cadence."""
    cfg = SimulatorConfig(
        seed=202, minutes=10, interval=15, quality=QualityLevel.EXCELLENT
    )
    return list(HouseholdScenario(cfg).records())


def _capture_from_records(records: list[dict]):
    """Turn simulator records into a ``Capture``, exactly as a collector would.

    Every sample is one delivered measurement. The replay tool must not care
    where the rows came from, which is the point: the same code path serves the
    simulator, ``ct_events.jsonl`` and the engine's ``ct_samples`` table.
    """
    module = _replay_module()
    samples = []
    for record in records:
        for ct_id, measurement in record["measurements"].items():
            if not measurement.get("delivered", True):
                continue
            samples.append(
                module.CaptureSample(
                    ct_id=ct_id,
                    entity_id=measurement["entity_id"],
                    value_w=float(measurement["value_w"]),
                    state_timestamp=datetime.fromisoformat(
                        measurement["state_timestamp"]
                    ),
                    received_at=datetime.fromisoformat(measurement["received_at"]),
                    quality=str(measurement.get("quality", "good")),
                    repeated=bool(measurement.get("repeated", False)),
                    sequence=int(measurement.get("sequence", 0) or 0),
                )
            )
    samples.sort(key=lambda s: s.received_at)
    return module.Capture(
        samples=samples,
        source="simulator",
        window_start=samples[0].received_at,
        window_end=samples[-1].received_at,
    )


@pytest.fixture(scope="module")
def replayed(simulated):
    module = _replay_module()
    from tools.simulator import REAL_HOME_TOPOLOGY

    capture = _capture_from_records(simulated)
    topology = REAL_HOME_TOPOLOGY.to_contract_topology()
    return module.replay(capture, topology, variant="nested", tick_s=15.0)


def test_replay_reproduces_the_simulated_whole_home_power(simulated, replayed) -> None:
    """Positive control: on a house with truth, the replay must find it.

    The audited topology nests the sub panel under the main panel, so the
    effective feeds are the two main-panel legs and their sum *is* the
    simulator's ``main_total_w``. A replay that double counts, drops a feed, or
    mis-scales a channel cannot land within 5 %.
    """
    truth = sum(
        float(r["ground_truth"]["main_total_w"]) for r in simulated
    ) / len(simulated)
    assert replayed.n_windows > 0
    assert replayed.n_samples > 0
    assert replayed.main_power_w == pytest.approx(truth, rel=0.05), (
        f"replay reported {replayed.main_power_w:.1f} W against a true "
        f"{truth:.1f} W whole-home draw"
    )
    # And the panel tree is genuinely declared, so the nested check is not vacuous.
    assert replayed.invariants["nested_check_records"] > 0
    assert replayed.invariants["nested_double_count_records"] == 0


def test_replay_uses_only_effective_feeds(replayed) -> None:
    """A nested sub feed must not appear as a feed the total was built from."""
    assert len(replayed.feeds_used) == 2, replayed.feeds_used
    assert all("sub" not in ct for ct in replayed.feeds_used), replayed.feeds_used


def test_replay_conserves_power(replayed) -> None:
    """``main - accounted == residual``, to within a watt."""
    balance = replayed.main_power_w - replayed.accounted_w
    assert balance == pytest.approx(replayed.residual_w, abs=1.0)


def test_replay_reports_no_per_device_error_on_a_real_house(replayed) -> None:
    """Honesty contract: no truth means no per-device error statistics.

    The report shape must still match the simulator's so a consumer can use one
    reader for both, but the strata stay empty and say why.
    """
    payload = _replay_module().evaluation_shape(replayed, topology_version=1)
    assert payload["per_device"] == {}
    assert payload["per_source"] == {}
    assert payload["invariants"]["per_device_error_available"] is False
    assert any("no independent ground truth" in note for note in payload["notes"])
    # Every key the simulator report has must be present.
    from tests.simulation.metrics import evaluate, oracle_predictor

    simulated_report = evaluate([], oracle_predictor).as_dict()
    assert set(simulated_report) <= set(payload)
    # Machine-readable, so a pipeline can diff two runs.
    assert json.loads(json.dumps(payload))["replay"]["n_windows"] == replayed.n_windows


def test_deadline_aborts_a_runaway_replay() -> None:
    """The hard deadline must fire, and must not be catchable by a bare loop.

    A solver that ignores a flag cannot be interrupted cooperatively. The tool
    therefore arms a real timer (``SIGALRM``) and raises ``ReplayTimeout``; this
    pins that the mechanism exists and reports rather than hanging.
    """
    module = _replay_module()
    with pytest.raises(module.ReplayTimeout), module._Deadline(0.2, hard_exit=False):
        # Busy work, longer than the deadline, with no cooperation.
        total = 0
        for i in range(50_000_000):
            total += i


def test_deadline_is_inert_when_disabled() -> None:
    """``seconds <= 0`` means "no deadline", not "abort immediately"."""
    module = _replay_module()
    with module._Deadline(0.0, hard_exit=False):
        pass


def test_aligner_key_strips_the_domain_prefix() -> None:
    """A capture may store either form; both must map to the same channel."""
    module = _replay_module()
    assert module.aligner_key("sensor.panel_a_ct1_power") == "panel_a_ct1_power"
    assert module.aligner_key("panel_a_ct1_power") == "panel_a_ct1_power"


def test_jsonl_loader_reads_the_collector_format(tmp_path) -> None:
    """``ct_events.jsonl`` round-trips into a capture, unmapped rows are noted."""
    module = _replay_module()
    path = tmp_path / "ct_events.jsonl"
    rows = [
        {"type": "state", "entity_id": "sensor.a", "state": "100.0",
         "last_updated": "2026-01-01T00:00:15+00:00",
         "received_at": "2026-01-01T00:00:16+00:00"},
        {"type": "state", "entity_id": "sensor.a", "state": "140.0",
         "last_updated": "2026-01-01T00:00:30+00:00",
         "received_at": "2026-01-01T00:00:31+00:00"},
        {"type": "state", "entity_id": "sensor.unknown", "state": "1.0",
         "last_updated": "2026-01-01T00:00:30+00:00",
         "received_at": "2026-01-01T00:00:31+00:00"},
        {"type": "snapshot"},  # not a state record
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    capture = module.load_capture_jsonl(
        path, minutes=60, entity_to_ct={"sensor.a": "ct_a"}
    )
    assert [s.ct_id for s in capture.samples] == ["ct_a", "ct_a"]
    assert [s.value_w for s in capture.samples] == [100.0, 140.0]
    assert any("channel not in topology" in note for note in capture.notes)


def test_panel_map_classifies_the_audited_topology() -> None:
    """The replay's nested guard reuses the harness split, not a second copy."""
    from tools.simulator import REAL_HOME_TOPOLOGY

    module = _replay_module()
    panel = module._panel_map(REAL_HOME_TOPOLOGY.to_contract_topology())
    assert set(panel["root"]) == {"ct_main_l1", "ct_main_l2"}
    assert panel["sub"], "the audited topology nests a sub feed"
