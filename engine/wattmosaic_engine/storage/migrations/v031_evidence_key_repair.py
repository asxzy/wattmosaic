"""Migration 31: strip non-contract keys from stored edge evidence JSON.

**Why.**  ``Model`` (``contracts/foundation.py``) sets ``extra="forbid"``, so an
edge whose ``evidence_json`` carries a key that is not an ``EdgeEvidence`` field
cannot be rebuilt as a contract at all.  Migration 14 backfilled ``topology_edges``
with ``{"source": "ct_channels.parent_ct_id"}`` (plus the ``paired_ct_id``,
``alias_of_ct_id`` and ``topology_relations`` variants), migration 15 did the same
for ``mapping_edges`` (``branch_ct_ids``, ``monitored_by_ct_ids``,
``auto_placement``, ``association``), and migration 29 read each dict, added the
``support_w``/``conflict_w`` decay weights and wrote *the whole dict back* -- so
the legacy key survived into the current schema.  Reading such a row raised
``ValidationError: evidence.source  Extra inputs are not permitted`` inside
``VersionService._edge_contract`` and the engine died at startup.  Every install
that ran v014 and has any parent/paired/alias channel or migrated relation is
affected (one production install: 16 channels with ``paired_ct_id``).

**What.**  For every column holding an edge-evidence blob (``topology_edges``
and ``mapping_edges``), decode the JSON, drop keys that are not fields of
``EdgeEvidence``, and write the row back **only if something changed**.  Valid
keys keep their values and their order verbatim; a row whose JSON is already
clean is not rewritten at all (``evidence_json`` stays byte-identical).  An
unparseable value is left exactly as it is -- this migration repairs known-bad
keys, it never "fixes" data it does not understand (the same ``continue`` rule
v029 uses).

The allowed key set comes from ``EdgeEvidence.model_fields`` rather than a
literal list, so a future evidence field is never stripped by an old migration.

**Why the data is dropped and not preserved.**  ``source`` never was a contract
field; it recorded which legacy column a backfilled edge came from.  That fact
is already durable, and better expressed, in the columns the backfill set:
``declared_by='migration'``, the deterministic edge id, ``valid_from``, and the
``relation`` derived from the source.  A key no contract accepts cannot be
"preserved in a field that exists" without inventing a meaning for it, and
folding it into ``declared_by`` would conflate provenance of a declaration with
the identity of its author.  The information is diagnostic only, from a released
migration that will not run again, so it is dropped -- but not silently: the
per-table counts and the distinct keys dropped are logged at INFO.  They are
deliberately *not* persisted: a repair must not add a row to a pre-existing
table (the rehearsal gate requires every pre-existing row count to survive).

Data-only: no table is created, no DDL, no auto-committing statement.  Re-running
the migration is a no-op, and the read-side sanitiser in ``versioning.service``
means a boot never depends on this migration having run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from typing import Any

from . import Migration

_LOGGER = logging.getLogger(__name__)

#: ``(table, primary key, evidence column)`` for every table that stores the
#: evidence of a ``TopologyEdgeV3`` / ``MappingEdge``.
_EVIDENCE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("topology_edges", "edge_id", "evidence_json"),
    ("mapping_edges", "mapping_edge_id", "evidence_json"),
)

#: Fallback field set if the contracts cannot be imported (a segmented storage
#: install must still be able to repair the rows it is handed).  Kept in sync by
#: ``tests/unit/test_migration_v031``; the import below is the real source.
_FALLBACK_FIELDS: frozenset[str] = frozenset(
    {
        "support",
        "conflict",
        "parent_only",
        "independent_causes",
        "last_support_at",
        "last_conflict_at",
        "support_w",
        "conflict_w",
    }
)


def evidence_fields() -> frozenset[str]:
    """The allowed evidence keys, read from the contract model itself."""
    try:
        from wattmosaic_engine.contracts import EdgeEvidence
    except Exception:  # pragma: no cover - only a segmented install can get here
        return _FALLBACK_FIELDS
    return frozenset(EdgeEvidence.model_fields)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def sanitise_evidence(raw: Any, allowed: frozenset[str]) -> tuple[str | None, list[str]]:
    """Return ``(new_json, dropped_keys)``; ``new_json`` is ``None`` when untouched.

    ``None`` means the stored value must be left byte-identical: either it is not
    a JSON object this migration understands, or it carries no key outside
    ``allowed``.  Valid keys are preserved with their values and their order.
    """
    try:
        decoded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None, []
    if not isinstance(decoded, dict):
        return None, []
    dropped = [str(key) for key in decoded if key not in allowed]
    if not dropped:
        return None, []
    cleaned = {key: value for key, value in decoded.items() if key in allowed}
    return json.dumps(cleaned, separators=(",", ":")), dropped


def _apply(conn: sqlite3.Connection) -> None:
    allowed = evidence_fields()
    tables = _tables(conn)
    repaired: dict[str, int] = {}
    dropped: Counter[str] = Counter()
    for table, key, column in _EVIDENCE_COLUMNS:
        if table not in tables:
            continue
        rows = conn.execute(f"SELECT {key}, {column} FROM {table}").fetchall()  # noqa: S608 - names are literals above
        updates: list[tuple[str, str]] = []
        for row_id, raw in rows:
            cleaned, removed = sanitise_evidence(raw, allowed)
            if cleaned is None:
                continue
            dropped.update(removed)
            updates.append((cleaned, str(row_id)))
        if updates:
            conn.executemany(
                f"UPDATE {table} SET {column} = ? WHERE {key} = ?",  # noqa: S608 - literals above
                updates,
            )
            repaired[table] = len(updates)
    if repaired:
        # Diagnostics only, never a row: the rehearsal gate requires every
        # pre-existing table's row count to survive a startup migration.
        _LOGGER.info(
            "migration v031 stripped non-contract evidence keys: %s (keys: %s)",
            ", ".join(f"{table}: {count} row(s)" for table, count in sorted(repaired.items())),
            ", ".join(f"{name} x{count}" for name, count in sorted(dropped.items())),
        )


MIGRATION = Migration(
    version=31,
    name="v031_evidence_key_repair",
    statements=(),
    apply=_apply,
)

#: Data-only migration: no table is created.
TIERS: dict[str, str] = {}

__all__ = [
    "MIGRATION",
    "TIERS",
    "evidence_fields",
    "sanitise_evidence",
]
