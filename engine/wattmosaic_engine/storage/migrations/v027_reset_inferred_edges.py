"""Migration 27: discard inferred topology evidence counted under the old T5 rule.

Before 0.3.0-dev.6 the T5 ledger counted two normal situations on a live house
as *conflict* against a containment edge: a co-step of a different size (the
parent also carries concurrent loads) and a parent step P3 did not flag.  True
containments went CONFLICTED with 36-74 % conflict, and the stored evidence of
every inferred edge is therefore wrong.  The runtime now resumes the ledger from
stored inferred edges after a restart, so that evidence would be reloaded.

This migration removes only edges that inference produced on its own:

* ``declared_by = 'inference'``;
* not pinned by the user;
* not referenced by any topology version (``topology_versions_v3.edge_ids_json``).

User-declared, pinned and versioned edges are untouched; discovery re-learns the
removed suggestions from live events under the corrected rule.  Re-running is a
no-op (nothing left to remove).
"""

from __future__ import annotations

import json
import sqlite3

from . import Migration


def _apply(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "topology_edges" not in tables:
        return
    referenced: set[str] = set()
    if "topology_versions_v3" in tables:
        for (raw,) in conn.execute("SELECT edge_ids_json FROM topology_versions_v3"):
            try:
                referenced.update(str(item) for item in json.loads(raw or "[]"))
            except (TypeError, ValueError):
                continue
    rows = conn.execute(
        "SELECT edge_id FROM topology_edges WHERE declared_by = 'inference' AND COALESCE(pinned, 0) = 0"
    ).fetchall()
    stale = [(str(edge_id),) for (edge_id,) in rows if str(edge_id) not in referenced]
    conn.executemany("DELETE FROM topology_edges WHERE edge_id = ?", stale)


MIGRATION = Migration(
    version=27,
    name="v027_reset_inferred_edges",
    statements=(),
    apply=_apply,
)

#: Data-only migration: no table is created.
TIERS: dict[str, str] = {}

__all__ = ["MIGRATION", "TIERS"]
