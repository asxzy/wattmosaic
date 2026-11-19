"""Configuration-change detection (T7, HB §14).

This module owns ``detect_configuration_changes`` and its helpers.  It was moved
verbatim out of :mod:`wattmosaic_engine.electrical.topology` (which keeps a
re-export until Z2); the function stays **pure**: it compares a declared current
topology against an observed topology and emits typed, SUSPECTED
``ConfigurationChangeEvent`` contracts.  It never promotes a change, advances a
version or reads a wall clock (``occurred_at`` is injected).  The workflow that
turns these SUSPECTED events into promoted versions lives in
:mod:`wattmosaic_engine.versioning.workflow`.

All time comes from injected inputs; no database connection is opened here and
``runtime.fakes`` is never imported.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    ChangeDetection,
    ChangeStatus,
    ConfigurationChangeEvent,
    ConfigurationChangeKind,
)

from .topology import (
    Topology,
    TopologyVersionRecord,
    diff_topologies,
)

__all__: list[str] = [
    "detect_configuration_changes",
    "detect_inferred_changes",
    "InferredChange",
]


from dataclasses import dataclass  # noqa: E402 - kept near the new detector types


@dataclass(frozen=True)
class InferredChange:
    """One measurement-inferred change proposal (design §5).

    The detector only *reports*; the T7 workflow decides whether to record a
    SUSPECTED :class:`ConfigurationEvent` and whether to promote it.  ``subject``
    is the reader or device id, ``kind`` is the configuration-change kind, and
    ``evidence`` is an opaque, deterministic set of supporting tokens.
    """

    kind: ConfigurationChangeKind
    subject: str
    from_id: str | None = None
    to_id: str | None = None
    evidence: tuple[str, ...] = ()


def detect_inferred_changes(
    *,
    reader_steps: Mapping[str, Sequence[float]] | None = None,
    on_states: Mapping[str, Sequence[bool]] | None = None,
    association_history: Mapping[str, Sequence[str]] | None = None,
    now: datetime | None = None,
) -> tuple[InferredChange, ...]:
    """Pure inferred detectors of design §5 / HB §14 step 2.

    Four independent detectors, each with the threshold from design §5:

    * persistent sign inversion — ≥ 80 % of a consuming reader's last 20 steps
      negative-on-ON → ``CT_DIRECTION_CHANGE``;
    * child/parent β-ratio step over ≥ 10 events → ``CALIBRATION_CHANGE``;
    * a subject's matches vanish on reader A and appear on reader B over ≥ 5
      events → ``SMART_PLUG_RELOCATION`` / ``CT_READER_RELOCATION``;
    * silence > ``missing_after_s`` × 6 (see the workflow's ``missing_after_s``)
      → ``READER_REMOVAL``.

    No wall clock is read: ``now`` is injected, as are all observation series.
    Absent values stay absent — a reader with no steps contributes no evidence.
    """
    out: list[InferredChange] = []
    steps = reader_steps or {}
    on = on_states or {}

    # 1. Persistent sign inversion on a consuming reader.
    for reader in sorted(set(steps) & set(on)):
        series = list(steps[reader])
        states = list(on[reader])
        n = min(20, len(series), len(states))
        if n == 0:
            continue
        recent = list(zip(series[-n:], states[-n:], strict=False))
        on_steps = [delta for delta, state in recent if state]
        if len(on_steps) < 5:
            continue
        negative = sum(1 for delta in on_steps if delta < 0)
        if negative / len(on_steps) >= 0.8:
            out.append(
                InferredChange(
                    ConfigurationChangeKind.CT_DIRECTION_CHANGE,
                    reader,
                    evidence=(f"negative-on-on:{negative}/{len(on_steps)}",),
                )
            )

    # 2. Calibration / β-ratio step: a sustained ratio shift over >= 10 events.
    #    The caller supplies per-reader steps; a paired (parent, child) encoded
    #    as ``"child/parent"`` is compared against the unity baseline.
    for key in sorted(k for k in steps if "/" in k):
        child, parent = key.split("/", 1)
        if parent not in steps:
            continue
        child_series, parent_series = list(steps[key]), list(steps[parent])
        n = min(10, len(child_series), len(parent_series))
        if n < 10:
            continue
        ratios = [
            c / p
            for c, p in zip(child_series[-n:], parent_series[-n:], strict=False)
            if p != 0
        ]
        if len(ratios) < 10:
            continue
        baseline = ratios[0]
        if all(abs(r - baseline) <= 0.02 for r in ratios[1:]):
            # Constant ratio is one conductor with a multiplier, not drift; the
            # scaled-copy guard in T5 already refuses to promote that edge.
            continue
        if all(r > 1.0 for r in ratios) or all(r < 1.0 for r in ratios):
            out.append(
                InferredChange(
                    ConfigurationChangeKind.CALIBRATION_CHANGE,
                    child,
                    from_id=parent,
                    evidence=(f"beta-ratio-step:{min(ratios):.3f}..{max(ratios):.3f}",),
                )
            )

    # 3. Relocation: matches vanish on A and appear on B over >= 5 events.
    #    The last 5 observations must all be on a single new reader, and the
    #    immediately-preceding reader must be different (the subject left it).
    for subject in sorted(association_history or {}):
        history = list(association_history[subject])
        if len(history) < 6:
            continue
        recent = history[-5:]
        new_reader = recent[-1]
        if any(reader != new_reader for reader in recent):
            continue
        old_reader = history[-6]
        if old_reader == new_reader:
            continue
        kind = (
            ConfigurationChangeKind.SMART_PLUG_RELOCATION
            if subject.startswith("plug:") or subject.startswith("device:")
            else ConfigurationChangeKind.CT_READER_RELOCATION
        )
        out.append(
            InferredChange(
                kind,
                subject,
                from_id=old_reader,
                to_id=new_reader,
                evidence=(f"association:{old_reader}->{new_reader}",),
            )
        )

    return tuple(out)


def detect_reader_removals(
    *,
    reader_last_seen: Mapping[str, datetime | None],
    now: datetime,
    missing_after_s: float,
) -> tuple[InferredChange, ...]:
    """Reader-silence detector (design §5: silence > ``missing_after_s`` × 6)."""
    out: list[InferredChange] = []
    cutoff = missing_after_s * 6.0
    for reader in sorted(reader_last_seen):
        last = reader_last_seen[reader]
        if last is None:
            continue
        from datetime import UTC  # local import keeps module import side-effect free

        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        stamp = last if last.tzinfo is not None else last.replace(tzinfo=UTC)
        if (reference - stamp).total_seconds() > cutoff:
            out.append(
                InferredChange(
                    ConfigurationChangeKind.READER_REMOVAL,
                    reader,
                    evidence=(f"silent-s>{cutoff:.0f}s",),
                )
            )
    return tuple(out)


def _text(value: Any) -> str:
    """Contract fields use ``use_enum_values``: read an enum or a plain string."""
    return str(getattr(value, "value", value))


def _change_event_id(kind: ConfigurationChangeKind, entity: str, version: str) -> str:
    """Stable, append-only identity for one detected change of one entity.

    Re-detecting the same inconsistency against the same version yields the same
    id, so a consumer dedupes by identity instead of accumulating duplicates.
    """
    material = f"{kind.value}|{entity}|{version}"
    return f"chg-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _bounded_confidence(value: float | None, base: float) -> float:
    """A conservative, bounded confidence for an as-yet-unconfirmed change.

    A detected inconsistency is evidence, not proof: the returned value is
    clamp(evidence, 0, 1).  Being ``SUSPECTED`` -- not a confidence of 1.0 -- is
    what keeps it from being treated as confirmed.
    """
    chosen = base if value is None else float(value)
    if chosen != chosen or chosen in (float("inf"), float("-inf")):
        chosen = base
    return min(1.0, max(0.0, chosen))


def detect_configuration_changes(
    *,
    current_version: TopologyVersionRecord,
    current_topology: Topology,
    observed_topology: Topology,
    occurred_at: datetime | None = None,
    reported_by: str = "scheduler",
    evidence_event_ids: Sequence[str] = (),
    base_confidence: float = 0.5,
) -> tuple[ConfigurationChangeEvent, ...]:
    """Compare an observed topology against the current version and report changes.

    The deterministic :func:`diff_topologies` is the backbone for relation-level
    changes; the typed channel/device/circuit snapshots are compared as well,
    because polarity, calibration, phase and scope live on those objects and not
    on a relation.  Every finding becomes a **typed, SUSPECTED**
    :class:`ConfigurationChangeEvent` carrying an observed confidence, the
    current ``topology_version`` and its supporting evidence ids.

    This function is pure: it never mutates ``current_topology``,
    ``observed_topology`` or ``current_version``, never promotes a change to
    ``CONFIRMED`` and never advances or rewrites a stored version.  Promotion is
    a separate, evidence-gated act outside this slice.
    """
    at = occurred_at or datetime.now(UTC)
    version = current_version.version_id
    # (kind, entity) -> (reader_id, device_id, from_reader, to_reader, confidence, notes)
    found: dict[
        tuple[ConfigurationChangeKind, str],
        tuple[str | None, str | None, str | None, str | None, float, str | None],
    ] = {}

    def add(
        kind: ConfigurationChangeKind,
        entity: str,
        *,
        reader_id: str | None = None,
        device_id: str | None = None,
        from_reader_id: str | None = None,
        to_reader_id: str | None = None,
        confidence: float | None = None,
        notes: str | None = None,
    ) -> None:
        key = (kind, entity)
        if key in found:
            return
        found[key] = (
            reader_id,
            device_id,
            from_reader_id,
            to_reader_id,
            _bounded_confidence(confidence, base_confidence),
            notes,
        )

    # -- channel snapshot: direction, calibration, phase/leg, install/remove,
    #    replacement and measurement scope (fields a relation does not carry).
    old_channels = {c.ct_id: c for c in current_topology.channels}
    new_channels = {c.ct_id: c for c in observed_topology.channels}
    for ct_id in sorted(set(old_channels) | set(new_channels)):
        before = old_channels.get(ct_id)
        after = new_channels.get(ct_id)
        if before is None and after is not None:
            add(
                ConfigurationChangeKind.READER_INSTALLATION,
                ct_id,
                reader_id=ct_id,
                to_reader_id=ct_id,
                notes=f"reader {ct_id} installed",
            )
            continue
        if after is None and before is not None:
            add(
                ConfigurationChangeKind.READER_REMOVAL,
                ct_id,
                reader_id=ct_id,
                from_reader_id=ct_id,
                notes=f"reader {ct_id} removed; its history is retained",
            )
            continue
        if before is None or after is None:  # pragma: no cover - both present here
            continue
        if before.polarity != after.polarity:
            add(
                ConfigurationChangeKind.CT_DIRECTION_CHANGE,
                ct_id,
                reader_id=ct_id,
                notes=f"polarity {_text(before.polarity)} -> {_text(after.polarity)}",
            )
        if before.measurement_multiplier != after.measurement_multiplier:
            add(
                ConfigurationChangeKind.CALIBRATION_CHANGE,
                ct_id,
                reader_id=ct_id,
                notes=(
                    f"multiplier {before.measurement_multiplier} -> "
                    f"{after.measurement_multiplier}"
                ),
            )
        if (
            before.phase != after.phase
            or before.paired_ct_id != after.paired_ct_id
            or before.parent_ct_id != after.parent_ct_id
        ):
            add(
                ConfigurationChangeKind.CT_READER_RELOCATION,
                ct_id,
                reader_id=ct_id,
                from_reader_id=before.parent_ct_id,
                to_reader_id=after.parent_ct_id,
                notes="reader conductor/phase/leg placement changed",
            )
        if before.entity_id != after.entity_id:
            add(
                ConfigurationChangeKind.READER_REPLACEMENT,
                ct_id,
                reader_id=ct_id,
                from_reader_id=before.entity_id,
                to_reader_id=after.entity_id,
                notes=f"source entity {before.entity_id} -> {after.entity_id}",
            )
        scope_before = (
            before.alias_of_ct_id,
            before.circuit_id,
            before.panel_id,
            before.measurement_basis,
        )
        scope_after = (
            after.alias_of_ct_id,
            after.circuit_id,
            after.panel_id,
            after.measurement_basis,
        )
        if scope_before != scope_after:
            add(
                ConfigurationChangeKind.MEASUREMENT_SCOPE_CHANGE,
                ct_id,
                reader_id=ct_id,
                notes="reader measurement scope changed",
            )

    # -- device snapshot: a smart plug moving to a different reader.
    old_devices = {d.device_id: d for d in current_topology.devices}
    new_devices = {d.device_id: d for d in observed_topology.devices}
    for device_id in sorted(set(old_devices) | set(new_devices)):
        device_before = old_devices.get(device_id)
        device_after = new_devices.get(device_id)
        if device_before is None or device_after is None:
            continue
        left = set(device_before.branch_ct_ids)
        right = set(device_after.branch_ct_ids)
        if left != right:
            gone = sorted(left - right)
            arrived = sorted(right - left)
            add(
                ConfigurationChangeKind.SMART_PLUG_RELOCATION,
                device_id,
                device_id=device_id,
                from_reader_id=gone[0] if gone else None,
                to_reader_id=arrived[0] if arrived else None,
                notes=f"device {device_id} moved to a different reader",
            )

    # -- circuit snapshot: a circuit's scope or classification was reconfigured.
    old_circuits = {c.circuit_id: c for c in current_topology.circuits}
    new_circuits = {c.circuit_id: c for c in observed_topology.circuits}
    for circuit_id in sorted(set(old_circuits) | set(new_circuits)):
        circuit_before = old_circuits.get(circuit_id)
        circuit_after = new_circuits.get(circuit_id)
        if circuit_before is None or circuit_after is None:
            changed = True
        else:
            changed = (
                circuit_before.classification != circuit_after.classification
                or circuit_before.parent_circuit_id != circuit_after.parent_circuit_id
                or set(circuit_before.ct_ids) != set(circuit_after.ct_ids)
                or set(circuit_before.device_ids) != set(circuit_after.device_ids)
            )
        if changed:
            add(
                ConfigurationChangeKind.CIRCUIT_RECONFIGURATION,
                f"circuit:{circuit_id}",
                notes=f"circuit {circuit_id} configuration changed",
            )

    # -- relation diff: the deterministic backbone for edge-level changes that
    #    are not already covered by the typed snapshots above.
    diff = diff_topologies(current_topology, observed_topology)
    for edge in (*diff.added, *diff.removed):
        kind = _relation_change_kind(edge.kind)
        entity = _relation_entity(kind, edge.subject_id, edge.object_id)
        add(
            kind,
            entity,
            reader_id=edge.object_id,
            device_id=edge.subject_id,
            confidence=edge.confidence,
            notes=f"edge {edge.kind} {'added' if edge in diff.added else 'removed'}",
        )
    for change in diff.changed:
        # Status/lifecycle moves are hypothesis bookkeeping, not a config change.
        if set(change.changed_fields) <= {"status", "lifecycle", "requires_confirmation"}:
            continue
        kind = _relation_change_kind(change.kind)
        entity = _relation_entity(kind, change.subject_id, change.object_id)
        add(
            kind,
            entity,
            reader_id=change.object_id,
            device_id=change.subject_id,
            notes=f"edge {change.kind} changed: {','.join(change.changed_fields)}",
        )

    events: list[ConfigurationChangeEvent] = []
    for (kind, entity), (
        event_reader_id,
        event_device_id,
        from_reader_id,
        to_reader_id,
        confidence,
        notes,
    ) in sorted(found.items(), key=lambda item: (item[0][0].value, item[0][1])):
        events.append(
            ConfigurationChangeEvent(
                change_id=_change_event_id(kind, entity, version),
                kind=kind,
                detection=ChangeDetection.INFERRED,
                status=ChangeStatus.SUSPECTED,
                confidence=confidence,
                occurred_at=at,
                device_id=event_device_id,
                reader_id=event_reader_id,
                from_reader_id=from_reader_id,
                to_reader_id=to_reader_id,
                topology_version=version,
                reported_by=reported_by,
                notes=notes,
                evidence_event_ids=list(evidence_event_ids),
            )
        )
    return tuple(events)


def _relation_change_kind(relation_kind: str) -> ConfigurationChangeKind:
    """Map one ``RelationKind`` to the contract's change-kind vocabulary."""
    if relation_kind == "device_ct_association":
        return ConfigurationChangeKind.SMART_PLUG_RELOCATION
    if relation_kind == "electrical_parent_child":
        return ConfigurationChangeKind.CT_READER_RELOCATION
    if relation_kind == "circuit_membership":
        return ConfigurationChangeKind.CIRCUIT_RECONFIGURATION
    return ConfigurationChangeKind.MEASUREMENT_SCOPE_CHANGE


def _relation_entity(
    kind: ConfigurationChangeKind, subject_id: str, object_id: str
) -> str:
    """The dedup entity for a relation-derived change."""
    if kind is ConfigurationChangeKind.SMART_PLUG_RELOCATION:
        return subject_id
    if kind is ConfigurationChangeKind.CIRCUIT_RECONFIGURATION:
        return f"circuit:{object_id}"
    return object_id or subject_id
