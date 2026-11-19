"""Aggregates and retention: the L1/L3 bucket tables plus the prune pass.

``upsert_power_aggregate``/``upsert_energy_aggregate`` write the rolled-up
buckets; ``energy_total`` reads one bucket size so the same physical energy is not
counted twice; :meth:`AggregatesRepository.prune` is the retention pass.

The pass reads *which* tables it may delete from
``storage/tiers.py::prunable_tables`` rather than restating a list here, so the
registry is the one declaration the policy reads (``REQ-STORAGE-001``).  Every
registered table the pass does not delete is reported with a count of ``0``, so
"deleted nothing" is visible rather than merely absent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ...contracts import utcnow
from ..migrations import LATEST_VERSION
from ..repositories import Repository, iso
from ..tiers import AGGREGATE_HORIZON, TABLE_TIERS, prunable_tables, table_tiers


class AggregatesRepository(Repository):
    def upsert_power_aggregate(
        self, device_id: str, bucket_start: datetime, bucket_s: int, mean_w: float, min_w: float,
        max_w: float, count: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO power_aggregates(device_id, bucket_start, bucket_s, mean_power_w,
                max_power_w, min_power_w, sample_count) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(device_id, bucket_start, bucket_s) DO UPDATE SET
                mean_power_w=excluded.mean_power_w, max_power_w=excluded.max_power_w,
                min_power_w=excluded.min_power_w, sample_count=excluded.sample_count""",
                (device_id, iso(bucket_start), bucket_s, mean_w, max_w, min_w, count),
            )

    def upsert_energy_aggregate(
        self, device_id: str, bucket_start: datetime, bucket_s: int, energy_kwh: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO energy_aggregates(device_id, bucket_start, bucket_s, energy_kwh)
                VALUES(?,?,?,?) ON CONFLICT(device_id, bucket_start, bucket_s)
                DO UPDATE SET energy_kwh=excluded.energy_kwh""",
                (device_id, iso(bucket_start), bucket_s, energy_kwh),
            )

    def energy_total(self, device_id: str, bucket_s: int | None = None) -> float:
        """Sum persisted energy for exactly one bucket size.

        ``energy_aggregates`` stores the *same* physical energy once per
        resolution (default 300 s and 3600 s), so summing every row would count
        the same kWh once per bucket size.  Mirroring
        ``DeviceRunRepository.energy_total``, ``bucket_s=None`` therefore picks
        the smallest persisted size (the most detailed series) and sums only
        that one.
        """
        with self._lock:
            if bucket_s is None:
                row = self._conn.execute(
                    "SELECT MIN(bucket_s) AS size FROM energy_aggregates WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                size = int(row["size"]) if row and row["size"] is not None else None
            else:
                size = int(bucket_s)
            if size is None:
                return 0.0
            row = self._conn.execute(
                "SELECT COALESCE(SUM(energy_kwh),0) AS e FROM energy_aggregates "
                "WHERE device_id=? AND bucket_s=?",
                (device_id, size),
            ).fetchone()
        return float(row["e"]) if row else 0.0

    def prune(self, raw_days: int = 30, aggregate_days: int = 730) -> dict[str, int]:
        """Delete rows past their horizon; never delete long-term state.

        Which tables are pruned, by which timestamp column and on which horizon,
        is read from :func:`wattmosaic_engine.storage.tiers.prunable_tables`
        rather than restated here.  The registry's horizon is authoritative for a
        table.

        The signature and its defaults are unchanged: ``main.py``'s maintenance
        loop still calls ``prune(retention_days_raw, retention_days_aggregates)``
        and gets exactly the behaviour it had before.
        """
        now = utcnow()
        raw_cutoff = iso(now - timedelta(days=raw_days))
        agg_cutoff = iso(now - timedelta(days=aggregate_days))
        removed: dict[str, int] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table, col, horizon in prunable_tables():
                    cutoff = agg_cutoff if horizon == AGGREGATE_HORIZON else raw_cutoff
                    cur = self._conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
                    removed[table] = cur.rowcount
                # Exempt state: every registered table the pass did not delete is
                # named here with a count of 0.
                for table in TABLE_TIERS:
                    if table not in removed:
                        removed[table] = 0
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            # ``inference_results`` is a prunable raw table: deleting the oldest
            # rows can drop a device's only row, so the cached latest map must be
            # rebuilt on the next read.
            self.db.invalidate_latest_results()
        return removed

    def export_device_data(self, device_id: str) -> dict[str, Any]:
        """Every persisted v3 row that belongs to one subject, for the export endpoint."""
        with self._lock:
            estimates = self._conn.execute(
                "SELECT * FROM inference_results WHERE device_id=? ORDER BY timestamp ASC",
                (device_id,),
            ).fetchall()
            observations = self._conn.execute(
                "SELECT * FROM learning_observations WHERE subject_id=? ORDER BY created_at ASC",
                (device_id,),
            ).fetchall()
            versions = self._conn.execute(
                "SELECT * FROM power_model_versions_v3 WHERE subject_id=? ORDER BY number ASC",
                (device_id,),
            ).fetchall()
        return {
            "device_id": device_id,
            "exported_at": iso(utcnow()),
            "inference_results": [dict(r) for r in estimates],
            "learning_observations": [dict(r) for r in observations],
            "model_versions": [dict(r) for r in versions],
        }

    def stats(self) -> dict[str, Any]:
        """Row counts, schema version and file size for ``GET /status``."""
        tables = (
            "ct_samples", "ha_events_v3", "inference_results", "learning_observations",
            "power_model_versions_v3", "power_aggregates", "energy_aggregates",
            "reconciliation_results", "data_quality_events", "system_events",
        )
        with self._lock:
            counts = {
                t: int(self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
                for t in tables
            }
        return {
            "schema_version": self.db.schema_version,
            "latest_schema_version": LATEST_VERSION,
            "counts": counts,
            "size_bytes": self.db.path.stat().st_size if self.db.path.exists() else 0,
            # The published tier classification, so a consumer of ``GET /status``
            # reads one registry rather than re-deriving tiers per table.
            # ``09-api-and-data-contracts.md`` §7.8.1 fixes the vocabulary:
            # one row per tier, ``tier`` in L1-L4, sub-classification by
            # ``table_name``.  This is the listing the API wave should call --
            # see ``storage/tiers.py::table_tiers`` -- not a second registry.
            "tiers": table_tiers(),
        }


__all__ = ["AggregatesRepository"]
