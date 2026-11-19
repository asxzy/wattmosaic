"""Residuals: the per-reader residual series and the unknown-load profiles.

``reader_residual_series`` (migration 19, S19) holds one signed residual point
per reader and instant; ``unknown_load_profiles`` the T6 profiles.  The legacy
install-wide ``residual_power`` table is archived read-only by migration 25.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...contracts import (
    ReaderResidualPoint,
    UnknownLoadProfileV3,
)
from ..repositories import Repository, enum_value, iso

#: Columns of ``reader_residual_series``, in INSERT order.  The point contract's
#: fields map one-to-one; ``at`` is an ISO instant so the PK sorts chronologically.
_POINT_COLUMNS = (
    "reader_id",
    "at",
    "residual_w",
    "baseline_w",
    "dynamic_w",
    "noise_sigma_w",
    "topology_version_id",
    "basis",
)


class ResidualRepository(Repository):
    def residual_history(
        self, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Per-reader residual points across every reader, newest first.

        Sign and nulls are preserved: a residual is never re-signed or zeroed.
        """
        query = "SELECT * FROM reader_residual_series"
        params: list[Any] = []
        if since is not None:
            query += " WHERE at >= ?"
            params.append(iso(since))
        query += " ORDER BY at DESC, reader_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- per-reader residual series (v019) --------------------------------- #
    @staticmethod
    def _point_row(point: ReaderResidualPoint) -> tuple[Any, ...]:
        return (
            point.reader_id,
            iso(point.at),
            point.residual_w,
            point.baseline_w,
            point.dynamic_w,
            point.noise_sigma_w,
            point.topology_version_id,
            enum_value(point.basis),
        )

    def put_points(self, batch: Iterable[ReaderResidualPoint]) -> int:
        """Append per-reader residual points.  PK ``(reader_id, at)`` rejects a
        duplicate instant for the same reader rather than double-counting it.

        Returns the number of rows written.  A batch is committed as one
        transaction so a reader's series is never left half-written.
        """
        payload = [self._point_row(point) for point in batch]
        if not payload:
            return 0
        columns = ", ".join(_POINT_COLUMNS)
        placeholders = ", ".join("?" * len(_POINT_COLUMNS))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    f"INSERT INTO reader_residual_series({columns}) VALUES({placeholders})",
                    payload,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(payload)

    def series(
        self, reader_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> list[dict[str, Any]]:
        """One reader's residual points in a closed time window, oldest first.

        Sign and nulls are preserved: ``None`` means unknown (HB §18), never
        0 W.  The ``(reader_id, at)`` PK keeps two readers' series independent.
        """
        query = "SELECT * FROM reader_residual_series WHERE reader_id = ?"
        params: list[Any] = [reader_id]
        if since is not None:
            query += " AND at >= ?"
            params.append(iso(since))
        if until is not None:
            query += " AND at <= ?"
            params.append(iso(until))
        query += " ORDER BY at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def latest(self, reader_id: str) -> dict[str, Any] | None:
        """The most recent residual point for one reader, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reader_residual_series WHERE reader_id = ? ORDER BY at DESC LIMIT 1",
                (reader_id,),
            ).fetchone()
        return dict(row) if row else None

    # -- unknown-load profiles (v019) -------------------------------------- #
    def put_profile(self, profile: UnknownLoadProfileV3) -> None:
        """Insert or replace one unknown-load profile by ``profile_id``.

        ``signature`` is a mapping and JSON-encoded; every lifecycle state,
        including ``retired``, is retained rather than deleted.
        """
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO unknown_load_profiles(
                    profile_id, scope_endpoint_id, lifecycle, signature_json,
                    event_count, independent_event_count, first_seen, last_seen)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    profile.profile_id,
                    profile.scope_endpoint_id,
                    profile.lifecycle,
                    json.dumps(profile.signature, default=str),
                    profile.event_count,
                    profile.independent_event_count,
                    iso(profile.first_seen),
                    iso(profile.last_seen),
                ),
            )

    def profiles(
        self, scope_endpoint_id: str | None = None, lifecycle: str | None = None
    ) -> list[dict[str, Any]]:
        """Profiles, optionally filtered by scope endpoint and/or lifecycle."""
        query = "SELECT * FROM unknown_load_profiles WHERE 1=1"
        params: list[Any] = []
        if scope_endpoint_id is not None:
            query += " AND scope_endpoint_id = ?"
            params.append(scope_endpoint_id)
        if lifecycle is not None:
            query += " AND lifecycle = ?"
            params.append(lifecycle)
        query += " ORDER BY last_seen ASC, profile_id ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["signature"] = json.loads(row.pop("signature_json") or "{}")
        return out

    # -- reconciliation ---------------------------------------------------- #


__all__ = ["ResidualRepository"]
