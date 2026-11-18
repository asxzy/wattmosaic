"""Migration 15: mapping edges/versions and the sparse matrix cell store (S15).

Rule 20 of the real-time spec keeps the subject↔reader mapping independent of
topology.  This migration separates the mapping that used to live inside the
declared device tree (``DeviceTopology.branch_ct_ids`` and
``DeviceConfiguration.monitored_by_ct_ids``) and the device↔CT association
relations into their own ``mapping_edges`` table, with an append-only
``mapping_versions`` snapshot ("map-1" on an upgraded install).

It also gives the matrix a persisted sparse current view (``matrix_cells``) and
an append-only history (``matrix_cell_versions``) so the matrix can be updated
incrementally and queried as of a time (HB §9.8–9.9, ``analysis/E-…`` C6).

The legacy scope is never rewritten.  Every statement uses ``IF NOT EXISTS`` and
every backfill insert uses ``INSERT OR IGNORE`` with a *deterministic* id, so
running ``apply`` twice is a true no-op -- including the "map-1" version whose
edge-id list is derived from the deterministic edge ids.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ..payload import decode_payload
from . import Migration

#: Storage tier of every table this migration creates (plan §5).  All four are
#: canonical-model state, so the tier-completeness test folds them into L4.
TIERS: dict[str, str] = {
    "mapping_edges": "L4",
    "mapping_versions": "L4",
    "matrix_cells": "L4",
    "matrix_cell_versions": "L4",
}

#: Stable fallback instant when a source row carries no usable timestamp.  Using
#: a constant (rather than ``utcnow()``) keeps a second application byte-identical.
_FALLBACK_TIME = "2026-01-01T00:00:00+00:00"

#: The version id backfilled for the legacy mapping state (DEC-09: "map-<n>").
_INITIAL_VERSION_ID = "map-1"
_INITIAL_VERSION_NUMBER = 1

#: Observability of every backfilled edge is INFERRED (§9.7): none of the legacy
#: sources was a direct endpoint measurement asserted with HB vocabulary.
_INFERRED = "inferred"

#: Verdicts recorded on a DEVICE_CT_ASSOCIATION relation map to the v3 edge
#: lifecycle.  ``supported`` is corroborated, ``single_event`` is only
#: PROVISIONAL (one event never confirms a relation), ``split`` is CONFLICTED and
#: ``ambiguous`` stays a CANDIDATE.  A relation that carries ``valid_to`` is
#: RETIRED regardless of its verdict (handled by :func:`_verdict_lifecycle`).
_VERDICT_LIFECYCLE = {
    "supported": "confirmed",
    "single_event": "provisional",
    "split": "conflicted",
    "ambiguous": "candidate",
}

#: Source priority when the same (subject, endpoint) is declared more than once.
#: A user declaration outranks an auto placement, which outranks an association.
_PRIORITY = {"branch": 0, "config_user": 1, "config_auto": 2, "association": 3}

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mapping_edges (
        mapping_edge_id TEXT PRIMARY KEY,
        subject_kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        observability TEXT NOT NULL,
        lifecycle TEXT NOT NULL,
        declared_by TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        valid_from TEXT NOT NULL,
        valid_until TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_map_edges_subject
    ON mapping_edges(subject_kind, subject_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_map_edges_endpoint ON mapping_edges(endpoint_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS mapping_versions (
        version_id TEXT PRIMARY KEY,
        number INTEGER NOT NULL,
        parent_version_id TEXT,
        mapping_edge_ids_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        change_event_id TEXT,
        created_at TEXT NOT NULL,
        digest TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_map_versions_number ON mapping_versions(number)
    """,
    """
    CREATE TABLE IF NOT EXISTS matrix_cells (
        row_kind TEXT NOT NULL,
        row_id TEXT NOT NULL,
        reader_id TEXT NOT NULL,
        load_component_id TEXT,
        mapping_json TEXT NOT NULL DEFAULT '{}',
        power_json TEXT NOT NULL DEFAULT '{}',
        event_association_json TEXT NOT NULL DEFAULT '{}',
        confidence_json TEXT NOT NULL DEFAULT '{}',
        provenance_json TEXT NOT NULL DEFAULT '{}',
        timestamps_json TEXT NOT NULL DEFAULT '{}',
        identifiability TEXT NOT NULL DEFAULT 'insufficient_evidence',
        group_id TEXT,
        visual TEXT NOT NULL DEFAULT 'unknown',
        mapping_status TEXT NOT NULL DEFAULT 'unknown',
        observability TEXT NOT NULL DEFAULT 'unknown',
        updated_at TEXT NOT NULL,
        derived INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (row_kind, row_id, reader_id)
    )
    """,
    # Indexed scalars are the hot filter columns of the bulk matrix read.  The
    # composite index starts with the equality columns so a reader/status filter
    # uses the whole key.
    """
    CREATE INDEX IF NOT EXISTS idx_matrix_cells_reader
    ON matrix_cells(reader_id, mapping_status, visual)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_matrix_cells_status ON matrix_cells(mapping_status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_matrix_cells_observability ON matrix_cells(observability)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_matrix_cells_identifiability ON matrix_cells(identifiability)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_matrix_cells_updated ON matrix_cells(updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS matrix_cell_versions (
        cell_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
        row_kind TEXT NOT NULL,
        row_id TEXT NOT NULL,
        reader_id TEXT NOT NULL,
        load_component_id TEXT,
        mapping_json TEXT NOT NULL DEFAULT '{}',
        power_json TEXT NOT NULL DEFAULT '{}',
        event_association_json TEXT NOT NULL DEFAULT '{}',
        confidence_json TEXT NOT NULL DEFAULT '{}',
        provenance_json TEXT NOT NULL DEFAULT '{}',
        timestamps_json TEXT NOT NULL DEFAULT '{}',
        identifiability TEXT NOT NULL DEFAULT 'insufficient_evidence',
        group_id TEXT,
        visual TEXT NOT NULL DEFAULT 'unknown',
        mapping_status TEXT NOT NULL DEFAULT 'unknown',
        observability TEXT NOT NULL DEFAULT 'unknown',
        updated_at TEXT NOT NULL,
        derived INTEGER NOT NULL DEFAULT 0,
        mapping_version_id TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cell_versions_cell
    ON matrix_cell_versions(row_kind, row_id, reader_id, valid_from)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cell_versions_version
    ON matrix_cell_versions(mapping_version_id)
    """,
)


