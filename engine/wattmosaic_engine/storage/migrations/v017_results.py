"""Migration 17: nullable inference results, snapshots and group estimates.

The pre-v17 ``inference_results`` has ``power_w REAL NOT NULL`` and no column
for status, identifiability, bounds, group or the version triple, so the DB
round-trip of an estimate silently loses everything except the number.  SQLite
cannot drop ``NOT NULL`` with ``ALTER TABLE``, so the two affected tables are
rebuilt with create-copy-rename *inside the migration transaction*.

DEC-26's legacy-data rule is applied while rows are copied: a row whose
``provenance_json`` marks ``power_w_is_placeholder`` becomes ``power_w = NULL``
and is flagged ``legacy_semantics='placeholder_zero'``; a ``direct_measured``
row is flagged ``legacy_direct``; everything else is ``legacy_v2``.  No old
number is ever reinterpreted as a validated/MEASURED estimate.

Two new tables are created here: ``inference_snapshots`` (L2, written only on
content change -- the hash is checked by the repository) and
``device_group_estimates`` (L1).  Their tiers are declared below so
``storage/tiers.py`` picks them up without an edit of its own.

Everything lives in the ``apply`` callable rather than ``statements`` so that
calling it twice is harmless: each rebuild checks for its own marker column
and the new tables use ``IF NOT EXISTS``.  An interrupted run rolls back to the
previous ``user_version`` because ``apply_migrations`` wraps it in
``BEGIN IMMEDIATE`` and never auto-commits.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import Migration, table_columns

#: Column order of the rebuilt ``inference_results``.  The first 24 names are
#: the v1-11 order verbatim (``power_w`` loses NOT NULL but keeps its position);
#: the rest are the v17 additions.  ``estimate_json`` keeps a canonical dump of
#: the whole estimate so a reader can reconstruct fields that have no dedicated
#: column (confidence vector, evidence refs, source readers).
INFERENCE_COLUMNS: tuple[str, ...] = (
    "id",
    "device_id",
    "timestamp",
    "power_w",
    "energy_kwh",
    "source",
    "confidence",
    "model_id",
    "model_version",
    "operating_stage",
    "stage_status",
    "sample_count",
    "quality_status",
    "measurement_age_seconds",
    "supporting_ct_channels_json",
    "explanation_json",
    "provenance_json",
    "confidence_tier",
    "monitoring_level",
    "power_behavior",
    "tier_reasons_json",
    "status",
    "identifiability",
    "lower_w",
    "upper_w",
    "bounds_basis",
    "group_id",
    "method",
    "level",
    "topology_version_id",
    "mapping_version_id",
    "model_set_id",
    "provisional",
    "legacy_semantics",
    "estimate_json",
)

#: Column order of the rebuilt ``reconciliation_results``: the v1-11 order
#: (with every watt/term column now nullable) plus ``group_w``.
RECONCILIATION_COLUMNS: tuple[str, ...] = (
    "id",
    "timestamp",
    "topology_version",
    "main_power_w",
    "l1_power_w",
    "l2_power_w",
    "direct_measured_w",
    "inferred_w",
    "model_estimated_w",
    "unknown_residual_w",
    "reconciliation_error_w",
    "reconciliation_error_ratio",
    "accounted_fraction",
    "quality_status",
    "explanation_json",
    "unmonitored_w",
    "meter_disagreement_w",
    "meter_spread_w",
    "meter_spread_exceeds_residual",
    "configured_prior_w",
    "measurement_uncertainty_w",
    "topology_confirmed",
    "group_w",
)

_INFERENCE_DDL = """
CREATE TABLE inference_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    power_w REAL,
    energy_kwh REAL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    model_id TEXT,
    model_version INTEGER,
    operating_stage TEXT,
    stage_status TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL,
    measurement_age_seconds REAL NOT NULL DEFAULT 0,
    supporting_ct_channels_json TEXT NOT NULL DEFAULT '[]',
    explanation_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    confidence_tier TEXT NOT NULL DEFAULT 'tier_0_unknown',
    monitoring_level TEXT NOT NULL DEFAULT 'unknown',
    power_behavior TEXT NOT NULL DEFAULT 'unknown',
    tier_reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT,
    identifiability TEXT,
    lower_w REAL,
    upper_w REAL,
    bounds_basis TEXT,
    group_id TEXT,
    method TEXT,
    level TEXT,
    topology_version_id TEXT,
    mapping_version_id TEXT,
    model_set_id TEXT,
    provisional INTEGER,
    legacy_semantics TEXT,
    estimate_json TEXT
)
"""

_INFERENCE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_inference_device_time ON inference_results(device_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_inference_time ON inference_results(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_inference_tier ON inference_results(confidence_tier)",
)

_RECONCILIATION_DDL = """
CREATE TABLE reconciliation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    topology_version INTEGER NOT NULL,
    main_power_w REAL,
    l1_power_w REAL,
    l2_power_w REAL,
    direct_measured_w REAL,
    inferred_w REAL,
    model_estimated_w REAL,
    unknown_residual_w REAL,
    reconciliation_error_w REAL,
    reconciliation_error_ratio REAL,
    accounted_fraction REAL,
    quality_status TEXT NOT NULL,
    explanation_json TEXT NOT NULL DEFAULT '[]',
    unmonitored_w REAL,
    meter_disagreement_w REAL,
    meter_spread_w REAL,
    meter_spread_exceeds_residual INTEGER,
    configured_prior_w REAL,
    measurement_uncertainty_w REAL,
    topology_confirmed INTEGER,
    group_w REAL
)
"""

_RECONCILIATION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_reconciliation_time ON reconciliation_results(timestamp)"
)

_NEW_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS inference_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id TEXT NOT NULL UNIQUE,
        observed_at TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        topology_version_id TEXT,
        mapping_version_id TEXT,
        model_set_id TEXT,
        content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inference_snapshots_hash ON inference_snapshots(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_inference_snapshots_time ON inference_snapshots(computed_at)",
    """
    CREATE TABLE IF NOT EXISTS device_group_estimates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        at TEXT NOT NULL,
        point_w REAL,
        lower_w REAL,
        upper_w REAL,
        bounds_basis TEXT,
        identifiability TEXT,
        member_ids_json TEXT NOT NULL DEFAULT '[]',
        source_reader_ids_json TEXT NOT NULL DEFAULT '[]',
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        topology_version_id TEXT,
        mapping_version_id TEXT,
        model_set_id TEXT,
        estimate_json TEXT,
        UNIQUE(group_id, at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_estimates_group_time ON device_group_estimates(group_id, at)",
)


