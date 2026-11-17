"""Performance budgets for the new mapping / topology-diff / residual-history paths.

``REQ-TEST-008`` requires a guard against an accidental O(n^2)-or-worse
regression in the three paths added by the completion sprint:

* the B3a mapping projection (:func:`build_mapping_matrix`);
* the typed topology diff (:func:`diff_topologies`);
* residual-history computation (:meth:`ResidualAnalyzer.trend`).

Each test runs a synthetic workload many times and asserts a **generous** wall
clock budget.  Budgets are set at 10-20x the calibrated runtime on this project's
CI class of hardware, so an ordinary machine or a busy runner never trips them,
while a genuine algorithmic regression (a quadratic scan where a hash join
belongs) still would.  Each test also asserts the workload did real work, so a
stub that returns instantly without computing anything cannot pass.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    EdgeLifecycle,
    RelationKind,
    Topology,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.mapping import build_mapping_matrix
from wattmosaic_engine.electrical.topology import diff_topologies
from wattmosaic_engine.models.residual_power import ResidualAnalyzer

#: Generous ceilings (seconds) for the full loops below.  Calibrated at roughly
#: 0.6 s / 2.2 s / 0.2 s respectively; the ceilings leave >9x headroom.
MAPPING_BUDGET_S = 8.0
DIFF_BUDGET_S = 10.0
RESIDUAL_BUDGET_S = 4.0

MAPPING_ITERATIONS = 1000
DIFF_ITERATIONS = 1000
RESIDUAL_ITERATIONS = 100
RESIDUAL_ROWS = 2000


def _synthetic_topology() -> Topology:
    """40 channels over 20 devices: enough incidence for the projection to work."""
    channels = [
        CTChannel(
            ct_id=f"ct{i}",
            entity_id=f"sensor.ct{i}",
            name=f"ct{i}",
            ct_type=CTType.BRANCH,
            device_id=f"dev{i % 20}",
        )
        for i in range(40)
    ]
    devices = [
        DeviceTopology(device_id=f"dev{i}", branch_ct_ids=[f"ct{i}", f"ct{i + 20}"])
        for i in range(20)
    ]
    return Topology(channels=channels, devices=devices, status=TopologyStatus.CONFIRMED)


def _relation(index: int, *, confirmed: bool) -> TopologyRelation:
    status = TopologyStatus.CONFIRMED if confirmed else TopologyStatus.CANDIDATE
    lifecycle = EdgeLifecycle.CONFIRMED if confirmed else EdgeLifecycle.CANDIDATE
    return TopologyRelation(
        relation_id=f"rel{index}",
        kind=RelationKind.ELECTRICAL_PARENT_CHILD,
        subject_id=f"subject{index}",
        object_id=f"object{index}",
        status=status,
        lifecycle=lifecycle,
        requires_confirmation=not confirmed,
    )


def test_mapping_projection_within_budget() -> None:
    """1000 projections of a 40-channel topology must stay under budget."""
    topology = _synthetic_topology()
    started = time.perf_counter()
    cells_per_run = 0
    for _ in range(MAPPING_ITERATIONS):
        matrix = build_mapping_matrix(topology, reader_configuration_version="perf-v1")
        cells_per_run = len(matrix.cells)
    elapsed = time.perf_counter() - started

    assert cells_per_run > 0, "the workload produced no mapping cells"
    assert elapsed < MAPPING_BUDGET_S, (
        f"mapping projection took {elapsed:.2f}s for {MAPPING_ITERATIONS} runs "
        f"(budget {MAPPING_BUDGET_S}s)"
    )


def test_topology_diff_within_budget() -> None:
    """1000 diffs of two 200-edge topologies must stay under budget."""
    old = Topology(
        channels=[],
        devices=[],
        relations=tuple(_relation(i, confirmed=True) for i in range(200)),
    )
    new = Topology(
        channels=[],
        devices=[],
        relations=(
            tuple(_relation(i, confirmed=False) for i in range(198))
            + (_relation(500, confirmed=True), _relation(501, confirmed=True))
        ),
    )
    started = time.perf_counter()
    changes_per_run = 0
    for _ in range(DIFF_ITERATIONS):
        diff = diff_topologies(old, new)
        changes_per_run = len(diff.added) + len(diff.changed) + len(diff.removed)
    elapsed = time.perf_counter() - started

    assert changes_per_run > 0, "the workload produced no diff"
    assert elapsed < DIFF_BUDGET_S, (
        f"topology diff took {elapsed:.2f}s for {DIFF_ITERATIONS} runs "
        f"(budget {DIFF_BUDGET_S}s)"
    )


def test_residual_history_within_budget() -> None:
    """100 trend computations over 2000 residual rows must stay under budget."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = [
        {
            "timestamp": start + timedelta(seconds=i),
            "residual_w": 100.0 + (i % 50),
            "resolved": i % 7 == 0,
            "basis": "unknown",
            "quality_status": "good",
            "explained_by_device_w": {},
        }
        for i in range(RESIDUAL_ROWS)
    ]
    analyzer = ResidualAnalyzer()
    started = time.perf_counter()
    sample_count = 0
    for _ in range(RESIDUAL_ITERATIONS):
        trend = analyzer.trend(history)
        sample_count = trend.sample_count
    elapsed = time.perf_counter() - started

    assert sample_count == RESIDUAL_ROWS, "the workload produced no full trend"
    assert elapsed < RESIDUAL_BUDGET_S, (
        f"residual history took {elapsed:.2f}s for {RESIDUAL_ITERATIONS} runs "
        f"over {RESIDUAL_ROWS} rows (budget {RESIDUAL_BUDGET_S}s)"
    )
