"""Audit chains: ``inference_records``, ``isolation_verdicts`` and evidence resolution.

The append-only side of inference.  ``inference_records`` is a revision chain (a
revision never overwrites its predecessor) and an ``evidence_refs`` entry must
still resolve after a restart, which is why the isolation verdicts live here too
rather than in process memory.

``resolve_evidence_ref`` is the single place that answers "does this reference
name a row that still exists?", dispatching to the repository that owns each
table's existence check.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ...contracts import EventIsolation, InferenceRecord, utcnow
from ..repositories import Repository, enum_value, iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3


class RecordsRepository(Repository):
    # -- inference record chain (contract 1.6.0, migration 9) -------------- #
    #
    # Append-only audit state, deliberately separate from ``inference_results``:
    # the old table is retention-pruned and readers depend on that.  These rows
    # are exempt from raw retention (see ``AUDIT_TABLES`` / ``prune``).
    def insert_inference_record(self, record: InferenceRecord) -> None:
        """Append one revision.  A revision never overwrites its predecessor."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO inference_records(record_id, device_id, timestamp,
                algorithm_version, contract_version, topology_version, revision,
                previous_record_id, previous_power_w, power_w, source, measurement_basis,
                confidence_json, previous_confidence_json, confidence_delta_json, reason,
                explanation_json, evidence_refs_json, evidence_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.record_id,
                    record.device_id,
                    iso(record.timestamp),
                    record.algorithm_version,
                    record.contract_version,
                    record.topology_version,
                    record.revision,
                    record.previous_record_id,
                    record.previous_power_w,
                    record.power_w,
                    enum_value(record.source),
                    enum_value(record.measurement_basis),
                    json.dumps(record.confidence.model_dump(mode="json"), default=str),
                    None
                    if record.previous_confidence is None
                    else json.dumps(
                        record.previous_confidence.model_dump(mode="json"), default=str
                    ),
                    json.dumps(record.confidence_delta, default=str),
                    record.reason,
                    json.dumps(list(record.explanation)),
                    json.dumps(list(record.evidence_refs)),
                    json.dumps(record.evidence, default=str),
                ),
            )

    @staticmethod
    def row_to_inference_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """Decode one ``inference_records`` row into a model-shaped dict."""
        data = dict(row)
        data["confidence"] = json.loads(data.pop("confidence_json") or "{}")
        raw_previous = data.pop("previous_confidence_json", None)
        data["previous_confidence"] = (
            None if raw_previous is None else json.loads(raw_previous)
        )
        data["confidence_delta"] = json.loads(data.pop("confidence_delta_json") or "{}")
        data["explanation"] = json.loads(data.pop("explanation_json") or "[]")
        data["evidence_refs"] = json.loads(data.pop("evidence_refs_json") or "[]")
        data["evidence"] = json.loads(data.pop("evidence_json") or "{}")
        return data

    #: Original private spelling, kept because later tasks call it.
    _row_to_inference_record = row_to_inference_record

    def inference_records_for_device(
        self, device_id: str, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Records for one device, oldest revision first.

        Ordered by ``(timestamp, revision)`` ascending so a caller can replay the
        chain in the order it happened; ``limit`` keeps the **newest** rows,
        which is what a bounded history read wants.
        """
        query = "SELECT * FROM inference_records WHERE device_id=?"
        params: list[Any] = [device_id]
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(iso(since))
        query += " ORDER BY timestamp DESC, revision DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            self.row_to_inference_record(row)
            for row in sorted(rows, key=lambda r: (r["timestamp"], r["revision"]))
        ]

    def latest_inference_record(self, device_id: str) -> dict[str, Any] | None:
        """The highest-revision record for a device, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM inference_records WHERE device_id=?
                ORDER BY revision DESC, timestamp DESC LIMIT 1""",
                (device_id,),
            ).fetchone()
        return None if row is None else self.row_to_inference_record(row)

    def inference_records_for_topology_version(
        self, topology_version: int | str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Read path for reconciliation: records produced under one topology version.

        Filters the existing ``inference_records.topology_version`` **integer**
        column; there is no status/flag column and none is added, so this needs
        no schema migration.  Reconciliation decides staleness by comparing the
        version a record already carries (immutably) with the version in force.
        Ordered oldest first so a consumer can replay the affected history in
        the order it happened; ``limit`` keeps the newest rows.
        """
        # Import locally to avoid a storage -> electrical -> storage import cycle
        # (the topology module is imported widely during package initialisation).
        from ...electrical.topology import version_number

        number = version_number(topology_version)
        if number is None:
            # An opaque id with no integer spelling matches no integer column.
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM inference_records WHERE topology_version=?
                ORDER BY timestamp DESC, revision DESC LIMIT ?""",
                (number, limit),
            ).fetchall()
        return [
            self.row_to_inference_record(row)
            for row in sorted(rows, key=lambda r: (r["timestamp"], r["revision"]))
        ]

    def record_exists(self, record_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM inference_records WHERE record_id=?", (str(record_id),)
            ).fetchone()
        return row is not None

    # -- isolation verdicts (migration 10) --------------------------------- #
    def insert_isolation_verdict(
        self, verdict: EventIsolation, *, algorithm_version: str = ""
    ) -> None:
        """Upsert one verdict by ``event_id``; a reanalysis replaces it.

        Verdicts are cited by ``inference_records.evidence_refs`` as
        ``event:<event_id>``, so they must outlive the process that produced
        them.  The event id is the identity: analysing the same event again
        updates its verdict rather than accumulating a second row.
        """
        with self._lock:
            self._conn.execute(
                """INSERT INTO isolation_verdicts(event_id, device_id, entity_id,
                status, reason, occurred_at, received_at, analysis_offset_s,
                isolation_window_s, window_start, window_end,
                competing_event_ids_json, competing_device_ids_json,
                power_window_contaminated, evidence_json, algorithm_version, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                device_id=excluded.device_id, entity_id=excluded.entity_id,
                status=excluded.status, reason=excluded.reason,
                occurred_at=excluded.occurred_at, received_at=excluded.received_at,
                analysis_offset_s=excluded.analysis_offset_s,
                isolation_window_s=excluded.isolation_window_s,
                window_start=excluded.window_start, window_end=excluded.window_end,
                competing_event_ids_json=excluded.competing_event_ids_json,
                competing_device_ids_json=excluded.competing_device_ids_json,
                power_window_contaminated=excluded.power_window_contaminated,
                evidence_json=excluded.evidence_json,
                algorithm_version=excluded.algorithm_version""",
                (
                    verdict.event_id,
                    verdict.device_id,
                    verdict.entity_id,
                    enum_value(verdict.status),
                    verdict.reason,
                    iso(verdict.occurred_at),
                    iso(verdict.received_at),
                    float(verdict.analysis_offset_s),
                    float(verdict.isolation_window_s),
                    iso(verdict.window_start),
                    iso(verdict.window_end),
                    json.dumps(list(verdict.competing_event_ids)),
                    json.dumps(list(verdict.competing_device_ids)),
                    int(bool(verdict.power_window_contaminated)),
                    json.dumps(verdict.evidence, default=str),
                    algorithm_version,
                    iso(utcnow()),
                ),
            )

    def isolation_verdict(self, event_id: str) -> dict[str, Any] | None:
        """One stored verdict, or ``None`` if this event was never analysed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM isolation_verdicts WHERE event_id=?", (str(event_id),)
            ).fetchone()
        return None if row is None else self.row_to_isolation_verdict(row)

    def isolation_verdicts(
        self, since: datetime | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Verdicts, oldest first, so a report can be rebuilt after a restart."""
        query = "SELECT * FROM isolation_verdicts"
        params: list[Any] = []
        if since is not None:
            query += " WHERE occurred_at >= ?"
            params.append(iso(since))
        query += " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            self.row_to_isolation_verdict(row)
            for row in sorted(rows, key=lambda r: (r["occurred_at"], r["event_id"]))
        ]

    @staticmethod
    def row_to_isolation_verdict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["competing_event_ids"] = json.loads(
            data.pop("competing_event_ids_json") or "[]"
        )
        data["competing_device_ids"] = json.loads(
            data.pop("competing_device_ids_json") or "[]"
        )
        data["power_window_contaminated"] = bool(data["power_window_contaminated"])
        data["evidence"] = json.loads(data.pop("evidence_json") or "{}")
        return data

    #: Original private spelling, kept because later tasks call it.
    _row_to_isolation_verdict = row_to_isolation_verdict

    # -- evidence references ----------------------------------------------- #
    def relation_exists(self, relation_id: str) -> bool:
        """Whether a typed topology edge with this id is stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM topology_relations WHERE relation_id=?",
                (str(relation_id),),
            ).fetchone()
        return row is not None

    def resolve_evidence_ref(self, ref: str) -> bool:
        """Whether one ``evidence_refs`` entry names a row that still exists.

        The whole point of an evidence reference is that it can be *checked*: a
        reference to a verdict kept only in memory, or to a row a later pass
        replaced, is a dangling claim.  Every prefix this engine emits is
        answered here, so a caller (and the test suite) can assert that a
        record is traceable instead of trusting the string.

        Recognised forms:

        ``event:<event_id>``        an isolation verdict (migration 10)
        ``relation:<relation_id>``  a typed topology edge
        ``circuit:<circuit_id>``    a classified circuit
        ``record:<record_id>``      an inference record
        ``sample:<ct_id>@<iso>``    a raw stored sample

        The per-table existence checks live with the table's own repository, so
        this method only owns the prefix vocabulary.
        """
        text = str(ref or "").strip()
        if ":" not in text:
            return False
        prefix, key = text.split(":", 1)
        if not key:
            return False
        if prefix == "event":
            return self.isolation_verdict(key) is not None
        if prefix == "relation":
            return self.relation_exists(key)
        if prefix == "circuit":
            return self.db.repos.topology.circuit_exists(key)
        if prefix == "record":
            return self.record_exists(key)
        if prefix == "sample":
            return self.db.repos.samples.sample_exists(key)
        return False


__all__ = ["RecordsRepository"]
