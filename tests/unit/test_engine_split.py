"""X02: splitting ``disaggregation/engine.py`` must not change results.

This task is a *no behaviour change* refactor.  The guard is three fixtures
lifted from ``test_disaggregation.py``:

* a **separable** concurrent-activation topology (``test_03``);
* a **shared-branch ambiguity** topology (``test_16``);
* a **nested** branch topology (``test_07``).

``DisaggregationEngine.infer`` is run on each and the whole ``InferenceBatch``
(results + reconciliation) is canonicalised to a ``stable_json`` string.  That
string is compared with a golden file captured from the engine *before* the
split, so a moved helper that silently changed an estimate is caught here even
if the lower-level scenario assertions still happen to pass.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceState,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation import DisaggregationEngine
from wattmosaic_engine.electrical import ObservationWindow

T0 = datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC)
CADENCE = 15.7
GOLDEN = Path(__file__).parent / "engine_split_golden.json"


# --------------------------------------------------------------------------- #
# Fixtures (copied verbatim from test_disaggregation.py)
# --------------------------------------------------------------------------- #
def _ch(cid, ct_type=CTType.BRANCH, *, phase=Phase.L1, parent=None, device=None, resolution=None):
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        device_id=device,
        resolution_w=resolution,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _mains():
    return [
        _ch("main_l1", CTType.MAIN, phase=Phase.L1, resolution=1.0),
        _ch("main_l2", CTType.MAIN, phase=Phase.L2, resolution=1.0),
    ]


def _device(did, *, phase=Phase.L1, branch_ct_ids):
    return DeviceTopology(
        device_id=did,
        phase=phase,
        topology_status=TopologyStatus.CONFIRMED,
        branch_ct_ids=branch_ct_ids,
    )


def _topology(channels, devices):
    return Topology(
        version=1,
        status=TopologyStatus.CONFIRMED,
        channels=channels,
        devices=devices,
    )


def _states(*ids, **overrides):
    return {
        did: DeviceState(entity_id=f"switch.{did}", state=overrides.get(did, "on"))
        for did in ids
    }


def _window(values, *, previous_states=None, transitions=None):
    hist = {}
    for cid, value in values.items():
        if value is None:
            continue
        start = T0 - timedelta(seconds=CADENCE * 5)
        hist[cid] = [
            (start + timedelta(seconds=CADENCE * i), float(value)) for i in range(6)
        ]
    return ObservationWindow.from_values(
        T0,
        values,
        histories=hist,
        cadence_s=CADENCE,
        previous_states=previous_states,
        state_transitions=transitions,
    )


def _separable():
    topo = _topology(
        [*_mains(), _ch("branch_a", parent="main_l1"), _ch("branch_b", parent="main_l2", phase=Phase.L2)],
        [
            _device("a", branch_ct_ids=["branch_a"], phase=Phase.L1),
            _device("b", branch_ct_ids=["branch_b"], phase=Phase.L2),
        ],
    )
    win = _window(
        {"main_l1": 300.0, "main_l2": 220.0, "branch_a": 300.0, "branch_b": 220.0},
        previous_states={"a": "off", "b": "off"},
        transitions=[
            dict(device_id="a", previous_state="off", new_state="on"),
            dict(device_id="b", previous_state="off", new_state="on"),
        ],
    )
    return topo, win, _states("a", "b")


def _shared_ambiguity():
    topo = _topology(
        [*_mains(), _ch("branch_shared", parent="main_l1")],
        [
            _device("d0", branch_ct_ids=["branch_shared"]),
            _device("d1", branch_ct_ids=["branch_shared"]),
        ],
    )
    win = _window(
        {"main_l1": 500.0, "main_l2": 0.0, "branch_shared": 500.0},
        previous_states={"d0": "off", "d1": "off"},
        transitions=[
            dict(device_id="d0", previous_state="off", new_state="on"),
            dict(device_id="d1", previous_state="off", new_state="on"),
        ],
    )
    return topo, win, _states("d0", "d1")


def _nested():
    topo = _topology(
        [
            *_mains(),
            _ch("branch_kitchen", parent="main_l1"),
            _ch("branch_fridge", parent="branch_kitchen"),
            _ch("branch_laundry", parent="main_l1"),
            _ch("ct_tv", device="tv"),
        ],
        [
            _device("lights", branch_ct_ids=["branch_kitchen"]),
            _device("fridge", branch_ct_ids=["branch_fridge"]),
            _device("washer", branch_ct_ids=["branch_laundry"]),
            _device("tv", branch_ct_ids=["ct_tv"]),
        ],
    )
    win = _window(
        {
            "main_l1": 900.0,
            "main_l2": 0.0,
            "branch_kitchen": 900.0,
            "branch_fridge": 200.0,
            "branch_laundry": 0.0,
            "ct_tv": 0.0,
        }
    )
    return topo, win, _states("lights", "fridge", "washer", "tv")


def _stable_json(batch) -> str:
    """Canonical bytes over every semantic field of an ``InferenceBatch``.

    Diagnostics are excluded: they carry wall-clock- and version-stamped
    metadata that is intentionally not part of a result's identity.  What must
    stay byte-identical across the split is the published results and the
    home-level power balance.
    """
    payload = {
        "results": [r.model_dump(mode="json") for r in batch.results],
        "reconciliation": batch.reconciliation.model_dump(mode="json"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


FIXTURES = {
    "separable": _separable,
    "shared_ambiguity": _shared_ambiguity,
    "nested": _nested,
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_infer_matches_the_pre_split_golden(name):
    topo, win, dev_states = FIXTURES[name]()
    batch = DisaggregationEngine().infer(
        win, device_states=dev_states, models={}, topology=topo
    )
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _stable_json(batch) == golden[name]


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_infer_is_deterministic_across_two_runs(name):
    topo, win, dev_states = FIXTURES[name]()
    first = DisaggregationEngine().infer(
        win, device_states=dev_states, models={}, topology=topo
    )
    second = DisaggregationEngine().infer(
        win, device_states=dev_states, models={}, topology=topo
    )
    assert _stable_json(first) == _stable_json(second)
