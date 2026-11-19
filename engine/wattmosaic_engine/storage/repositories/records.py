"""Audit chains: ``inference_records``, ``isolation_verdicts`` and evidence resolution.

The append-only side of inference.  ``inference_records`` is a revision chain (a
revision never overwrites its predecessor) and an ``evidence_refs`` entry must
still resolve after a restart, which is why the isolation verdicts live here too
rather than in process memory.

``resolve_evidence_ref`` is the single place that answers "does this reference
name a row that still exists?", dispatching to the repository that owns each
table's existence check.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..repositories import Repository, iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3


class RecordsRepository(Repository):
    # -- inference record chain (contract 1.6.0, migration 9) -------------- #
    #
    # Append-only audit state, deliberately separate from ``inference_results``:
    # the old table is retention-pruned and readers depend on that.  These rows
    # are exempt from raw retention (see ``AUDIT_TABLES`` / ``prune``).
    @staticmethod
    def row_to_inference_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """Decode one ``inference_records`` row into a model-shaped dict."""
        data = dict(row)
        data["confidence"] = json.loads(data.pop("confidence_json") or "{}")
        raw_previous = data.pop("previous_confidence_json", None)
        data["previous_confidence"] = (
            None if raw_previous is None else json.loads(raw_previous)
        )
        data["confidence_delta"] = json.loads(data.pop("confidence_delta_json") or "{}")
        data["explanation"] = json.loads(data.pop("explanation_json") or "[]")
        data["evidence_refs"] = json.loads(data.pop("evidence_refs_json") or "[]")
        data["evidence"] = json.loads(data.pop("evidence_json") or "{}")
        return data

    #: Original private spelling, kept because later tasks call it.
    _row_to_inference_record = row_to_inference_record

    def inference_records_for_device(
        self, device_id: str, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Records for one device, oldest revision first.

        Ordered by ``(timestamp, revision)`` ascending so a caller can replay the
        chain in the order it happened; ``limit`` keeps the **newest** rows,
        which is what a bounded history read wants.
        """
        query = "SELECT * FROM inference_records WHERE device_id=?"
        params: list[Any] = [device_id]
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(iso(since))
        query += " ORDER BY timestamp DESC, revision DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            self.row_to_inference_record(row)
            for row in sorted(rows, key=lambda r: (r["timestamp"], r["revision"]))
        ]

    def latest_inference_record(self, device_id: str) -> dict[str, Any] | None:
        """The highest-revision record for a device, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM inference_records WHERE device_id=?
                ORDER BY revision DESC, timestamp DESC LIMIT 1""",
                (device_id,),
            ).fetchone()
        return None if row is None else self.row_to_inference_record(row)

    # -- isolation verdicts (migration 10) --------------------------------- #
    # -- evidence references ----------------------------------------------- #


__all__ = ["RecordsRepository"]


def _version_number(value: int | str | None) -> int | None:
    """The integer behind a topology version identifier, or ``None``.

    ``inference_records.topology_version`` is an integer column; a version may
    be spelled ``7``, ``"7"``, ``"tv-0007"`` or ``"topo-7"``.  An opaque id with
    no integer spelling matches no row.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    for prefix in ("tv-", "topo-"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return int(text) if text.isdigit() else None
