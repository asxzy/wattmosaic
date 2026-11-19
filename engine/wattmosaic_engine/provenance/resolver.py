"""Typed provenance resolver and the nine HB §18 answers (D4).

A published :class:`~wattmosaic_engine.contracts.DeviceEstimate` carries only
identifiers: reader ids, evidence refs and version ids.  This module turns those
identifiers into human- and machine-checkable answers without ever reading a
measurement value from a place the estimate did not name.  It is pure: all state
is injected (a snapshot, an optional evidence lookup and a clock), so the same
inputs always produce the same :class:`ProvenanceAnswer`.

The nine questions (HB §18) are, in order:

1. which Reader measurements contributed;
2. which event associations contributed;
3. which device power model was used;
4. which topology version was used;
5. which mapping version was used;
6. is the output measured, estimated, allocated or unknown;
7. is the individual device identifiable;
8. what are the valid lower and upper bounds;
9. when was the estimate last updated.

``upper_w``/``lower_w`` are nullable (HB §18): an absent bound stays ``None``,
never ``0.0``.  A group member never gets an individual value — the resolver
reports the group id and the group's bounds instead of inventing a number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from wattmosaic_engine.contracts import (
    Bounds,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceKind,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceSnapshot,
    VersionTriple,
)

__all__: list[str] = [
    "EvidenceLookup",
    "SnapshotEvidenceLookup",
    "ProvenanceAnswer",
    "ProvenanceResolver",
    "answer_for",
    "QUESTION_KEYS",
]

#: Stable machine-readable keys for the nine HB §18 questions, in order.
QUESTION_KEYS: tuple[str, ...] = (
    "reader_measurements",
    "event_associations",
    "model_version_id",
    "topology_version_id",
    "mapping_version_id",
    "status",
    "identifiability",
    "bounds",
    "updated_at",
)


# --------------------------------------------------------------------------- #
# Evidence resolution.
# --------------------------------------------------------------------------- #


@runtime_checkable
class EvidenceLookup(Protocol):
    """A read-only source of resolved evidence for an :class:`EvidenceRef`."""

    def resolve(self, ref: EvidenceRef) -> Any | None: ...


class SnapshotEvidenceLookup:
    """Resolve evidence refs against one in-memory snapshot.

    The snapshot is the authoritative published state (HB §15: no hidden state),
    so a reference that is not represented there is *unresolved* — it is reported
    as ``None`` rather than guessed from a database connection or a wall clock.
    Reader measurements are keyed by ``reader_id``; the version triple answers
    version refs; evidence refs that name other kinds can be supplied through
    ``extra`` by the caller.
    """

    def __init__(
        self,
        snapshot: InferenceSnapshot,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._extra = dict(extra or {})

    def resolve(self, ref: EvidenceRef) -> Any | None:
        kind = str(getattr(ref, "kind", ref))
        identifier = str(getattr(ref, "id", ""))
        key = f"{kind}:{identifier}"
        if key in self._extra:
            return self._extra[key]
        if kind == EvidenceKind.SAMPLE.value:
            return next(
                (m for m in self._snapshot.reader_measurements if m.reader_id == identifier),
                None,
            )
        if kind == EvidenceKind.OBSERVATION.value:
            return next(
                (m for m in self._snapshot.reader_measurements if m.reader_id == identifier),
                None,
            )
        versions: VersionTriple = self._snapshot.versions
        if kind == "edge":
            return identifier if identifier == versions.topology_version_id else None
        if kind == "mapping_edge":
            return identifier if identifier == versions.mapping_version_id else None
        if kind == "model_version":
            return identifier if identifier == versions.model_set_id else None
        return None


def _parse_ref(ref: Any) -> EvidenceRef | None:
    if isinstance(ref, EvidenceRef):
        return ref
    if isinstance(ref, Mapping):
        try:
            return EvidenceRef.model_validate(dict(ref))
        except Exception:  # noqa: BLE001 - a malformed ref is unresolved, not fatal
            return None
    return None


def _ref_key(ref: EvidenceRef) -> str:
    return f"{ref.kind}:{ref.id}"


# --------------------------------------------------------------------------- #
# The typed answer (§9 "typed ProvenanceAnswer").
# --------------------------------------------------------------------------- #


class ProvenanceAnswer:
    """The typed answer to the nine HB §18 questions for one estimate.

    Every field is read-only in spirit (the class is a plain immutable view of
    already-computed data).  ``reader_measurements`` and ``event_associations``
    are lists of resolved evidence, not raw refs; each entry is ``None`` only
    when the corresponding ref cannot be resolved from the injected evidence.
    """

    __slots__ = (
        "estimate_id",
        "estimate_kind",
        "reader_measurements",
        "event_associations",
        "unresolved_refs",
        "model_version_id",
        "topology_version_id",
        "mapping_version_id",
        "status",
        "identifiability",
        "lower_w",
        "upper_w",
        "bounds_basis",
        "updated_at",
        "group_id",
        "point_w",
        "questions",
    )

    def __init__(
        self,
        *,
        estimate_id: str,
        estimate_kind: Literal["device", "group"],
        reader_measurements: list[Any],
        event_associations: list[Any],
        unresolved_refs: list[str],
        model_version_id: str | None,
        versions: VersionTriple,
        status: EstimateStatus | str | None,
        identifiability: IdentifiabilityStatus | str,
        bounds: Bounds,
        updated_at: datetime,
        group_id: str | None = None,
        point_w: float | None = None,
    ) -> None:
        self.estimate_id = estimate_id
        self.estimate_kind = estimate_kind
        self.reader_measurements = tuple(reader_measurements)
        self.event_associations = tuple(event_associations)
        self.unresolved_refs = tuple(unresolved_refs)
        self.model_version_id = model_version_id
        self.topology_version_id = versions.topology_version_id
        self.mapping_version_id = versions.mapping_version_id
        self.status = _enum_value(status)
        self.identifiability = _enum_value(identifiability)
        self.lower_w = bounds.lower_w
        self.upper_w = bounds.upper_w
        self.bounds_basis = _enum_value(bounds.basis)
        self.updated_at = updated_at
        self.group_id = group_id
        self.point_w = point_w
        # Question 9's answer includes the estimate's point value and group id
        # alongside the nine HB §18 fields, so provenance answers are complete
        # and can stand in for the estimate itself at the HTTP boundary.
        self.questions = {**self._build_questions(), "point_w": point_w, "group_id": group_id}

    def _build_questions(self) -> dict[str, Any]:
        """The nine answers, keyed by :data:`QUESTION_KEYS`."""
        return {
            "reader_measurements": list(self.reader_measurements),
            "event_associations": list(self.event_associations),
            "model_version_id": self.model_version_id,
            "topology_version_id": self.topology_version_id,
            "mapping_version_id": self.mapping_version_id,
            "status": self.status,
            "identifiability": self.identifiability,
            "bounds": {
                "lower_w": self.lower_w,
                "upper_w": self.upper_w,
                "basis": self.bounds_basis,
            },
            "updated_at": self.updated_at,
        }

    def answer(self, question: str) -> Any:
        """Answer one of the nine questions by its :data:`QUESTION_KEYS` key."""
        try:
            return self.questions[question]
        except KeyError as exc:
            raise KeyError(
                f"unknown HB §18 question {question!r}; expected one of {QUESTION_KEYS}"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view of all nine answers (datetimes left as objects)."""
        return dict(self.questions)

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        """Pydantic-compatible dump so the HTTP layer can serialise the answer.

        ``mode="json"`` renders datetimes as ISO strings; the default keeps them
        as ``datetime`` objects for in-process callers.
        """
        result = self.as_dict()
        if mode == "json":
            result["updated_at"] = self.updated_at.isoformat()
            if getattr(self, "reader_measurements", None) is not None:
                result["reader_measurements"] = [
                    m.model_dump(mode="json") if hasattr(m, "model_dump") else m
                    for m in self.reader_measurements
                ]
                result["event_associations"] = [
                    e.model_dump(mode="json") if hasattr(e, "model_dump") else e
                    for e in self.event_associations
                ]
        return result

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"ProvenanceAnswer(estimate_id={self.estimate_id!r}, status={self.status!r}, "
            f"identifiability={self.identifiability!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProvenanceAnswer):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __hash__(self) -> int:  # pragma: no cover - identity convenience
        return hash((self.estimate_id, self.status, self.identifiability))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


