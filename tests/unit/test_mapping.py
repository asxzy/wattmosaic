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
from wattmosaic_engine.disaggregation.mapping import (
    ReconciliationResult,
    StaleRecord,
    reconcile_topology_change,
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


# --------------------------------------------------------------------------- #
# Reconciliation after a topology version change
# (REQ-TOPO-009, REQ-CHANGE-004/007, REQ-MAPPING-005, gap G-08)
# --------------------------------------------------------------------------- #


class _Record:
    """A minimal inference-record stand-in carrying the version it ran under."""

    def __init__(self, record_id: str, topology_version: int) -> None:
        self.record_id = record_id
        self.topology_version = topology_version


def _old_topology() -> Topology:
    return _topology(
        [_channel("br_a", device="dev_a")],
        [_device("dev_a", branches=["br_a"])],
    )


def _new_topology() -> Topology:
    # The new version moves dev_b onto a new reader br_b and adds it.
    return _topology(
        [
            _channel("br_a", device="dev_a"),
            _channel("br_b", device="dev_b"),
        ],
        [
            _device("dev_a", branches=["br_a"]),
            _device("dev_b", branches=["br_b"]),
        ],
    )


def _reconcile(**overrides):
    defaults = dict(
        old_version=1,
        new_version=2,
        new_topology=_new_topology(),
        reader_configuration_version=Rcv,
    )
    defaults.update(overrides)
    return reconcile_topology_change(**defaults)


def test_reconciliation_rederives_matrix_from_the_new_topology() -> None:
    """The re-derived matrix is exactly B3a's projection of the new topology."""
    new_topology = _new_topology()
    result = _reconcile(new_topology=new_topology)
    assert isinstance(result, ReconciliationResult)
    expected = build_mapping_matrix(
        new_topology,
        reader_configuration_version=Rcv,
        topology_version="tv-00000002",
    )
    assert result.matrix.stable_json() == expected.stable_json()
    # The new topology has a device/cell the old one did not.
    assert {(c.device_id, c.reader_id) for c in result.matrix.cells} == {
        ("dev_a", "br_a"),
        ("dev_b", "br_b"),
    }
    assert result.matrix.topology_version == "tv-00000002"


def test_reconciliation_flags_old_version_records_and_cells_as_stale() -> None:
    old_matrix = build_mapping_matrix(
        _old_topology(),
        reader_configuration_version=Rcv,
        topology_version="tv-00000001",
    )
    records = [_Record("rec-old-1", 1), _Record("rec-old-2", 1), _Record("rec-new", 2)]
    result = _reconcile(current_matrix=old_matrix, inference_records=records)

    # Records under the old version are flagged; the new-version one is not.
    assert result.affected_record_ids == ("rec-old-1", "rec-old-2")
    assert "rec-new" not in result.affected_record_ids
    # The old matrix's cell (stamped v1) is flagged too.
    assert result.affected_cell_keys == ("dev_a|br_a",)
    assert all(isinstance(item, StaleRecord) for item in result.stale)
    assert all(item.reason == "topology_version_superseded" for item in result.stale)


def test_reconciliation_does_not_delete_or_rewrite_history() -> None:
    """Records and the current matrix are unchanged in content by reconciliation."""
    records = [_Record("rec-old-1", 1)]
    before = [(r.record_id, r.topology_version) for r in records]
    old_matrix = build_mapping_matrix(
        _old_topology(), reader_configuration_version=Rcv, topology_version="tv-00000001"
    )
    matrix_before = old_matrix.stable_json()
    result = _reconcile(current_matrix=old_matrix, inference_records=records)

    assert [(r.record_id, r.topology_version) for r in records] == before
    assert old_matrix.stable_json() == matrix_before
    # The verdict is a *separate* value; it is not written back onto the record.
    assert result.stale[0].record_id == "rec-old-1"
    assert not hasattr(records[0], "stale")


def test_reconciliation_is_idempotent() -> None:
    records = [_Record("rec-old-1", 1), _Record("rec-old-2", 1)]
    old_matrix = build_mapping_matrix(
        _old_topology(), reader_configuration_version=Rcv, topology_version="tv-00000001"
    )
    first = _reconcile(current_matrix=old_matrix, inference_records=records)
    second = _reconcile(current_matrix=old_matrix, inference_records=records)
    assert first.as_dict() == second.as_dict()
    assert first.matrix.stable_json() == second.matrix.stable_json()
    assert first.stale == second.stale


def test_reconciliation_strict_mode_flags_only_the_named_old_version() -> None:
    """With both endpoints known, only artefacts carrying old_version are flagged."""
    records = [
        _Record("rec-v1", 1),
        _Record("rec-v3", 3),
        _Record("rec-v2", 2),
    ]
    result = _reconcile(inference_records=records, strict_new_only=True)
    # Only v1 is the named predecessor; the stray v3 is not touched.
    assert result.affected_record_ids == ("rec-v1",)


def test_reconciliation_does_not_promote_or_advance_a_version() -> None:
    """The caller's version numbers are echoed unchanged; nothing is promoted."""
    result = _reconcile(old_version=1, new_version=2)
    assert result.old_version == 1
    assert result.new_version == 2
    assert result.matrix.topology_version == "tv-00000002"
    # No change-status/suspicion mutation occurs here: promotion is separate.
    assert "status" not in result.as_dict()


# --------------------------------------------------------------------------- #
# Device-major axes: CTs are readers, real devices are rows
# --------------------------------------------------------------------------- #


def _ct_auto_circuit(did: str, ct_id: str) -> DeviceTopology:
    """A discovery-generated CT circuit: device id is ``dev_<ct_id>``."""
    return DeviceTopology(
        device_id=did,
        name=ct_id,
        branch_ct_ids=[ct_id],
        topology_status=TopologyStatus.CANDIDATE,
        metadata={"shared_circuit": True, "requires_confirmation": True},
    )


def test_ct_auto_circuits_are_not_device_rows_but_cts_are_readers() -> None:
    topology = _topology(
        [_channel("ct_kitchen"), _channel("ct_hall")],
        [
            _ct_auto_circuit("dev_ct_kitchen", "ct_kitchen"),
            _ct_auto_circuit("dev_ct_hall", "ct_hall"),
        ],
    )
    matrix = _project(topology)
    # CT ids live on the reader/column axis only.
    assert set(matrix.readers) == {"ct_kitchen", "ct_hall"}
    # No CT-derived circuit surfaces as a device/row.
    assert matrix.devices == ()
    assert "dev_ct_kitchen" not in {c.device_id for c in matrix.cells}
    assert "dev_ct_hall" not in {c.device_id for c in matrix.cells}
    assert all(c.reader_id in matrix.readers for c in matrix.cells)


def test_real_device_and_ct_circuit_coexist_with_clean_axes() -> None:
    """A real HA/config-backed device stays a row; its CT stays a column."""
    real = DeviceTopology(
        device_id="dev_fridge",
        name="Kitchen fridge",
        branch_ct_ids=["ct_kitchen"],
        metadata={"device_config": {"device_type": "appliance"}},
    )
    topology = _topology(
        [_channel("ct_kitchen"), _channel("ct_hall")],
        [real, _ct_auto_circuit("dev_ct_hall", "ct_hall")],
    )
    matrix = _project(topology)
    assert matrix.devices == ("dev_fridge",)
    assert set(matrix.readers) == {"ct_kitchen", "ct_hall"}
    assert "dev_fridge" in {c.device_id for c in matrix.cells}
    assert "dev_ct_hall" not in matrix.devices


def test_device_sources_add_ha_and_config_devices_to_the_row_axis() -> None:
    """HA registry and DeviceConfiguration sources feed the device axis."""
    topology = _topology(
        [_channel("ct_kitchen")],
        [_ct_auto_circuit("dev_ct_kitchen", "ct_kitchen")],
    )
    matrix = build_mapping_matrix(
        topology,
        reader_configuration_version=Rcv,
        device_sources=[
            [
                {"device_id": "ha_kitchen_light", "name": "Kitchen light"},
                {"id": "ha_washer", "name_by_user": "Washer"},
            ],
            [{"device_id": "dev_fridge", "name": "Fridge"}],
        ],
    )
    assert matrix.devices == ("dev_fridge", "ha_kitchen_light", "ha_washer")
    assert matrix.readers == ("ct_kitchen",)
    assert "dev_ct_kitchen" not in matrix.devices


def test_device_names_label_the_axis_with_human_readable_ha_names() -> None:
    """Opaque HA ids get an informative label; raw ids are never invented names."""
    topology = _topology(
        [_channel("ct_kitchen")],
        [_ct_auto_circuit("dev_ct_kitchen", "ct_kitchen")],
    )
    matrix = build_mapping_matrix(
        topology,
        reader_configuration_version=Rcv,
        device_sources=[
            [
                {"id": "ha_light_abc", "name_by_user": "Kitchen light"},
                {"device_id": "ha_washer_xyz", "name": "Washer"},
                {"id": "ha_unnamed", "name_by_user": None, "name": None},
            ],
            [{"device_id": "dev_fridge", "name": "Fridge"}],
        ],
    )
    assert matrix.device_names == {
        "ha_light_abc": "Kitchen light",
        "ha_washer_xyz": "Washer",
        "dev_fridge": "Fridge",
    }
    assert "ha_unnamed" not in matrix.device_names
    assert "ha_light_abc" in matrix.devices  # labelled ids are real axis rows


def test_device_names_are_published_and_byte_stable_in_as_dict() -> None:
    """The additive map is in the payload and stable_json order is canonical."""
    topology = _topology([_channel("ct_x")], [DeviceTopology(device_id="dev_a")])
    matrix = build_mapping_matrix(
        topology,
        reader_configuration_version=Rcv,
        device_sources=[[{"id": "hb", "name": "Beta"}, {"id": "ha", "name": "Alpha"}]],
    )
    payload = matrix.as_dict()
    assert payload["device_names"] == {"ha": "Alpha", "hb": "Beta"}
    assert list(payload["device_names"]) == ["ha", "hb"]  # sorted
    assert matrix.as_dict() == build_mapping_matrix(
        topology,
        reader_configuration_version=Rcv,
        device_sources=[[{"id": "ha", "name": "Alpha"}, {"id": "hb", "name": "Beta"}]],
    ).as_dict()
    # No sources -> empty name map (the additive field never invents labels).
    assert _project(topology).as_dict()["device_names"] == {}


def test_axes_are_deterministic_and_sorted() -> None:
    topology = _topology(
        [_channel("ct_b"), _channel("ct_a")],
        [
            DeviceTopology(device_id="dev_z", branch_ct_ids=["ct_a"]),
            DeviceTopology(device_id="dev_a", branch_ct_ids=["ct_b"]),
        ],
    )
    matrix = _project(topology)
    assert matrix.devices == tuple(sorted(matrix.devices))
    assert matrix.readers == tuple(sorted(matrix.readers))
    assert matrix.stable_json() == _project(topology).stable_json()


def test_unknown_load_prefix_is_never_a_device_row() -> None:
    topology = _topology(
        [_channel("ct_x")],
        [DeviceTopology(device_id="unknown-load::ct_x", branch_ct_ids=[])],
    )
    matrix = _project(topology)
    assert "unknown-load::ct_x" not in matrix.devices
