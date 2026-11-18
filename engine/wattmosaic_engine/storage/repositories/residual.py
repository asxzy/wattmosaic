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
from datetime import datetime
from typing import Any

from ...contracts import Reconciliation, ResidualPower, utcnow
from ..repositories import Repository, enum_value, iso


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