# --------------------------------------------------------------------------- #
# Tolerant legacy readers
# --------------------------------------------------------------------------- #


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_object(raw: Any) -> dict[str, Any]:
    parsed = decode_payload(raw) if isinstance(raw, str) else None
    return parsed if isinstance(parsed, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _as_text(item))]


def _branch_ct_ids(raw: Any) -> list[str]:
    """``branch_ct_ids_json`` is normally a bare list; tolerate a dict too."""
    parsed = decode_payload(raw) if isinstance(raw, str) else None
    if isinstance(parsed, dict):
        parsed = parsed.get("branch_ct_ids") or parsed.get("branch_ct_ids_json")
    return _string_list(parsed)


def _edge_id(subject_kind: str, subject_id: str, endpoint_id: str) -> str:
    """Deterministic id so a re-run produces the same id for the same edge."""
    canonical = f"{subject_kind}|{subject_id}|{endpoint_id}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"map-edge-{digest}"


def _verdict_lifecycle(verdict: str, valid_to: str | None) -> str:
    """Retired relations carry an end; otherwise map the recorded verdict."""
    if valid_to is not None:
        return "retired"
    return _VERDICT_LIFECYCLE.get(verdict.strip().lower(), "candidate")


def _edge(
    *,
    subject_id: str,
    endpoint_id: str,
    lifecycle: str,
    declared_by: str,
    source: str,
    valid_from: str = _FALLBACK_TIME,
    valid_until: str | None = None,
    subject_kind: str = "device",
) -> dict[str, Any]:
    return {
        "mapping_edge_id": _edge_id(subject_kind, subject_id, endpoint_id),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "endpoint_id": endpoint_id,
        "observability": _INFERRED,
        "lifecycle": lifecycle,
        # Every legacy source is attributed to the migration; the originating
        # declaration is preserved in the evidence ledger for provenance.
        "declared_by": declared_by,
        "evidence_json": json.dumps({"source": source}),
        "valid_from": valid_from,
        "valid_until": valid_until,
    }


# --------------------------------------------------------------------------- #
# Backfill the three mapping sources
# --------------------------------------------------------------------------- #


