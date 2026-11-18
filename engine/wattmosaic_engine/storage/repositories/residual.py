"""Residual power: the ``residual_power`` table and ``reconciliation_results``.

``residual_power`` is the install-wide residual series the current engine writes.
Migration 19 (S19) adds the per-reader series and the unknown-load profiles and
flags these rows ``scope_unknown``; they are never deleted here.

``reconciliation_results`` is the per-cycle balance row.  Migration 17 (S17)
rebuilds ``inference_results`` around the same cycle, but the reconciliation
table keeps its shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...contracts import (
    ReaderResidualPoint,
    Reconciliation,
    ResidualPower,
    UnknownLoadProfileV3,
    utcnow,
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
    def insert_residual_power(
        self,
        residual: ResidualPower,
        timestamp: datetime | None = None,
        reconciliation_id: int | None = None,
    ) -> None:
        """Append one residual observation.  Sign and kind are never rewritten."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO residual_power(timestamp, reconciliation_id, residual_w, kind,
                resolved, unresolved_since, explained_by_json, quality_status, basis,
                baseline_w, dynamic_w) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    iso(timestamp or utcnow()),
                    reconciliation_id,
                    residual.residual_w,
                    enum_value(residual.kind),
                    int(bool(residual.resolved)),
                    iso(residual.unresolved_since),
                    json.dumps(residual.explained_by_device_w, default=str),
                    enum_value(residual.quality_status),
                    enum_value(residual.basis),
                    residual.baseline_w,
                    residual.dynamic_w,
                ),
            )

    def residual_history(
        self, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Residual observations, newest first, decoded for a reader."""
        query = "SELECT * FROM residual_power"
        params: list[Any] = []
        if since is not None:
            query += " WHERE timestamp >= ?"
            params.append(iso(since))
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            row["explained_by_device_w"] = json.loads(row.pop("explained_by_json") or "{}")
            row["resolved"] = bool(row["resolved"])
        return out

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
    def insert_reconciliation(self, rec: Reconciliation) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w, l1_power_w,
                l2_power_w, direct_measured_w, inferred_w, model_estimated_w, configured_prior_w,
                unknown_residual_w, unmonitored_w, meter_disagreement_w, meter_spread_w,
                meter_spread_exceeds_residual,
                reconciliation_error_w, reconciliation_error_ratio, accounted_fraction,
                measurement_uncertainty_w, quality_status, topology_confirmed,
                explanation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    iso(rec.timestamp), rec.topology_version, rec.main_power_w, rec.l1_power_w, rec.l2_power_w,
                    rec.direct_measured_w, rec.inferred_w, rec.model_estimated_w, rec.configured_prior_w,
                    rec.unknown_residual_w, rec.unmonitored_w, rec.meter_disagreement_w,
                    rec.meter_spread_w, int(bool(rec.meter_spread_exceeds_residual)),
                    rec.reconciliation_error_w, rec.reconciliation_error_ratio, rec.accounted_fraction,
                    rec.measurement_uncertainty_w, rec.quality_status, int(bool(rec.topology_confirmed)),
                    json.dumps(rec.explanation),
                ),
            )

    def latest_reconciliation(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reconciliation_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


__all__ = ["ResidualRepository"]
