"""Regression: stored edge evidence with a legacy ``source`` key must load.

**The outage.**  ``Model`` forbids extra keys, and :class:`EdgeEvidence` has no
``source`` field.  Migration 14 backfilled topology edges with
``{"source": "ct_channels.paired_ct_id"}`` (and the parent/alias/
``topology_relations`` variants), migration 15 did the same for mapping edges,
and migration 29 wrote the whole dict back when seeding the decay weights.  Any
such row made ``VersionService.topology_view`` raise

    ValidationError: evidence.source  Extra inputs are not permitted

inside ``_edge_contract`` -- and ``EngineCore.start`` calls it through
``_wire_v3_services``, so the engine died on every boot.  One installation had
16 channels with ``paired_ct_id``: guaranteed, not exotic.

The decoder therefore sanitises *on read*: only keys that are fields of
``EdgeEvidence`` survive, values are preserved, and the ``extra="forbid"`` policy
itself stays intact so a genuine contract violation elsewhere still raises.
These tests fail on the commit that shipped the outage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from wattmosaic_engine.contracts import EdgeEvidence, TopologyEdgeV3
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.versioning import VersionService, edge_evidence_payload
from wattmosaic_engine.versioning.service import _edge_contract, _map_edge_contract

NOW = "2026-01-01T00:00:00+00:00"

#: The exact dicts v014 + v029 leave in ``topology_edges.evidence_json``.
LEGACY_TOPOLOGY_SOURCES = (
    "ct_channels.parent_ct_id",
    "ct_channels.paired_ct_id",
    "ct_channels.alias_of_ct_id",
    "topology_relations",
)


def _topology_row(edge_id: str, evidence_json: str) -> dict[str, object]:
    """A raw S14 repository row, shaped like ``topology_v3.edges()`` returns."""
    return {
        "edge_id": edge_id,
        "relation": "contains",
        "source_endpoint_id": "parent",
        "target_endpoint_id": edge_id,
        "lifecycle": "structural",
        "declared_by": "migration",
        "beta": 1.0,
        "beta_basis": "unity",
        "evidence_json": evidence_json,
        "valid_from": NOW,
        "valid_until": None,
        "pinned": 0,
    }


def _mapping_row(edge_id: str, evidence_json: str) -> dict[str, object]:
    """A raw S15 row, shaped like ``mapping.mapping_edges()`` returns."""
    return {
        "mapping_edge_id": edge_id,
        "subject_kind": "device",
        "subject_id": "dev",
        "endpoint_id": "reader",
        "observability": "direct",
        "lifecycle": "confirmed",
        "declared_by": "migration",
        "evidence_json": evidence_json,
        "valid_from": NOW,
        "valid_until": None,
    }


def _clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _service(db: Database) -> VersionService:
    return VersionService(
        topology_repo=db.repos.topology_v3,
        mapping_repo=db.repos.mapping,
        clock=_clock,
        model_set_id="mset-test",
    )


# --------------------------------------------------------------------------- #
# The contract still forbids extras: the tolerance is only at the row boundary.
# --------------------------------------------------------------------------- #


def test_contract_still_rejects_a_legacy_source_key() -> None:
    """``extra="forbid"`` is untouched -- the fix must not be a global loosening."""
    with pytest.raises(ValidationError) as raised:
        EdgeEvidence(**{"source": "ct_channels.paired_ct_id"})
    assert "Extra inputs are not permitted" in str(raised.value)


# --------------------------------------------------------------------------- #
# Read-side tolerance, per legacy source, through the real contract builders.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", LEGACY_TOPOLOGY_SOURCES)
def test_stored_topology_edge_with_legacy_source_loads(source: str) -> None:
    raw = json.dumps({"source": source, "support_w": 0.0, "conflict_w": 0.0})
    edge = _edge_contract(_topology_row("e-1", raw))
    assert isinstance(edge, TopologyEdgeV3)
    assert edge.evidence.support_w == 0.0
    assert edge.evidence.conflict_w == 0.0


def test_unknown_keys_are_dropped_and_valid_values_survive() -> None:
    """Valid keys keep their exact values; only the unknown keys are removed."""
    raw = json.dumps(
        {
            "source": "ct_channels.paired_ct_id",
            "legacy_kind": "ct_measurement_overlap",
            "support": 7,
            "conflict": 2,
            "parent_only": 1,
            "independent_causes": 3,
            "support_w": 4.5,
            "conflict_w": 1.25,
        }
    )
    evidence = _edge_contract(_topology_row("e-1", raw)).evidence
    assert evidence.support == 7
    assert evidence.conflict == 2
    assert evidence.parent_only == 1
    assert evidence.independent_causes == 3
    assert evidence.support_w == pytest.approx(4.5)
    assert evidence.conflict_w == pytest.approx(1.25)
    assert not hasattr(evidence, "source")
    assert not hasattr(evidence, "legacy_kind")


def test_decoder_reads_the_allowed_keys_from_the_model() -> None:
    """The allow-list is ``EdgeEvidence.model_fields``, so it cannot rot."""
    allowed = set(EdgeEvidence.model_fields)
    payload = {name: None for name in allowed} | {"source": "legacy", "nope": 1}
    assert set(edge_evidence_payload(payload)) == allowed


def test_decoder_tolerates_non_dict_and_unparseable_json() -> None:
    assert edge_evidence_payload("{not json") == {}
    assert edge_evidence_payload("[]") == {}
    assert edge_evidence_payload(None) == {}
    assert edge_evidence_payload("") == {}
    # A clean dict is returned with its values, unmodified.
    assert edge_evidence_payload({"support": 2, "support_w": 1.5}) == {
        "support": 2,
        "support_w": 1.5,
    }


def test_decoder_rejects_an_evidence_object_carrying_extras() -> None:
    """A contract object is already validated: nothing to strip, nothing lost."""
    assert edge_evidence_payload(EdgeEvidence(support=3, support_w=1.5)) == {
        "support": 3,
        "support_w": 1.5,
    }


# --------------------------------------------------------------------------- #
# The outage path itself: a real database and ``topology_view``.
# --------------------------------------------------------------------------- #


def test_topology_view_loads_a_database_with_legacy_evidence(tmp_path: Path) -> None:
    """``EngineCore.start`` -> ``_wire_v3_services`` -> ``topology_view``.

    This is the exact call the crash trace pointed at.  Before the fix it raised
    ``ValidationError`` and the engine never started.
    """
    db = Database(tmp_path / "legacy.db")
    try:
        conn = db._conn
        edge_ids = []
        for index, source in enumerate(LEGACY_TOPOLOGY_SOURCES):
            edge_id = f"e-{index}"
            edge_ids.append(edge_id)
            conn.execute(
                "INSERT INTO topology_edges(edge_id, relation, source_endpoint_id, "
                "target_endpoint_id, lifecycle, declared_by, beta, beta_basis, "
                "evidence_json, valid_from, pinned) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    edge_id, "contains", "parent", edge_id, "structural", "migration",
                    1.0, "unity",
                    json.dumps({"source": source, "support_w": 0.0, "conflict_w": 0.0}),
                    NOW, 0,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO topology_versions_v3(version_id, number, "
            "parent_version_id, edge_ids_json, status, change_event_id, created_at, "
            "digest, metadata_json) VALUES('topo-legacy', 900, NULL, ?, 'active', "
            "NULL, ?, 'x', '{}')",
            (json.dumps(sorted(edge_ids)), NOW),
        )
        conn.execute(
            "UPDATE topology_versions_v3 SET status='superseded' "
            "WHERE version_id != 'topo-legacy'"
        )
        conn.commit()

        view = _service(db).topology_view("topo-legacy")
        assert sorted(edge.edge_id for edge in view.edges()) == sorted(edge_ids)
        for edge in view.edges():
            assert edge.evidence.support_w == 0.0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The mapping path had exactly the same exposure (migration 15).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source",
    ("branch_ct_ids", "monitored_by_ct_ids", "auto_placement", "association"),
)
def test_stored_mapping_edge_with_legacy_source_loads(source: str) -> None:
    """Migration 15 wrote ``{"source": <legacy mapping source>}``; same crash."""
    edge = _map_edge_contract(_mapping_row("m-1", json.dumps({"source": source})))
    assert edge.mapping_edge_id == "m-1"
    assert edge.evidence.support == 0


def test_mapping_view_loads_a_database_with_legacy_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy-map.db")
    try:
        conn = db._conn
        conn.execute(
            "INSERT INTO mapping_edges(mapping_edge_id, subject_kind, subject_id, "
            "endpoint_id, observability, lifecycle, declared_by, evidence_json, "
            "valid_from) VALUES('m-legacy', 'device', 'dev', 'reader', 'direct', "
            "'confirmed', 'migration', ?, ?)",
            (json.dumps({"source": "branch_ct_ids", "support": 2}), NOW),
        )
        conn.execute(
            "INSERT OR REPLACE INTO mapping_versions(version_id, number, "
            "parent_version_id, mapping_edge_ids_json, status, change_event_id, "
            "created_at, digest) VALUES('map-legacy', 900, NULL, '[\"m-legacy\"]', "
            "'active', NULL, ?, 'x')",
            (NOW,),
        )
        conn.execute(
            "UPDATE mapping_versions SET status='superseded' "
            "WHERE version_id != 'map-legacy'"
        )
        conn.commit()

        view = _service(db).mapping_view("map-legacy")
        edges = list(view.edges())
        assert [edge.mapping_edge_id for edge in edges] == ["m-legacy"]
        assert edges[0].evidence.support == 2
    finally:
        db.close()
