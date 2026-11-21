"""Snapshot and read-time device views.

Owned by D4 after X03 (snapshot assembly and provenance).  The legacy
``SnapshotMixin`` read path is unchanged; this module additionally provides
:class:`SnapshotAssembler`, the D4 composition seam that turns live inference
results into an HB §12.1 :class:`InferenceSnapshot`, persists it through S17's
hash-deduplicated ``put_snapshot``, and publishes a copy-on-write reference.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from ..api.facade import InvalidRequest, NotFound
from ..contracts import (
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    InferenceSnapshot,
    JointInferenceResult,
    MeasurementBasis,
    MeasurementType,
    ReaderMeasurement,
    ReaderResidualPoint,
    ReconciliationV3,
    SnapshotId,
    VersionTriple,
)
from ..mapping.display_tree import build_display_tree_v3

log = logging.getLogger("wattmosaic.main")


# --------------------------------------------------------------------------- #
# D4: snapshot assembly (HB §12.1) and copy-on-write publication.
# --------------------------------------------------------------------------- #


@runtime_checkable
class SnapshotStore(Protocol):
    """The S17 seam :class:`SnapshotAssembler` depends on (no DB import here)."""

    def put_snapshot(self, snapshot: InferenceSnapshot) -> bool: ...

    def latest_snapshot(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class AssembleInputs:
    """Injected inputs to one :meth:`SnapshotAssembler.on_result` call.

    ``clock`` is the only source of ``computed_at``; no wall clock is read in
    this module.  ``snapshot_id`` may be supplied by a replay driver for a
    deterministic id; otherwise it is derived from the assembled content.
    """

    result: JointInferenceResult
    held_readings: Mapping[str, Any] = field(default_factory=dict)
    quality: DataQualityReport | None = None
    residual_points: Sequence[ReaderResidualPoint] = ()
    observed_at: datetime | None = None
    snapshot_id: SnapshotId | None = None
    stale_sources: Sequence[str] = ()
    unresolved_events: Sequence[str] = ()
    #: The active topology's root reader(s) (``topology_root_readers``): the
    #: main reading is their sum and the root residual their residual.  Empty
    #: means the topology has no root, so ``main_w``/``residual_w`` are ``None``.
    root_reader_ids: Sequence[str] = ()


def _sum_all(values: Sequence[float | None]) -> float | None:
    """Sum of every term, or ``None`` when there is none or one is absent.

    A partial sum over the root readers would publish a fabricated main
    reading (HB §18): one missing leg makes the whole total unknown.
    """
    if not values or any(value is None for value in values):
        return None
    return float(sum(values))  # type: ignore[arg-type]


def _reading_value(reading: Any) -> float | None:
    """Extract a measurement from a P2 ``HeldReading`` or a plain mapping.

    Absent measurements stay ``None`` (HB §18); a stale or missing held reading
    is reported as ``None`` rather than falling back to the last known value as a
    fake measurement.
    """
    if reading is None:
        return None
    if isinstance(reading, Mapping):
        if str(reading.get("state", "held")) in ("stale", "missing"):
            return None
        raw = reading.get("value", reading.get("value_w", reading.get("active_power_w")))
    else:
        state = getattr(reading, "state", "held")
        if str(getattr(state, "value", state)) in ("stale", "missing"):
            return None
        raw = getattr(reading, "value", None)
        if raw is None:
            raw = getattr(reading, "active_power_w", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _measurement(
    reader_id: str,
    reading: Any,
    *,
    at: datetime,
    versions: VersionTriple,
) -> ReaderMeasurement:
    """Build one §2.1 normalized reading; unsupported fields stay ``None``."""
    observed = at
    received = at
    sequence = 0
    quality_flags: list[Any] = []
    if isinstance(reading, Mapping):
        parsed = reading.get("observed_at")
        if isinstance(parsed, datetime):
            observed = parsed
        parsed = reading.get("received_at")
        if isinstance(parsed, datetime):
            received = parsed
        if reading.get("sequence") is not None:
            try:
                sequence = int(reading["sequence"])
            except (TypeError, ValueError):
                sequence = 0
        flags = reading.get("quality_flags")
        if isinstance(flags, (list, tuple)):
            quality_flags = list(flags)
    else:
        parsed = getattr(reading, "observed_at", None)
        if isinstance(parsed, datetime):
            observed = parsed
        parsed = getattr(reading, "received_at", None)
        if isinstance(parsed, datetime):
            received = parsed
        if getattr(reading, "sequence", None) is not None:
            try:
                sequence = int(reading.sequence)
            except (TypeError, ValueError):
                sequence = 0
    return ReaderMeasurement(
        reader_id=reader_id,
        observed_at=observed,
        received_at=received,
        active_power_w=_reading_value(reading),
        phase="unknown",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="d4",
        measurement_basis=MeasurementBasis.MONITOR_REPORTED.value,
        quality_flags=quality_flags,
        sequence=sequence,
    )


def _stale_and_unresolved(
    inputs: AssembleInputs,
) -> tuple[list[str], list[str]]:
    """Derive stale readers and unresolved event groups from the result."""
    stale = [str(name) for name in inputs.stale_sources]
    for reader_id, reading in inputs.held_readings.items():
        state = reading.get("state") if isinstance(reading, Mapping) else getattr(reading, "state", None)
        state = str(getattr(state, "value", state))
        if state in ("stale", "missing") and reader_id not in stale:
            stale.append(str(reader_id))
    unresolved = [str(name) for name in inputs.unresolved_events]
    status = str(getattr(inputs.result.status, "value", inputs.result.status))
    group_id = getattr(inputs.result, "event_group_id", None)
    if status in ("unresolved", "conflicted") and group_id and str(group_id) not in unresolved:
        unresolved.append(str(group_id))
    return stale, unresolved


class SnapshotAssembler:
    """Build, persist and publish HB §12.1 snapshots from live results.

    ``store`` is the S17 repository seam (``put_snapshot`` / ``latest_snapshot``)
    and ``clock`` is injected, so the assembler is pure apart from the explicit
    persistence call.  ``on_result`` merges per subject — the latest result in
    ``prior`` wins, and the incoming ``result`` overwrites subjects it names —
    so multiple per-subject solve results coalesce into one snapshot.

    Publication is copy-on-write: :meth:`current` hands out an immutable value
    (a Pydantic model) and the internal reference is replaced wholesale, never
    mutated in place.  A concurrent reader therefore sees either the previous
    snapshot or the new one, never a half-updated object.
    """

    __slots__ = ("_store", "_clock", "_current", "_current_digest")

    def __init__(self, store: SnapshotStore, clock: Any) -> None:
        self._store = store
        self._clock = clock
        self._current: InferenceSnapshot | None = None
        self._current_digest: str | None = None

    # -- publication -------------------------------------------------------- #
    @property
    def current(self) -> InferenceSnapshot | None:
        """The last published snapshot, or ``None`` before the first result."""
        return self._current

    def restore(self, snapshot: InferenceSnapshot | None) -> None:
        """Adopt an already-built snapshot (e.g. ``latest_snapshot`` on start)."""
        self._current = snapshot

    def load_latest(self) -> InferenceSnapshot | None:
        """Load the last persisted snapshot on restart (HB §15: no hidden state)."""
        row = self._store.latest_snapshot()
        if row is None:
            self._current = None
            return None
        snapshot = self._decode(row)
        self._current = snapshot
        return snapshot

    @staticmethod
    def _decode(row: Mapping[str, Any]) -> InferenceSnapshot:
        """Decode an S17 ``latest_snapshot`` row into a contract object."""
        payload = row.get("payload")
        if isinstance(payload, InferenceSnapshot):
            return payload
        if isinstance(payload, Mapping):
            data = dict(payload)
        elif isinstance(payload, str):
            data = json.loads(payload)
        else:
            raw = row.get("payload_json")
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        # The stored payload may contain point-width Bounds that the public
        # validator only admits through the private constructor (I5's MEASURED
        # estimates).  Open the shared repair gate while validating.
        return SnapshotAssembler._decode_validated(data)

    @staticmethod
    def _decode_validated(data: dict[str, Any]) -> InferenceSnapshot:
        # The S17 snapshot reader and the S16 event-record reader share one
        # repair implementation; keep this thin wrapper as the stable call site.
        from ..storage.repositories.inference import repair_point_bounds

        with repair_point_bounds(data):
            return InferenceSnapshot.model_validate(data)

    # -- assembly ----------------------------------------------------------- #
    def on_result(
        self,
        result: JointInferenceResult,
        held_readings: Mapping[str, Any] | None = None,
        quality: DataQualityReport | None = None,
        residual_points: Sequence[ReaderResidualPoint] = (),
        *,
        inputs: AssembleInputs | None = None,
    ) -> InferenceSnapshot:
        """Assemble, persist and publish one snapshot for ``result``.

        Parameters may be supplied either positionally or through ``inputs``;
        the latter carries optional deterministic overrides (``at``,
        ``snapshot_id``, stale/unresolved lists) used by replay and tests.
        """
        if inputs is None:
            inputs = AssembleInputs(
                result=result,
                held_readings=held_readings or {},
                quality=quality,
                residual_points=residual_points or (),
            )
        clock_now = self._clock()
        at = inputs.observed_at or getattr(inputs.result, "computed_at", None) or clock_now
        versions: VersionTriple = inputs.result.versions

        stale, unresolved = _stale_and_unresolved(inputs)

        # Merge per subject: prior published estimates first, then the incoming
        # result's estimates overwrite the subjects they name.  The same rule
        # applies to groups, so the latest per-group value always wins.
        base = self._current
        devices = {e.subject_id: e for e in (base.device_estimates if base else ())}
        for estimate in inputs.result.device_estimates:
            devices[estimate.subject_id] = estimate
        groups = {g.group_id: g for g in (base.group_estimates if base else ())}
        for group in inputs.result.group_estimates:
            groups[group.group_id] = group

        measurements = [
            _measurement(reader_id, reading, at=at, versions=versions)
            for reader_id, reading in sorted(inputs.held_readings.items())
        ]
        residuals = list(inputs.residual_points)
        residual_by_reader = {
            r.reader_id: r.residual_w for r in residuals
        }
        # The main reading is the topology roots' reading (parallel mains and
        # verified legs summed); the residual is the roots' residual.  Neither
        # is chosen by reader name, and neither is the other (HB §12.1).
        roots = tuple(str(r) for r in inputs.root_reader_ids)
        held_by_reader = {str(k): v for k, v in inputs.held_readings.items()}
        main_w = _sum_all([_reading_value(held_by_reader.get(r)) for r in roots])
        residual_w = _sum_all([residual_by_reader.get(r) for r in roots])
        # HB §18: absent is None, but a sum over a *known, empty* set is a real
        # 0 W.  The published device/group sets are always known here, so their
        # totals are 0.0 when nothing qualifies.  Only ESTIMATED/MEASURED points
        # are identified power and only groups with a point are group power; a
        # device or group without a point is not part of these terms (its load
        # stays in the reader residual), exactly as before.
        identified = float(
            sum(
                float(e.point_w)
                for e in devices.values()
                if e.point_w is not None
                and e.status in (EstimateStatus.ESTIMATED, EstimateStatus.MEASURED)
            )
        )
        group_w = float(sum(float(g.point_w) for g in groups.values() if g.point_w is not None))
        # Unknown load follows the same rule as identified_w/group_w (HB §18):
        # 0.0 over a known, empty set of unknown estimates; the sum when every
        # estimate carries a value; None when any estimate is unconstrained.
        # The solvers now emit delta_w=None for a scope no solved row constrains,
        # so an unconstrained scope's load stays "not computed" rather than a
        # fabricated 0 W (which is what made the HB §17 prelude total None).
        unknowns = tuple(getattr(inputs.result, "unknown_estimates", ()) or ())
        unknown_parts: list[float] = []
        unknown_absent = False
        for u in unknowns:
            value = u.point_w if u.point_w is not None else u.delta_w
            if value is None:
                unknown_absent = True
                break
            unknown_parts.append(float(value))
        unknown_w: float | None = None if unknown_absent else float(sum(unknown_parts))
        reconciliation = ReconciliationV3(
            main_w=main_w,
            identified_w=identified,
            group_w=group_w,
            unknown_w=unknown_w,
            residual_w=residual_w,
        )

        # The snapshot id is derived from the *content*, so a changed state gets
        # a distinct id (S17's table has a UNIQUE constraint on snapshot_id) and
        # an unchanged state gets the same id — which ``put_snapshot`` then
        # deduplicates by content hash.
        # The version triple and the residual points are content too: a new model
        # set (or residual) with otherwise identical estimates is a changed
        # snapshot, and S17 hashes both, so reusing the old id would collide.
        payload = {
            "versions": versions.model_dump(mode="json"),
            "residuals": [r.model_dump(mode="json") for r in residuals],
            "devices": [
                devices[key].model_dump(mode="json") for key in sorted(devices)
            ],
            "groups": [
                groups[key].model_dump(mode="json") for key in sorted(groups)
            ],
            "measurements": [m.model_dump(mode="json") for m in measurements],
            "reconciliation": reconciliation.model_dump(mode="json"),
            "stale_sources": stale,
            "unresolved_events": unresolved,
        }
        content_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if inputs.snapshot_id:
            snapshot_id = inputs.snapshot_id
        elif base is not None and self._current_digest == content_digest:
            # Unchanged state: the same snapshot, the same id (S17 dedups it).
            snapshot_id = base.snapshot_id
        elif base is not None:
            # A changed state.  Chain the id to its predecessor: content that
            # returns to an *earlier* state (A -> B -> A) would otherwise reuse
            # A's id, violate S17's UNIQUE(snapshot_id) and silently keep B
            # published (the snapshot never swapped).
            snapshot_id = "snap-" + hashlib.sha256(
                (content_digest + base.snapshot_id).encode("utf-8")
            ).hexdigest()[:16]
        else:
            snapshot_id = "snap-" + content_digest[:16]

        snapshot = InferenceSnapshot(
            snapshot_id=snapshot_id,
            observed_at=at,
            computed_at=clock_now,
            versions=versions,
            reader_measurements=measurements,
            device_estimates=[devices[key] for key in sorted(devices)],
            group_estimates=[groups[key] for key in sorted(groups)],
            residual_estimates=residuals,
            reconciliation=reconciliation,
            # Use the observed instant, never a fresh wall-clock timestamp: a
            # re-publish of identical content must keep an identical content
            # hash so S17's hash deduplication actually suppresses the write.
            system_quality=inputs.quality or DataQualityReport(timestamp=at),
            stale_sources=stale,
            unresolved_events=unresolved,
        )
        self._store.put_snapshot(snapshot)
        # Copy-on-write: replace the reference, never mutate the published model.
        self._current = snapshot
        self._current_digest = content_digest
        return snapshot


# --------------------------------------------------------------------------- #
# Static description of the assembler contract (for introspection/tests).
# --------------------------------------------------------------------------- #

ASSEMBLER_SEQUENCE: tuple[str, ...] = (
    "merge per subject (latest result wins)",
    "stale_sources and unresolved_events",
    "persist via S17 put_snapshot (hash-deduplicated)",
    "publish copy-on-write",
)


class SnapshotMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def _v3_snapshot(self) -> InferenceSnapshot | None:
        """The latest published v3 snapshot, with no hidden in-memory state.

        P6 installs a :class:`SnapshotAssembler` as ``snapshot_assembler``
        (``_snapshot_assembler_v3`` is accepted for older composition roots);
        until it has published, the S17 persisted snapshot is the single source
        of truth (HB §15).  This method never fabricates an empty snapshot — a
        missing row means "not yet computed" and callers turn that into a 404.
        """
        assembler = getattr(self, "snapshot_assembler", None) or getattr(
            self, "_snapshot_assembler_v3", None
        )
        if assembler is not None and assembler.current is not None:
            return assembler.current
        repos = getattr(self.db, "repos", None)
        if repos is None:
            return None
        try:
            row = repos.inference.latest_snapshot()
        except Exception:  # noqa: BLE001 - optional S17 storage
            return None
        if row is None:
            return None
        return SnapshotAssembler._decode(row)

    def snapshot_v3(self) -> InferenceSnapshot:
        """Serve the v3 ``InferenceSnapshot`` (used by ``/snapshot?v=3``)."""
        snapshot = self._v3_snapshot()
        if snapshot is None:
            raise NotFound("no v3 inference snapshot has been published")
        return snapshot

    def devices_v3(self) -> list[DeviceEstimate]:
        """Device estimates from the latest v3 snapshot."""
        snapshot = self._v3_snapshot()
        return list(snapshot.device_estimates) if snapshot else []

    def device_v3(self, device_id: str) -> DeviceEstimate:
        """One device estimate, or 404 when the snapshot has no such subject."""
        for estimate in self.devices_v3():
            if estimate.subject_id == device_id:
                return estimate
        raise NotFound(f"device {device_id!r} not found in the v3 snapshot")

    def groups_v3(self) -> list[DeviceGroupEstimate]:
        """Group estimates from the latest v3 snapshot."""
        snapshot = self._v3_snapshot()
        return list(snapshot.group_estimates) if snapshot else []

    def group_v3(self, group_id: str) -> DeviceGroupEstimate:
        """One group estimate, or 404 when the snapshot has no such group."""
        for group in self.groups_v3():
            if group.group_id == group_id:
                return group
        raise NotFound(f"group {group_id!r} not found in the v3 snapshot")

    def _provenance_lookup(self, snapshot: InferenceSnapshot) -> Any:
        """Snapshot lookup plus the persisted HA events the snapshot cites.

        An estimate names the HA changes of its event group as ``ha_event``
        refs; the snapshot itself holds no HA events, so they are resolved from
        the S16 ``ha_events_v3`` rows the runtime persists for every published
        group (HB §18 "which event associations contributed?").
        """
        from ..provenance.resolver import SnapshotEvidenceLookup

        extra: dict[str, Any] = {}
        getter = getattr(self, "ha_event_record", None)
        if callable(getter):
            for estimate in (*snapshot.device_estimates, *snapshot.group_estimates):
                for ref in getattr(estimate, "evidence_refs", ()) or ():
                    if getattr(ref, "kind", None) != "ha_event":
                        continue
                    key = f"ha_event:{ref.id}"
                    if key in extra:
                        continue
                    record = getter(ref.id)
                    if record is not None:
                        extra[key] = record
        return SnapshotEvidenceLookup(snapshot, extra=extra)

    def provenance(self, kind: str, identifier: str) -> Any:
        """Answer the HB §18 provenance questions for an estimate or evidence ref.

        ``kind`` is ``device``/``component``/``group`` for an estimate answer, or
        one of the :class:`~wattmosaic_engine.contracts.EvidenceKind` values for
        a raw evidence resolution.
        """
        from ..provenance import ProvenanceResolver

        snapshot = self._v3_snapshot()
        if snapshot is None:
            raise NotFound("no v3 inference snapshot has been published")
        resolver = ProvenanceResolver(snapshot, lookup=self._provenance_lookup(snapshot))
        if kind in ("device", "component"):
            return resolver.for_device(self.device_v3(identifier))
        if kind == "group":
            return resolver.for_group(self.group_v3(identifier))
        # An evidence kind: resolve the typed ref directly.
        from ..contracts import EvidenceRef

        try:
            ref = EvidenceRef(kind=kind, id=identifier)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - invalid kind is a 400
            raise InvalidRequest(f"unknown evidence kind {kind!r}") from exc
        value = resolver.lookup.resolve(ref)
        if value is None:
            raise NotFound(f"evidence {kind}:{identifier} does not resolve")
        return value

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """Read-time panel hierarchy built from the active v3 topology (T1).

        The tree is T1's ``primary_display_tree()`` over the active version's
        confirmed CONTAINS edges, with every :class:`MeasurementEndpoint` as a
        reader leaf (:func:`build_display_tree_v3`).  ``include_nesting`` is
        accepted for wire compatibility; nesting proposals are now PROVISIONAL
        edges of the T5 evidence ledger, served by ``/topology/v3``.
        """
        endpoints = self._matrix_endpoints()
        versions = self._matrix_versions()
        graph = self._matrix_topology_graph(endpoints, versions["topology_version_id"])
        tree = build_display_tree_v3(
            graph,
            generated_at=self._clock().isoformat().replace("+00:00", "Z"),
            devices_by_reader=self._mapped_devices_by_reader(versions["mapping_version_id"]),
        )
        tree["topology_version"] = versions["topology_version_id"]
        tree["mapping_version"] = versions["mapping_version_id"]
        # E6 §1: the panel hierarchy is built from one placement per child at
        # any confidence, not only from STRUCTURAL edges.  The engine owns the
        # derivation; ``structural_links`` tells the panel which links are
        # authoritative H edges (the legacy builder's roots are kept for
        # compatibility and as the fallback).
        ledger = getattr(self, "topology_inference", None)
        report = getattr(ledger, "placements", None) if ledger is not None else None
        if report is not None:
            tree["placements"] = report.as_dict()
            active = [
                (str(e.source_endpoint_id), str(e.target_endpoint_id))
                for e in graph.edges_all
                if str(getattr(e.relation, "value", e.relation)) == "contains"
                and str(getattr(e.lifecycle, "value", e.lifecycle)) == "structural"
            ]
            tree["structural_links"] = active
            tree["roots"] = self._placement_roots(graph, report, active)
        return tree

    def _placement_roots(
        self,
        graph: Any,
        report: Any,
        structural_links: Sequence[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Build the panel hierarchy from placements (E6 §1).

        Every placed child appears under its single parent; a ``both_legs``
        child appears under both parents with a chip; unplaced readers are
        listed separately under an "Unplaced measurements" group.  Structural
        links are passed through so the panel can style them differently.
        """
        labels = {str(e.endpoint_id): str(e.display_name or e.endpoint_id) for e in graph.endpoints}
        structural = set(structural_links)
        by_child = getattr(report, "by_child", {}) or {}
        children: dict[str, list[str]] = {}
        roots: list[str] = []
        unplaced: list[str] = []
        for reader in sorted(labels):
            placement = by_child.get(reader)
            if placement is None or getattr(placement, "parent", None) is None:
                if getattr(placement, "both_legs", ()):
                    continue  # drawn under both parents below
                roots.append(reader)
                unplaced.append(reader)
                continue
            children.setdefault(str(placement.parent), []).append(reader)

        def node(reader: str, depth: int) -> dict[str, Any]:
            kids = sorted(children.get(reader, ()))
            placement = by_child.get(reader)
            chips: list[dict[str, str]] = []
            if placement is not None and getattr(placement, "band", None) == "low":
                chips.append({"text": "low confidence", "tone": "warn"})
            return {
                "id": f"reader:{reader}",
                "kind": "circuit",
                "label": labels.get(reader, reader),
                "reader_id": reader,
                "depth_hint": depth,
                "band": getattr(placement, "band", None) if placement is not None else None,
                "structural": False,  # a root has no parent link
                "chips": chips,
                "children": [_child(c, depth + 1) for c in kids],
            }

        def _child(reader: str, depth: int) -> dict[str, Any]:
            placement = by_child.get(reader)
            chips: list[dict[str, str]] = []
            if placement is not None and getattr(placement, "both_legs", ()):
                chips.append({"text": "240 V, leg not identifiable", "tone": "warn"})
            if placement is not None and getattr(placement, "band", None) == "low":
                chips.append({"text": "low confidence", "tone": "warn"})
            parent = getattr(placement, "parent", None) if placement is not None else None
            is_structural = bool(parent and (parent, reader) in structural)
            kids = sorted(children.get(reader, ()))
            return {
                "id": f"reader:{reader}",
                "kind": "circuit",
                "label": labels.get(reader, reader),
                "reader_id": reader,
                "depth_hint": depth,
                "band": getattr(placement, "band", None) if placement is not None else None,
                "structural": is_structural,
                "chips": chips,
                "children": [_child(c, depth + 1) for c in kids],
            }

        out: list[dict[str, Any]] = []
        for reader in roots:
            if reader in unplaced and not children.get(reader):
                continue  # listed under "Unplaced measurements" instead
            out.append(node(reader, 1))
        # A ``both_legs`` child is drawn under each of its two parents.
        for reader, placement in sorted(by_child.items()):
            both = getattr(placement, "both_legs", ()) or ()
            if not both:
                continue
            for parent in both:
                out.append(
                    {
                        "id": f"group:{labels.get(parent, parent)}:1",
                        "kind": "group",
                        "label": labels.get(parent, parent),
                        "depth_hint": 1,
                        "children": [_child(reader, 2)],
                    }
                )
        if unplaced:
            out.append(
                {
                    "id": "group:Unplaced measurements:1",
                    "kind": "group",
                    "label": "Unplaced measurements",
                    "depth_hint": 1,
                    "children": [
                        {
                            "id": f"reader:{r}",
                            "kind": "circuit",
                            "label": labels.get(r, r),
                            "reader_id": r,
                            "depth_hint": 2,
                            "children": [],
                        }
                        for r in sorted(unplaced)
                    ],
                }
            )
        return out

    def _mapped_devices_by_reader(self, mapping_version_id: str) -> dict[str, list[dict[str, Any]]]:
        """reader -> the devices/components the active mapping places on it.

        Independent of topology: a mapping edge names the most specific reader,
        so a device assigned to a channel is shown with that channel wherever
        the channel currently sits in the hierarchy.
        """
        try:
            rows = self.db.repos.mapping.mapping_edges(mapping_version_id)
        except Exception:  # noqa: BLE001 - no mapping yet is an empty mapping
            log.debug("mapping edges unavailable for the panel tree", exc_info=True)
            return {}
        names: dict[str, str] = {}
        out: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, str]] = set()
        for row in rows:
            kind = str(row.get("subject_kind") or "")
            lifecycle = str(row.get("lifecycle") or "")
            if kind not in ("device", "component") or lifecycle in ("rejected", "retired"):
                continue
            if row.get("valid_until"):
                continue
            subject, reader = str(row.get("subject_id") or ""), str(row.get("endpoint_id") or "")
            if not subject or not reader or (subject, reader) in seen:
                continue
            seen.add((subject, reader))
            if subject not in names:
                device = None
                try:
                    device = self.db.repos.entities.get_device(subject)
                except Exception:  # noqa: BLE001 - a label is cosmetic
                    device = None
                names[subject] = str((device or {}).get("name") or subject)
            out.setdefault(reader, []).append(
                {
                    "device_id": subject,
                    "label": names[subject],
                    "lifecycle": lifecycle or None,
                    "declared_by": row.get("declared_by"),
                }
            )
        return out

    def export(self, device_id: str) -> dict[str, Any]:
        try:
            data = self.db.export_device_data(device_id)
        except Exception as exc:  # noqa: BLE001
            raise InvalidRequest(f"export failed: {exc}") from exc
        has_data = any(value for key, value in data.items() if key != "device_id")
        if not has_data and self.db.repos.entities.get_device(device_id) is None:
            raise NotFound(f"device {device_id!r} not found")
        return data


def _safe_history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row.get("timestamp"),
        "power_w": row.get("power_w"),
        "source": row.get("source"),
        "confidence": row.get("confidence"),
        "quality_status": row.get("quality_status"),
        "operating_stage": row.get("operating_stage"),
    }
