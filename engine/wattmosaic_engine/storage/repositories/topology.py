"""Declared topology state: ``ct_channels``, ``devices``, ``topology_versions``, ``circuits``.

``save_topology`` replaces the whole declared tree; ``load_topology`` rebuilds the
contract model from the per-row tables.  Migration 13 (S13) backfills the
canonical endpoint/device tables from these rows, and migration 14 (S14) will
read them as the source of the declared edge set; until then these stay the
storage of record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...contracts import Circuit, CTChannel, DeviceTopology, Topology, TopologyStatus, utcnow
from ..payload import encode_if_worthwhile
from ..repositories import Repository, enum_value, iso


class TopologyRepository(Repository):
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
                        reliability, topology_status, metadata_json, panel_id, panel_name, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            ch.ct_id, ch.entity_id, ch.name, ch.ct_type, ch.phase, ch.parent_ct_id,
                            ch.polarity, ch.device_id, ch.paired_ct_id, ch.alias_of_ct_id,
                            float(ch.measurement_multiplier),
                            int(ch.enabled), ch.resolution_w,
                            ch.expected_interval_s, ch.reliability, ch.topology_status,
                            json.dumps(ch.metadata), ch.panel_id, ch.panel_name, now,
                        ),
                    )
                for dev in topology.devices:
                    self._conn.execute(
                        """INSERT INTO devices(device_id, name, dedicated_ct_ids_json, branch_ct_ids_json,
                        phase, double_pole, parent_device_id, topology_status, enabled, metadata_json,
                        updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            dev.device_id, dev.name, json.dumps([]),
                            json.dumps(dev.branch_ct_ids), dev.phase, int(dev.double_pole),
                            dev.parent_device_id, dev.topology_status, 1,
                            json.dumps(dev.metadata), now,
                        ),
                    )
                # A topology snapshot is the largest JSON document in the schema
                # (37 302 bytes on the reference install, 3 151 compressed), so
                # it is the one column compression pays on.  The decoder reads
                # both forms, so an older row loads unchanged.
                snapshot = json.dumps(topology.model_dump(mode="json"), default=str)
                stored_snapshot, _ = encode_if_worthwhile(snapshot)
                cur = self._conn.execute(
                    "INSERT INTO topology_versions(created_at, status, payload_json, note) VALUES(?,?,?,?)",
                    (now, topology.status, stored_snapshot, note),
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
        # ``panel_id``/``panel_name`` were added to ``ct_channels`` in schema 8.
        # A database copied from an older install may still lack them, so the
        # column set is probed once rather than assumed.
        # ``sqlite3.Row`` iterates *values*, so the column names must be asked
        # for explicitly; binding them first also keeps the check readable.
        column_names = list(chans[0].keys()) if chans else []
        has_panel_columns = "panel_id" in column_names
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
                    # ``panel_*`` arrived in schema 8.  Reading them through a
                    # membership test keeps this working against a connection
                    # opened before the migration ran, or against a hand-copied
                    # database from an older install, instead of raising
                    # KeyError on every load.  (``sqlite3.Row.__contains__``
                    # tests *values*, so the column set has to be explicit.)
                    panel_id=r["panel_id"] if has_panel_columns else None,
                    panel_name=r["panel_name"] if has_panel_columns else None,
                )
                for r in chans
            ],
            devices=[
                DeviceTopology(
                    device_id=r["device_id"], name=r["name"],
                    branch_ct_ids=json.loads(r["branch_ct_ids_json"]), phase=r["phase"],
                    double_pole=bool(r["double_pole"]), parent_device_id=r["parent_device_id"],
                    topology_status=r["topology_status"], metadata=json.loads(r["metadata_json"]),
                )
                for r in devs
            ],
        )

    # -- circuits ---------------------------------------------------------- #
    def upsert_circuit(self, circuit: Circuit) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO circuits(circuit_id, name, panel_id, classification,
                classification_status, parent_circuit_id, ct_ids_json, device_ids_json,
                measurement_basis, metadata_json, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(circuit_id) DO UPDATE SET
                name=excluded.name, panel_id=excluded.panel_id,
                classification=excluded.classification,
                classification_status=excluded.classification_status,
                parent_circuit_id=excluded.parent_circuit_id,
                ct_ids_json=excluded.ct_ids_json, device_ids_json=excluded.device_ids_json,
                measurement_basis=excluded.measurement_basis,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (
                    circuit.circuit_id,
                    circuit.name,
                    circuit.panel_id,
                    enum_value(circuit.classification),
                    enum_value(circuit.classification_status),
                    circuit.parent_circuit_id,
                    json.dumps(list(circuit.ct_ids)),
                    json.dumps(list(circuit.device_ids)),
                    enum_value(circuit.measurement_basis),
                    json.dumps(circuit.metadata, default=str),
                    iso(utcnow()),
                ),
            )

    def circuits_all(self) -> list[dict[str, Any]]:
        """Every circuit, ordered by id, decoded into model-shaped dicts."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM circuits ORDER BY circuit_id ASC"
            ).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            row["ct_ids"] = json.loads(row.pop("ct_ids_json") or "[]")
            row["device_ids"] = json.loads(row.pop("device_ids_json") or "[]")
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        return out

    def circuit_exists(self, circuit_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM circuits WHERE circuit_id=?", (str(circuit_id),)
            ).fetchone()
        return row is not None

    def topology_version_snapshot(self) -> dict[str, Any] | None:
        """The newest stored topology snapshot, decoded, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version, created_at, status, payload_json, note FROM topology_versions "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()
        return None if row is None else dict(row)


__all__ = ["TopologyRepository"]
