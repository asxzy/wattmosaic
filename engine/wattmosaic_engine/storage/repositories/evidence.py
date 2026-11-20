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
from .inference import repair_point_bounds


class EvidenceRepository(Repository):
    # -- event evidence --------------------------------------------------- #
    def put_evidence(
        self, evidence: EventEvidence, *, supersedes_evidence_id: str | None = None
    ) -> None:
        """Append one immutable evidence record.  A colliding id raises.

        There is deliberately no ``ON CONFLICT`` clause: silently replacing an
        evidence row would break replay after raw samples expire (DEC-30).

        ``supersedes_evidence_id`` persists the link P4 carries only in the
        in-memory ``EvidenceBatch.metadata`` (migration 21).  A consumer hands it
        in from ``batch.supersedes``; the immutable contract record itself is
        never given a metadata field.  An absent link is stored as ``NULL``.
        """
        samples = [[iso(at), None if value is None else float(value)] for at, value in evidence.samples]
        flags = [enum_value(flag) for flag in evidence.quality_flags]
        with self._lock:
            self._conn.execute(
                """INSERT INTO event_evidence(evidence_id, reader_id, measurement_event_id,
                window_start, window_end, samples_json, baseline_w, sigma_w, delta_w,
                quality_flags_json, topology_version_id, finalized, supersedes_evidence_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    None if supersedes_evidence_id is None else str(supersedes_evidence_id),
                ),
            )

    def _evidence_columns(self) -> set[str]:
        """Column names of ``event_evidence`` (a pre-v026 DB has no ``revision``)."""
        return {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(event_evidence)").fetchall()
        }

    @staticmethod
    def _evidence_values(evidence: EventEvidence) -> tuple[Any, ...]:
        return (
            evidence.evidence_id,
            evidence.reader_id,
            evidence.measurement_event_id,
            iso(evidence.window_start),
            iso(evidence.window_end),
            json.dumps(
                [[iso(at), None if value is None else float(value)] for at, value in evidence.samples],
                default=str,
            ),
            evidence.baseline_w,
            evidence.sigma_w,
            float(evidence.delta_w),
            json.dumps([enum_value(flag) for flag in evidence.quality_flags]),
            evidence.topology_version_id,
            int(bool(evidence.finalized)),
        )

    #: The columns written by :meth:`put_evidence` / :meth:`upsert_evidence`.
    _EVIDENCE_BASE_COLUMNS: tuple[str, ...] = (
        "evidence_id",
        "reader_id",
        "measurement_event_id",
        "window_start",
        "window_end",
        "samples_json",
        "baseline_w",
        "sigma_w",
        "delta_w",
        "quality_flags_json",
        "topology_version_id",
        "finalized",
    )

    def upsert_evidence(self, evidence: EventEvidence) -> bool:
        """Revise the one live evidence row for this event/reader in place.

        Z8: a growing window must not append a new row per post-sample (that was
        the ~98-rows/event growth defect).  The row is keyed by its deterministic
        ``evidence_id`` -- one per ``(measurement_event_id, reader_id)`` -- so a
        repeat write updates the *same* row (samples_json replaced, not appended
        to) and bumps ``revision``.  Returns ``True`` when a row was inserted,
        ``False`` when an existing row was revised.  ``supersedes_evidence_id``
        is never touched here: it belongs only to late-data revisions.
        """
        columns = self._evidence_columns()
        names: list[str] = list(self._EVIDENCE_BASE_COLUMNS)
        values = list(self._evidence_values(evidence))
        if "supersedes_evidence_id" in columns:
            names.append("supersedes_evidence_id")
            values.append(None)
        if "revision" in columns:
            names.append("revision")
            values.append(0)
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != "evidence_id")
        if "revision" in columns:
            updates += ", revision=revision+1"
        with self._lock:
            self._conn.execute(
                f"INSERT INTO event_evidence({', '.join(names)}) VALUES({placeholders}) "
                f"ON CONFLICT(evidence_id) DO UPDATE SET {updates}",
                tuple(values),
            )
            row = self._conn.execute(
                "SELECT revision FROM event_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            # ``changes()`` after the ON CONFLICT is 1 for both insert and update,
            # so compare the revision to decide which happened.
            was_insert = row is not None and (
                "revision" not in columns or int(row[0]) == 0
            )
        return was_insert

    def clear_evidence_samples(self, evidence_id: str) -> int:
        """Reduce a superseded row's ``samples_json`` to a bounded summary.

        DEC-30 keeps the immutable summary (Δ, baseline, σ, quality) but not a
        duplicate of the raw window samples.  When a late-data revision is
        written, the predecessor keeps its audit row and its provenance link
        while its samples are thinned to the first and last points.  Returns the
        number of points dropped.  Idempotent (thinning an already-thin payload
        drops nothing).
        """
        from ..tiers import _thin_samples  # local import: avoids a module cycle

        with self._lock:
            row = self._conn.execute(
                "SELECT samples_json FROM event_evidence WHERE evidence_id=?", (str(evidence_id),)
            ).fetchone()
            if row is None:
                return 0
            kept, dropped = _thin_samples(row[0])
            if dropped:
                self._conn.execute(
                    "UPDATE event_evidence SET samples_json=? WHERE evidence_id=?",
                    (json.dumps(kept, default=str), str(evidence_id)),
                )
            return int(dropped)

    def finalize_evidence(self, evidence: EventEvidence) -> EventEvidence:
        """Mark the one live evidence row FINAL (in place, no new row).

        The window closes as CONFIRMED/UNRESOLVED; the single live row is updated
        to ``finalized=1`` so retention can treat it as durable evidence.  If no
        post-sample ever arrived (so the row was never inserted), the same
        deterministic id is inserted as FINAL.  Either way exactly one row exists
        for the event/reader after close.
        """
        # Update the stored row in place so its accumulated samples and revision
        # are preserved; the in-memory copy in ``evidence`` may be stale.  If no
        # post-sample ever arrived the row does not exist yet -- insert FINAL.
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event_evidence SET finalized=1 WHERE evidence_id=?",
                (evidence.evidence_id,),
            )
            inserted = cur.rowcount == 0
        if inserted:
            self.upsert_evidence(evidence.model_copy(update={"finalized": True}))
        return self.evidence(evidence.evidence_id)

    @staticmethod
    def evidence_from_row(row: sqlite3.Row | dict[str, Any]) -> EventEvidence:
        data = dict(row)
        # ``supersedes_evidence_id`` is provenance, not a contract field: the K02
        # model forbids extra fields, so it is read through ``supersedes_evidence_id``
        # below rather than passed to the model constructor.
        data.pop("supersedes_evidence_id", None)
        # ``revision`` is the Z8 in-place revision counter, not a contract field.
        data.pop("revision", None)
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

    # -- supersession provenance (migration 21) ---------------------------- #
    def supersedes_evidence_id(self, evidence_id: str) -> str | None:
        """The immutable revision this record supersedes, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT supersedes_evidence_id FROM event_evidence WHERE evidence_id=?",
                (str(evidence_id),),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def referencing_evidence_ids(self, evidence_id: str) -> tuple[str, ...]:
        """Ids of records that still reference ``evidence_id`` as superseded."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT evidence_id FROM event_evidence WHERE supersedes_evidence_id=? "
                "ORDER BY evidence_id",
                (str(evidence_id),),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

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

    def has_group_revision(self, group_id: str, revision: int) -> bool:
        """Whether ``(group_id, revision)`` is already stored.

        Group ids are content-derived (a hash of the member sub-event ids), so a
        restarted engine that replays HA states or backfills history re-derives
        groups it stored before.  P5 asks this before appending a revision so a
        replay adopts the stored row instead of violating the primary key.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM event_groups WHERE group_id=? AND revision=? LIMIT 1",
                (str(group_id), int(revision)),
            ).fetchone()
        return row is not None

    def update_group_lifecycle(self, group: EventGroup) -> None:
        """Record a lifecycle transition of an already-stored revision.

        Only the lifecycle columns (``status``, ``closed_at``) move: a revision's
        sub-events, reader deltas, grouping reasons and versions are immutable
        and a *content* change is always a new revision (``put_group_revision``).
        Without this, a group written at DETECTED/MATCHING stayed so on disk
        forever, ``closed_at`` was never set, and I4's history query
        (CONFIRMED/REVISED with ``closed_at``) could never see it.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE event_groups SET status=?, closed_at=? WHERE group_id=? AND revision=?",
                    (
                        enum_value(group.status),
                        iso(group.closed_at) if group.closed_at is not None else None,
                        str(group.group_id),
                        int(group.revision),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def put_group_subjects(
        self,
        group_id: str,
        revision: int,
        members: list[tuple[str, str, float | None, float | None, str | None]],
    ) -> int:
        """Record the subjects that took part in a stored group revision.

        ``members`` are ``(subject_kind, subject_id, delta_w, sigma_w,
        reader_id)``.  The reader evidence rows written by
        :meth:`put_group_revision` stay; these rows are what lets
        :meth:`history_rows_for_identifiability` answer "which subjects did this
        independent event touch" for I4.  Idempotent (``INSERT OR IGNORE``).
        """
        written = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for kind, subject_id, delta_w, sigma_w, reader_id in members:
                    cur = self._conn.execute(
                        """INSERT OR IGNORE INTO event_group_members(group_id, revision,
                        subject_kind, subject_id, delta_w, sigma_w, reader_id)
                        VALUES(?,?,?,?,?,?,?)""",
                        (str(group_id), int(revision), str(kind), str(subject_id),
                         delta_w, sigma_w, reader_id),
                    )
                    written += cur.rowcount
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return written

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
        result_payload = json.loads(data.pop("result_json") or "{}")
        # A MEASURED/IDENTIFIABLE estimate's zero-width bounds lose their trusted
        # flag when dumped to JSON.  Open the shared repair gate while rebuilding
        # the contract object, exactly as the S17 snapshot reader does.
        with repair_point_bounds(result_payload):
            data["result"] = JointInferenceResult(**result_payload)
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
