"""HA and device events: the ``device_events`` table.

The raw event log.  Classified **L2** (event evidence) but pruned on the raw
horizon by the shipped retention pass -- see the recorded conflict in
``storage/tiers.py::prunable_tables``.  Migration 16 (S16) adds ``event_evidence``
and ``ha_events_v3`` for the new write path; this table keeps its behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ...contracts import utcnow
from ..repositories import Repository, iso


class EventsRepository(Repository):
    def insert_device_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO device_events(event_id, device_id, entity_id, event_type,
                previous_state, new_state, attributes_json, occurred_at, received_at, processed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event["event_id"], event["device_id"], event["entity_id"], event["event_type"],
                    event.get("previous_state"), event.get("new_state"),
                    json.dumps(event.get("attributes", {}), default=str), iso(event["occurred_at"]),
                    iso(event.get("received_at") or utcnow()), iso(event.get("processed_at")),
                ),
            )

    def recent_device_events(self, since: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_events WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT ?",
                (iso(since), limit),
            ).fetchall()
        return [dict(r) for r in rows]

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
