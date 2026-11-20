"""Migration 28: re-learn inferred topology under the step-anchored T5 rule.

Until this release T5 measured a candidate parent around the *group's* onset,
which sits a minute or more from a member's own step, counted an unmeasured
parent as "did not move", and had no relative match tolerance.  On the live dev
server true containments collected conflict (sub-panel feeds CONFLICTED) while
wrong pairs confirmed (a 240 V load's two legs as parent/child, a branch under
the other leg).  The stored evidence of every inferred edge is therefore wrong.

Migration 27 kept edges referenced by *any* topology version, but versions
written before 0.3.0-dev.6 captured the whole ledger (hundreds of edges), so
almost nothing was re-learned.  This migration:

* deletes every unpinned ``declared_by = 'inference'`` edge that the active
  topology version does not reference;
* resets the active version's unpinned inferred edges to CANDIDATE with empty
  evidence -- they leave the hierarchy (it is built from CONFIRMED edges) and
  must re-earn confirmation; the version row itself is untouched (append-only).

Only T5 ledger edges (``te-inf::...``) are affected: user-declared, pinned and
legacy-backfilled (v014) edges are never touched.  Re-running is a no-op.
"""

from __future__ import annotations

import json
import sqlite3

from . import Migration

_EMPTY_EVIDENCE = json.dumps(
    {
        "support": 0,
        "conflict": 0,
        "parent_only": 0,
        "independent_causes": 0,
        "last_support_at": None,
        "last_conflict_at": None,
    },
    separators=(",", ":"),
)


def _apply(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "topology_edges" not in tables:
        return
    active: set[str] = set()
    if "topology_versions_v3" in tables:
        for (raw,) in conn.execute(
            "SELECT edge_ids_json FROM topology_versions_v3 WHERE status = 'active'"
        ):
            try:
                active.update(str(item) for item in json.loads(raw or "[]"))
            except (TypeError, ValueError):
                continue
    # Only the T5 ledger's own edges (``te-inf::<parent>::<child>``); edges
    # backfilled from the legacy relations table by v014 are not re-learned.
    rows = conn.execute(
        "SELECT edge_id FROM topology_edges WHERE declared_by = 'inference' "
        "AND COALESCE(pinned, 0) = 0 AND edge_id LIKE 'te-inf::%'"
    ).fetchall()
    inferred = [str(edge_id) for (edge_id,) in rows]
    conn.executemany(
        "DELETE FROM topology_edges WHERE edge_id = ?",
        [(edge_id,) for edge_id in inferred if edge_id not in active],
    )
    conn.executemany(
        "UPDATE topology_edges SET lifecycle = 'candidate', evidence_json = ? WHERE edge_id = ?",
        [(_EMPTY_EVIDENCE, edge_id) for edge_id in inferred if edge_id in active],
    )


MIGRATION = Migration(
    version=28,
    name="v028_relearn_inferred_edges",
    statements=(),
    apply=_apply,
)

#: Data-only migration: no table is created.
TIERS: dict[str, str] = {}

__all__ = ["MIGRATION", "TIERS"]
