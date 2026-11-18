"""M1: pure matrix projection core (design §8, HB §9/§10, AT-01/AT-13).

All inputs are deterministic fakes from K08 (``runtime.fakes``).  The projection
is pure: no DB, no wall clock, and absent estimates are ``None`` (HB §18).
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    Bounds,
    CellVisual,
    ConfidenceVectorV3,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    MappingStatus,
    Observability,
    VersionTriple,
)
from wattmosaic_engine.mapping import (
    build_columns,
    build_rows,
    materialise_cells,
    materialise_matrix,
    stable_json,
)
from wattmosaic_engine.runtime.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)
READERS = ["Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C"]


class _TreeTopology(FakeTopology):
    """K08 fake topology with the T1 ``primary_display_tree`` seam added."""

    def primary_display_tree(self) -> dict[str, object]:
        return HB17_TREE


def _topology() -> _TreeTopology:
    return _TreeTopology.from_tree(HB17_TREE)  # type: ignore[return-value]


def _mapping() -> FakeMapping:
    return FakeMapping.from_pairs({("device", "light-1"): "Circuit A"})


def _operator() -> object:
    return fake_operator(
        _topology(),
        _mapping(),
        READERS,
        [("device", "light-1")],
    )


def _estimate(
    subject_id: str = "light-1",
    *,
    point_w: float | None = 45.0,
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
    status: EstimateStatus = EstimateStatus.ESTIMATED,
    method: InferenceMethod = InferenceMethod.JOINT_DELTA,
    group_id: str | None = None,
    bounds: Bounds | None = None,
) -> DeviceEstimate:
    if bounds is None:
        bounds = Bounds(lower_w=None if point_w is None else 0.0, upper_w=None)
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=bounds,
        status=status,
        identifiability=ident,
        group_id=group_id,
        method=method,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=["Circuit A"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )


def _result(
    estimates: list[DeviceEstimate],
    *,
    groups: list[DeviceGroupEstimate] | None = None,
    residual: dict[str, float | None] | None = None,
) -> JointInferenceResult:
    return JointInferenceResult(
        result_id="test-result",
        status=EventGroupStatus.CONFIRMED,
        device_estimates=estimates,
        group_estimates=groups or [],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader=residual or {},
        versions=VT,
        computed_at=T0,
    )


# --------------------------------------------------------------------------- #
# AT-01 — a 45 W light on Circuit A appears in Circuit A, Subpanel and Main.
# --------------------------------------------------------------------------- #


def test_at01_light_propagates_to_ancestors_and_counts_once() -> None:
    result = _result([_estimate()], residual={"Main": 100.0})
    cells, totals, physical = materialise_cells(
        _operator(), result, _mapping(), {}, None
    )

    by_reader = {c.reader_id: c for c in cells if c.row_id == "light-1"}
    assert set(by_reader) == {"Circuit A", "Subpanel", "Main"}

    # The same load is EXPECTED on ancestors, DIRECT on its own circuit.
    assert by_reader["Circuit A"].mapping.observability == Observability.DIRECT
    assert by_reader["Circuit A"].mapping.relation_type == "direct_endpoint"
    assert by_reader["Subpanel"].mapping.observability == Observability.EXPECTED
    assert by_reader["Main"].mapping.observability == Observability.EXPECTED

    # All three publish the current estimate (from estimates, not a model total).
    assert all(c.power.estimated_power_w == 45.0 for c in by_reader.values())
    assert by_reader["Circuit A"].visual == CellVisual.CONFIRMED_INFERRED

    # The physical total counts the light once, plus the main residual.
    assert physical.identified_w == 45.0
    assert physical.residual_w == 100.0
    assert physical.total_w == 145.0


def test_at01_expected_ancestors_are_confirmed_inferred() -> None:
    result = _result([_estimate()])
    cells, _, _ = materialise_cells(_operator(), result, _mapping(), {}, None)
    ancestor = next(
        c for c in cells
        if c.row_id == "light-1" and c.reader_id == "Main"
    )
    # A CONFIRMED CONTAINS path gives EXPECTED observability and a CONFIRMED
    # mapping, so the deterministic category is CONFIRMED_INFERRED (design §8).
    assert ancestor.mapping.observability == Observability.EXPECTED
    assert ancestor.mapping.mapping_status == MappingStatus.CONFIRMED
    assert ancestor.visual == CellVisual.CONFIRMED_INFERRED


def test_physical_total_counts_each_subject_once() -> None:
    est1 = _estimate("light-1")
    est2 = _estimate("light-2", point_w=10.0)
    # Two subjects share an ancestor; each must be counted once.
    topology = _topology()
    mapping = FakeMapping.from_pairs(
        {("device", "light-1"): "Circuit A", ("device", "light-2"): "Circuit A"}
    )
    op = fake_operator(topology, mapping, READERS, [("device", "light-1"), ("device", "light-2")])
    result = _result([est1, est2], residual={"Main": 7.0})
    _, _, physical = materialise_cells(op, result, mapping, {}, None)
    assert physical.identified_w == 55.0
    assert physical.total_w == 62.0


# --------------------------------------------------------------------------- #
# Group row / identifiability (DEC-19, HB §8.4).
# --------------------------------------------------------------------------- #


def test_group_row_carries_sum_and_members_are_group_only_with_none() -> None:
    group = DeviceGroupEstimate(
        group_id="grp-1",
        member_ids=["lamp-a", "lamp-b"],
        point_w=250.0,
        bounds=Bounds(lower_w=0.0, upper_w=250.0, basis="group_sum"),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["Circuit A"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )
    lamp_a = _estimate(
        "lamp-a",
        point_w=None,
        ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
        status=EstimateStatus.UNKNOWN,
        method=InferenceMethod.NONE,
        group_id="grp-1",
        bounds=Bounds(lower_w=0.0, upper_w=250.0, basis="group_sum"),
    )
    result = _result(
        [lamp_a],
        groups=[group],
    )
    topology = _topology()
    mapping = FakeMapping.from_pairs({("device", "lamp-a"): "Circuit A"})
    op = fake_operator(topology, mapping, READERS, [("device", "lamp-a")])
    cells, _, physical = materialise_cells(op, result, mapping, {}, None)

    member = next(c for c in cells if c.row_id == "lamp-a")
    assert member.power.estimated_power_w is None
    assert member.visual == CellVisual.GROUP_ONLY
    assert member.group_id == "grp-1"
    assert physical.group_w == 250.0


def test_non_identifiable_member_never_gets_a_number() -> None:
    # A NON_IDENTIFIABLE estimate cannot carry a point at the contract level,
    # so exercise the suppression on a duck-typed estimate carrying one anyway
    # (the projection must never publish a number for such a member).
    class DuckEstimate:
        subject_kind = "device"
        subject_id = "light-1"
        point_w = 90.0  # deliberately present, must be suppressed
        bounds = Bounds(lower_w=0.0, upper_w=None)
        status = EstimateStatus.UNKNOWN
        identifiability = IdentifiabilityStatus.NON_IDENTIFIABLE
        group_id = "grp-x"
        method = InferenceMethod.NONE
        confidence = ConfidenceVectorV3()
        source_reader_ids = ["Circuit A"]
        evidence_refs: list[object] = []
        versions = VT
        updated_at = T0

    from wattmosaic_engine.contracts import JointInferenceResult as JIR

    result = JIR.model_construct(
        result_id="duck",
        status=EventGroupStatus.CONFIRMED,
        device_estimates=[DuckEstimate()],  # type: ignore[list-item]
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={},
        versions=VT,
        computed_at=T0,
    )
    cells, _, _ = materialise_cells(_operator(), result, _mapping(), {}, None)
    cell = next(c for c in cells if c.row_id == "light-1")
    assert cell.power.estimated_power_w is None


# --------------------------------------------------------------------------- #
# Reader residual vs unknown profile are distinct kinds; None not 0.
# --------------------------------------------------------------------------- #


def test_reader_residual_and_unknown_profile_are_different_kinds() -> None:
    result = _result([_estimate()])
    # Inject a residual + unknown estimate via the result's unknown list.
    from wattmosaic_engine.contracts import UnknownEstimate

    result.unknown_estimates = [
        UnknownEstimate(scope_reader_id="Main", delta_w=12.0, point_w=12.0)
    ]
    result.residual_w_by_reader = {"Main": -5.0}
    cells, _, _ = materialise_cells(_operator(), result, _mapping(), {}, None)
    residual = next(c for c in cells if c.row_kind == "reader_residual")
    unknown = next(c for c in cells if c.row_kind == "unknown_profile")
    assert residual.row_kind != unknown.row_kind
    assert residual.power.estimated_power_w == -5.0  # signed, never clamped
    assert unknown.power.estimated_power_w == 12.0


def test_absent_residual_is_none_not_zero() -> None:
    result = _result([_estimate()], residual={"Main": None})
    cells, _, physical = materialise_cells(_operator(), result, _mapping(), {}, None)
    residual = next((c for c in cells if c.row_kind == "reader_residual"), None)
    if residual is not None:
        assert residual.power.estimated_power_w is None
    assert physical.residual_w is None


# --------------------------------------------------------------------------- #
# Byte-identical determinism across two runs.
# --------------------------------------------------------------------------- #


def test_stable_json_byte_identical_across_two_runs() -> None:
    result = _result([_estimate()], residual={"Main": 100.0})
    cells1, totals1, physical1 = materialise_cells(
        _operator(), result, _mapping(), {}, None
    )
    cells2, totals2, physical2 = materialise_cells(
        _operator(), result, _mapping(), {}, None
    )
    assert stable_json(cells1) == stable_json(cells2)
    assert stable_json(totals1) == stable_json(totals2)
    assert stable_json(physical1) == stable_json(physical2)


def test_materialise_matrix_smoke() -> None:
    result = _result([_estimate()], residual={"Main": 100.0})
    cols = build_columns(_topology(), [
        {"endpoint_id": r, "display_name": r, "panel_id": "Main"} for r in READERS
    ])
    rows = build_rows(readers=READERS)
    payload = materialise_matrix(
        _operator(), result, _mapping(), {}, None,
        columns=cols, rows=rows, generated_at=T0, mapping_version_id="map-1",
    )
    assert payload.mapping_version_id == "map-1"
    assert len(payload.cells) == 4
    assert payload.physical_total.total_w == 145.0
