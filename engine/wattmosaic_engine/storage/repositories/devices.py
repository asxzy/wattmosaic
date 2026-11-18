"""Per-device configuration and HA entity bindings.

``device_configurations`` is the user's declaration of what each device is and
which HA entities describe it; the payload column is the source of truth and
``ha_entity_bindings`` is its projection (migration 9).  Migration 13 (S13)
backfills the canonical ``physical_devices``/``ha_entities`` tables from both.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ...contracts import (
    DeviceConfiguration,
    DeviceConfigurationSet,
    utcnow,
)
from ..payload import decode_payload, encode_if_worthwhile
from ..repositories import Repository, enum_value, iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3


class DevicesRepository(Repository):
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
                            encode_if_worthwhile(
                                json.dumps(config.model_dump(mode="json"))
                            )[0],
                            now,
                        )
                        for config in configurations.devices
                    ],
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES(?,?)",
                    ("device_configuration_version", str(configurations.version)),
                )
                # Project the typed entity roles into their own table (migration
                # 9).  The payload above remains the source of truth -- a 1.5.0
                # row has no bindings and every reader still works -- but the
                # projection is what lets a consumer ask "which entities belong to
                # this device, by role?" without parsing every payload, and what
                # makes "at most one power contributor" checkable in SQL.
                self._conn.execute("DELETE FROM ha_entity_bindings")
                self._conn.executemany(
                    """INSERT INTO ha_entity_bindings(entity_id, device_id, role,
                    is_load_evidence, contributes_power, attributes_key)
                    VALUES(?,?,?,?,?,?)""",
                    [
                        (
                            binding.entity_id,
                            binding.device_id,
                            enum_value(binding.role),
                            int(bool(binding.is_load_evidence)),
                            int(bool(binding.contributes_power)),
                            binding.attributes_key,
                        )
                        for config in configurations.devices
                        for binding in config.entity_bindings
                    ],
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
                config = DeviceConfiguration.model_validate(decode_payload(row["payload_json"]))
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
            return DeviceConfiguration.model_validate(decode_payload(row["payload_json"]))
        except Exception:  # noqa: BLE001 - corrupt rows are reported as absent
            return None

    def entity_bindings(self, device_id: str | None = None) -> list[dict[str, Any]]:
        """The ``ha_entity_bindings`` projection, optionally for one device."""
        query = "SELECT * FROM ha_entity_bindings"
        params: list[Any] = []
        if device_id is not None:
            query += " WHERE device_id=?"
            params.append(str(device_id))
        query += " ORDER BY device_id ASC, entity_id ASC"
        with self._lock:
            rows: list[sqlite3.Row] = self._conn.execute(query, params).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["is_load_evidence"] = bool(row["is_load_evidence"])
            row["contributes_power"] = bool(row["contributes_power"])
        return out


__all__ = ["DevicesRepository"]