# --------------------------------------------------------------------------- #
# The resolver.
# --------------------------------------------------------------------------- #


class ProvenanceResolver:
    """Answer the nine HB §18 questions for estimates in a snapshot.

    ``lookup`` may be any object with ``resolve(ref) -> Any | None``.  When it is
    omitted, :class:`SnapshotEvidenceLookup` is used so every reference is
    resolved strictly against the published snapshot.  A database-backed caller
    can pass a small adapter that delegates to ``Database.resolve_evidence_ref``
    (or a repository) without coupling this module to storage.
    """

    #: Kinds of evidence ref that count as "event associations" (question 2).
    EVENT_REF_KINDS: frozenset[str] = frozenset(
        {
            EvidenceKind.EVENT_EVIDENCE.value,
            EvidenceKind.EVENT_GROUP.value,
            EvidenceKind.HA_EVENT.value,
        }
    )

    def __init__(
        self,
        snapshot: InferenceSnapshot,
        *,
        lookup: EvidenceLookup | None = None,
        groups: Mapping[str, DeviceGroupEstimate] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self.lookup: EvidenceLookup = lookup or SnapshotEvidenceLookup(snapshot)
        self._groups: Mapping[str, DeviceGroupEstimate] = (
            groups if groups is not None else {g.group_id: g for g in snapshot.group_estimates}
        )

    def _resolve_refs(
        self, refs: Sequence[EvidenceRef]
    ) -> tuple[list[Any], list[str]]:
        resolved: list[Any] = []
        unresolved: list[str] = []
        for ref in refs:
            value = self.lookup.resolve(ref)
            key = _ref_key(ref)
            if value is None:
                unresolved.append(key)
            else:
                resolved.append(value)
        return resolved, unresolved

    def for_device(self, estimate: DeviceEstimate) -> ProvenanceAnswer:
        """Answer the nine questions for one :class:`DeviceEstimate`."""
        refs = [parsed for ref in getattr(estimate, "evidence_refs", ()) if (parsed := _parse_ref(ref))]
        measurements: list[Any] = []
        associations: list[Any] = []
        unresolved: list[str] = []
        for ref in refs:
            value = self.lookup.resolve(ref)
            key = _ref_key(ref)
            if value is None:
                unresolved.append(key)
            elif ref.kind in self.EVENT_REF_KINDS:
                associations.append(value)
            else:
                measurements.append(value)

        # A group member has no individual value anywhere in the snapshot: its
        # point_w is None, and the answer points at the group for bounds/value.
        group: DeviceGroupEstimate | None = None
        group_id = getattr(estimate, "group_id", None)
        if group_id is not None:
            group = self._groups.get(group_id)

        bounds = estimate.bounds
        if group is not None and estimate.point_w is None:
            # The group's justified sum bounds are the only published bounds;
            # the member retains its own (null) point and identifiability.
            bounds = group.bounds

        return ProvenanceAnswer(
            estimate_id=f"{estimate.subject_kind}:{estimate.subject_id}",
            estimate_kind="device",
            reader_measurements=measurements,
            event_associations=associations,
            unresolved_refs=unresolved,
            model_version_id=getattr(estimate, "model_version_id", None),
            versions=estimate.versions,
            status=estimate.status,
            identifiability=estimate.identifiability,
            bounds=bounds,
            updated_at=estimate.updated_at,
            group_id=group_id,
            point_w=estimate.point_w,
        )

    def for_group(self, group: DeviceGroupEstimate) -> ProvenanceAnswer:
        """Answer the nine questions for one :class:`DeviceGroupEstimate`."""
        refs = [parsed for ref in getattr(group, "evidence_refs", ()) if (parsed := _parse_ref(ref))]
        measurements: list[Any] = []
        associations: list[Any] = []
        unresolved: list[str] = []
        for ref in refs:
            value = self.lookup.resolve(ref)
            key = _ref_key(ref)
            if value is None:
                unresolved.append(key)
            elif ref.kind in self.EVENT_REF_KINDS:
                associations.append(value)
            else:
                measurements.append(value)

        return ProvenanceAnswer(
            estimate_id=f"group:{group.group_id}",
            estimate_kind="group",
            reader_measurements=measurements,
            event_associations=associations,
            unresolved_refs=unresolved,
            model_version_id=None,
            versions=group.versions,
            status=None,
            identifiability=group.identifiability,
            bounds=group.bounds,
            updated_at=group.updated_at,
            group_id=group.group_id,
            point_w=group.point_w,
        )

    def answer(
        self,
        estimate: DeviceEstimate | DeviceGroupEstimate,
    ) -> ProvenanceAnswer:
        """Answer the nine questions for either estimate kind."""
        if isinstance(estimate, DeviceGroupEstimate):
            return self.for_group(estimate)
        return self.for_device(estimate)


def answer_for(
    estimate: DeviceEstimate | DeviceGroupEstimate,
    snapshot: InferenceSnapshot,
    *,
    lookup: EvidenceLookup | None = None,
    groups: Mapping[str, DeviceGroupEstimate] | None = None,
) -> ProvenanceAnswer:
    """Convenience wrapper: resolve one estimate against one snapshot."""
    return ProvenanceResolver(snapshot, lookup=lookup, groups=groups).answer(estimate)
