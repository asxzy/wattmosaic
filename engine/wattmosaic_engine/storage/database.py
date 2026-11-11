"""SQLite persistence layer (WAL, versioned migrations, bounded retention).

Thread-safety: a single ``Database`` instance is created per process and guarded
by a ``threading.Lock``; all engine writes happen on the asyncio loop thread, so
the lock is cheap and prevents accidental concurrent writer corruption.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..contracts import (
    CTChannel,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceModelParameters,
    DeviceModelVersion,
    DeviceTopology,
    InferenceResult,
    LearningSample,
    ModelStatus,
    ModelType,
    Reconciliation,
    Topology,
    TopologyStatus,
    utcnow,
)
from .migrations import LATEST_VERSION, apply_migrations, current_version


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _enum_value(value: Any) -> str:
    """Plain string for a contract enum, tolerant of a bare string."""
    return str(getattr(value, "value", value) if value is not None else "unknown")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.schema_version = apply_migrations(self._conn)

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA wal_autocheckpoint=1000")

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> None:
        """Cheap liveness probe for health endpoints.

        Deliberately does **not** run ``PRAGMA integrity_check``.  That pragma
        reads the entire database file — seconds on a few hundred MB and far
        longer on slow storage — so it must never sit on a probe path that runs
        every few seconds.  It raises if the connection is unusable.
        """
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def integrity_check(self) -> dict[str, Any]:
        """Full database verification. Expensive: call at startup/diagnostics only.

        ``PRAGMA integrity_check`` already performs every check ``quick_check``
        does, so this is a whole-file scan.  Never call it from a health probe.
        """
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
            quick = self._conn.execute("PRAGMA quick_check").fetchone()
        return {
            "integrity": row[0] if row else "unknown",
            "quick_check": quick[0] if quick else "unknown",
            "schema_version": self.schema_version,
            "latest_schema_version": LATEST_VERSION,
            "path": str(self.path),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- engine state ----------------------------------------------------- #
    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO engine_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), iso(utcnow())),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- topology --------------------------------------------------------- #
    @staticmethod
    def _insertion_order(channels: Iterable[CTChannel]) -> list[CTChannel]:
        """Order channels so a parent is always written before its children.

        ``ct_channels.parent_ct_id`` is a self-referencing foreign key and
        ``PRAGMA foreign_keys=ON`` is set, so inserting a nested channel before
        the channel it hangs from aborts the whole transaction with
        ``FOREIGN KEY constraint failed``.  The wizard sends channels in
        discovery order, which puts a sub-panel feed before the service feed on
        this installation, so this ordering is required for any real nested
        topology -- not just a nicety.

        Unknown parents and cycles are tolerated: they are already reported by
        ``TopologyGraph``, and refusing to persist the whole topology would be a
        worse failure than storing a relationship the engine will ignore.  Any
        channel whose parent never becomes insertable is appended in its
        original order.
        """
        ordered: list[CTChannel] = []
        pending = {ch.ct_id: ch for ch in channels}
        emitted: set[str] = set()
        progress = True
        while pending and progress:
            progress = False
            for ct_id in list(pending):
                parent = pending[ct_id].parent_ct_id
                if parent is None or parent not in pending or parent in emitted:
                    ordered.append(pending.pop(ct_id))
                    emitted.add(ct_id)
                    progress = True
        ordered.extend(pending.values())
        return ordered

    def save_topology(self, topology: Topology, note: str = "") -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM ct_channels")
                self._conn.execute("DELETE FROM devices")
                now = iso(utcnow())
                for ch in self._insertion_order(topology.channels):
                    self._conn.execute(
                        """INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase, parent_ct_id,
                        polarity, device_id, paired_ct_id, alias_of_ct_id, measurement_multiplier,
                        enabled, resolution_w, expected_interval_s,
                        reliability, topology_status, metadata_json, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            ch.ct_id, ch.entity_id, ch.name, ch.ct_type, ch.phase, ch.parent_ct_id,
                            ch.polarity, ch.device_id, ch.paired_ct_id, ch.alias_of_ct_id,
                            float(ch.measurement_multiplier),
                            int(ch.enabled), ch.resolution_w,
                            ch.expected_interval_s, ch.reliability, ch.topology_status,
                            json.dumps(ch.metadata), now,
                        ),
                    )
                for dev in topology.devices:
                    self._conn.execute(
                        """INSERT INTO devices(device_id, name, dedicated_ct_ids_json, branch_ct_ids_json,
                        phase, double_pole, parent_device_id, topology_status, enabled, metadata_json, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            dev.device_id, dev.name, json.dumps(dev.dedicated_ct_ids),
                            json.dumps(dev.branch_ct_ids), dev.phase, int(dev.double_pole),
                            dev.parent_device_id, dev.topology_status, 1,
                            json.dumps(dev.metadata), now,
                        ),
                    )
                cur = self._conn.execute(
                    "INSERT INTO topology_versions(created_at, status, payload_json, note) VALUES(?,?,?,?)",
                    (now, topology.status, json.dumps(topology.model_dump(mode="json"), default=str), note),
                )
                version = int(cur.lastrowid or 0)
                self._conn.execute("COMMIT")
                return version
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load_topology(self) -> Topology | None:
        with self._lock:
            chans = self._conn.execute("SELECT * FROM ct_channels").fetchall()
            devs = self._conn.execute("SELECT * FROM devices").fetchall()
            latest = self._conn.execute(
                "SELECT version, status, payload_json, created_at FROM topology_versions "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if not chans and not devs:
            return None
        # The per-row tables carry channels/devices only; the *topology-level*
        # status lives on ``topology_versions``.  Reading it back matters: a
        # user-confirmed topology that reloaded as ``candidate`` would have every
        # learning sample rejected as ``ambiguous_topology`` forever, so a
        # restart would silently disable learning.
        status = TopologyStatus.CANDIDATE
        created_at = utcnow()
        if latest is not None:
            try:
                status = TopologyStatus(str(latest["status"]))
            except ValueError:
                status = TopologyStatus.CANDIDATE
            raw_created = latest["created_at"]
            if raw_created:
                try:
                    created_at = datetime.fromisoformat(str(raw_created))
                except ValueError:
                    created_at = utcnow()
        return Topology(
            version=int(latest["version"]) if latest else 1,
            created_at=created_at,
            status=status,
            channels=[
                CTChannel(
                    ct_id=r["ct_id"], entity_id=r["entity_id"], name=r["name"], ct_type=r["ct_type"],
                    phase=r["phase"], parent_ct_id=r["parent_ct_id"], polarity=r["polarity"],
                    device_id=r["device_id"], paired_ct_id=r["paired_ct_id"],
                    alias_of_ct_id=r["alias_of_ct_id"],
                    measurement_multiplier=float(r["measurement_multiplier"]),
                    enabled=bool(r["enabled"]),
                    resolution_w=r["resolution_w"], expected_interval_s=r["expected_interval_s"],
                    reliability=r["reliability"], topology_status=r["topology_status"],
                    metadata=json.loads(r["metadata_json"]),
                )
                for r in chans
            ],
            devices=[
                DeviceTopology(
                    device_id=r["device_id"], name=r["name"],
                    dedicated_ct_ids=json.loads(r["dedicated_ct_ids_json"]),
                    branch_ct_ids=json.loads(r["branch_ct_ids_json"]), phase=r["phase"],
                    double_pole=bool(r["double_pole"]), parent_device_id=r["parent_device_id"],
                    topology_status=r["topology_status"], metadata=json.loads(r["metadata_json"]),
                )
                for r in devs
            ],
        )

    # -- device configuration --------------------------------------------- #
    def save_device_configurations(self, configurations: DeviceConfigurationSet) -> None:
        """Replace the stored per-device configuration set atomically.

        The whole table is rewritten so a removal is a real removal rather than a
        stale row nobody reads.
        """
        now = iso(utcnow())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM device_configurations")
                self._conn.executemany(
                    """INSERT INTO device_configurations(device_id, payload_json, updated_at)
                    VALUES(?,?,?)""",
                    [
                        (
                            str(config.device_id),
                            json.dumps(config.model_dump(mode="json")),
                            now,
                        )
                        for config in configurations.devices
                    ],
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES(?,?)",
                    ("device_configuration_version", str(configurations.version)),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load_device_configurations(self) -> DeviceConfigurationSet:
        with self._lock:
            rows = self._conn.execute(
                "SELECT device_id, payload_json, updated_at FROM device_configurations "
                "ORDER BY device_id ASC"
            ).fetchall()
            version_row = self._conn.execute(
                "SELECT value FROM schema_metadata WHERE key='device_configuration_version'"
            ).fetchone()
        devices: list[DeviceConfiguration] = []
        updated_at: datetime | None = None
        for row in rows:
            try:
                config = DeviceConfiguration.model_validate(json.loads(row["payload_json"]))
            except Exception:  # noqa: BLE001 - one corrupt row must not break loading
                continue
            devices.append(config)
            try:
                row_updated = datetime.fromisoformat(str(row["updated_at"]))
            except (TypeError, ValueError):
                row_updated = None
            if row_updated is not None and (updated_at is None or row_updated > updated_at):
                updated_at = row_updated
        version = 1
        if version_row is not None:
            try:
                version = int(version_row["value"])
            except (TypeError, ValueError):
                version = 1
        if updated_at is None:
            return DeviceConfigurationSet(version=version)
        return DeviceConfigurationSet(version=version, updated_at=updated_at, devices=devices)

    def get_device_configuration(self, device_id: str) -> DeviceConfiguration | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM device_configurations WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return DeviceConfiguration.model_validate(json.loads(row["payload_json"]))
        except Exception:  # noqa: BLE001 - corrupt rows are reported as absent
            return None

    # -- samples ---------------------------------------------------------- #
    def insert_ct_samples(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = [
            (
                r["ct_id"], r["entity_id"], float(r["value_w"]), float(r["raw_value"]), r.get("unit", "W"),
                iso(r["state_timestamp"]), iso(r["received_at"]), iso(r.get("physical_timestamp")),
                r.get("quality", "good"), int(bool(r.get("repeated"))), int(r.get("sequence", 0)),
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """INSERT INTO ct_samples(ct_id, entity_id, value_w, raw_value, unit, state_timestamp,
                    received_at, physical_timestamp, quality, repeated, sequence)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(payload)

    def ct_samples_between(
        self, start: datetime, end: datetime, ct_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ct_samples WHERE received_at >= ? AND received_at <= ?"
        params: list[Any] = [iso(start), iso(end)]
        if ct_ids:
            query += f" AND ct_id IN ({','.join('?' * len(ct_ids))})"
            params.extend(ct_ids)
        query += " ORDER BY received_at ASC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def latest_ct_sample(self, ct_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ct_samples WHERE ct_id=? ORDER BY id DESC LIMIT 1", (ct_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_ct_samples(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.* FROM ct_samples s JOIN (SELECT ct_id, MAX(id) AS mid FROM ct_samples GROUP BY ct_id) m
                ON s.ct_id = m.ct_id AND s.id = m.mid"""
            ).fetchall()
        return {r["ct_id"]: dict(r) for r in rows}

    # -- events ----------------------------------------------------------- #
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

    # -- inference -------------------------------------------------------- #
    def insert_inference_results(self, results: Iterable[InferenceResult]) -> int:
        payload = [
            (
                r.device_id, iso(r.timestamp), r.power_w, r.energy_kwh, r.source, r.confidence,
                r.model_id, r.model_version, r.operating_stage, r.stage_status, r.sample_count,
                r.quality_status, r.measurement_age_seconds, json.dumps(r.supporting_ct_channels),
                json.dumps(r.explanation), json.dumps(r.provenance, default=str),
                _enum_value(r.confidence_tier),
                _enum_value(r.monitoring_level),
                _enum_value(r.power_behavior),
                json.dumps(list(r.tier_reasons)),
            )
            for r in results
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """INSERT INTO inference_results(device_id, timestamp, power_w, energy_kwh, source,
                    confidence, model_id, model_version, operating_stage, stage_status, sample_count,
                    quality_status, measurement_age_seconds, supporting_ct_channels_json, explanation_json,
                    provenance_json, confidence_tier, monitoring_level, power_behavior,
                    tier_reasons_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(payload)

    def latest_inference_results(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM inference_results WHERE id IN
                (SELECT MAX(id) FROM inference_results GROUP BY device_id) ORDER BY device_id"""
            ).fetchall()
        return [dict(r) for r in rows]

    def inference_history(self, device_id: str, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM inference_results WHERE device_id=? AND timestamp>=?
                ORDER BY timestamp DESC LIMIT ?""",
                (device_id, iso(since), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- reconciliation --------------------------------------------------- #
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

    # -- learning --------------------------------------------------------- #
    def insert_learning_sample(self, sample: LearningSample) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO learning_samples(sample_id, device_id, captured_at, duration_s,
                baseline_power_w, event_power_w, delta_power_w, control_value, control_variable,
                supporting_ct_ids_json, quality_score, accepted, rejection_reason, notes_json, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sample.sample_id, sample.device_id, iso(sample.captured_at), sample.duration_s,
                    sample.baseline_power_w, sample.event_power_w, sample.delta_power_w,
                    sample.control_value, sample.control_variable, json.dumps(sample.supporting_ct_ids),
                    sample.quality_score, int(sample.accepted), sample.rejection_reason,
                    json.dumps(sample.notes), iso(utcnow()),
                ),
            )

    def learning_samples(self, device_id: str | None = None, accepted_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM learning_samples WHERE 1=1"
        params: list[Any] = []
        if device_id:
            q += " AND device_id=?"
            params.append(device_id)
        if accepted_only:
            q += " AND accepted=1"
        q += " ORDER BY captured_at ASC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def count_learning_samples(self, device_id: str, accepted_only: bool = True) -> int:
        q = "SELECT COUNT(*) AS n FROM learning_samples WHERE device_id=?"
        if accepted_only:
            q += " AND accepted=1"
        with self._lock:
            return int(self._conn.execute(q, (device_id,)).fetchone()["n"])

    # -- models ----------------------------------------------------------- #
    def save_model_version(self, model: DeviceModelVersion, activate: bool = False) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO device_model_versions(device_id, model_type, version, status,
                    created_at, trained_from, trained_to, sample_count, training_quality,
                    validation_metrics_json, parameters_json, confidence, notes_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        model.device_id, model.model_type, model.version, model.status, iso(model.created_at),
                        iso(model.trained_from), iso(model.trained_to), model.sample_count,
                        model.training_quality, json.dumps(model.validation_metrics),
                        json.dumps(model.parameters.model_dump(), default=str), model.confidence,
                        json.dumps(model.notes),
                    ),
                )
                model_id = f"{model.device_id}:{model.model_type}"
                active = model.version if activate else None
                self._conn.execute(
                    """INSERT INTO device_models(model_id, device_id, model_type, active_version, frozen,
                    created_at, updated_at) VALUES(?,?,?,?,0,?,?)
                    ON CONFLICT(model_id, device_id) DO UPDATE SET
                    active_version=COALESCE(?, active_version), updated_at=excluded.updated_at""",
                    (model_id, model.device_id, model.model_type, active, iso(utcnow()), iso(utcnow()), active),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def active_model(self, device_id: str, model_type: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if model_type:
                row = self._conn.execute(
                    """SELECT v.* FROM device_model_versions v JOIN device_models m
                    ON v.device_id=m.device_id AND v.model_type=m.model_type AND v.version=m.active_version
                    WHERE v.device_id=? AND v.model_type=?""",
                    (device_id, model_type),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """SELECT v.* FROM device_model_versions v JOIN device_models m
                    ON v.device_id=m.device_id AND v.model_type=m.model_type AND v.version=m.active_version
                    WHERE v.device_id=? ORDER BY v.created_at DESC LIMIT 1""",
                    (device_id,),
                ).fetchone()
        return dict(row) if row else None

    def model_versions(self, device_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_model_versions WHERE device_id=? ORDER BY version DESC", (device_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_model_status(self, device_id: str, model_type: str, version: int, status: ModelStatus) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE device_model_versions SET status=? WHERE device_id=? AND model_type=? AND version=?",
                (status.value if hasattr(status, "value") else status, device_id, model_type, version),
            )

    def set_model_frozen(self, device_id: str, frozen: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_flags(device_id, frozen, updated_at) VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET frozen=excluded.frozen, updated_at=excluded.updated_at""",
                (device_id, int(frozen), iso(utcnow())),
            )
            self._conn.execute(
                "UPDATE device_models SET frozen=?, updated_at=? WHERE device_id=?",
                (int(frozen), iso(utcnow()), device_id),
            )

    def is_model_frozen(self, device_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT frozen FROM device_flags WHERE device_id=?", (device_id,)
            ).fetchone()
        return bool(row and row["frozen"])

    def rollback_model(self, device_id: str, model_type: str) -> int | None:
        with self._lock:
            rows = self._conn.execute(
                """SELECT version FROM device_model_versions WHERE device_id=? AND model_type=?
                ORDER BY version DESC LIMIT 2""",
                (device_id, model_type),
            ).fetchall()
            if len(rows) < 2:
                return None
            previous = int(rows[1]["version"])
            self._conn.execute(
                "UPDATE device_model_versions SET status='rolled_back' WHERE device_id=? AND model_type=? AND version=?",
                (device_id, model_type, rows[0]["version"]),
            )
            self._conn.execute(
                """UPDATE device_models SET active_version=?, updated_at=? WHERE device_id=? AND model_type=?""",
                (previous, iso(utcnow()), device_id, model_type),
            )
            return previous

    # -- aggregation / retention ----------------------------------------- #
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

    def log_quality_event(self, code: str, message: str, severity: str = "warning", channel_id: str | None = None,
                          details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO data_quality_events(timestamp, channel_id, severity, code, message, details_json)
                VALUES(?,?,?,?,?,?)""",
                (iso(utcnow()), channel_id, severity, code, message, json.dumps(details or {}, default=str)),
            )

    def log_system_event(self, level: str, category: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO system_events(timestamp, level, category, message, details_json)
                VALUES(?,?,?,?,?)""",
                (iso(utcnow()), level, category, message, json.dumps(details or {}, default=str)),
            )

    def prune(self, raw_days: int = 30, aggregate_days: int = 730) -> dict[str, int]:
        now = utcnow()
        raw_cutoff = iso(now - timedelta(days=raw_days))
        agg_cutoff = iso(now - timedelta(days=aggregate_days))
        removed: dict[str, int] = {}
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table, col in (
                    ("ct_samples", "received_at"),
                    ("inference_results", "timestamp"),
                    ("device_events", "occurred_at"),
                    ("device_state_samples", "received_at"),
                    ("data_quality_events", "timestamp"),
                    ("system_events", "timestamp"),
                    ("reconciliation_results", "timestamp"),
                ):
                    cur = self._conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (raw_cutoff,))
                    removed[table] = cur.rowcount
                for table in ("power_aggregates", "energy_aggregates"):
                    cur = self._conn.execute(f"DELETE FROM {table} WHERE bucket_start < ?", (agg_cutoff,))
                    removed[table] = cur.rowcount
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return removed

    def export_device_data(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            inferences = self._conn.execute(
                "SELECT * FROM inference_results WHERE device_id=? ORDER BY timestamp ASC", (device_id,)
            ).fetchall()
            samples = self._conn.execute(
                "SELECT * FROM learning_samples WHERE device_id=? ORDER BY captured_at ASC", (device_id,)
            ).fetchall()
            models = self._conn.execute(
                "SELECT * FROM device_model_versions WHERE device_id=? ORDER BY version ASC", (device_id,)
            ).fetchall()
        return {
            "device_id": device_id,
            "exported_at": iso(utcnow()),
            "inference_results": [dict(r) for r in inferences],
            "learning_samples": [dict(r) for r in samples],
            "model_versions": [dict(r) for r in models],
        }

    def stats(self) -> dict[str, Any]:
        tables = (
            "ct_samples", "device_events", "inference_results", "learning_samples",
            "device_model_versions", "power_aggregates", "energy_aggregates",
            "reconciliation_results", "data_quality_events", "system_events",
        )
        with self._lock:
            counts = {
                t: int(self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]) for t in tables
            }
        return {
            "schema_version": self.schema_version,
            "latest_schema_version": LATEST_VERSION,
            "counts": counts,
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }


def default_parameters() -> DeviceModelParameters:
    return DeviceModelParameters()


__all__ = [
    "Database",
    "ModelType",
    "TopologyStatus",
    "default_parameters",
    "current_version",
]