def _collect_candidates(conn: sqlite3.Connection) -> list[tuple[int, dict[str, Any]]]:
    """Gather mapping-edge candidates, tagged with a source priority."""
    candidates: list[tuple[int, dict[str, Any]]] = []
    device_times: dict[str, str] = {}

    # Source 1: declared branch CT ids on the legacy device tree.  A user
    # declaration becomes INFERRED with lifecycle CONFIRMED.
    if _table_exists(conn, "devices"):
        devices = conn.execute(
            "SELECT device_id, branch_ct_ids_json, updated_at FROM devices ORDER BY device_id"
        ).fetchall()
        for row in devices:
            device_id = _as_text(row[0])
            if device_id is None:
                continue
            device_times[device_id] = _as_text(row[2]) or _FALLBACK_TIME
            for ct_id in _branch_ct_ids(row[1]):
                candidates.append(
                    (
                        _PRIORITY["branch"],
                        _edge(
                            subject_id=device_id,
                            endpoint_id=ct_id,
                            lifecycle="confirmed",
                            declared_by="migration",
                            source="branch_ct_ids",
                            valid_from=device_times[device_id],
                        ),
                    )
                )

    # Source 2: monitored_by_ct_ids on the device configuration payload.  The
    # configured_by field distinguishes a user declaration from an auto
    # placement (which stays PROVISIONAL).
    if _table_exists(conn, "device_configurations"):
        rows = conn.execute(
            "SELECT device_id, payload_json, updated_at FROM device_configurations "
            "ORDER BY device_id"
        ).fetchall()
        for row in rows:
            device_id = _as_text(row[0])
            if device_id is None:
                continue
            payload = _json_object(row[1])
            ct_ids = _string_list(payload.get("monitored_by_ct_ids"))
            if not ct_ids:
                continue
            is_auto = str(payload.get("configured_by") or "").strip().lower() == "auto"
            priority = _PRIORITY["config_auto" if is_auto else "config_user"]
            lifecycle = "provisional" if is_auto else "confirmed"
            source = "auto_placement" if is_auto else "monitored_by_ct_ids"
            valid_from = _as_text(row[2]) or device_times.get(device_id, _FALLBACK_TIME)
            for ct_id in ct_ids:
                candidates.append(
                    (
                        priority,
                        _edge(
                            subject_id=device_id,
                            endpoint_id=ct_id,
                            lifecycle=lifecycle,
                            declared_by="migration",
                            source=source,
                            valid_from=valid_from,
                        ),
                    )
                )

    # Source 3: DEVICE_CT_ASSOCIATION relations.  Keep valid_to as valid_until;
    # such a relation is superseded and becomes RETIRED.
    if _table_exists(conn, "topology_relations"):
        rows = conn.execute(
            "SELECT subject_id, object_id, evidence_json, valid_from, valid_to "
            "FROM topology_relations WHERE kind='DEVICE_CT_ASSOCIATION' "
            "ORDER BY created_at, relation_id"
        ).fetchall()
        for row in rows:
            device_id, ct_id = _as_text(row[0]), _as_text(row[1])
            if device_id is None or ct_id is None:
                continue
            evidence = _json_object(row[2])
            verdict = str(evidence.get("verdict") or "")
            valid_from = _as_text(row[3]) or device_times.get(device_id, _FALLBACK_TIME)
            valid_to = _as_text(row[4])
            candidates.append(
                (
                    _PRIORITY["association"],
                    _edge(
                        subject_id=device_id,
                        endpoint_id=ct_id,
                        lifecycle=_verdict_lifecycle(verdict, valid_to),
                        declared_by="migration",
                        source="association",
                        valid_from=valid_from,
                        valid_until=valid_to,
                    ),
                )
            )
    return candidates


def _backfill_edges(conn: sqlite3.Connection) -> list[str]:
    """Insert deduplicated edges; return the sorted ids for the version snapshot."""
    # Deduplicate by (subject_kind, subject_id, endpoint_id), keeping the
    # highest-priority source.  A stable sort makes ties deterministic.
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for _, edge in sorted(_collect_candidates(conn), key=lambda pair: pair[0]):
        key = (edge["subject_kind"], edge["subject_id"], edge["endpoint_id"])
        if key not in best:
            best[key] = edge
    for edge in sorted(best.values(), key=lambda e: e["mapping_edge_id"]):
        conn.execute(
            """INSERT OR IGNORE INTO mapping_edges(
                mapping_edge_id, subject_kind, subject_id, endpoint_id, observability,
                lifecycle, declared_by, evidence_json, valid_from, valid_until)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                edge["mapping_edge_id"],
                edge["subject_kind"],
                edge["subject_id"],
                edge["endpoint_id"],
                edge["observability"],
                edge["lifecycle"],
                edge["declared_by"],
                edge["evidence_json"],
                edge["valid_from"],
                edge["valid_until"],
            ),
        )
    return sorted(edge["mapping_edge_id"] for edge in best.values())


def _backfill_version(conn: sqlite3.Connection, edge_ids: list[str]) -> None:
    """Create the single "map-1" snapshot of the backfilled edge set."""
    ids_json = json.dumps(edge_ids)
    digest = hashlib.sha256(ids_json.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO mapping_versions(
            version_id, number, parent_version_id, mapping_edge_ids_json, status,
            change_event_id, created_at, digest)
        VALUES(?,?,?,?,?,?,?,?)""",
        (
            _INITIAL_VERSION_ID,
            _INITIAL_VERSION_NUMBER,
            None,
            ids_json,
            "active",
            None,
            _FALLBACK_TIME,
            digest,
        ),
    )


def _apply(conn: sqlite3.Connection) -> None:
    """Backfill inside the migration transaction (no auto-commit statements)."""
    edge_ids = _backfill_edges(conn)
    _backfill_version(conn, edge_ids)


MIGRATION = Migration(
    version=15,
    name="mapping_and_cells",
    statements=_STATEMENTS,
    apply=_apply,
)

__all__ = ["MIGRATION", "TIERS"]
