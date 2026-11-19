"""Learning samples and deterministic v3 learning observations.

The legacy ``learning_samples`` table keeps the collector's behaviour.  Migration
18 (S18) adds ``learning_observations`` (L4): device-attributed observations with
content-derived ids, written idempotently by :meth:`put_observation`.  A subject
is *dirty* when it has a confirmed observation newer than its active version's
evidence (the moment that version was fitted), which is how the background
learner (L6) re-derives its work queue without hidden in-memory state.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ...contracts import LearningObservation
from ..repositories import Repository, enum_value, iso


class LearningRepository(Repository):
    # -- v3 learning observations (S18) ---------------------------------- #
    def put_observation(self, observation: LearningObservation) -> bool:
        """Insert an observation idempotently by its deterministic id.

        Returns ``True`` when a row was written and ``False`` when the id was
        already present (a replay of the same event is a true no-op).  A plain
        ``INSERT`` -- never ``INSERT OR REPLACE`` -- keeps an existing row and
        its status intact.
        """
        ref = observation.event_group_ref
        with self._lock:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO learning_observations(
                    observation_id, subject_kind, subject_id, endpoint_id,
                    event_group_ref_json, transition, attributes_json, delta_w,
                    level_w, status, rejection_reason, identifiability, basis, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation.observation_id,
                    observation.subject_kind,
                    observation.subject_id,
                    observation.endpoint_id,
                    json.dumps(ref.model_dump(mode="json"), sort_keys=True),
                    observation.transition,
                    json.dumps(observation.attributes, sort_keys=True, default=str),
                    observation.delta_w,
                    observation.level_w,
                    observation.status,
                    observation.rejection_reason,
                    enum_value(observation.identifiability),
                    enum_value(observation.basis),
                    iso(observation.created_at),
                ),
            )
            return cursor.rowcount > 0

    def observations(
        self,
        subject_id: str,
        status: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """A subject's observations, oldest first, optionally status/time filtered."""
        q = "SELECT * FROM learning_observations WHERE subject_id=?"
        params: list[Any] = [subject_id]
        if status is not None:
            q += " AND status=?"
            params.append(status)
        if since is not None:
            q += " AND created_at>=?"
            params.append(iso(since))
        q += " ORDER BY created_at ASC, observation_id ASC"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["event_group_ref"] = json.loads(row["event_group_ref_json"])
            row["attributes"] = json.loads(row["attributes_json"])
        return out

    def dirty_subjects(self) -> list[tuple[str, str]]:
        """Subjects with a confirmed observation newer than their active version.

        "Newer" means later than the ``created_at`` of the subject's active
        version (the evidence cutoff).  A subject with no active version is dirty
        when it has any confirmed observation.  The active version is derived
        from the event log exactly as :meth:`.models.ModelsRepository.active_version`
        does -- last event ``activated``, with ``superseded``/``rolled_back``
        removing eligibility -- so the two never disagree.  The result is ordered
        and deduplicated so the learner's queue is deterministic.
        """
        with self._lock:
            # Latest confirmed observation per subject.
            obs = self._conn.execute(
                """SELECT subject_kind, subject_id, MAX(created_at) AS latest
                   FROM learning_observations WHERE status='confirmed'
                   GROUP BY subject_kind, subject_id"""
            ).fetchall()
            if not obs:
                return []
            # Every event joined to its version, in log order.  Derive, per
            # subject, the creation instant of the single active version.
            events = self._conn.execute(
                """SELECT v.subject_id, v.version_id, v.created_at AS version_created,
                          e.event, e.at, e.id
                   FROM power_model_events e
                   JOIN power_model_versions_v3 v ON v.version_id=e.version_id
                   ORDER BY e.at ASC, e.id ASC"""
            ).fetchall()

        # version_id -> last event, and subject -> active (at, created_at).
        last_event: dict[str, Any] = {}
        for event in events:
            last_event[event["version_id"]] = event
        active: dict[str, tuple[str, str]] = {}
        for event in events:
            if last_event[event["version_id"]]["event"] != "activated":
                continue
            key = str(event["subject_id"])
            candidate = (str(event["at"]), str(event["version_created"]))
            if key not in active or candidate[0] > active[key][0]:
                active[key] = candidate

        dirty: set[tuple[str, str]] = set()
        for row in obs:
            subject = (str(row["subject_kind"]), str(row["subject_id"]))
            active_info = active.get(str(row["subject_id"]))
            if active_info is None or str(row["latest"]) > active_info[1]:
                dirty.add(subject)
        return sorted(dirty)


__all__ = ["LearningRepository"]
