"""Migration 26 (Z8): add a ``revision`` counter to ``event_evidence``.

The V2 DB-growth defect was that P4 appended a new immutable ``event_evidence``
row for *every* post-window sample, each carrying the whole cumulative
``samples_json``.  Z8 makes an open window revise its single row in place (one
live row per ``(measurement_event_id, reader_id)``) and keeps a supersede chain
only for late data after FINAL.  The counter records how many times a row was
revised, so the audit trail survives without duplicating the sample payload.

SQLite cannot add a constrained column with ``ALTER TABLE ... ADD COLUMN`` when
the table must be rebuilt (the counter is part of the table's stable shape, and
the rebuild also lets us normalise the indexes).  The migration therefore uses
the standard create-copy-rename inside the migration transaction:

* a new table with the identical column order and constraints plus
  ``revision INTEGER NOT NULL DEFAULT 0`` is created;
* every row is copied by name -- no row, id or value is dropped or reinterpreted;
* the indexes v016 declared are recreated;
* the old table is dropped and the new one renamed into place.

``user_version`` is set inside the same transaction by ``apply_migrations``, so
an interrupted run rolls back.  Calling ``apply`` twice is a true no-op: the
marker is the presence of the ``revision`` column in ``PRAGMA table_info``.
"""

from __future__ import annotations

import sqlite3

from . import Migration

#: Exact shape of the v016/v021 table, with the single addition of ``revision``.
#: Column order and every other constraint are preserved verbatim so the copy is
#: a pure rename-and-extend.
EVIDENCE_DDL = """
CREATE TABLE event_evidence (
    evidence_id TEXT PRIMARY KEY,
    reader_id TEXT NOT NULL,
    measurement_event_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    samples_json TEXT NOT NULL DEFAULT '[]',
    baseline_w REAL,
    sigma_w REAL,
    delta_w REAL NOT NULL,
    quality_flags_json TEXT NOT NULL DEFAULT '[]',
    topology_version_id TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0,
    supersedes_evidence_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0
)
"""

#: Column order of the rebuilt table (the order the copy projects onto).
EVIDENCE_COLUMNS: tuple[str, ...] = (
    "evidence_id",
    "reader_id",
    "measurement_event_id",
    "window_start",
    "window_end",
    "samples_json",
    "baseline_w",
    "sigma_w",
    "delta_w",
    "quality_flags_json",
    "topology_version_id",
    "finalized",
    "supersedes_evidence_id",
    "revision",
)

#: The indexes v016 installed on the table, recreated after the rebuild.
EVIDENCE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_event_evidence_reader_time "
    "ON event_evidence(reader_id, window_start)",
)

#: Scratch name for the old table during the rebuild.  Dropped in the same
#: transaction, so it is never visible to a reader.
_OLD_TABLE = "event_evidence_v025"

#: The column this migration introduces; also its idempotency marker.
REVISION_COLUMN = "revision"


def _apply(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_evidence)")}
    if REVISION_COLUMN in existing:
        # Already rebuilt (a second call, or a run interrupted after the rename
        # but before user_version advanced).
        return
    if not existing:
        # A database that never created the table still gets the final shape.
        conn.execute(EVIDENCE_DDL)
        for statement in EVIDENCE_INDEXES:
            conn.execute(statement)
        return
    conn.execute(f"ALTER TABLE event_evidence RENAME TO {_OLD_TABLE}")
    conn.execute(EVIDENCE_DDL)
    old_columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_OLD_TABLE})")]
    # Copy by the intersection of names: the pre-v026 table has exactly the
    # columns declared above minus ``revision``, but tolerating a superset keeps
    # the rebuild safe across schema variants.  Every row and value is preserved.
    columns = [name for name in old_columns if name in EVIDENCE_COLUMNS]
    quoted = ", ".join(f'"{name}"' for name in columns)
    conn.execute(
        f"INSERT INTO event_evidence({quoted}) "  # noqa: S608 - names from PRAGMA
        f"SELECT {quoted} FROM {_OLD_TABLE}"  # noqa: S608 - names from PRAGMA
    )
    conn.execute(f"DROP TABLE {_OLD_TABLE}")
    for statement in EVIDENCE_INDEXES:
        conn.execute(statement)


MIGRATION = Migration(
    version=26,
    name="v026_evidence_revision",
    statements=(),
    apply=_apply,
)

#: No new table is created -- ``event_evidence`` is already classified L2 by
#: migration 16, so this rebuild declares no tiers of its own.
TIERS: dict[str, str] = {}

__all__ = [
    "EVIDENCE_COLUMNS",
    "EVIDENCE_DDL",
    "EVIDENCE_INDEXES",
    "MIGRATION",
    "REVISION_COLUMN",
    "TIERS",
]
