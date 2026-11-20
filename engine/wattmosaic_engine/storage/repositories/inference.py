"""Per-cycle inference output and the v17 estimate/snapshot persistence.

Deliberately separate from :mod:`.records`, the append-only revision chain that
evidence references resolve against.  This table is retention-pruned and polled
by every reader, so it is cached (see
:meth:`InferenceRepository.latest_inference_results`).

Migration 17 rebuilds ``inference_results`` with nullable power and the new
estimate columns; the v3 handoff contracts (:class:`JointInferenceResult`,
:class:`InferenceSnapshot`) are written here through :meth:`put_estimates`,
:meth:`put_snapshot` and friends.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from ...contracts import (
    Bounds,
    DeviceEstimate,
    InferenceSnapshot,
    JointInferenceResult,
    utcnow,
)
from ...contracts.foundation import _BOUNDS_ALLOW_POINT
from ..repositories import Repository, enum_value, iso

#: Dedicated columns of the rebuilt ``inference_results``, in INSERT order.
#: The first 21 are the legacy write path (identical to the pre-v17 SQL below);
#: the rest carry the v3 estimate fields.  The full estimate (confidence vector,
#: evidence refs, source readers, ...) is kept in ``estimate_json`` so a reader
#: can reconstruct a :class:`DeviceEstimate` without losing a field.
_ESTIMATE_COLUMNS: tuple[str, ...] = (
    "device_id",
    "timestamp",
    "power_w",
    "energy_kwh",
    "source",
    "confidence",
    "model_id",
    "model_version",
    "operating_stage",
    "stage_status",
    "sample_count",
    "quality_status",
    "measurement_age_seconds",
    "supporting_ct_channels_json",
    "explanation_json",
    "provenance_json",
    "confidence_tier",
    "monitoring_level",
    "power_behavior",
    "tier_reasons_json",
    "status",
    "identifiability",
    "lower_w",
    "upper_w",
    "bounds_basis",
    "group_id",
    "method",
    "level",
    "topology_version_id",
    "mapping_version_id",
    "model_set_id",
    "provisional",
    "estimate_json",
)


def _json_default(value: Any) -> Any:
    """Encode an enum as its value and a Pydantic model as a JSON-friendly dict."""
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(value, default=_json_default)


@contextmanager
def repair_point_bounds(payload: dict[str, Any]) -> Iterator[None]:
    """Open the trusted gate that admits a stored I5 point interval on read.

    A MEASURED / IDENTIFIABLE estimate legitimately carries a zero-width bound
    ``[p, p]`` (HB §8.5).  The public :class:`~wattmosaic_engine.contracts.Bounds`
    validator deliberately refuses such an interval from an untrusted payload —
    only the private ``Bounds._point`` constructor may open it, so a fabricated
    point can never enter through JSON (DEC-19).  When I5 dumps that estimate to
    storage the trusted flag is lost, so rebuilding the contract object from a
    stored JSON dict would raise.

    This helper prepares a *trusted, already-persisted* payload for validation:

    1. every zero-width bound dict with a justification ``basis`` is rebuilt as
       a :class:`Bounds` instance through the private ``Bounds._point`` path;
    2. the context-local gate is opened while the caller validates the parent
       object, so re-validating those nested instances is admitted.

    The gate is reset in a ``finally`` and is context-local, so a concurrent
    payload read never leaks into it.  This is the single shared repair used by
    the S17 snapshot reader and the S16 event-record reader (the same technique
    :mod:`mapping.projection` uses for a persisted ``CellPower`` point interval).
    Nothing widens an interval or changes a value: the payload is already
    persisted, trusted data, and the gate is open only for the duration of its
    validation, never for an untrusted/new payload.
    """
    for key in ("device_estimates", "group_estimates"):
        for estimate in payload.get(key, ()) or ():
            if not isinstance(estimate, dict):
                continue
            bounds = estimate.get("bounds")
            if not isinstance(bounds, dict):
                continue
            low, high, basis = bounds.get("lower_w"), bounds.get("upper_w"), bounds.get("basis")
            if low is not None and high is not None and low == high and basis is not None:
                estimate["bounds"] = Bounds._point(float(low), basis)  # type: ignore[arg-type]
    token = _BOUNDS_ALLOW_POINT.set(True)
    try:
        yield
    finally:
        _BOUNDS_ALLOW_POINT.reset(token)


def _estimate_row(estimate: DeviceEstimate, versions: Any, at: str) -> tuple[Any, ...]:
    """One row of ``inference_results`` for a v3 device estimate.

    A non-identifiable member has ``point_w=None`` (never a fabricated zero).
    The legacy columns that have no v3 counterpart take neutral, honest values:
    ``source`` is the method, ``confidence`` is the identifiability component
    of the confidence vector when present, and an explicit ``estimate_json``
    carries everything a reader needs to rebuild the typed estimate.

    HB §18: an absent confidence component is ``None``, never ``0``.  The
    ``confidence`` column is nullable (migration 22), so a valid estimate whose
    :class:`~wattmosaic_engine.contracts.ConfidenceVectorV3` has no
    identifiability component is persisted as ``NULL`` rather than failing a
    ``NOT NULL`` constraint.
    """
    bounds = estimate.bounds
    confidence = estimate.confidence
    confidence_score = (
        confidence.identifiability if confidence is not None else None
    )
    return (
        estimate.subject_id,
        iso(estimate.updated_at) or at,
        estimate.point_w,
        None,  # energy_kwh -- a v3 estimate has no energy figure
        enum_value(estimate.method),
        confidence_score,
        None,  # model_id
        None,  # model_version (the typed id is in estimate_json)
        None,  # operating_stage
        enum_value(estimate.status),
        0,  # sample_count
        enum_value(estimate.status),  # quality_status
        0.0,  # measurement_age_seconds
        _dump(list(estimate.source_reader_ids)),
        _dump([ref.model_dump(mode="json") for ref in estimate.evidence_refs]),
        _dump({"result_fields": True}),
        "tier_0_unknown",  # confidence_tier
        enum_value(estimate.status),  # monitoring_level
        "unknown",  # power_behavior
        "[]",  # tier_reasons_json
        enum_value(estimate.status),
        enum_value(estimate.identifiability),
        bounds.lower_w,
        bounds.upper_w,
        enum_value(bounds.basis) if bounds.basis is not None else None,
        estimate.group_id,
        enum_value(estimate.method),
        enum_value(estimate.level) if estimate.level is not None else None,
        versions.topology_version_id,
        versions.mapping_version_id,
        versions.model_set_id,
        int(bool(estimate.provisional)),
        _dump(estimate.model_dump(mode="json")),
    )


class InferenceRepository(Repository):
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

    # -- v3 joint result -------------------------------------------------- #
    def put_estimates(self, result: JointInferenceResult) -> int:
        """Persist every device estimate of a joint result.

        All estimates are written in one transaction so a reader never sees a
        half-published result.  The latest-results cache is invalidated on a
        successful commit, matching the legacy write path.
        """
        at = iso(result.computed_at) or iso(utcnow())
        payload = [
            _estimate_row(estimate, result.versions, at)
            for estimate in result.device_estimates
        ]
        if not payload:
            return 0
        placeholders = ", ".join("?" for _ in _ESTIMATE_COLUMNS)
        sql = (
            f"INSERT INTO inference_results({', '.join(_ESTIMATE_COLUMNS)}) "  # noqa: S608
            f"VALUES({placeholders})"
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(sql, payload)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self.db.invalidate_latest_results()
        return len(payload)

    def latest_estimates(self) -> list[dict[str, Any]]:
        """The newest persisted v3 device estimate per subject, decoded."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM inference_results WHERE id IN
                (SELECT MAX(id) FROM inference_results GROUP BY device_id)
                AND estimate_json IS NOT NULL ORDER BY device_id"""
            ).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["estimate"] = json.loads(row["estimate_json"])
        return out

    # -- v3 snapshots (sparse) -------------------------------------------- #
    @staticmethod
    def _snapshot_content(snapshot: InferenceSnapshot) -> dict[str, Any]:
        """The change-sensitive part of a snapshot.

        Timestamps and the snapshot id are deliberately excluded: a re-publish
        of identical content at a later instant must not write a new row.
        """
        return {
            "versions": snapshot.versions.model_dump(mode="json"),
            "reader_measurements": [str(m) for m in snapshot.reader_measurements],
            "device_estimates": [e.model_dump(mode="json") for e in snapshot.device_estimates],
            "group_estimates": [g.model_dump(mode="json") for g in snapshot.group_estimates],
            "residual_estimates": [str(r) for r in snapshot.residual_estimates],
            "reconciliation": snapshot.reconciliation.model_dump(mode="json"),
            "system_quality": snapshot.system_quality.model_dump(mode="json"),
            "stale_sources": list(snapshot.stale_sources),
            "unresolved_events": list(snapshot.unresolved_events),
        }

    def put_snapshot(self, snapshot: InferenceSnapshot) -> bool:
        """Persist a snapshot only when its content changes.

        Returns ``True`` when a row was written and ``False`` for a duplicate
        (the latest stored content hash already matches).  A second call with an
        identical snapshot is a true no-op, which is what keeps a 60 s heartbeat
        from filling the table.
        """
        content = self._snapshot_content(snapshot)
        content_hash = hashlib.sha256(_dump(content).encode("utf-8")).hexdigest()
        with self._lock:
            latest = self._conn.execute(
                "SELECT content_hash FROM inference_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest is not None and latest[0] == content_hash:
                return False
            if self._conn.execute(
                "SELECT 1 FROM inference_snapshots WHERE snapshot_id = ? LIMIT 1",
                (snapshot.snapshot_id,),
            ).fetchone() is not None:
                # The id is content-derived: an older row (e.g. written before a
                # restart that re-derived the same state) already holds exactly
                # this snapshot.  Adopt it; never re-insert.
                return False
            self._conn.execute(
                """INSERT INTO inference_snapshots(
                    snapshot_id, observed_at, computed_at, topology_version_id,
                    mapping_version_id, model_set_id, content_hash, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot.snapshot_id,
                    iso(snapshot.observed_at),
                    iso(snapshot.computed_at),
                    snapshot.versions.topology_version_id,
                    snapshot.versions.mapping_version_id,
                    snapshot.versions.model_set_id,
                    content_hash,
                    _dump(snapshot.model_dump(mode="json")),
                    iso(utcnow()),
                ),
            )
        return True

    def latest_snapshot(self) -> dict[str, Any] | None:
        """The last persisted snapshot, with its payload decoded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM inference_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(row["payload_json"])
        return result

    def snapshot_as_of(self, at: datetime) -> dict[str, Any] | None:
        """The last persisted snapshot observed at or before ``at`` (HB §9.9).

        Snapshots are append-only, so this as-of read is what a later change
        (e.g. a calibration declaration) can never rewrite.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM inference_snapshots WHERE observed_at <= ? "
                "ORDER BY observed_at DESC, id DESC LIMIT 1",
                (iso(at),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(row["payload_json"])
        return result

    # -- v3 group estimates ------------------------------------------------ #
    def put_group_estimate(self, group: Any) -> None:
        """Append one group estimate row.  ``(group_id, at)`` is unique, so a
        repeated publish for the same instant is idempotent.
        """
        bounds = group.bounds
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO device_group_estimates(
                    group_id, at, point_w, lower_w, upper_w, bounds_basis, identifiability,
                    member_ids_json, source_reader_ids_json, evidence_refs_json,
                    topology_version_id, mapping_version_id, model_set_id, estimate_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group.group_id,
                    iso(group.updated_at) or iso(utcnow()),
                    group.point_w,
                    bounds.lower_w,
                    bounds.upper_w,
                    enum_value(bounds.basis) if bounds.basis is not None else None,
                    enum_value(group.identifiability),
                    _dump(list(group.member_ids)),
                    _dump(list(group.source_reader_ids)),
                    _dump([ref.model_dump(mode="json") for ref in group.evidence_refs]),
                    group.versions.topology_version_id,
                    group.versions.mapping_version_id,
                    group.versions.model_set_id,
                    _dump(group.model_dump(mode="json")),
                ),
            )

    def group_history(self, group_id: str, since: datetime) -> list[dict[str, Any]]:
        """A group's estimate history since ``since``, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM device_group_estimates
                WHERE group_id=? AND at>=? ORDER BY at ASC, id ASC""",
                (group_id, iso(since)),
            ).fetchall()
        out = [dict(row) for row in rows]
        for row in out:
            row["estimate"] = json.loads(row["estimate_json"])
        return out


__all__ = ["InferenceRepository", "repair_point_bounds"]
