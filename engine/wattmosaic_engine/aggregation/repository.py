"""Thin repository for ``device_runs`` and aggregation recovery queries.

Why a separate connection?
    ``storage/database.py`` (owned by another workstream) exposes no public
    method for ``device_runs``.  Editing it is explicitly out of scope, and
    reaching into ``Database._conn`` would couple us to a private attribute.
    This repository therefore opens its *own* short-lived SQLite connection to
    the same database file.  The database is configured in WAL mode, so one
    extra writer connection is safe; we set ``busy_timeout`` and keep
    transactions short.

If the storage owner later adds ``Database.save_device_run`` this class can be
swapped for it without touching the aggregator.  Reported as a contract gap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import OperatingStage, Source, StageStatus
from ..storage.database import Database, iso
from .models import DeviceRun


class DeviceRunRepository:
    """Persists :class:`DeviceRun` rows and answers recovery queries."""

    def __init__(self, database: Database) -> None:
        self._path = Path(database.path)
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    # -- writes ----------------------------------------------------------- #
    def save_run(self, run: DeviceRun) -> None:
        row = run.as_row()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO device_runs(run_id, device_id, started_at, ended_at, energy_kwh,
                peak_power_w, mean_power_w, stages_json, source)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                ended_at=excluded.ended_at, energy_kwh=excluded.energy_kwh,
                peak_power_w=excluded.peak_power_w, mean_power_w=excluded.mean_power_w,
                stages_json=excluded.stages_json, source=excluded.source""",
                (
                    row["run_id"],
                    row["device_id"],
                    iso(run.started_at),
                    iso(run.ended_at),
                    float(run.energy_kwh),
                    float(run.peak_power_w),
                    float(run.mean_power_w),
                    json.dumps(row["stages"], default=str),
                    str(row["source"]),
                ),
            )
            conn.execute("DELETE FROM operating_stages WHERE run_id=?", (run.run_id,))
            for stage in run.stages:
                conn.execute(
                    """INSERT INTO operating_stages(device_id, run_id, stage_name, status, started_at,
                    ended_at, power_mean_w, energy_kwh) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run.device_id,
                        run.run_id,
                        stage.name,
                        stage.status.value if hasattr(stage.status, "value") else str(stage.status),
                        iso(run.started_at),
                        iso(run.ended_at),
                        float(stage.power_mean_w),
                        float(
                            max(0.0, stage.power_mean_w)
                            * max(0.0, (stage.max_duration_s or run.runtime_s))
                            / 3_600_000.0
                        ),
                    ),
                )

    def delete_run(self, run_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM operating_stages WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM device_runs WHERE run_id=?", (run_id,))

    # -- reads ------------------------------------------------------------ #
    def runs(
        self,
        device_id: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM device_runs WHERE device_id=?"
        params: list[Any] = [device_id]
        if since is not None:
            query += " AND started_at >= ?"
            params.append(iso(since))
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            try:
                data["stages"] = json.loads(data.pop("stages_json", "[]") or "[]")
            except ValueError:
                data["stages"] = []
            out.append(data)
        return out

    def run_count(self, device_id: str | None = None) -> int:
        with self._lock, self._connect() as conn:
            if device_id is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM device_runs").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM device_runs WHERE device_id=?", (device_id,)
                ).fetchone()
        return int(row["n"]) if row else 0

    def stages(self, device_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM operating_stages WHERE device_id=?
                ORDER BY started_at DESC LIMIT ?""",
                (device_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_bucket(self, device_id: str, bucket_s: int) -> datetime | None:
        """Start of the most recently persisted energy bucket for a device."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT MAX(bucket_start) AS last FROM energy_aggregates
                WHERE device_id=? AND bucket_s=?""",
                (device_id, int(bucket_s)),
            ).fetchone()
        if not row or not row["last"]:
            return None
        try:
            parsed = datetime.fromisoformat(str(row["last"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def distinct_inference_devices(self, since: datetime | None = None) -> list[str]:
        query = "SELECT DISTINCT device_id FROM inference_results"
        params: list[Any] = []
        if since is not None:
            query += " WHERE timestamp >= ?"
            params.append(iso(since))
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return sorted(str(row["device_id"]) for row in rows)

    def energy_total(self, device_id: str, bucket_s: int | None = None) -> float:
        """Sum persisted energy for exactly one bucket size.

        Summing every bucket size would multiply the same energy by the number
        of configured resolutions (see ``EnergyAggregator.energy_total``).
        ``bucket_s=None`` picks the smallest (most detailed) persisted size.
        """
        with self._lock, self._connect() as conn:
            if bucket_s is None:
                row = conn.execute(
                    "SELECT MIN(bucket_s) AS size FROM energy_aggregates WHERE device_id=?",
                    (device_id,),
                ).fetchone()
                size = int(row["size"]) if row and row["size"] is not None else None
            else:
                size = int(bucket_s)
            if size is None:
                return 0.0
            total = conn.execute(
                "SELECT COALESCE(SUM(energy_kwh),0) AS e FROM energy_aggregates WHERE device_id=? AND bucket_s=?",
                (device_id, size),
            ).fetchone()
        return float(total["e"]) if total else 0.0

    def bucket_energy(self, device_id: str, bucket_start: datetime, bucket_s: int) -> float:
        """Energy already persisted for one bucket (0.0 when absent)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT energy_kwh FROM energy_aggregates
                WHERE device_id=? AND bucket_start=? AND bucket_s=?""",
                (device_id, iso(bucket_start), int(bucket_s)),
            ).fetchone()
        return float(row["energy_kwh"]) if row and row["energy_kwh"] is not None else 0.0


def run_from_row(row: dict[str, Any]) -> DeviceRun | None:
    """Convert a ``device_runs`` row back into a :class:`DeviceRun`."""
    if not row:
        return None
    try:
        started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    ended_raw = row.get("ended_at")
    ended = None
    if ended_raw:
        try:
            ended = datetime.fromisoformat(str(ended_raw).replace("Z", "+00:00"))
        except ValueError:
            ended = None
    stages: list[OperatingStage] = []
    for stage in row.get("stages") or []:
        if isinstance(stage, dict):
            try:
                stages.append(OperatingStage(**stage))
            except (TypeError, ValueError):
                continue
    try:
        source = Source(str(row.get("source") or Source.INFERRED.value))
    except ValueError:
        source = Source.INFERRED
    return DeviceRun(
        run_id=str(row.get("run_id") or ""),
        device_id=str(row.get("device_id") or ""),
        started_at=started if started.tzinfo else started.replace(tzinfo=UTC),
        ended_at=ended,
        energy_kwh=float(row.get("energy_kwh") or 0.0),
        peak_power_w=float(row.get("peak_power_w") or 0.0),
        mean_power_w=float(row.get("mean_power_w") or 0.0),
        stages=stages,
        stage_status=(
            StageStatus.IDENTIFIED
            if stages and all(s.status != StageStatus.UNKNOWN.value for s in stages)
            else StageStatus.UNKNOWN
        ),
        source=source,
    )


__all__ = ["DeviceRunRepository", "run_from_row"]
