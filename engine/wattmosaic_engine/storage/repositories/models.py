"""Power models: ``device_models``, ``device_model_versions``, ``device_flags``.

Append-only version rows plus one activation pointer per ``(device_id,
model_type)``.  Migration 18 (S18) adds the v3 registry
(``power_model_versions_v3``, ``power_model_events``) and marks these rows
read-only as ``LEGACY_ABSOLUTE`` (DEC-31); until then this is the model store.

``rollback_model`` activates the previous version and marks the newest one
``rolled_back``.  A rejected candidate stays rejected: the status is written on
the version row, so a later rollback can never reactivate it.
"""

from __future__ import annotations

import json
from typing import Any

from ...contracts import DeviceModelVersion, ModelStatus, utcnow
from ..repositories import Repository, enum_value, iso


class ModelsRepository(Repository):
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
                (enum_value(status), device_id, model_type, version),
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


__all__ = ["ModelsRepository"]
