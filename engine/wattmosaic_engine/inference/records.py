"""Versioned event inference records (I6, HB §12.2, design §7).

An :class:`EventInferenceRecord` is a *versioned interpretation* of one event
group.  The evidence it was built from is immutable: appending a revision never
edits an earlier record or its evidence bytes.  A record carries the event-group
:class:`~wattmosaic_engine.contracts.EventGroupStatus` (PROVISIONAL on first
publication, REVISED after late data, CONFIRMED/UNRESOLVED on window closure) and
the full :class:`~wattmosaic_engine.contracts.JointInferenceResult`.

Two layers are provided:

* :class:`RecordWriter` — the append-only primitive.  ``write(result, reason)``
  allocates the next ``interpretation_revision``, links ``previous_record_id``
  to the current head and persists via an optional S16 repository.  It never
  edits an existing revision.
* :class:`EventRecordStore` — the stateful-but-injected facade used by callers
  (and by AT-18).  It holds the records, their immutable evidence bytes and the
  head per group; every mutating operation delegates to :class:`RecordWriter`.

Production code depends only on contracts and the injected repository protocol;
it never imports ``runtime.fakes``.  No clock is read here: every ``at`` is
supplied by the caller, so replay is deterministic (L7, AT-19).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import model_validator

from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EventGroupStatus,
    EventInferenceRecord,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    UnknownEstimate,
    VersionTriple,
)

__all__: list[str] = [
    "VersionedEventRecord",
    "RecordWriter",
    "EventRecordStore",
    "build_initial_result",
    "revise_result",
    "DEFAULT_VERSIONS",
]

Reason = Literal[
    "initial", "late_data", "window_closed", "model_update", "topology_update", "replay"
]

#: Neutral version triple used when a caller does not supply one.  A real caller
#: (I7/P6) passes the operator's current triple from D1.
DEFAULT_VERSIONS = VersionTriple(
    topology_version_id="topo-0",
    mapping_version_id="map-0",
    model_set_id="mset-unspecified",
)

#: Deterministic fallback timestamp.  This is a fixed constant, never the wall
#: clock: a caller that cares about real time injects ``at`` explicitly.  A
#: fixed default keeps simple callers reproducible under replay (L7, AT-19).
DEFAULT_AT = datetime(2026, 1, 1, tzinfo=UTC)


class VersionedEventRecord(EventInferenceRecord):
    """An :class:`EventInferenceRecord` with a fast-read event status.

    ``status`` mirrors ``result.status``; it is stored on the record so a reader
    (e.g. ``GET /events/{group_id}``) need not deserialize the whole result just
    to report where the group is in the HB §12.3 lifecycle.
    """

    status: EventGroupStatus

    @model_validator(mode="after")
    def _status_matches_result(self) -> VersionedEventRecord:
        if self.status != self.result.status:
            raise ValueError("record status must match result.status")
        return self

    @property
    def group_id(self) -> str:
        """Alias for ``event_group_id`` (readers and AT-18 use either name)."""
        return self.event_group_id


class RecordSink(Protocol):
    """The S16 append surface :class:`RecordWriter` persists through."""

    def put_record(self, record: EventInferenceRecord) -> None: ...


# --------------------------------------------------------------------------- #
# Result builders (pure).
# --------------------------------------------------------------------------- #


def _stable_id(prefix: str, parts: list[Any]) -> str:
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def build_initial_result(
    *,
    group_id: str,
    readers: list[str] | tuple[str, ...],
    at: datetime,
    versions: VersionTriple = DEFAULT_VERSIONS,
    status: EventGroupStatus = EventGroupStatus.PROVISIONAL,
) -> JointInferenceResult:
    """The first, fast PROVISIONAL interpretation of a group.

    Only the readers that have actually reported are represented; every absent
    delta stays ``None`` (HB §18) and no device is attributed yet.  This is the
    DEC-15 behaviour: publish promptly from the earliest reader, never fabricate
    an attribution before the corroborating readers arrive.
    """
    return JointInferenceResult(
        event_group_id=group_id,
        event_group_revision=1,
        status=status,
        device_estimates=[],
        group_estimates=[],
        unknown_estimates=[
            UnknownEstimate(scope_reader_id=reader, delta_w=None) for reader in readers
        ],
        candidates=[],
        matches={},
        residual_w_by_reader={reader: None for reader in readers},
        versions=versions,
        computed_at=at,
    )


def _rebuild_result(
    previous: JointInferenceResult, **changes: Any
) -> JointInferenceResult:
    """Rebuild a result with overrides, re-deriving its content ``result_id``.

    ``model_copy`` bypasses Pydantic's ``mode="before"`` validator, so a changed
    model-set version would otherwise leave a stale ``result_id``.  Dropping the
    old id lets the contract derive a fresh content hash from the new fields.
    """
    payload = previous.model_dump(mode="python")
    payload.pop("result_id", None)
    payload.update(changes)
    return JointInferenceResult.model_validate(payload)


def revise_result(
    previous: JointInferenceResult,
    *,
    reader: str,
    delta_w: float,
    at: datetime,
    status: EventGroupStatus = EventGroupStatus.REVISED,
    versions: VersionTriple | None = None,
    model_version_id: str | None = None,
) -> JointInferenceResult:
    """A new result that attributes ``delta_w`` to a late reader.

    ``previous`` is never mutated.  The new device estimate is a genuine
    attribution (ESTIMATED / JOINT_DELTA) with justified bounds, so ``result_id``
    changes and a revision is distinguishable from its predecessor (AT-18).
    """
    subject_id = f"candidate::{reader}"
    estimate = DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=float(delta_w),
        bounds=Bounds(lower_w=delta_w - 1.0, upper_w=delta_w + 1.0, basis="posterior"),
        status=EstimateStatus.ESTIMATED,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=[reader],
        evidence_refs=[],
        versions=versions or previous.versions,
        model_version_id=model_version_id,
        updated_at=at,
        provisional=True,
    )
    unknown = [
        u for u in previous.unknown_estimates if u.scope_reader_id != reader
    ]
    residual = dict(previous.residual_w_by_reader)
    residual[reader] = None
    return JointInferenceResult(
        event_group_id=previous.event_group_id,
        event_group_revision=previous.event_group_revision,
        status=status,
        device_estimates=[*previous.device_estimates, estimate],
        group_estimates=list(previous.group_estimates),
        unknown_estimates=unknown,
        candidates=list(previous.candidates),
        matches=dict(previous.matches),
        residual_w_by_reader=residual,
        versions=versions or previous.versions,
        computed_at=at,
    )


# --------------------------------------------------------------------------- #
# Append-only writer.
# --------------------------------------------------------------------------- #


@dataclass
class RecordWriter:
    """Appends versioned interpretations to one event group.

    Every ``write`` links to the current head and increments the revision.  The
    writer holds no head of its own: the owning :class:`EventRecordStore`
    supplies it, which keeps the append rule in one place.
    """

    group_id: str
    sink: RecordSink | None = None

    def write(
        self,
        result: JointInferenceResult,
        reason: Reason,
        *,
        previous: VersionedEventRecord | None,
        at: datetime,
        record_id: str | None = None,
        status: EventGroupStatus | None = None,
    ) -> VersionedEventRecord:
        """Append one revision; ``previous`` is ``None`` only for revision 1."""
        revision = 1 if previous is None else previous.interpretation_revision + 1
        current_status = status or result.status
        rid = record_id or _stable_id(
            "rec", [self.group_id, revision, getattr(result, "result_id", None), str(reason)]
        )
        record = VersionedEventRecord(
            record_id=rid,
            event_group_id=self.group_id,
            interpretation_revision=revision,
            previous_record_id=None if previous is None else previous.record_id,
            result=result,
            reason=reason,
            created_at=at,
            status=current_status,
        )
        if self.sink is not None:
            self.sink.put_record(record)
        return record


# --------------------------------------------------------------------------- #
# Stateful-but-injected store facade.
# --------------------------------------------------------------------------- #


@dataclass
class EventRecordStore:
    """In-memory record store with immutable evidence bytes and a head per group.

    A caller may inject an S16 :class:`RecordSink` (e.g. ``db.repos.evidence``)
    so every append is also persisted; with no sink the store is fully in-memory
    and deterministic, which is what the acceptance test exercises.
    """

    sink: RecordSink | None = None
    records: dict[str, VersionedEventRecord] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, bytes] = field(default_factory=dict)
    group_evidence: dict[str, bytes] = field(default_factory=dict)
    _group_seq: int = 0

    def _new_group_id(self, readers: Sequence[str]) -> str:
        self._group_seq += 1
        return _stable_id("grp", [list(readers), self._group_seq])

    @staticmethod
    def _evidence_bytes(readers: Sequence[str], deltas: dict[str, float | None]) -> bytes:
        """Canonical, immutable evidence snapshot for a group's readers."""
        payload = {
            "readers": list(readers),
            "deltas": {reader: deltas.get(reader) for reader in readers},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )

    def _writer(self, group_id: str) -> RecordWriter:
        return RecordWriter(group_id=group_id, sink=self.sink)

    def _append(
        self,
        group_id: str,
        result: JointInferenceResult,
        reason: Reason,
        *,
        at: datetime,
        status: EventGroupStatus | None = None,
    ) -> VersionedEventRecord:
        head_id = self.heads.get(group_id)
        previous = self.records[head_id] if head_id is not None else None
        record = self._writer(group_id).write(
            result, reason, previous=previous, at=at, status=status
        )
        # Append only: an existing id never gets overwritten.
        if record.record_id in self.records:
            raise ValueError(f"record id collision: {record.record_id}")
        self.records[record.record_id] = record
        self.heads[group_id] = record.record_id
        # Evidence is frozen at the group level.  A revision links back to the
        # same immutable bytes; getters are therefore stable under revision.
        self.evidence[record.record_id] = self.group_evidence.get(group_id, b"")
        return record

    def open_provisional(
        self,
        readers: list[str] | tuple[str, ...],
        *,
        at: datetime = DEFAULT_AT,
        versions: VersionTriple = DEFAULT_VERSIONS,
        group_id: str | None = None,
    ) -> VersionedEventRecord:
        """Open a group and publish its first PROVISIONAL interpretation.

        ``at`` is injected (no wall clock).  ``group_id`` may be supplied by the
        caller (e.g. the P5 group id) to keep the record aligned with the group
        that produced it; otherwise a deterministic id is allocated.
        """
        gid = group_id or self._new_group_id(readers)
        if gid not in self.group_evidence:
            self.group_evidence[gid] = self._evidence_bytes(
                readers, {reader: None for reader in readers}
            )
        result = build_initial_result(
            group_id=gid, readers=readers, at=at, versions=versions
        )
        return self._append(gid, result, "initial", at=at)

    def apply_late_sample(
        self,
        group_id: str,
        *,
        reader: str,
        delta_w: float,
        late_by_s: float,
        at: datetime = DEFAULT_AT,
        versions: VersionTriple | None = None,
        model_version_id: str | None = None,
    ) -> VersionedEventRecord:
        """Append a REVISED interpretation after a late reader sample.

        ``late_by_s`` documents how late the sample was (DEC-15); it is recorded
        in the revision reason trail but never changes the original evidence.
        """
        head_id = self.heads.get(group_id)
        if head_id is None:
            raise KeyError(f"unknown event group: {group_id}")
        previous = self.records[head_id]
        revised = revise_result(
            previous.result,
            reader=reader,
            delta_w=delta_w,
            at=at,
            status=EventGroupStatus.REVISED,
            versions=versions,
            model_version_id=model_version_id,
        )
        return self._append(
            group_id,
            revised,
            "late_data",
            at=at,
        )

    def close_window(
        self,
        group_id: str,
        *,
        at: datetime = DEFAULT_AT,
        status: EventGroupStatus = EventGroupStatus.CONFIRMED,
        versions: VersionTriple | None = None,
    ) -> VersionedEventRecord:
        """Append the window-closure interpretation (CONFIRMED or UNRESOLVED)."""
        if status not in (
            EventGroupStatus.CONFIRMED,
            EventGroupStatus.UNRESOLVED,
        ):
            raise ValueError("window closure must be CONFIRMED or UNRESOLVED")
        head_id = self.heads.get(group_id)
        if head_id is None:
            raise KeyError(f"unknown event group: {group_id}")
        previous = self.records[head_id]
        result = _rebuild_result(previous.result, status=status, computed_at=at)
        if versions is not None:
            result = _rebuild_result(result, versions=versions)
        return self._append(group_id, result, "window_closed", at=at, status=status)

    def revise_for_update(
        self,
        group_id: str,
        *,
        reason: Literal["model_update", "topology_update", "replay"],
        at: datetime = DEFAULT_AT,
        versions: VersionTriple | None = None,
        result: JointInferenceResult | None = None,
    ) -> VersionedEventRecord:
        """Append a revision for a model/topology update or a historical replay.

        A model-version change appends a ``replay`` revision (AT-19 support)
        while leaving every earlier interpretation byte-identical.
        """
        head_id = self.heads.get(group_id)
        if head_id is None:
            raise KeyError(f"unknown event group: {group_id}")
        previous = self.records[head_id]
        if result is None:
            result = _rebuild_result(previous.result, computed_at=at)
            if versions is not None:
                result = _rebuild_result(result, versions=versions)
        return self._append(group_id, result, reason, at=at)

    def get(self, record_id: str) -> VersionedEventRecord | None:
        """The exact stored record object; its bytes never change after append."""
        return self.records.get(str(record_id))

    def latest(self, group_id: str) -> VersionedEventRecord | None:
        head_id = self.heads.get(group_id)
        return self.records[head_id] if head_id is not None else None

    def history(self, group_id: str) -> list[VersionedEventRecord]:
        """All revisions of a group, oldest first (immutable, traceable)."""
        head = self.heads.get(group_id)
        chain: list[VersionedEventRecord] = []
        seen: set[str] = set()
        while head is not None and head in self.records and head not in seen:
            seen.add(head)
            record = self.records[head]
            chain.append(record)
            head = record.previous_record_id
        if not chain:  # no head set (e.g. records loaded directly) -> sort by revision
            chain = sorted(
                (r for r in self.records.values() if r.event_group_id == group_id),
                key=lambda r: r.interpretation_revision,
            )
        return list(reversed(chain))

    def evidence_bytes(self, record_id: str) -> bytes:
        """The immutable evidence snapshot attached to one record.

        A missing record has empty bytes (there is no evidence to report), never
        a fabricated placeholder.  Once written these bytes are never edited.
        """
        return self.evidence.get(str(record_id), b"")
