"""Immutable event evidence, event-group revisions and inference records.

The storage half of HB §6/§12 (migration 16).  Three invariants shape this DAO:

* **Evidence is immutable.**  :meth:`put_evidence` is a plain ``INSERT``; a
  second insert with the same ``evidence_id`` raises ``sqlite3.IntegrityError``
  rather than updating the row.  A revision is a *new* record with a new id.
* **Groups are immutable per revision.**  ``event_groups`` is keyed by
  ``(group_id, revision)``; :meth:`put_group_revision` refuses to overwrite an
  existing revision, and :meth:`latest_group` reads the highest one.
* **Interpretations are versioned.**  :meth:`put_record` appends an
  ``EventInferenceRecord``; every read path rebuilds the contract model, so the
  revision-chain validator (``revision == 1`` iff ``previous_record_id is None``)
  holds on read as well as on construction.

:meth:`history_rows_for_identifiability` is the query I4 consumes: one row per
independent FINAL event group, with the historical reader deltas for the
requested subjects already projected onto a stable column order.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from ...contracts import (
    EventEvidence,
    EventGroup,
    EventGroupStatus,
    EventInferenceRecord,
    JointInferenceResult,
    QualityFlag,
    ReaderDelta,
    SubEvent,
    VersionTriple,
)
from ..repositories import Repository, as_utc, enum_value, iso


class EvidenceRepository(Repository):
    # -- event evidence --------------------------------------------------- #
    def put_evidence(self, evidence: EventEvidence) -> None:
        """Append one immutable evidence record.  A colliding id raises.

        There is deliberately no ``ON CONFLICT`` clause: silently replacing an
        evidence row would break replay after raw samples expire (DEC-30).
        """
        samples = [[iso(at), None if value is None else float(value)] for at, value in evidence.samples]
        flags = [enum_value(flag) for flag in evidence.quality_flags]
        with self._lock:
            self._conn.execute(
                """INSERT INTO event_evidence(evidence_id, reader_id, measurement_event_id,
                window_start, window_end, samples_json, baseline_w, sigma_w, delta_w,
                quality_flags_json, topology_version_id, finalized)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence.evidence_id,
                    evidence.reader_id,
                    evidence.measurement_event_id,
                    iso(evidence.window_start),
                    iso(evidence.window_end),
                    json.dumps(samples, default=str),
                    evidence.baseline_w,
                    evidence.sigma_w,
                    float(evidence.delta_w),
                    json.dumps(flags),
                    evidence.topology_version_id,
                    int(bool(evidence.finalized)),
                ),
            )

    @staticmethod
    def evidence_from_row(row: sqlite3.Row | dict[str, Any]) -> EventEvidence:
        data = dict(row)
        samples = [
            (as_utc(at), None if value is None else float(value))
            for at, value in json.loads(data.pop("samples_json") or "[]")
        ]
        data["samples"] = samples
        data["quality_flags"] = [QualityFlag(f) for f in json.loads(data.pop("quality_flags_json") or "[]")]
        data["finalized"] = bool(data["finalized"])
        return EventEvidence(**data)

    def evidence(self, evidence_id: str) -> EventEvidence | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM event_evidence WHERE evidence_id=?", (str(evidence_id),)
            ).fetchone()
        return None if row is None else self.evidence_from_row(row)

    def evidence_for_event(self, measurement_event_id: str) -> list[EventEvidence]:
        """All evidence revisions for one detected reader change, oldest window first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_evidence WHERE measurement_event_id=? "
                "ORDER BY window_start, evidence_id",
                (measurement_event_id,),
            ).fetchall()
        return [self.evidence_from_row(row) for row in rows]

    # -- event groups ----------------------------------------------------- #
    @staticmethod
    def _group_row(group: EventGroup) -> tuple[Any, ...]:
        return (
            group.group_id,
            group.revision,
            enum_value(group.status),
            iso(group.opened_at),
            iso(group.closed_at),
            json.dumps([s.model_dump(mode="json") for s in group.sub_events], default=str),
            json.dumps([d.model_dump(mode="json") for d in group.reader_deltas], default=str),
            json.dumps(list(group.grouping_reasons)),
            json.dumps(group.versions.model_dump(mode="json"), default=str),
        )

    @staticmethod
    def group_from_row(row: sqlite3.Row | dict[str, Any]) -> EventGroup:
        data = dict(row)
        data["status"] = EventGroupStatus(data["status"])
        data["opened_at"] = as_utc(data["opened_at"])
        data["closed_at"] = as_utc(data["closed_at"]) if data["closed_at"] else None
        data["sub_events"] = [SubEvent(**s) for s in json.loads(data.pop("sub_events_json") or "[]")]
        data["reader_deltas"] = [
            ReaderDelta(**d) for d in json.loads(data.pop("reader_deltas_json") or "[]")
        ]
        data["grouping_reasons"] = json.loads(data.pop("grouping_reasons_json") or "[]")
        data["versions"] = VersionTriple(**json.loads(data.pop("versions_json") or "{}"))
        return EventGroup(**data)

    def put_group_revision(self, group: EventGroup) -> None:
        """Append one group revision.  A duplicate ``(group_id, revision)`` raises."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO event_groups(group_id, revision, status, opened_at, closed_at,
                    sub_events_json, reader_deltas_json, grouping_reasons_json, versions_json)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    self._group_row(group),
                )
                for delta in group.reader_deltas:
                    if delta.evidence_ref is not None and delta.evidence_ref.kind == "event_evidence":
                        subject = delta.evidence_ref.id
                    else:
                        subject = delta.reader_id
                    self._conn.execute(
                        """INSERT OR IGNORE INTO event_group_members(group_id, revision,
                        subject_kind, subject_id, delta_w, sigma_w, reader_id)
                        VALUES(?,?,?,?,?,?,?)""",
                        (
                            group.group_id,
                            group.revision,
                            "reader",
                            subject,
                            delta.delta_w,
                            delta.sigma_w,
                            delta.reader_id,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def latest_group(self, group_id: str) -> EventGroup | None:
        """The highest stored revision of ``group_id``, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM event_groups WHERE group_id=? ORDER BY revision DESC LIMIT 1",
                (str(group_id),),
            ).fetchone()
        return None if row is None else self.group_from_row(row)

    def group_revision(self, group_id: str, revision: int) -> EventGroup | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM event_groups WHERE group_id=? AND revision=?",
                (str(group_id), int(revision)),
            ).fetchone()
        return None if row is None else self.group_from_row(row)

    def groups(
        self,
        since: datetime | None = None,
        status: EventGroupStatus | str | None = None,
        *,
        limit: int | None = None,
    ) -> list[EventGroup]:
        """Groups, newest first, optionally filtered by opening time and status."""
        query = "SELECT * FROM event_groups WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            query += " AND opened_at >= ?"
            params.append(iso(since))
        if status is not None:
            query += " AND status = ?"
            params.append(enum_value(status))
        query += " ORDER BY opened_at DESC, group_id DESC, revision DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self.group_from_row(row) for row in rows]

    # -- inference records ------------------------------------------------ #
    @staticmethod
    def _record_row(record: EventInferenceRecord) -> tuple[Any, ...]:
        result = record.result
        return (
            record.record_id,
            record.event_group_id,
            record.interpretation_revision,
            record.previous_record_id,
            json.dumps(result.model_dump(mode="json"), default=str),
            str(record.reason),
            iso(record.created_at),
        )

    @staticmethod
    def record_from_row(row: sqlite3.Row | dict[str, Any]) -> EventInferenceRecord:
        data = dict(row)
        data["result"] = JointInferenceResult(**json.loads(data.pop("result_json") or "{}"))
        data["created_at"] = as_utc(data["created_at"])
        # Rebuilding the model runs its revision-chain validator on read.
        return EventInferenceRecord(**data)

    def put_record(self, record: EventInferenceRecord) -> None:
        """Append one versioned interpretation.  An existing id/revision raises."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO event_inference_records(record_id, event_group_id,
                interpretation_revision, previous_record_id, result_json, reason, created_at)
                VALUES(?,?,?,?,?,?,?)""",
                self._record_row(record),
            )

    def records_for_group(self, group_id: str) -> list[EventInferenceRecord]:
        """All interpretations of one group, oldest revision first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_inference_records WHERE event_group_id=? "
                "ORDER BY interpretation_revision, created_at",
                (str(group_id),),
            ).fetchall()
        return [self.record_from_row(row) for row in rows]

    def records_for_subject(
        self, subject_id: str, since: datetime | None = None
    ) -> list[EventInferenceRecord]:
        """Interpretations whose group contains ``subject_id`` as a member."""
        query = (
            "SELECT DISTINCT r.* FROM event_inference_records r "
            "JOIN event_group_members m ON m.group_id = r.event_group_id "
            "WHERE m.subject_id = ?"
        )
        params: list[Any] = [str(subject_id)]
        if since is not None:
            query += " AND r.created_at >= ?"
            params.append(iso(since))
        query += " ORDER BY r.created_at, r.event_group_id, r.interpretation_revision"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self.record_from_row(row) for row in rows]

    # -- identifiability history ------------------------------------------ #
    def history_rows_for_identifiability(
        self,
        subject_ids: list[str] | tuple[str, ...],
        since: datetime | None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Independent FINAL event groups as experiment-matrix rows for I4.

        One row per closed CONFIRMED/REVISED group revision, newest first,
        carrying a ``pattern`` that maps each requested subject id to its
        signed historical ``delta_w`` (``None`` when that subject did not take
        part in the event -- **absent is None, never 0**, HB §18).  I4 stacks
        these rows with the current group's operator rows to build its
        experiment matrix; co-toggle deduplication is I4's job, not the query's.

        The returned dicts also expose ``group_id``, ``revision``, ``status``,
        ``opened_at``, ``closed_at``, ``versions`` and ``reader_deltas`` so a
        consumer can build evidence refs without a second round-trip.
        """
        subjects = tuple(dict.fromkeys(str(s) for s in subject_ids))
        if not subjects:
            return []
        query = (
            "SELECT * FROM event_groups WHERE status IN ('confirmed', 'revised') "
            "AND closed_at IS NOT NULL"
        )
        params: list[Any] = []
        if since is not None:
            query += " AND closed_at >= ?"
            params.append(iso(since))
        query += " ORDER BY closed_at DESC, group_id DESC, revision DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        placeholders = ",".join("?" * len(subjects))
        out: list[dict[str, Any]] = []
        for row in rows:
            group = self.group_from_row(row)
            with self._lock:
                members = self._conn.execute(
                    f"SELECT subject_id, delta_w, sigma_w, reader_id FROM event_group_members "
                    f"WHERE group_id=? AND revision=? AND subject_id IN ({placeholders})",
                    (group.group_id, group.revision, *subjects),
                ).fetchall()
            by_subject = {str(m["subject_id"]): m for m in members}
            pattern = {sid: (None if sid not in by_subject else by_subject[sid]["delta_w"]) for sid in subjects}
            out.append(
                {
                    "group_id": group.group_id,
                    "revision": group.revision,
                    "status": enum_value(group.status),
                    "opened_at": group.opened_at,
                    "closed_at": group.closed_at,
                    "versions": group.versions,
                    "pattern": pattern,
                    "reader_deltas": group.reader_deltas,
                    "members": [
                        {
                            "subject_id": str(m["subject_id"]),
                            "delta_w": m["delta_w"],
                            "sigma_w": m["sigma_w"],
                            "reader_id": m["reader_id"],
                        }
                        for m in members
                    ],
                }
            )
        return out


__all__ = ["EvidenceRepository"]
