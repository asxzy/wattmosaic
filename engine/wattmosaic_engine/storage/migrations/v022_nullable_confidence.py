"""Migration 22 (ZF): make ``inference_results.confidence`` nullable.

Migration 17 rebuilt ``inference_results`` with ``confidence REAL NOT NULL``
because the legacy write path always had a scalar confidence.  The v3 estimate
writer (:meth:`InferenceRepository.put_estimates`) reuses that column for the
``identifiability`` component of a :class:`ConfidenceVectorV3`, but that
component is optional and legitimately ``None`` when it was not assessed (HB
§18: absent is ``None``, never ``0``).  A valid estimate with no identifiability
score therefore raises ``IntegrityError`` on the NOT NULL column.

SQLite has no ``ALTER TABLE ... ALTER COLUMN``, so the table is rebuilt with
create-copy-rename *inside the migration transaction*:

* a new table with the identical column order and constraints except that
  ``confidence`` loses ``NOT NULL`` is created;
* every row is copied by name (no column is dropped, reordered or retyped);
* the three indexes v017 declared are recreated;
* the old table is dropped and the new one renamed into place.

The same transaction sets ``user_version`` (see ``apply_migrations``), so an
interrupted run rolls back to the previous version.  Calling ``apply`` twice is
a true no-op: the marker is the ``notnull`` flag of ``confidence`` in
``PRAGMA table_info`` -- once the column is nullable the rebuild is skipped.
"""

from __future__ import annotations

import sqlite3

from . import Migration
from .v017_results import INFERENCE_COLUMNS

#: Exact shape of the v017 table, with the single change that ``confidence`` is
#: nullable.  Column order and every other constraint are preserved verbatim so
#: the copy below is a pure rename-and-relax.
_INFERENCE_DDL = """
CREATE TABLE inference_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    power_w REAL,
    energy_kwh REAL,
    source TEXT NOT NULL,
    confidence REAL,
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

#: The same indexes v017 installed on the table; recreated after the rebuild so
#: no index is lost (plan §5: "preserving all rows, columns and indexes").
_INFERENCE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_inference_device_time ON inference_results(device_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_inference_time ON inference_results(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_inference_tier ON inference_results(confidence_tier)",
)

#: Scratch name for the old table during the rebuild.  It is dropped in the same
#: transaction, so it is never visible to a reader.
_OLD_TABLE = "inference_results_v021"


def _confidence_not_null(conn: sqlite3.Connection) -> bool | None:
    """Whether ``confidence`` currently carries NOT NULL; ``None`` if absent."""
    for row in conn.execute("PRAGMA table_info(inference_results)").fetchall():
        if str(row[1]) == "confidence":
            return bool(row[3])
    return None


def _apply(conn: sqlite3.Connection) -> None:
    not_null = _confidence_not_null(conn)
    if not_null is False:
        # Already rebuilt (a second call, or a run interrupted after the rename
        # but before user_version advanced).
        return
    if not_null is None:
        # A database that never created the table still gets the final shape.
        conn.execute(_INFERENCE_DDL)
    else:
        conn.execute(f"ALTER TABLE inference_results RENAME TO {_OLD_TABLE}")
        conn.execute(_INFERENCE_DDL)
        old_columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_OLD_TABLE})").fetchall()]
        # Copy by the intersection of names: the v017 table has exactly the
        # columns declared above, but tolerating a superset keeps the rebuild
        # safe across schema variants.  No row is ever reinterpreted.
        columns = [name for name in old_columns if name in INFERENCE_COLUMNS]
        quoted = ", ".join(f'"{name}"' for name in columns)
        conn.execute(
            f"INSERT INTO inference_results({quoted}) "  # noqa: S608 - names from PRAGMA
            f"SELECT {quoted} FROM {_OLD_TABLE}"  # noqa: S608 - names from PRAGMA
        )
        conn.execute(f"DROP TABLE {_OLD_TABLE}")
    for statement in _INFERENCE_INDEXES:
        conn.execute(statement)


MIGRATION = Migration(
    version=22,
    name="v022_nullable_confidence",
    statements=(),
    apply=_apply,
)

#: No new table is created -- ``inference_results`` is already classified L1 by
#: migration 17, so this data-only rebuild declares no tiers of its own.
TIERS: dict[str, str] = {}

__all__ = ["MIGRATION", "TIERS"]
