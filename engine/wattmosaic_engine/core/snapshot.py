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
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ..api.facade import InvalidRequest, NotFound, confidence_tier_vocabulary
from ..contracts import (
    Bounds,
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    InferenceSnapshot,
    JointInferenceResult,
    MeasurementBasis,
    MeasurementType,
    MonitoringLevel,
    ReaderMeasurement,
    ReaderResidualPoint,
    ReconciliationV3,
    SnapshotId,
    SnapshotResponse,
    VersionTriple,
)

# ZI: the legacy read view (``SnapshotMixin``) still serves the shared legacy
# response contract and legacy topology until ZC removes them.  Both are reached
# through the legacy module namespace so this area binds no legacy name.
from ..contracts import legacy as _legacy
from ..mapping.display_tree import TREE_VERSION, build_display_tree
from .rows import row_to_inference_result, row_to_reconciliation

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

    __slots__ = ("_store", "_clock", "_current")

    def __init__(self, store: SnapshotStore, clock: Any) -> None:
        self._store = store
        self._clock = clock
        self._current: InferenceSnapshot | None = None

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
        # validator only allows through the private constructor (I5's MEASURED
        # estimates).  Rebuild those through ``Bounds._point`` before validating.
        SnapshotAssembler._repair_point_bounds(data)
        return InferenceSnapshot.model_validate(data)

    @staticmethod
    def _repair_point_bounds(data: dict[str, Any]) -> None:
        for estimate in data.get("device_estimates", ()):
            bounds = estimate.get("bounds")
            if not isinstance(bounds, dict):
                continue
            low, high, basis = bounds.get("lower_w"), bounds.get("upper_w"), bounds.get("basis")
            if low is not None and high is not None and low == high and basis is not None:
                estimate["bounds"] = Bounds._point(low, basis).model_dump(mode="json")  # type: ignore[arg-type]

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
        main_w: float | None = None
        for candidate in ("Main", "main", "ct_main"):
            if candidate in residual_by_reader:
                main_w = residual_by_reader[candidate]
                break
        identified = sum(
            float(e.point_w)
            for e in devices.values()
            if e.point_w is not None and e.status in (EstimateStatus.ESTIMATED, EstimateStatus.MEASURED)
        ) or None
        group_w = sum(float(g.point_w) for g in groups.values() if g.point_w is not None) or None
        unknown_w = None
        unknowns = getattr(inputs.result, "unknown_estimates", ())
        if unknowns:
            parts = [
                float(u.point_w if u.point_w is not None else u.delta_w)
                for u in unknowns
                if (u.point_w if u.point_w is not None else u.delta_w) is not None
            ]
            unknown_w = sum(parts) or None
        reconciliation = ReconciliationV3(
            main_w=main_w,
            identified_w=identified,
            group_w=group_w,
            unknown_w=unknown_w,
            residual_w=residual_by_reader.get("Main", residual_by_reader.get("main")),
        )

        # The snapshot id is derived from the *content*, so a changed state gets
        # a distinct id (S17's table has a UNIQUE constraint on snapshot_id) and
        # an unchanged state gets the same id — which ``put_snapshot`` then
        # deduplicates by content hash.
        payload = {
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
        snapshot_id = inputs.snapshot_id or "snap-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

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

    def snapshot(self) -> SnapshotResponse:
        rows = self.db.latest_inference_results()
        results = [row_to_inference_result(r) for r in rows]
        rec_row = self.db.latest_reconciliation()
        return SnapshotResponse(
            timestamp=self._clock(),
            reconciliation=row_to_reconciliation(rec_row, clock=self._clock),
            results=results,
            quality=self.latest_quality,
        )

    # -- D4 v3 snapshot / device / group / provenance reads ------------------ #
    def _v3_snapshot(self) -> InferenceSnapshot | None:
        """The latest published v3 snapshot, with no hidden in-memory state.

        P6 installs a :class:`SnapshotAssembler` as ``_snapshot_assembler_v3``;
        before that, the S17 persisted snapshot is the single source of truth
        (HB §15).  This method never fabricates an empty snapshot — a missing
        row means "not yet computed" and callers turn that into a 404.
        """
        assembler = getattr(self, "_snapshot_assembler_v3", None)
        if assembler is not None:
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
        resolver = ProvenanceResolver(snapshot)
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

    def devices(self) -> list[dict[str, Any]]:
        topology = self._active_topology()
        devices = topology.devices if topology else []
        configurations = self._effective_configurations(topology)
        recorded_results = self._latest_results_map()
        out: list[dict[str, Any]] = []
        for dev in devices:
            recorded = recorded_results.get(dev.device_id)
            out.append(
                {
                    "device_id": dev.device_id,
                    "name": dev.name,
                    "phase": dev.phase,
                    "double_pole": dev.double_pole,
                    "topology_status": dev.topology_status,
                    "branch_ct_ids": list(dev.branch_ct_ids),
                    "latest_result": (
                        recorded.model_dump(mode="json") if recorded is not None else None
                    ),
                    "confidence_tier": self._tier_text(recorded),
                    "configuration": self._configuration_payload(
                        configurations.get(dev.device_id)
                    ),
                    "model": self._active_model_summary(dev.device_id),
                }
            )
        return out

    def device(self, device_id: str) -> dict[str, Any]:
        topology = self._active_topology()
        dev = topology.device_map().get(device_id) if topology else None
        result = self.latest_results.get(device_id) or self._latest_result_from_db(device_id)
        if dev is None and result is None:
            raise NotFound(f"device {device_id!r} not found")
        configuration = None
        if dev is not None:
            configurations = self._effective_configurations(topology)
            configuration = self._configuration_payload(configurations.get(device_id))
        return {
            "device_id": device_id,
            "name": dev.name if dev else None,
            "topology": dev.model_dump(mode="json") if dev else None,
            "latest_result": result.model_dump(mode="json") if result else None,
            "confidence_tier": self._tier_text(result),
            "configuration": configuration,
            "model": self._active_model_summary(device_id),
            "history": self._device_history(device_id),
        }

    def _device_history(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent inference results for one device, newest first.

        ``Database.inference_history`` filters with ``timestamp >= since``.  The
        pre-1.6.0 call passed ``since=self._clock()``, which excludes every row that
        has already been written -- and a stored result is always in the past.
        The endpoint therefore always reported ``history: []`` while the table
        held rows (``docs/assessment_gap_analysis.md`` G17).  The lower bound is
        now an explicit, configured lookback.
        """
        try:
            since = self._clock() - timedelta(
                seconds=max(1.0, float(self.config.history_lookback_s))
            )
            rows = self.db.inference_history(device_id, since=since, limit=limit)
        except Exception:  # noqa: BLE001
            return []
        return [_safe_history_row(r) for r in rows]

    def _latest_results_map(self) -> dict[str, Any]:
        """Latest known result per device: in-memory first, stored rows second.

        ``latest_inference_results()`` is a whole-table GROUP BY over
        ``inference_results``.  Asking it once per device is an N+1 query:
        measured at ~670 ms for 28 devices against a 343 560-row table, which
        would stall the event loop on every caller.  Fetch it once, index it,
        and let the in-memory map win, so a result computed since the last
        persist is not shadowed by an older row.

        The database fallback matters for more than speed.  Right after a
        restart ``latest_results`` is empty until the first inference cycle, so
        a reader that consulted only the in-memory map would report every
        device as unknown/0 W while ``/devices`` reported real power -- the
        same install showing two different answers on two tabs.

        Cost note: ``latest_inference_results`` is a covering-index scan whose
        price grows with the number of retained rows (~24 ms warm and ~590 ms
        cold at the production 343k).  It is deliberately not bounded by
        timestamp: a device that stopped reporting a month ago still has a
        legitimate latest row, and dropping it would be a correctness change,
        not an optimisation.  Callers that can be slow therefore run this in a
        thread -- see the ``devices`` and ``topology_tree`` routes.
        """
        try:
            latest_rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            # Not silent: right after a restart the in-memory map is empty, so a
            # swallowed error here makes every device read as unknown on both
            # ``/devices`` and ``/topology/tree`` with nothing in the log to
            # explain it.  Degrade, but say so.
            log.warning(
                "latest results unavailable; serving in-memory results only",
                exc_info=True,
            )
            latest_rows = []
        merged: dict[str, Any] = {}
        for row in latest_rows:
            device_key = row.get("device_id")
            if not device_key:
                continue
            try:
                merged[str(device_key)] = row_to_inference_result(row)
            except Exception:  # noqa: BLE001
                continue
        merged.update(self.latest_results)
        return merged

    def _latest_result_from_db(self, device_id: str) -> _legacy.InferenceResult | None:
        try:
            rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            if row.get("device_id") == device_id:
                try:
                    return row_to_inference_result(row)
                except Exception:  # noqa: BLE001
                    return None
        return None

    def _effective_configurations(
        self, topology: _legacy.Topology | None
    ) -> dict[str, Any]:
        """Resolve every device's effective configuration once per call.

        The configuration layer is part of the optional inference backend, so the
        import is guarded: a plain engine (no disaggregation package) still serves
        ``/devices`` and simply omits the resolved configuration.
        """
        if topology is None:
            return {}
        try:
            from ..device_configuration import effective_configs

            return effective_configs(topology)
        except Exception:  # noqa: BLE001 - optional backend must not break reads
            return {}

    @staticmethod
    def _monitoring_text(value: Any) -> str:
        """Publish the monitoring axis only as a value the contract defines.

        Nothing produces the retired ``dedicated`` level any more, but the
        payload must not lean on that implicitly: a stored declaration that
        still carries it is read back from metadata, so an unrecognised value
        degrades to ``unknown`` instead of being served verbatim.
        """
        raw = getattr(value, "value", value)
        try:
            return MonitoringLevel(str(raw)).value
        except ValueError:
            return MonitoringLevel.UNKNOWN.value

    @classmethod
    def _configuration_payload(cls, config: Any) -> dict[str, Any] | None:
        """JSON-ready view of one :class:`EffectiveConfig` (enums as strings)."""
        if config is None:
            return None

        def _text(value: Any) -> Any:
            return getattr(value, "value", value)

        def _list(value: Any) -> list[Any]:
            return [str(item) for item in (value or ())]

        return {
            "device_type": _text(getattr(config, "device_type", None)),
            "connectivity": _text(getattr(config, "connectivity", None)),
            "monitoring": cls._monitoring_text(getattr(config, "monitoring", None)),
            "behavior": _text(getattr(config, "behavior", None)),
            "monitored_by_ct_ids": _list(getattr(config, "monitored_by_ct_ids", ())),
            "load_entity_ids": _list(getattr(config, "load_entity_ids", ())),
            "mode_entity_ids": _list(getattr(config, "mode_entity_ids", ())),
            "expected_on_w": getattr(config, "expected_on_w", None),
            "expected_standby_w": getattr(config, "expected_standby_w", None),
            "shared_unmodelled": bool(getattr(config, "shared_unmodelled", False)),
            "notes": _list(getattr(config, "notes", ())),
        }

    @staticmethod
    def _tier_text(result: _legacy.InferenceResult | None) -> str | None:
        if result is None:
            return None
        tier = getattr(result, "confidence_tier", None)
        if tier is None:
            return None
        return str(getattr(tier, "value", tier))

    def confidence_tiers(self) -> dict[str, Any]:
        """Static ordered tier vocabulary (labels clients must not hard-code)."""
        return confidence_tier_vocabulary()

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """Read-time panel → circuits → devices hierarchy (contract §1).

        Cheap by construction: it reads the already-cached topology and one
        indexed query for the latest result per device (the same ~25 ms query
        ``/devices`` makes), and it never scans CT samples.  The expensive
        nesting inference runs **only** when ``include_nesting`` is explicitly
        requested; even then it can only merge duplicate panels or add advisory
        suggestions, never change leaf counts.
        """
        topology = self._active_topology()
        if topology is None:
            return {
                "version": TREE_VERSION,
                "status": "candidate",
                "generated_at": self._clock().isoformat().replace("+00:00", "Z"),
                "topology_version": 0,
                "stats": {
                    "panels": 0,
                    "circuits": 0,
                    "devices": 0,
                    "unplaced_devices": 0,
                    "unplaced_circuits": 0,
                    "max_depth": 0,
                    "duplicate_feeds": 0,
                },
                "roots": [],
                "flat": {"panels": [], "circuits": [], "devices": []},
                "notes": ["no topology configured"],
            }
        nesting = self.nesting_candidates() if include_nesting else None
        return build_display_tree(
            topology,
            latest_results=self._latest_results_map(),
            generated_at=self._clock().isoformat().replace("+00:00", "Z"),
            include_nesting=include_nesting,
            nesting=nesting,
        )

    def export(self, device_id: str) -> dict[str, Any]:
        try:
            data = self.db.export_device_data(device_id)
        except Exception as exc:  # noqa: BLE001
            raise InvalidRequest(f"export failed: {exc}") from exc
        if not data.get("inference_results") and not data.get("learning_samples") and not data.get("model_versions"):
            topology = self._active_topology()
            known = topology.device_map() if topology else {}
            if device_id not in known:
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
