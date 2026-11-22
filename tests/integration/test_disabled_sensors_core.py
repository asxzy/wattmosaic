"""WM_DISABLED_SENSORS on a real EngineCore (Database, VersionService, ledger).

A configured-off sensor must leave every consumer: the matrix and panel tree,
placements (its children re-homed), the reader exclusions inference uses, the
HA deny list and the T5 structure memory.  Removing it from the list re-enables
it.  Synthetic ids only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EndpointConfigRevision,
    MeasurementEndpoint,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ENDPOINTS = {
    "meter_a_l1": "sensor.meter_a_ct1_power",
    "meter_b_l1": "sensor.meter_b_ct1_power",
    "circuit_c": "sensor.circuit_c_power",
}


def _clock() -> datetime:
    return T0


@pytest.fixture
def core(tmp_path: Path) -> Any:
    db = Database(tmp_path / "disabled.db")
    for endpoint_id, entity_id in ENDPOINTS.items():
        endpoint = MeasurementEndpoint(
            endpoint_id=endpoint_id, kind="ct", entity_id=entity_id, display_name=endpoint_id,
            scope="circuit", scope_status="candidate", enabled=True, created_at=T0,
        )
        row = endpoint.model_dump(mode="json")
        row["metadata"] = {"source": "ha_discovery"}
        db.repos.entities.upsert_endpoint(row)
        db.repos.entities.append_endpoint_revision(
            EndpointConfigRevision(
                endpoint_id=endpoint_id, revision=1, valid_from=T0, phase="unknown",
                polarity="normal", multiplier=1.0, calibration_version="discovery",
                measurement_type="instantaneous", declared_by="discovery",
            )
        )
    config = EngineConfig(
        database_path=str(tmp_path / "disabled.db"),
        ha_token="not-used",
        disabled_sensors=("meter_b_*",),
    )
    engine = EngineCore(config, db, clock=_clock)
    try:
        yield engine
    finally:
        db.close()


def _contains(edge_id: str, parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id, relation=RelationType.CONTAINS, source_endpoint_id=parent,
        target_endpoint_id=child, lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference", beta=1.0, beta_basis="unity", evidence=EdgeEvidence(),
        valid_from=T0, pinned=False,
    )


def _seed_topology(core: Any) -> None:
    """circuit_c sits under the soon-disabled meter_b_l1, like a running install."""
    from wattmosaic_engine.electrical.topology_inference import TopologyInferenceView

    edge = _contains("e-c", "meter_b_l1", "circuit_c")
    versions = core._v3_topology_service().versions
    versions.promote_topology(versions.propose_topology([edge], None), "test")
    core.topology_inference = TopologyInferenceView(edges=(edge,), clock=_clock)


def _tree_readers(nodes: list[dict]) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        if node.get("reader_id"):
            out.add(str(node["reader_id"]))
        out |= _tree_readers(node.get("children") or [])
    return out


def test_disabled_sensor_leaves_every_consumer(core: Any) -> None:
    _seed_topology(core)
    core._t5_memory.record("meter_b_l1", 1000.0, 5.0)
    core._t5_memory.record("meter_a_l1", 1000.0, 5.0)
    # Sanity: before the reconcile the sensor is placed and in the tree.
    assert core.panel_tree()["placements"]["circuit_c"]["parent"] == "meter_b_l1"

    result = core._apply_disabled_sensors()

    assert result.endpoint_ids == {"meter_b_l1"}
    row = {r["endpoint_id"]: r for r in core.db.repos.entities.endpoints()}["meter_b_l1"]
    assert not row["enabled"]
    # Matrix / tree endpoints.
    assert "meter_b_l1" not in {str(e.endpoint_id) for e in core._matrix_endpoints()}
    tree = core.panel_tree()
    assert "meter_b_l1" not in _tree_readers(tree["roots"])
    assert "meter_b_l1" not in tree["placements"]
    assert tree["placements"]["circuit_c"]["parent"] is None, "child re-homed, not lost"
    assert "circuit_c" in _tree_readers(tree["roots"])
    assert tree["disabled_sensors"] == ["meter_b_l1"]
    assert any("WM_DISABLED_SENSORS" in note for note in tree["notes"])
    # /topology/v3 placements agree.
    assert "meter_b_l1" not in core.topology_v3().get("placements", {})
    # Inference exclusions, HA deny list, structure memory.
    assert core._excluded_readers()["meter_b_l1"] == "disabled_by_config"
    assert "sensor.meter_b_ct1_power" in core.hasource.deny_entities
    assert "meter_b_*" in core.hasource.deny_entities
    assert core._t5_memory.readers() == ("meter_a_l1",)


def test_removing_the_entry_reenables_the_sensor(core: Any) -> None:
    core._apply_disabled_sensors()
    core.config.disabled_sensors = ()

    result = core._apply_disabled_sensors()

    assert result.reenabled == ("meter_b_l1",)
    row = {r["endpoint_id"]: r for r in core.db.repos.entities.endpoints()}["meter_b_l1"]
    assert row["enabled"]
    assert "meter_b_l1" in {str(e.endpoint_id) for e in core._matrix_endpoints()}
    assert "meter_b_l1" not in core._excluded_readers()
    assert "sensor.meter_b_ct1_power" not in core.hasource.deny_entities
