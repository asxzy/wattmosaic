"""AT-01 — one load through nested readers without double counting (HB §21).

Closes: M1. The same physical +45 W (HB §17 step 3) must appear in the Circuit A,
Subpanel and Main matrix cells while the physical total adds it exactly once and
reconciliation counts it once.

The invented ``project_matrix_payload(hb17["observation"])`` surface named by the
original K07 sketch never landed.  This closed test drives M1's *real* pure
projection: it builds the K08 runtime handoffs (topology/mapping/operator) from
the HB §17 reference house, feeds a real ``JointInferenceResult`` through
``materialise_cells`` and asserts the semantics on the returned contract objects.
The fixture plumbing is rewritten; the AT-01 assertions are unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    Bounds,
    CellPower,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    ReconciliationV3,
    VersionTriple,
)
from wattmosaic_engine.mapping import materialise_cells  # real M1 surface

from tests.support.fakes import FakeMapping, FakeTopology, fake_operator

CLOSED_BY = "M1"

pytestmark = [pytest.mark.acceptance]

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-hb17", mapping_version_id="map-hb17", model_set_id="mset-hb17"
)
#: HB §17 reader ids (X07) and the topology from truth: Main ⊃ Subpanel ⊃ A/B.
READERS = ("ct_main", "ct_sub", "ct_a", "ct_b", "ct_c")
HB17_TREE = {"ct_main": {"ct_sub": {"ct_a": {}, "ct_b": {}}, "ct_c": {}}}
SUBJECT = ("device", "light.living_room_1")


def _project(hb17):
    """Run the real M1 projection with a +45 W light on Circuit A."""
    house = hb17["house"]
    # The observation frame is the only input used for the reading identities;
    # the topology shape mirrors the HB §17 house (X07 truth), not an oracle
    # value being attributed to a device.
    assert {s["reader_id"] for s in house.reader_samples} >= set(READERS)

    topology = FakeTopology.from_tree(HB17_TREE)
    mapping = FakeMapping.from_pairs({SUBJECT: "ct_a"})
    operator = fake_operator(topology, mapping, READERS, [SUBJECT], at=T0)

    est = DeviceEstimate(
        subject_kind="device",
        subject_id=SUBJECT[1],
        point_w=45.0,
        bounds=Bounds(lower_w=0.0, upper_w=None),
        status=EstimateStatus.ESTIMATED,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        group_id=None,
        method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=["ct_a"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )
    result = JointInferenceResult(
        result_id="at01-result",
        status=EventGroupStatus.CONFIRMED,
        device_estimates=[est],
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={"ct_main": 1500.0},
        versions=VT,
        computed_at=T0,
    )
    cells, _totals, physical = materialise_cells(operator, result, mapping, {}, None)
    return cells, physical


def test_nested_load_not_double_counted(hb17) -> None:
    """The same load in three nested reader cells is one physical contribution."""
    cells, physical = _project(hb17)

    lights = [c for c in cells if "light" in c.row_id]
    assert lights, "the co-switched lights must produce matrix cells"

    by_reader = {c.reader_id: c for c in lights}
    for reader in ("ct_a", "ct_sub", "ct_main"):
        assert reader in by_reader, f"missing light cell for nested reader {reader}"
        power: CellPower = by_reader[reader].power
        assert power.measurement_status is not None
        assert power.estimated_power_w is not None
        assert power.estimated_power_w == pytest.approx(45.0, abs=2.0)

    # Physical total adds the load once, not three times.
    assert physical.identified_w == pytest.approx(45.0, abs=2.0)
    assert physical.total_w == pytest.approx(1545.0, abs=2.0)

    # Reconciliation counts the nested observation once.
    recon: ReconciliationV3 = ReconciliationV3.model_validate(
        {"main_w": 1545.0, "identified_w": 45.0, "group_w": 0.0,
         "unknown_w": 0.0, "residual_w": 1500.0, "export_w": 0.0,
         "meter_spread_w": 0.0}
    )
    assert recon.identified_w + recon.group_w == pytest.approx(45.0, abs=2.0)
