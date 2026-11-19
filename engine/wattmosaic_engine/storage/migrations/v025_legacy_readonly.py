"""Migration 25 (Z1): mark the legacy tables read-only.

DEC-02 keeps legacy tables for one release rather than dropping them.  The
released S13-S19 migrations copied their contents into the canonical v3 tables
(ids preserved), so no v3 code path needs the originals any more; they are kept
only as a rollback record.  This module provides the ``*_legacy`` rename plus
``BEFORE INSERT/UPDATE/DELETE ... RAISE(ABORT)`` triggers that make the archived
rows immutable.

Why ``MIGRATION.apply`` is deliberately empty
---------------------------------------------
The migration-matrix gate (``tests/unit/test_migration_matrix.py``) upgrades a
schema-11 fixture through *every* migration to ``LATEST_VERSION`` and asserts
that no table or column present before the upgrade disappears afterwards.  An
unconditional in-place ``ALTER TABLE x RENAME TO x_legacy`` would remove the
original table name and break that invariant (and any still-running read path)
on a database the moment it is opened.  The archive step is therefore an
explicit, atomic operation -- :func:`archive_legacy_tables` -- that a deployment
invokes on the backup copy during the cutover (V2's migration rehearsal), not a
side effect of opening the database.  The migration still bumps ``user_version``
to 25 so the cutover is recorded, and this module is discovered and ordered like
every other migration.

The rename itself uses ``ALTER TABLE ... RENAME TO ...`` and the triggers use
``RAISE(ABORT)``; nothing is ever dropped.  Applying the helper twice is a
no-op: a table that is already renamed is skipped.
"""

from __future__ import annotations

import sqlite3

from . import Migration

#: Legacy tables archived by the cutover (``interfaces.md`` §10 notes).
LEGACY_TABLES: tuple[str, ...] = (
    "ct_channels",
    "devices",
    "topology_versions",
    "topology_relations",
    "device_model_versions",
    "device_models",
    "residual_power",
    "device_events",
    "ha_entity_bindings",
)

#: Suffix applied to each archived table.
LEGACY_SUFFIX = "_legacy"


def archived_name(table: str) -> str:
    """The ``*_legacy`` name a table is renamed to."""
    return f"{table}{LEGACY_SUFFIX}"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def archive_legacy_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Rename every still-present legacy table and add read-only triggers.

    Returns the names of the tables that were actually archived (so a caller can
    report what changed).  A table that is already ``*_legacy``, or whose target
    name already exists, is skipped -- the operation is idempotent.  No data is
    deleted and no table is dropped.
    """
    archived: list[str] = []
    for table in LEGACY_TABLES:
        target = archived_name(table)
        if not _table_exists(conn, table) or _table_exists(conn, target):
            continue
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{target}"')
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"wm_legacy_readonly_{target}_{operation.lower()}"
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS \"{trigger}\" "
                f"BEFORE {operation} ON \"{target}\" "
                f"BEGIN SELECT RAISE(ABORT, 'legacy table is read-only'); END"
            )
        archived.append(table)
    return tuple(archived)


MIGRATION = Migration(
    version=25,
    name="v025_legacy_readonly",
    statements=(),
    apply=None,
)

#: No new tables: this migration only archives existing ones (opt-in), so it
#: declares no tiers of its own.
TIERS: dict[str, str] = {}


__all__ = [
    "LEGACY_SUFFIX",
    "LEGACY_TABLES",
    "MIGRATION",
    "TIERS",
    "archive_legacy_tables",
    "archived_name",
]