def _load_json(text: Any) -> dict[str, Any]:
    """Decode a JSON object column, tolerating NULL, garbage and non-objects."""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_columns_ordered(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _rebuild_inference_results(conn: sqlite3.Connection) -> None:
    """Create-copy-rename ``inference_results`` with nullable power + new columns.

    Idempotent: a database that already carries ``legacy_semantics`` (the marker
    column of this rebuild) is left alone, which covers both a second direct
    ``apply`` call and a run interrupted after the copy but before the version
    bump.
    """
    if "legacy_semantics" in table_columns(conn, "inference_results"):
        return
    if not table_columns(conn, "inference_results"):
        # A database that never created the legacy table (e.g. an empty file)
        # still gets the final shape.
        conn.execute(_INFERENCE_DDL)
        for statement in _INFERENCE_INDEXES:
            conn.execute(statement)
        return

    old_table = "inference_results_v016"
    conn.execute(f"ALTER TABLE inference_results RENAME TO {old_table}")
    conn.execute(_INFERENCE_DDL)
    old_columns = _table_columns_ordered(conn, old_table)
    rows = conn.execute(
        f"SELECT {', '.join(old_columns)} FROM {old_table}"  # noqa: S608 - names from PRAGMA
    ).fetchall()
    copied_columns = [*old_columns, "legacy_semantics"]
    markers = ", ".join("?" for _ in copied_columns)
    insert = (
        f"INSERT INTO inference_results({', '.join(copied_columns)}) VALUES({markers})"  # noqa: S608
    )
    payload: list[list[Any]] = []
    for row in rows:
        values = dict(zip(old_columns, row, strict=False))
        provenance = _load_json(values.get("provenance_json"))
        if provenance.get("power_w_is_placeholder") is True:
            values["power_w"] = None
            legacy = "placeholder_zero"
        elif values.get("source") == "direct_measured":
            legacy = "legacy_direct"
        else:
            legacy = "legacy_v2"
        values["legacy_semantics"] = legacy
        payload.append([values.get(column) for column in copied_columns])
    conn.executemany(insert, payload)
    conn.execute(f"DROP TABLE {old_table}")
    for statement in _INFERENCE_INDEXES:
        conn.execute(statement)


def _rebuild_reconciliation_results(conn: sqlite3.Connection) -> None:
    """Make every watt/term nullable and add ``group_w``, preserving all rows."""
    if "group_w" in table_columns(conn, "reconciliation_results"):
        return
    if not table_columns(conn, "reconciliation_results"):
        conn.execute(_RECONCILIATION_DDL)
        conn.execute(_RECONCILIATION_INDEX)
        return

    old_table = "reconciliation_results_v016"
    conn.execute(f"ALTER TABLE reconciliation_results RENAME TO {old_table}")
    conn.execute(_RECONCILIATION_DDL)
    old_columns = _table_columns_ordered(conn, old_table)
    conn.execute(
        f"INSERT INTO reconciliation_results({', '.join(old_columns)}) "  # noqa: S608
        f"SELECT {', '.join(old_columns)} FROM {old_table}"  # noqa: S608
    )
    conn.execute(f"DROP TABLE {old_table}")
    conn.execute(_RECONCILIATION_INDEX)


def _apply(conn: sqlite3.Connection) -> None:
    for statement in _NEW_TABLE_STATEMENTS:
        conn.execute(statement)
    _rebuild_inference_results(conn)
    _rebuild_reconciliation_results(conn)


#: Declared here so ``storage/tiers.py`` classifies the new tables without an
#: edit of its own.  The two rebuilt tables are already classified as L1 in the
#: base registry, so declaring them again would be a duplicate.
TIERS: dict[str, str] = {
    "inference_snapshots": "L2",
    "device_group_estimates": "L1",
}

MIGRATION = Migration(
    version=17,
    name="v017_results",
    statements=(),
    apply=_apply,
)

__all__ = [
    "INFERENCE_COLUMNS",
    "MIGRATION",
    "RECONCILIATION_COLUMNS",
    "TIERS",
]
