"""HA and device events: the ``device_events`` table.

The raw event log.  Classified **L2** (event evidence) but pruned on the raw
horizon by the shipped retention pass -- see the recorded conflict in
``storage/tiers.py::prunable_tables``.  Migration 16 (S16) adds ``event_evidence``
and ``ha_events_v3`` for the new write path; this table keeps its behaviour.
"""

from __future__ import annotations

import json
from typing import Any

from ...contracts import utcnow
from ..repositories import Repository, iso


class EventsRepository(Repository):
    # -- operational logs -------------------------------------------------- #
    def log_quality_event(
        self, code: str, message: str, severity: str = "warning", channel_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO data_quality_events(timestamp, channel_id, severity, code, message, details_json)
                VALUES(?,?,?,?,?,?)""",
                (iso(utcnow()), channel_id, severity, code, message, json.dumps(details or {}, default=str)),
            )

    def log_system_event(
        self, level: str, category: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO system_events(timestamp, level, category, message, details_json)
                VALUES(?,?,?,?,?)""",
                (iso(utcnow()), level, category, message, json.dumps(details or {}, default=str)),
            )


__all__ = ["EventsRepository"]
