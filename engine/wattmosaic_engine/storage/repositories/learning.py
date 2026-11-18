"""Learning samples: the ``learning_samples`` table.

Captured observations that feed model fitting (classified L4).  Migration 18
(S18) adds ``learning_observations`` with deterministic ids; this table keeps the
existing collector's behaviour until then.
"""

from __future__ import annotations

import json
from typing import Any

from ...contracts import LearningSample, utcnow
from ..repositories import Repository, iso


class LearningRepository(Repository):
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

    def learning_samples(
        self, device_id: str | None = None, accepted_only: bool = True
    ) -> list[dict[str, Any]]:
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


__all__ = ["LearningRepository"]
