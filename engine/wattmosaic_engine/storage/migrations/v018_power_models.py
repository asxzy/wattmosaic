"""Migration 18 (S18): power models v2, model events and learning observations.

The legacy ``device_model_versions`` table is absolute-basis and its numbers were
credited to the whole supporting circuit (``analysis/F-…`` §1, C1/C3).  HB
§16.6-7 / DEC-31 forbid reinterpreting those numbers as validated power, so this
migration copies every legacy row into an append-only
``power_model_versions_v3`` with ``basis='LEGACY_ABSOLUTE'`` and a tagged
``{"kind": "legacy", "parameters": <old json>}`` spec, and logs one
``superseded`` / ``legacy_absolute`` event for it.  **No legacy copy is active:**
the status-event log is the only source of "active", and only ``activated``
events activate (DEC-31).  The old table is left untouched (Z1/v25 renames it).

The three new tables are all L4 (versioned model state / deterministic
observations, ``interfaces.md`` §10).  Everything -- DDL and backfill -- lives in
the ``apply`` callable so that calling it twice directly is a true no-op and a
failure mid-apply rolls back to the previous ``user_version``: every ``CREATE``
uses ``IF NOT EXISTS``, every version insert is ``INSERT OR IGNORE`` on its
primary key, and an event is inserted only when its exact row is absent.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from . import Migration

#: Stable fallback instant for legacy rows with a missing/uncorrupt timestamp.
#: A constant (rather than ``utcnow()``) keeps a second application identical.
_FALLBACK_TIME = "2026-01-01T00:00:00+00:00"

#: Legacy ``ModelType`` -> v3 ``ModelClass`` (K06).  The legacy taxonomy names
#: implementations; the v3 class is the declared load shape, so several legacy
#: types collapse onto one class.  Unknown values fall back to FIXED.
_LEGACY_MODEL_CLASS: dict[str, str] = {
    "binary": "FIXED",
    "residual": "FIXED",
    "dimmable": "ADJUSTABLE",
    "variable_speed": "ADJUSTABLE",
    "multi_state": "DYNAMIC",
    "time_varying": "DYNAMIC",
    "composite": "COMPONENT_SUM",
}

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS power_model_versions_v3 (
        version_id TEXT PRIMARY KEY,
        subject_kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        number INTEGER NOT NULL,
        model_class TEXT NOT NULL,
        basis TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        topology_version_at_training TEXT,
        trainer_version TEXT NOT NULL,
        parent_version_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (subject_kind, subject_id, number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_power_model_versions_subject "
    "ON power_model_versions_v3(subject_kind, subject_id, number)",
    """
    CREATE TABLE IF NOT EXISTS power_model_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id TEXT NOT NULL,
        event TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_power_model_events_version_time "
    "ON power_model_events(version_id, at, id)",
    """
    CREATE TABLE IF NOT EXISTS learning_observations (
        observation_id TEXT PRIMARY KEY,
        subject_kind TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        event_group_ref_json TEXT NOT NULL,
        transition TEXT NOT NULL,
        attributes_json TEXT NOT NULL DEFAULT '{}',
        delta_w REAL,
        level_w REAL,
        status TEXT NOT NULL,
        rejection_reason TEXT,
        identifiability TEXT NOT NULL,
        basis TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_learning_observations_subject "
    "ON learning_observations(subject_kind, subject_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_learning_observations_status_time "
    "ON learning_observations(status, created_at)",
)

#: Storage tier of every table created here (interfaces.md §10).
TIERS: dict[str, str] = {
    "power_model_versions_v3": "L4",
    "power_model_events": "L4",
    "learning_observations": "L4",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timestamp(value: Any) -> str:
    """Normalise a stored timestamp, with a stable fallback (DEC-31 copies)."""
    return _text(value) or _FALLBACK_TIME


def _json_object(raw: Any) -> dict[str, Any]:
    """Parse a JSON object cell; never raise on legacy/corrupt content."""
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _legacy_event_present(conn: sqlite3.Connection, version_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM power_model_events "
        "WHERE version_id=? AND event='superseded' AND reason='legacy_absolute' LIMIT 1",
        (version_id,),
    ).fetchone()
    return row is not None


def _backfill_legacy_models(conn: sqlite3.Connection) -> None:
    """Copy each ``device_model_versions`` row as a read-only legacy version.

    Legacy identity is ``(device, model_type, version)`` while v3 identity is
    ``(subject_kind, subject_id, number)``: a device may have had several model
    types with the same version number, so a second occurrence on one device is
    assigned the next free number.  The allocation depends only on the legacy
    rows (in a fixed order), so a repeated apply computes the same version ids.
    """
    if not _table_exists(conn, "device_model_versions"):
        return
    rows = conn.execute(
        """SELECT device_id, model_type, version, created_at, parameters_json
           FROM device_model_versions ORDER BY device_id, model_type, version"""
    ).fetchall()

    used: dict[str, set[int]] = defaultdict(set)
    insert_version = (
        """INSERT OR IGNORE INTO power_model_versions_v3(
            version_id, subject_kind, subject_id, number, model_class, basis,
            spec_json, schema_version, evidence_refs_json, topology_version_at_training,
            trainer_version, parent_version_id, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    )
    insert_event = (
        "INSERT INTO power_model_events(version_id, event, reason, at) VALUES(?,?,?,?)"
    )

    for row in rows:
        device_id = _text(row[0])
        if device_id is None:
            continue
        try:
            legacy_number = int(row[2])
        except (TypeError, ValueError):
            legacy_number = 1

        number = legacy_number
        while number in used[device_id]:
            number += 1
        used[device_id].add(number)

        version_id = f"mdl-{device_id}-{number}"
        created_at = _timestamp(row[3])
        model_type = _text(row[1]) or ""
        parameters = _json_object(row[4])
        spec = {"kind": "legacy", "parameters": parameters}
        conn.execute(
            insert_version,
            (
                version_id,
                "device",
                device_id,
                number,
                _LEGACY_MODEL_CLASS.get(model_type, "FIXED"),
                "LEGACY_ABSOLUTE",
                json.dumps(spec, default=str, sort_keys=True),
                1,  # SPEC_SCHEMA_VERSION
                "[]",
                None,
                "legacy",
                None,
                created_at,
            ),
        )
        # One `superseded` event per legacy copy: no legacy model is active
        # after migration.  The existence check makes this idempotent even
        # though `power_model_events` has an autoincrement id (no natural PK).
        if not _legacy_event_present(conn, version_id):
            conn.execute(
                insert_event, (version_id, "superseded", "legacy_absolute", created_at)
            )


def _apply(conn: sqlite3.Connection) -> None:
    """Create the three tables (idempotently) and copy the legacy models."""
    for statement in _STATEMENTS:
        conn.execute(statement)
    _backfill_legacy_models(conn)


MIGRATION = Migration(
    version=18,
    name="v018_power_models",
    statements=(),
    apply=_apply,
)

__all__ = ["MIGRATION", "TIERS"]
