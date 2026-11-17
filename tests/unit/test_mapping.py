"""Unit tests for the derived Device↔Reader Mapping Matrix projection.

The projection is pure: no clock, no storage, no per-sample snapshot.  These
tests build real ``Topology`` contracts and assert determinism, all four
cardinalities, unknown-load handling, nullable bounds, the status vocabulary
and the REQ-MAPPING-007 coverage block.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceReaderMappingCell,
    DeviceTopology,
    EdgeLifecycle,
    MappingCellStatus,
    MappingObservability,
    Phase,
    RelationKind,
    Topology,
    TopologyRelation,
    TopologyStatus,
    UnknownLoadProfile,
    UnknownResidualEvent,
)
from wattmosaic_engine.disaggregation import (
    CONFIRM_MIN_EVENTS,
    MappingEvidence,
    MappingMatrix,
    build_mapping_matrix,
)
from wattmosaic_engine.electrical.models import ModelView

T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)

Rcv = "rc-1"
Tvv = "tv-1"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _channel(
    cid: str,
    *,
    device: str | None = None,
    parent: str | None = None,
    ct_type: CTType = CTType.BRANCH,
    enabled: bool = True,
) -> CTChannel:
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=Phase.L1,
        parent_ct_id=parent,
        device_id=device,
        enabled=enabled,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _device(did: str, *, branches: Sequence[str] = ()) -> DeviceTopology:
    return DeviceTopology(
        device_id=did,
        branch_ct_ids=list(branches),
        topology_status=TopologyStatus.CONFIRMED,
    )


def _topology(
    channels: Sequence[CTChannel],
    devices: Sequence[DeviceTopology],
    *,
    relations: Sequence[TopologyRelation] = (),
) -> Topology:
    return Topology(
        version=1,
        channels=list(channels),
        devices=list(devices),
        relations=list(relations),
    )


def _association(
    device_id: str,
    ct_id: str,
    *,
    verdict: str = "supported",
    n_supporting: int = CONFIRM_MIN_EVENTS,
    contribution: float = 100.0,
    status: TopologyStatus = TopologyStatus.CANDIDATE,
    confidence: float = 0.8,
) -> TopologyRelation:
    supporting = [
        {
            "scaled_delta_w": contribution,
            "ct_delta_w": contribution,
            "device_delta_w": contribution,
            "occurred_at": T0.isoformat(),
        }
        for _ in range(n_supporting)
    ]
    confirmed = status is TopologyStatus.CONFIRMED
    return TopologyRelation(
        relation_id=f"rel-{device_id}-{ct_id}",
        kind=RelationKind.DEVICE_CT_ASSOCIATION,
        subject_id=device_id,
        object_id=ct_id,
        status=status,
        # Confirmation is a user act declared on *both* axes (contract guard):
        # a CONFIRMED status requires a CONFIRMED lifecycle and no outstanding
        # confirmation requirement.
        lifecycle=EdgeLifecycle.CONFIRMED if confirmed else EdgeLifecycle.CANDIDATE,
        requires_confirmation=not confirmed,
        confidence=confidence,
        evidence={
            "verdict": verdict,
            "supporting": supporting,
            "stats": {
                "median_delta_w": contribution,
                "contribution": 1.0,
                "basis": "ct_measured",
            },
        },
        declared_by="inference",
        created_at=T0,
        valid_from=T0,
    )


def _event(reader_id: str, *, event_id: str, value: float = 45.0) -> UnknownResidualEvent:
    return UnknownResidualEvent(
        event_id=event_id,
        scope_id=reader_id,
        reader_id=reader_id,
        window_start=T0,
        window_end=T0,
        dynamic_w=value,
        residual_w=value,
        threshold_w=value,
        min_duration_s=0.0,
        window=[value],
    )


def _project(
    topology: Topology,
    *,
    models: dict | None = None,
    evidence: MappingEvidence | None = None,
) -> MappingMatrix:
    return build_mapping_matrix(
        topology,
        models=models,
        evidence=evidence,
        reader_configuration_version=Rcv,
        topology_version=Tvv,
    )


# --------------------------------------------------------------------------- #
# Determinism / no snapshotting (REQ-MAPPING-001, REQ-MAPPING-005)
# --------------------------------------------------------------------------- #


def test_same_inputs_produce_byte_identical_output() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )
    first = _project(topology)
    second = build_mapping_matrix(
        topology,
        reader_configuration_version=Rcv,
        topology_version=Tvv,
    )
    assert first.stable_json() == second.stable_json()
    assert first.as_dict() == second.as_dict()


def test_no_per_sample_snapshot_two_calls_are_identical_even_with_models_and_evidence() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[_association("dev_a", "br_a")],
    )
    models = {"dev_a": _model("dev_a", version=3, power=120.0, quality=0.9)}
    evidence = MappingEvidence(
        associations=(_association("dev_a", "br_a"),),
        unknown_residual_events=(_event("br_unknown", event_id="e1"),),
    )
    a = _project(topology, models=models, evidence=evidence)
    b = _project(topology, models=models, evidence=evidence)
    assert a.stable_json() == b.stable_json()
    # The stable bytes are round-trippable JSON (no live objects sneak in).
    assert json.loads(a.stable_json()) == a.as_dict()


def test_stable_json_ignores_source_iteration_order_but_is_pure() -> None:
    """The projection never mutates its inputs."""
    topology = _topology(
        [_channel("br_a", device="dev_a"), _channel("br_b", device="dev_b")],
        [_device("dev_a", branches=["br_a"]), _device("dev_b", branches=["br_b"])],
    )
    before = topology.model_dump_json()
    _project(topology)
    assert topology.model_dump_json() == before


# --------------------------------------------------------------------------- #
# Cardinality (REQ-MAPPING-002)
# --------------------------------------------------------------------------- #


def test_one_to_one_cardinality() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )
    cells = _project(topology).cells
    assert len(cells) == 1
    assert cells[0].relation_type == "one_to_one"
    assert (cells[0].device_id, cells[0].reader_id) == ("dev_a", "br_a")


def test_one_to_many_cardinality() -> None:
    # dev_a sits under two branch CTs -> one device, two readers.
    topology = _topology(
        [
            _channel("br_a1", device="dev_a"),
            _channel("br_a2", device="dev_a"),
        ],
        [_device("dev_a", branches=["br_a1", "br_a2"])],
    )
    cells = _project(topology).cells
    assert len(cells) == 2
    assert {c.relation_type for c in cells} == {"one_to_many"}
    matrix = _project(topology)
    assert set(matrix.coverage.non_summing_readers) <= {"br_a1", "br_a2"}


def test_many_to_one_cardinality() -> None:
    # Two devices on one shared reader.
    topology = _topology(
        [_channel("br_shared")],
        [
            _device("dev_a", branches=["br_shared"]),
            _device("dev_b", branches=["br_shared"]),
        ],
    )
    cells = _project(topology).cells
    assert len(cells) == 2
    assert {c.relation_type for c in cells} == {"many_to_one"}
    assert "br_shared" in _project(topology).coverage.non_summing_readers


def test_many_to_many_cardinality() -> None:
    topology = _topology(
        [
            _channel("br_1", device="dev_a"),
            _channel("br_2", device="dev_b"),
        ],
        [
            _device("dev_a", branches=["br_1", "br_2"]),
            _device("dev_b", branches=["br_1", "br_2"]),
        ],
    )
    cells = _project(topology).cells
    assert len(cells) == 4
    assert {c.relation_type for c in cells} == {"many_to_many"}
    assert set(_project(topology).coverage.non_summing_readers) == {"br_1", "br_2"}


# --------------------------------------------------------------------------- #
# Unknown loads (REQ-MAPPING-004)
# --------------------------------------------------------------------------- #


def _model(device_id: str, *, version: int, power: float, quality: float = 0.8) -> ModelView:
    return ModelView(
        device_id=device_id,
        version=version,
        estimated_power_w=power,
        validation_quality=quality,
    )


def test_unknown_load_cell_has_no_fabricated_device_id() -> None:
    evidence = MappingEvidence(
        unknown_residual_events=(_event("br_unknown", event_id="e1"),),
    )
    matrix = _project(_topology([], []), evidence=evidence)
    unknown = [c for c in matrix.cells if c.is_unknown_load]
    assert len(unknown) == 1
    cell = unknown[0]
    assert cell.reader_id == "br_unknown"
    assert cell.device_id.startswith("unknown-load::")
    assert cell.device_id != "br_unknown"  # not a fabricated reader-as-device
    assert cell.status == MappingCellStatus.NON_IDENTIFIABLE
    assert cell.estimated_contribution_w == 45.0
    assert cell.evidence_count == 1
    assert "unexplained_scope" in {s["reason"] for s in cell.sources_used}


def test_unknown_load_links_profile_when_present() -> None:
    profile = UnknownLoadProfile(
        profile_id="ulp-7",
        scope_id="br_unknown",
        reader_id="br_unknown",
        first_seen_at=T0,
        last_seen_at=T0,
        label="mystery load",
    )
    evidence = MappingEvidence(
        unknown_load_profiles=(profile,),
        unknown_residual_events=(_event("br_unknown", event_id="e1"),),
    )
    matrix = _project(_topology([], []), evidence=evidence)
    unknown = next(c for c in matrix.cells if c.is_unknown_load)
    assert unknown.unknown_load_profile_id == "ulp-7"


def test_uncertain_mapping_is_marked_conflicted_not_omitted() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[_association("dev_a", "br_a", verdict="ambiguous", confidence=0.6)],
    )
    cell = _project(topology).cells[0]
    assert cell.status == MappingCellStatus.CONFLICTED
    # Ambiguity caps the association confidence dimension (REQ-MAPPING-006).
    assert cell.confidence.ct_association is not None
    assert cell.confidence.ct_association <= 0.25


# --------------------------------------------------------------------------- #
# Bounds, observability, provenance, statuses (REQ-MAPPING-003, 006, 007)
# --------------------------------------------------------------------------- #


def test_nullable_bounds_when_only_a_point_estimate_is_supported() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[_association("dev_a", "br_a", contribution=110.0)],
    )
    cell = _project(topology).cells[0]
    # Two identical supporting deltas -> min == max == estimate (real evidence).
    assert cell.estimated_contribution_w == 110.0
    assert cell.min_contribution_w == 110.0
    assert cell.max_contribution_w == 110.0


def test_bounds_are_null_when_no_evidence_supports_them() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )
    cell = _project(topology).cells[0]
    assert cell.estimated_contribution_w is None
    assert cell.min_contribution_w is None
    assert cell.max_contribution_w is None
    assert cell.observability == MappingObservability.DIRECT.value
    assert cell.status == MappingCellStatus.UNKNOWN


def test_unknown_observability_carries_no_contribution() -> None:
    topology = _topology(
        [_channel("br_shared")],
        [_device("dev_unknown_loc", branches=[])],
        relations=[
            TopologyRelation(
                relation_id="rel-u",
                kind=RelationKind.DEVICE_CT_ASSOCIATION,
                subject_id="dev_unknown_loc",
                object_id="br_shared",
                evidence={"verdict": "supported"},
                declared_by="inference",
                created_at=T0,
            )
        ],
    )
    cell = next(c for c in _project(topology).cells if c.device_id == "dev_unknown_loc")
    # No evidence rows -> no estimate, even though a relation exists.
    assert cell.estimated_contribution_w is None


def test_provenance_fields_come_from_inputs() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[_association("dev_a", "br_a")],
    )
    models = {"dev_a": _model("dev_a", version=4, power=95.0)}
    cell = _project(topology, models=models).cells[0]
    assert cell.model_version == 4
    assert cell.topology_version == Tvv
    assert cell.reader_configuration_version == Rcv
    sources = {(s["source"], s["reason"]) for s in cell.sources_used}
    assert ("association", "verdict:supported") in sources
    assert ("topology", "channel_map") in sources
    assert ("model", "estimated_power") in sources


def test_model_only_cell_is_learning_with_bounded_estimate() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )
    models = {"dev_a": ModelView(device_id="dev_a", version=2, estimated_power_w=60.0, upper_bound_w=80.0)}
    cell = _project(topology, models=models).cells[0]
    assert cell.status == MappingCellStatus.LEARNING
    assert cell.estimated_contribution_w == 60.0
    assert cell.min_contribution_w is None
    assert cell.max_contribution_w == 80.0


def test_supported_relation_yields_provisional_then_confirmed_at_the_bar() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[
            _association("dev_a", "br_a", n_supporting=CONFIRM_MIN_EVENTS - 1)
        ],
    )
    assert _project(topology).cells[0].status == MappingCellStatus.PROVISIONAL
    topology_confirmed = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[
            _association(
                "dev_a",
                "br_a",
                n_supporting=CONFIRM_MIN_EVENTS,
                status=TopologyStatus.CONFIRMED,
            )
        ],
    )
    assert _project(topology_confirmed).cells[0].status == MappingCellStatus.CONFIRMED


def test_status_vocabulary_is_used_by_the_projection() -> None:
    """Every status the projection emits is in the six-state-plus-original set."""
    allowed = {s.value for s in MappingCellStatus}
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
        relations=[
            _association("dev_a", "br_a", status=TopologyStatus.CONFIRMED),
        ],
    )
    matrix = _project(
        topology,
        evidence=MappingEvidence(
            unknown_residual_events=(_event("br_unknown", event_id="e1"),)
        ),
    )
    for cell in matrix.cells:
        assert cell.status in allowed
    # A NON_IDENTIFIABLE unknown-load cell is emitted.
    assert any(c.status == MappingCellStatus.NON_IDENTIFIABLE for c in matrix.cells)


def test_coverage_names_unobserved_devices_and_empty_readers() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a"), _channel("br_idle")],
        [_device("dev_a", branches=["br_a"]), _device("dev_orphan", branches=[])],
    )
    coverage = _project(topology).coverage
    assert coverage.unobserved_devices == ("dev_orphan",)
    assert "br_idle" in coverage.readers_without_attributable_device
    assert coverage.as_dict()["unobserved_devices"] == ["dev_orphan"]


def test_cells_are_contract_instances() -> None:
    topology = _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )
    for cell in _project(topology).cells:
        assert isinstance(cell, DeviceReaderMappingCell)
        # Round-trips through the frozen contract unchanged.
        assert DeviceReaderMappingCell.model_validate(cell.model_dump()).model_dump() == cell.model_dump()
