"""K05: matrix contracts — structured cells, trees and drill-down payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import legacy, matrix
from wattmosaic_engine.contracts.matrix import (
    CellDetail,
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    CellVisual,
    ColumnAnnotation,
    ColumnNode,
    ConfidenceVectorV3,
    EvidenceRef,
    MappingCell,
    MappingStatus,
    MatrixPayload,
    PhysicalTotal,
    ReaderColumnSummary,
    ReaderDetail,
    RowDetail,
    RowKind,
    RowNode,
    VersionTriple,
)

SCHEMA_MODELS = [model for _stem, model in matrix.SCHEMA_EXPORTS]

OBSERVABLE_LEVELS = {"direct", "expected", "inferred", "partial", "conflicted"}
NO_ESTIMATE_LEVELS = {"unknown", "not_observable"}


# --------------------------------------------------------------------------- #
# Sample builders (all datetimes fixed, all numeric fields default to None).
# --------------------------------------------------------------------------- #


def _mapping(**overrides: Any) -> CellMapping:
    data: dict[str, Any] = {
        "observability": "direct",
        "relation_type": "DIRECT_ENDPOINT",
        "mapping_status": "confirmed",
        "mapping_confidence": 0.9,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": None,
    }
    data.update(overrides)
    return CellMapping(**data)


def _power(**overrides: Any) -> CellPower:
    data: dict[str, Any] = {
        "measurement_status": "estimated",
        "estimated_power_w": 48.0,
        "bounds": {"lower_w": 40.0, "upper_w": 55.0, "basis": "posterior"},
        "standby_power_w": None,
        "operating_state": None,
        "expected_profile_ref": None,
        "uncertainty_w": 4.0,
    }
    data.update(overrides)
    return CellPower(**data)


def _cell(**overrides: Any) -> MappingCell:
    data: dict[str, Any] = {
        "row_kind": "device",
        "row_id": "pd_tv",
        "load_component_id": None,
        "reader_id": "ct_plug_tv",
        "mapping": _mapping(),
        "power": _power(),
        "identifiability": "identifiable",
        "group_id": None,
        "event_association": CellEvents(
            matched_event_count=3,
            confirmed_match_count=2,
            provisional_match_count=1,
        ),
        "confidence": ConfidenceVectorV3(measurement_quality=0.8, freshness=0.9),
        "provenance": CellProvenance(
            sources_used=["reader_measurement", "model"],
            evidence_refs=[EvidenceRef(kind="sample", id="sample-1")],
            topology_version_id="topo-3",
            model_version_id="mdl-pd_tv-1",
            mapping_version_id="map-7",
        ),
        "timestamps": CellTimestamps(
            first_observed_at="2026-01-01T00:00:00Z",
            last_observed_at="2026-01-02T00:00:00Z",
            last_validated_at=None,
            updated_at="2026-01-02T00:00:00Z",
        ),
        "visual": "confirmed_inferred",
        "derived": False,
    }
    data.update(overrides)
    return MappingCell(**data)


def _leaf(node_id: str, reader_id: str) -> ColumnNode:
    return ColumnNode(
        node_id=node_id,
        level="L4",
        label=reader_id,
        kind="reader",
        reader_id=reader_id,
        children=[],
        annotations=[],
    )


def _payload(**overrides: Any) -> MatrixPayload:
    data: dict[str, Any] = {
        "mapping_version_id": "map-7",
        "versions": VersionTriple(
            topology_version_id="topo-3",
            mapping_version_id="map-7",
            model_set_id="mset-0123456789ab",
        ),
        "generated_at": "2026-01-02T00:00:00Z",
        "columns": [
            ColumnNode(
                node_id="dom-1",
                level="L0",
                label="Home",
                kind="domain",
                reader_id=None,
                children=[
                    ColumnNode(
                        node_id="panel-1",
                        level="L1",
                        label="Main panel",
                        kind="panel",
                        reader_id=None,
                        children=[_leaf("leaf-main", "ct_main")],
                        annotations=[],
                    )
                ],
                annotations=[],
            )
        ],
        "rows": [
            RowNode(
                node_id="room-1",
                kind="room",
                label="Living room",
                row_id=None,
                children=[
                    RowNode(
                        node_id="row-tv",
                        kind="device",
                        label="Television",
                        row_id="pd_tv",
                        children=[],
                    )
                ],
            )
        ],
        "cells": [_cell()],
        "reader_totals": {
            "ct_main": ReaderColumnSummary(
                measured_w=1500.0,
                known_contrib_w=900.0,
                residual_w=600.0,
                unknown_w=60.0,
            )
        },
        "physical_total": PhysicalTotal(
            measured_w=1500.0,
            identified_w=900.0,
            group_w=45.0,
            unknown_w=60.0,
            residual_w=600.0,
            total_w=1500.0,
        ),
    }
    data.update(overrides)
    return MatrixPayload(**data)


def _samples() -> dict[type, Any]:
    cell = _cell()
    payload = _payload()
    return {
        CellMapping: _mapping(),
        CellPower: _power(),
        CellEvents: CellEvents(matched_event_count=3),
        CellProvenance: cell.provenance,
        CellTimestamps: cell.timestamps,
        MappingCell: cell,
        ColumnAnnotation: ColumnAnnotation(
            kind="PAIRED_LEG", related_reader_id="ct_other", note="leg B"
        ),
        ColumnNode: payload.columns[0],
        RowNode: payload.rows[0],
        ReaderColumnSummary: payload.reader_totals["ct_main"],
        PhysicalTotal: payload.physical_total,
        MatrixPayload: payload,
        CellDetail: CellDetail(cell=cell, evidence_refs=cell.provenance.evidence_refs),
        ReaderDetail: ReaderDetail(reader_id="ct_main", measured_w=1500.0),
        RowDetail: RowDetail(row_id="pd_tv", kind="device", label="Television", cells=[cell]),
    }


# --------------------------------------------------------------------------- #
# Naming / export consistency (contract-task rules, Wave 1).
# --------------------------------------------------------------------------- #


def test_no_public_name_collides_with_legacy() -> None:
    legacy_names = set(dir(legacy))
    collisions = sorted(name for name in matrix.__all__ if name in legacy_names)
    assert collisions == [], f"matrix __all__ names shadow legacy: {collisions}"


def test_every_exported_model_is_in_all_and_schema_exports() -> None:
    schema_models = set(SCHEMA_MODELS)
    for model in schema_models:
        assert model.__name__ in matrix.__all__
    for name in matrix.__all__:
        assert hasattr(matrix, name)


def test_matrix_names_reachable_from_facade() -> None:
    assert contracts.MappingCell is MappingCell
    assert contracts.MatrixPayload is MatrixPayload


# --------------------------------------------------------------------------- #
# JSON round-trips.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_model_round_trips_through_json(model: type) -> None:
    samples = _samples()
    instance = samples[model]
    payload = instance.model_dump_json()
    assert model.model_validate_json(payload) == instance


# --------------------------------------------------------------------------- #
# Vocabulary / enum contents (HB §9.7).
# --------------------------------------------------------------------------- #


def test_mapping_status_has_no_identifiability_values() -> None:
    assert {m.value for m in MappingStatus} == {
        "unknown",
        "learning",
        "provisional",
        "confirmed",
        "suspected_change",
        "conflicted",
        "retired",
    }
    identifiability_words = {"identifiable", "non_identifiable", "group_identifiable"}
    assert identifiability_words.isdisjoint({m.value for m in MappingStatus})


def test_row_kind_members() -> None:
    assert {m.value for m in RowKind} == {
        "device",
        "component",
        "group",
        "unknown_profile",
        "reader_residual",
        "summary",
    }


def test_cell_visual_members() -> None:
    assert {m.value for m in CellVisual} == {
        "directly_measured",
        "confirmed_inferred",
        "provisional",
        "group_only",
        "unknown",
        "not_observable",
        "conflicted",
        "retired",
    }


# --------------------------------------------------------------------------- #
# Validators: estimate inside bounds (ported from legacy).
# --------------------------------------------------------------------------- #


def test_bounds_bracket_the_estimate() -> None:
    cell = _cell(power=_power(estimated_power_w=48.0))
    assert cell.power.estimated_power_w == 48.0


@pytest.mark.parametrize(
    "power",
    [
        _power(estimated_power_w=20.0),  # below lower bound
        _power(estimated_power_w=60.0),  # above upper bound
    ],
)
def test_estimate_outside_bounds_is_rejected(power: CellPower) -> None:
    with pytest.raises(ValidationError):
        _cell(power=power)


def test_inverted_bounds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _cell(power=_power(bounds={"lower_w": 10.0, "upper_w": 5.0}))


def test_none_estimate_needs_no_bounds() -> None:
    cell = _cell(power=_power(estimated_power_w=None, bounds=None))
    assert cell.power.estimated_power_w is None


# --------------------------------------------------------------------------- #
# Validators: observability ⇒ no estimate.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", sorted(NO_ESTIMATE_LEVELS))
def test_unknown_or_not_observable_forbids_an_estimate(level: str) -> None:
    with pytest.raises(ValidationError):
        _cell(mapping=_mapping(observability=level), power=_power(estimated_power_w=12.0))


@pytest.mark.parametrize("level", sorted(OBSERVABLE_LEVELS))
def test_observable_levels_allow_an_estimate(level: str) -> None:
    cell = _cell(mapping=_mapping(observability=level), power=_power(estimated_power_w=None))
    assert cell.mapping.observability == level


# --------------------------------------------------------------------------- #
# Validators: GROUP_ONLY ⇒ no estimate on member cells.
# --------------------------------------------------------------------------- #


def test_group_only_visual_forbids_an_estimate() -> None:
    with pytest.raises(ValidationError):
        _cell(visual="group_only", power=_power(estimated_power_w=12.0))


def test_group_only_visual_allows_none() -> None:
    cell = _cell(visual="group_only", power=_power(estimated_power_w=None))
    assert cell.visual == "group_only"
    assert cell.power.estimated_power_w is None


# --------------------------------------------------------------------------- #
# Validators: ColumnNode kind ⇔ reader_id ⇔ no children.
# --------------------------------------------------------------------------- #


def test_reader_node_requires_a_reader_id_and_no_children() -> None:
    with pytest.raises(ValidationError):
        ColumnNode(
            node_id="x",
            level="L4",
            label="x",
            kind="reader",
            reader_id=None,
            children=[],
            annotations=[],
        )
    with pytest.raises(ValidationError):
        ColumnNode(
            node_id="x",
            level="L4",
            label="x",
            kind="reader",
            reader_id="ct_x",
            children=[_leaf("y", "ct_y")],
            annotations=[],
        )


def test_non_reader_node_forbids_a_reader_id() -> None:
    with pytest.raises(ValidationError):
        ColumnNode(
            node_id="x",
            level="L1",
            label="x",
            kind="panel",
            reader_id="ct_x",
            children=[],
            annotations=[],
        )


@pytest.mark.parametrize("kind", ["domain", "panel", "subpanel", "circuit", "unassigned"])
def test_non_reader_node_level_constraints(kind: str) -> None:
    node = ColumnNode(
        node_id="x", level="L1", label="x", kind=kind, reader_id=None, children=[], annotations=[]
    )
    assert node.reader_id is None


# --------------------------------------------------------------------------- #
# Validators: unique reader leaves across the whole columns tree (AT-13).
# --------------------------------------------------------------------------- #


def test_duplicate_reader_leaf_is_rejected() -> None:
    duplicate_columns = [
        ColumnNode(
            node_id="dom-1",
            level="L0",
            label="Home",
            kind="domain",
            reader_id=None,
            children=[_leaf("leaf-main", "ct_main"), _leaf("leaf-dup", "ct_main")],
            annotations=[],
        )
    ]
    with pytest.raises(ValidationError):
        _payload(columns=duplicate_columns)


def test_distinct_reader_leaves_are_accepted() -> None:
    payload = _payload()
    readers = [
        leaf.reader_id
        for leaf in payload.columns[0].children[0].children
        if leaf.kind == "reader"
    ]
    assert readers == ["ct_main"]


def test_cell_reader_ids_need_not_be_column_leaves() -> None:
    payload = _payload(cells=[_cell(reader_id="ct_plug_tv")])
    assert payload.cells[0].reader_id == "ct_plug_tv"


# --------------------------------------------------------------------------- #
# Validators: SUMMARY rows ⇒ derived.
# --------------------------------------------------------------------------- #


def test_summary_row_requires_derived_true() -> None:
    with pytest.raises(ValidationError):
        _cell(row_kind="summary", derived=False)


def test_summary_row_accepts_derived_true() -> None:
    cell = _cell(row_kind="summary", derived=True)
    assert cell.derived is True


def test_non_summary_row_defaults_to_not_derived() -> None:
    assert _cell().derived is False


# --------------------------------------------------------------------------- #
# Drill-down payloads.
# --------------------------------------------------------------------------- #


def test_drill_down_payloads_round_trip() -> None:
    cell = _cell()
    cell_detail = CellDetail(
        cell=cell,
        mapping_history=[],
        event_groups=[],
        evidence_refs=cell.provenance.evidence_refs,
        confidence_components=cell.confidence,
        uncertainty_reasons=[],
        contradictions=[],
    )
    reader_detail = ReaderDetail(
        reader_id="ct_main",
        scope="main_feed",
        topology_relations=[],
        measured_w=1500.0,
        known_contrib_w=900.0,
        known_contributions=[],
        residual_w=600.0,
        unknown_w=60.0,
    )
    row_detail = RowDetail(
        row_id="pd_tv",
        kind="device",
        label="Television",
        operating_state=None,
        power_profile_ref=None,
        observed_readers=["ct_plug_tv"],
        mapping_history=[],
        cells=[cell],
    )
    for obj, typ in (
        (cell_detail, CellDetail),
        (reader_detail, ReaderDetail),
        (row_detail, RowDetail),
    ):
        assert typ.model_validate_json(obj.model_dump_json()) == obj


# --------------------------------------------------------------------------- #
# The hand-written HB §17 fixture validates.
# --------------------------------------------------------------------------- #


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "matrix" / "hb17_payload.json"


def test_hb17_fixture_is_a_valid_matrix_payload() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = MatrixPayload.model_validate(raw)
    assert payload.mapping_version_id
    assert MatrixPayload.model_validate_json(payload.model_dump_json()) == payload


def test_hb17_fixture_reader_leaves_are_unique() -> None:
    payload = MatrixPayload.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    def leaves(nodes: list[ColumnNode]) -> list[str]:
        result: list[str] = []
        for node in nodes:
            if node.kind == "reader":
                result.append(str(node.reader_id))
            else:
                result.extend(leaves(node.children))
        return result

    readers = leaves(payload.columns)
    assert len(readers) == len(set(readers))
    assert set(readers) == {
        "ct_main",
        "ct_subpanel",
        "ct_circuit_a",
        "ct_circuit_b",
        "ct_circuit_c",
        "plug_tv",
    }
