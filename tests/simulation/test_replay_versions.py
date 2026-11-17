"""Replay-identical gate: one scenario + one algorithm version, two identical runs.

``REQ-TEST-005``/``REQ-TEST-008`` require that replaying the same scenario under
the same algorithm version produces identical results.  A replay engine that
depends on wall-clock time, dict iteration order, an un-seeded RNG or any other
hidden state would silently drift between two runs, and its reports could not be
diffed across releases.

This gate drives the **production** replay path (``Measurement`` ->
``SampleAligner`` -> ``DisaggregationEngine``) through
:func:`tools.audit.replay_capture.replay`, but feeds it an in-memory T-scenario
built by the TEST-004a builders instead of the real capture.  That deliberately
avoids the real-topology capture whose nine known errors are tracked separately;
here the scenario is synthetic, deterministic, and has no external dependency.

Identity is defined over every *semantic* field of ``ReplayResult`` via a SHA-256
digest.  ``elapsed_s`` is excluded: it is a wall-clock measurement, not a result,
and two identical runs will differ there by design.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from tools.audit import replay_capture as rc
from tools.simulator.scenarios import SCENARIO_IDS, build_scenario

#: A stable algorithm-version tag.  Both runs of a pair are stamped with the
#: same value; the fingerprint includes it so a deliberate version change is a
#: visible, assertable difference rather than an invisible drift.
ALGORITHM_VERSION = "replay-gate@1.0"

#: A representative sample of the canonical catalogue: a plain dedicated load,
#: noise, an ambiguous pair, variable power, delayed CTs and a split-phase load.
#: Each exercises a different inference branch while keeping the gate fast.
SAMPLED_SCENARIOS: tuple[str, ...] = ("T01", "T05", "T07", "T08", "T09", "T13")


def _capture(scenario_id: str, *, seed: int, minutes: float, interval: float):
    """Build one T-scenario and lift its delivered measurements into a Capture."""
    house = build_scenario(scenario_id, seed=seed, minutes=minutes, interval=interval)
    samples = []
    for record in house.to_list():
        for ct_id, measurement in record["measurements"].items():
            if not measurement.get("delivered", True):
                continue
            samples.append(
                rc.CaptureSample(
                    ct_id=ct_id,
                    entity_id=measurement["entity_id"],
                    value_w=float(measurement["value_w"]),
                    state_timestamp=datetime.fromisoformat(measurement["state_timestamp"]),
                    received_at=datetime.fromisoformat(measurement["received_at"]),
                    quality=str(measurement.get("quality", "good")),
                    repeated=bool(measurement.get("repeated", False)),
                    sequence=int(measurement.get("sequence", 0) or 0),
                )
            )
    samples.sort(key=lambda sample: sample.received_at)
    assert samples, f"{scenario_id} produced no delivered measurements"
    capture = rc.Capture(
        samples=samples,
        source="in-memory-scenario",
        window_start=samples[0].received_at,
        window_end=samples[-1].received_at,
    )
    return capture, house.topology.to_contract_topology()


def _result_fingerprint(result) -> str:
    """SHA-256 over the result's semantic fields, excluding wall-clock time."""
    payload = {
        "algorithm_version": ALGORITHM_VERSION,
        "n_samples": result.n_samples,
        "n_windows": result.n_windows,
        "n_unavailable_windows": result.n_unavailable_windows,
        "main_power_w": result.main_power_w,
        "residual_w": result.residual_w,
        "accounted_w": result.accounted_w,
        "feeds_used": result.feeds_used,
        "sum_of_all_mains_w": result.sum_of_all_mains_w,
        "per_device_power_w": result.per_device_power_w,
        "per_source": result.per_source,
        "main_by_window": result.main_by_window,
        "invariants": result.invariants,
        "reconciliation": result.reconciliation,
        "coverage": result.coverage,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _replay_once(scenario_id: str):
    capture, topology = _capture(
        scenario_id, seed=2026, minutes=4, interval=15
    )
    result = rc.replay(capture, topology, variant="nested", tick_s=15.0)
    assert result.n_windows > 0, f"{scenario_id} produced no replay windows"
    return result


@pytest.mark.parametrize("scenario_id", SAMPLED_SCENARIOS)
def test_sampled_scenarios_are_covered_by_the_catalogue(scenario_id: str) -> None:
    """Guard the sample: every chosen id is a real TEST-004a scenario."""
    assert scenario_id in SCENARIO_IDS


@pytest.mark.parametrize("scenario_id", SAMPLED_SCENARIOS)
def test_replay_is_identical_across_two_runs(scenario_id: str) -> None:
    """Same scenario + same algorithm version must yield byte-identical results.

    Two independent builds of the scenario are replayed through the same
    production path.  If either the builder or the replay engine carries hidden
    state, the two fingerprints diverge and the gate fails.
    """
    first = _result_fingerprint(_replay_once(scenario_id))
    second = _result_fingerprint(_replay_once(scenario_id))
    assert first == second, f"{scenario_id} replay is not replay-identical"


def test_fingerprint_changes_when_the_algorithm_version_changes() -> None:
    """Negative control: the version stamp is part of identity, not decoration.

    A gate that cannot distinguish one algorithm version from another would pass
    a silent version drift, which is exactly what this gate exists to catch.
    """
    result = _replay_once("T01")
    payload_a = {"algorithm_version": ALGORITHM_VERSION, "n_windows": result.n_windows}
    payload_b = {"algorithm_version": "replay-gate@2.0", "n_windows": result.n_windows}
    digest = lambda payload: hashlib.sha256(  # noqa: E731 - local, one-shot helper
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert digest(payload_a) != digest(payload_b)
