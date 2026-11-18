"""Per-cycle inference output: the ``inference_results`` table.

Deliberately separate from :mod:`.records`, the append-only revision chain that
evidence references resolve against.  This table is retention-pruned and polled
by every reader, so it is cached (see
:meth:`InferenceRepository.latest_inference_results`).  Migration 17 (S17)
rebuilds it with nullable power, status, identifiability, bounds and version
columns; the SQL here is the pre-v17 shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...contracts import InferenceResult
from ..repositories import Repository, enum_value, iso


class InferenceRepository(Repository):
    def insert_inference_results(self, results: Iterable[InferenceResult]) -> int:
        payload = [
            (
                r.device_id, iso(r.timestamp), r.power_w, r.energy_kwh, r.source, r.confidence,
                r.model_id, r.model_version, r.operating_stage, r.stage_status, r.sample_count,
                r.quality_status, r.measurement_age_seconds, json.dumps(r.supporting_ct_channels),
                json.dumps(r.explanation), json.dumps(r.provenance, default=str),
                enum_value(r.confidence_tier),
                enum_value(r.monitoring_level),
                enum_value(r.power_behavior),
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
            # Invalidate the latest-results cache only after a successful commit,
            # so a rolled-back batch cannot stampede readers into a needless
            # re-scan.  The generation counter also guards a reader that opened
            # its snapshot before this write committed (see the method below).
            self.db.invalidate_latest_results()
        return len(payload)

    def latest_inference_results(self) -> list[dict[str, Any]]:
        """The newest result per device, cached across reads until the next write.

        The underlying query is a covering-index scan of the whole table (~24 ms
        warm, ~590 ms cold at the production 343k rows).  The panel fires it from
        both ``/snapshot`` and ``/devices`` in one poll, and the overview repeats
        that every 8 s, so the same full scan ran several times per refresh.  A
        covering index on ``(device_id, id)`` does **not** help: measured on a
        120k-row synthetic table the plan still reported ``SCAN inference_results
        USING COVERING INDEX`` and the read only improved 7.51 ms -> 6.62 ms.
        The expensive part is touching every historical row at all, which an
        index cannot avoid for ``MAX(id) GROUP BY device_id``.

        Instead, cache the result for the lifetime of the data it describes.  The
        cache is keyed to ``Database._write_generation``, bumped on every
        committed ``inference_results`` insert under the lock.  A second
        connection is opened via ``Database._reader()`` so the long scan never
        holds the lock (a concurrent ``ping()`` on ``/health`` must not queue
        behind it); because a reader can capture its snapshot before a concurrent
        write commits, it rechecks the generation after the query and caches only
        if it is still current.  The next read after any write re-scans and
        refreshes the cache, so no stale result survives a write.  ``prune`` and
        ``reset`` also bump the generation so a deleted/reset database is never
        served old rows.
        """
        with self._lock:
            generation = self.db._write_generation
            cached = self.db._latest_results_cache
            if cached is not None and generation == self.db._cache_generation:
                return cached
        with self._reader() as conn:
            rows = conn.execute(
                """SELECT * FROM inference_results WHERE id IN
                (SELECT MAX(id) FROM inference_results GROUP BY device_id) ORDER BY device_id"""
            ).fetchall()
        result = [dict(r) for r in rows]
        with self._lock:
            # Only cache a snapshot that still reflects the committed data: if a
            # write landed while we were scanning, leave the cache invalid so the
            # next read re-scans instead of serving a stale map.
            if self.db._write_generation == generation:
                self.db._latest_results_cache = result
                self.db._cache_generation = generation
        return result

    def inference_history(self, device_id: str, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM inference_results WHERE device_id=? AND timestamp>=?
                ORDER BY timestamp DESC LIMIT ?""",
                (device_id, iso(since), limit),
            ).fetchall()
        return [dict(r) for r in rows]


__all__ = ["InferenceRepository"]
