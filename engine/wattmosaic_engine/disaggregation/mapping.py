"""Derived Device↔Reader Mapping Matrix projection (REQ-MAPPING-001…007).

This module is a *read-time projection* over three existing inputs:

* the electrical :class:`~wattmosaic_engine.contracts.Topology` (channels,
  devices, containment and typed relations);
* the device-model views (``electrical.models.ModelView``) keyed by device id;
* evidence: ``DEVICE_CT_ASSOCIATION`` relations and unknown-load/residual
  records.

It never writes storage, never reads a clock, and never takes a sample.  A cell
is recomputed only from the three inputs above, so the same inputs always yield
a byte-identical result (REQ-MAPPING-001, REQ-MAPPING-005): there is no
per-sample snapshot.  The matrix cites the source of every value it publishes
through :attr:`DeviceReaderMappingCell.sources_used`.

Honesty rules that shape the projection
---------------------------------------
* A contribution, minimum or maximum is published **only** where real evidence
  (an association's matched deltas, or a model's estimate/bound) supports it.
  Otherwise the field is ``None``.  Observability ``UNKNOWN`` always carries
  ``None`` for all three, as the contract requires.
* Unknown observed-but-unidentified loads get an explicit
  :attr:`DeviceReaderMappingCell.is_unknown_load` marker with a namespaced,
  opaque ``device_id`` (never a fabricated real device id).
* Cardinality (1:1 / 1:N / N:1 / N:N) is derived from the topology's own
  device↔reader incidence; a shared conductor is reported as
  ``NON_IDENTIFIABLE`` rather than split into invented per-device cells.
* Complexity (how many readers) never multiplies confidence (REQ-MAPPING-006);
  confidence dimensions come only from the association/model/status evidence.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    ConfidenceVector,
    CTChannel,
    DeviceReaderMappingCell,
    DeviceTopology,
    MappingCellStatus,
    MappingObservability,
    MeasurementBasis,
    RelationKind,
    Topology,
    TopologyRelation,
    TopologyStatus,
    UnknownLoadProfile,
    UnknownResidualEvent,
)
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.topology import build_graph, version_token

from .association import (
    AMBIGUOUS_MULTIPLIER,
    SPLIT_MULTIPLIER,
    VERDICT_AMBIGUOUS,
    VERDICT_SINGLE_EVENT,
    VERDICT_SUPPORTED,
)

#: The full evidence bar for a CONFIRMED cell.  ``2`` matches the association
#: module's default ``min_events``; it is a *count*, never folded into confidence.
CONFIRM_MIN_EVENTS = 2

#: Deterministic method tag: a name, never a blended score.
METHOD = "mapping_projection:v1"

#: Namespace for the opaque ``device_id`` of an unknown-load cell.  It can never
#: collide with a real device id because no real device id is prefixed this way.
_UNKNOWN_ID_PREFIX = "unknown-load::"

_VERDICT_AMBIGUOUS = str(VERDICT_AMBIGUOUS)
_VERDICT_SINGLE = str(VERDICT_SINGLE_EVENT)
_VERDICT_SUPPORTED = str(VERDICT_SUPPORTED)
_VERDICT_SPLIT = "split"


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MappingEvidence:
    """The evidence side of the projection, all inputs optional/neutral."""

    associations: tuple[TopologyRelation, ...] = ()
    unknown_load_profiles: tuple[UnknownLoadProfile, ...] = ()
    unknown_residual_events: tuple[UnknownResidualEvent, ...] = ()


@dataclass(frozen=True)
class MappingCoverage:
    """REQ-MAPPING-007: what the matrix cannot see."""

    unobserved_devices: tuple[str, ...] = ()
    unexplained_scopes: tuple[str, ...] = ()
    readers_without_attributable_device: tuple[str, ...] = ()
    #: Readers whose summed cells would double-count one device (REQ-MAPPING-002).
    non_summing_readers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "unobserved_devices": list(self.unobserved_devices),
            "unexplained_scopes": list(self.unexplained_scopes),
            "readers_without_attributable_device": list(
                self.readers_without_attributable_device
            ),
            "non_summing_readers": list(self.non_summing_readers),
        }


@dataclass(frozen=True)
class MappingMatrix:
    """The derived matrix: cells plus its own coverage statement."""

    cells: tuple[DeviceReaderMappingCell, ...]
    coverage: MappingCoverage
    reader_configuration_version: str
    topology_version: str | None = None
    #: Device-major axes (REQ: CTs are columns, real devices are rows).  Additive
    #: and defaulted so existing callers/envelopes stay byte-compatible.
    devices: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """A stable, JSON-round-trippable view for byte-identical comparison."""
        return {
            "reader_configuration_version": self.reader_configuration_version,
            "topology_version": self.topology_version,
            "cells": [cell.model_dump(mode="json") for cell in self.cells],
            "coverage": self.coverage.as_dict(),
            "devices": list(self.devices),
            "readers": list(self.readers),
        }

    def stable_json(self) -> str:
        """Canonical bytes: two projections of the same inputs are identical."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Small helpers (no clock, no hidden state)
# --------------------------------------------------------------------------- #


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _rounded(value: float) -> float:
    return round(value, 6)


def _source(source: str, reason: str) -> dict[str, str]:
    return {"source": source, "reason": reason}


def _sorted_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(sources, key=lambda item: (item["source"], item["reason"]))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _delta_values(rows: Sequence[Any]) -> list[float]:
    """Finite per-observation contributions from an association's matched rows."""
    out: list[float] = []
    for row in rows:
        for key in ("scaled_delta_w", "ct_delta_w"):
            value = _finite(_field(row, key))
            if value is not None:
                out.append(value)
                break
    return out


# --------------------------------------------------------------------------- #
# Incidence: which readers observe which devices
# --------------------------------------------------------------------------- #


def _declared_pairs(topology: Topology) -> set[tuple[str, str]]:
    """Device↔reader pairs declared by the topology structure itself."""
    pairs: set[tuple[str, str]] = set()
    device_ids = {d.device_id for d in topology.devices}
    channel_map = topology.channel_map()
    for dev in topology.devices:
        for cid in dev.branch_ct_ids:
            if cid in channel_map:
                pairs.add((dev.device_id, cid))
    for cid, channel in channel_map.items():
        owner = channel.device_id
        if owner is not None and owner in device_ids:
            pairs.add((owner, cid))
    return pairs


def _is_ct_auto_circuit(device: DeviceTopology, channels: Mapping[str, CTChannel]) -> bool:
    """True for a discovery-generated CT circuit, not a real appliance.

    Passive discovery emits one ``DeviceTopology`` per measured CT conductor
    (``dev_<ct_id>``) so the topology validates and the circuit is visible.  Such
    a row describes a *conductor* that may feed zero or more appliances; it is
    not an appliance itself.  CTs belong on the reader/column axis, so these
    circuits must not surface as device rows.

    The discriminator is structural and conservative: a device whose id is
    ``dev_<ct_id>`` for an actual channel, or whose metadata carries a discovery
    circuit marker, is a CT circuit.  A config/materialised-backed device (or an
    explicitly named/declared device such as a user fixture) is a real device and
    is kept even if it sits behind a CT whose id happens to share its slug.
    """
    metadata = device.metadata or {}
    # A device a person configured/declared is real regardless of its id shape;
    # never drop it just because a slug resembles a CT id.
    config_markers = (
        "device_config",
        "materialised_from_config",
        "monitoring_level",
        "power_behavior",
        "connectivity",
    )
    if any(key in metadata for key in config_markers):
        return False
    markers = ("shared_circuit", "ct_group", "pair_confidence")
    if any(key in metadata for key in markers):
        return True
    device_id = device.device_id
    if device_id.startswith("dev_"):
        # Discovery names every auto-circuit exactly ``dev_<ct_id>``; that ct id
        # is a real channel, while a real appliance's ``dev_`` slug is not.
        return device_id[len("dev_") :] in channels
    return False


def _real_device_ids(
    topology: Topology,
    *,
    device_sources: Sequence[Sequence[Any]] = (),
) -> tuple[str, ...]:
    """Real device ids for the device/row axis, in stable order.

    Sources, in priority order:

    * ``device_sources`` — optional, additive lists of device records supplied by
      the caller (HA registry devices and/or DeviceConfiguration-backed devices).
      Each record may be a mapping or an object exposing ``device_id`` and an
      optional ``name``.  A HA registry id may be either ``device_id`` or ``id``.
    * ``topology.devices`` — a DeviceTopology row that is not a discovery CT
      auto-circuit (see :func:`_is_ct_auto_circuit`).  Config-backed devices and
      auto-placed HA appliances live here.

    CT circuit rows are never returned: CTs belong on the reader axis only.
    """
    channels = topology.channel_map()
    real: set[str] = set()

    for source in device_sources:
        for entry in source:
            if not isinstance(entry, Mapping) and not hasattr(entry, "device_id"):
                continue
            identifier = _field(entry, "device_id") or _field(entry, "id")
            if identifier is None:
                continue
            text = str(identifier).strip()
            if not text or text.startswith(_UNKNOWN_ID_PREFIX):
                continue
            real.add(text)

    for device in topology.devices:
        if _is_ct_auto_circuit(device, channels):
            continue
        if device.device_id.startswith(_UNKNOWN_ID_PREFIX):
            continue
        real.add(device.device_id)

    return tuple(sorted(real))


def _reader_ids(topology: Topology) -> tuple[str, ...]:
    """The reader/column axis: every real CT channel in the topology."""
    return tuple(sorted(topology.channel_map()))


def _association_pairs(
    relations: Sequence[TopologyRelation],
    known_devices: set[str],
    known_readers: set[str],
) -> dict[tuple[str, str], TopologyRelation]:
    """Valid association edges keyed by (device, reader); evidence wins on dupes."""
    out: dict[tuple[str, str], TopologyRelation] = {}
    for relation in relations:
        if str(getattr(relation.kind, "value", relation.kind)) != RelationKind.DEVICE_CT_ASSOCIATION.value:
            continue
        pair = (relation.subject_id, relation.object_id)
        if pair[0] not in known_devices or pair[1] not in known_readers:
            continue
        out[pair] = relation
    return out


def _cardinality(n_readers: int, n_devices: int) -> str:
    if n_readers > 1:
        return "many_to_many" if n_devices > 1 else "one_to_many"
    return "many_to_one" if n_devices > 1 else "one_to_one"


# --------------------------------------------------------------------------- #
# Per-cell derivation
# --------------------------------------------------------------------------- #


def _observability(
    *,
    reader_is_owner: bool,
    is_attribution_ct: bool,
    is_enclosing: bool,
    has_association: bool,
) -> tuple[MappingObservability, bool]:
    """Return (observability, identifiable) for a declared pairing."""
    if reader_is_owner:
        return MappingObservability.DIRECT, True
    if is_attribution_ct:
        return MappingObservability.DERIVED, True
    if is_enclosing:
        # A device behind a shared conductor: observed, not identifiable from
        # the topology alone.  A supported association may still identify it.
        return MappingObservability.INDIRECT, not has_association
    if has_association:
        return MappingObservability.DERIVED, True
    return MappingObservability.UNKNOWN, False


def _contribution(
    *,
    relation: TopologyRelation | None,
    model: ModelView | None,
) -> tuple[float | None, float | None, float | None, list[float]]:
    """(estimate, min, max, supporting-deltas) from real evidence only."""
    if relation is not None:
        stats = _field(relation.evidence, "stats", {}) or {}
        supporting = list(_field(relation.evidence, "supporting", ()) or ())
        joint = list(_field(stats, "joint", ()) or ())
        deltas = _delta_values([*supporting, *joint])
        if deltas:
            estimate = _rounded(_median(deltas))
            return estimate, _rounded(min(deltas)), _rounded(max(deltas)), supporting
        # ``stats['contribution']`` is a device/CT *ratio*, not watts, so it is
        # never published as a watt estimate.  ``median_delta_w`` is the CT's
        # matched step in watts -- the only honest watt fallback here.
        watt_median = _finite(_field(stats, "median_delta_w"))
        if watt_median is not None:
            return _rounded(watt_median), None, None, supporting
    if model is not None:
        model_estimate = _finite(model.estimated_power_w)
        if model_estimate is not None:
            upper = _finite(model.upper_bound_w)
            if upper is not None and upper < model_estimate:
                upper = None
            return _rounded(model_estimate), None, (None if upper is None else _rounded(upper)), []
    return None, None, None, []


def _status(
    *,
    verdict: str,
    relation: TopologyRelation | None,
    evidence_count: int,
    model: ModelView | None,
    identifiable: bool,
    observability: MappingObservability,
) -> MappingCellStatus:
    if observability is MappingObservability.INDIRECT and not identifiable:
        return MappingCellStatus.NON_IDENTIFIABLE
    if verdict in (_VERDICT_AMBIGUOUS, "conflicted"):
        return MappingCellStatus.CONFLICTED
    if verdict == _VERDICT_SUPPORTED:
        confirmed = relation is not None and TopologyStatus(relation.status) is TopologyStatus.CONFIRMED
        if confirmed and evidence_count >= CONFIRM_MIN_EVENTS:
            return MappingCellStatus.CONFIRMED
        return MappingCellStatus.PROVISIONAL
    if verdict == _VERDICT_SPLIT:
        return MappingCellStatus.PROVISIONAL
    if verdict == _VERDICT_SINGLE or model is not None:
        return MappingCellStatus.LEARNING
    return MappingCellStatus.UNKNOWN


def _confidence(
    *,
    relation: TopologyRelation | None,
    model: ModelView | None,
    verdict: str,
) -> ConfidenceVector:
    ct_association: float | None = None
    topology_conf: float | None = None
    if relation is not None:
        ct_association = _finite(relation.confidence)
        if TopologyStatus(relation.status) is TopologyStatus.CONFIRMED:
            topology_conf = 1.0
        # Ambiguity caps confidence explicitly (REQ-MAPPING-006); it never
        # rewards the number of readers.
        if verdict == _VERDICT_AMBIGUOUS and ct_association is not None:
            ct_association = min(ct_association, AMBIGUOUS_MULTIPLIER)
        elif verdict == _VERDICT_SPLIT and ct_association is not None:
            ct_association = min(ct_association, SPLIT_MULTIPLIER)
    power = None
    if model is not None:
        quality = model.quality
        power = _rounded(quality) if quality > 0.0 else None
    return ConfidenceVector(
        power=power,
        ct_association=ct_association,
        topology=topology_conf,
        method=METHOD,
    )


def _basis(relation: TopologyRelation | None, model: ModelView | None, fallback: MeasurementBasis) -> MeasurementBasis:
    if relation is not None:
        raw = _field(_field(relation.evidence, "stats", {}), "basis")
        if raw is not None:
            try:
                return MeasurementBasis(str(getattr(raw, "value", raw)))
            except ValueError:
                pass
    if model is not None:
        return MeasurementBasis.ESTIMATED_APPLIANCE_TOTAL
    return fallback


def _last_validated(relation: TopologyRelation | None, verdict: str) -> datetime | None:
    if relation is not None and verdict == _VERDICT_SUPPORTED:
        for value in (relation.valid_from, relation.created_at):
            if isinstance(value, datetime):
                return value
    return None


def _device_cell(
    *,
    device_id: str,
    reader_id: str,
    relation: TopologyRelation | None,
    model: ModelView | None,
    relation_type: str,
    observability: MappingObservability,
    identifiable: bool,
    basis: MeasurementBasis,
    sources: list[dict[str, str]],
    reader_configuration_version: str,
    topology_version: str | None,
) -> DeviceReaderMappingCell:
    estimate, low, high, supporting = _contribution(relation=relation, model=model)
    if observability is MappingObservability.UNKNOWN:
        estimate, low, high = None, None, None
    verdict = ""
    if relation is not None:
        verdict = str(_field(relation.evidence, "verdict", "") or "")
    evidence_count = len(supporting)
    if relation is not None and evidence_count == 0:
        supporting_events = _field(relation.evidence, "supporting", ())
        evidence_count = len(supporting_events) if supporting_events is not None else 0
    status = _status(
        verdict=verdict,
        relation=relation,
        evidence_count=evidence_count,
        model=model,
        identifiable=identifiable,
        observability=observability,
    )
    confidence = _confidence(relation=relation, model=model, verdict=verdict)
    last_validated = _last_validated(relation, verdict)
    return DeviceReaderMappingCell(
        device_id=device_id,
        reader_id=reader_id,
        reader_configuration_version=reader_configuration_version,
        observability=observability,
        estimated_contribution_w=estimate,
        min_contribution_w=low,
        max_contribution_w=high,
        confidence=confidence,
        evidence_count=evidence_count,
        last_validated_at=last_validated,
        status=status,
        basis=basis,
        relation_type=relation_type,
        sources_used=_sorted_sources(sources),
        model_version=None if model is None else model.version,
        topology_version=topology_version,
        is_unknown_load=False,
        unknown_load_profile_id=None,
    )


# --------------------------------------------------------------------------- #
# Unknown loads (REQ-MAPPING-004)
# --------------------------------------------------------------------------- #


def _unknown_cells(
    *,
    profiles: Sequence[UnknownLoadProfile],
    events: Sequence[UnknownResidualEvent],
    reader_configuration_version: str,
    topology_version: str | None,
) -> tuple[list[DeviceReaderMappingCell], tuple[str, ...]]:
    by_reader: dict[str, list[UnknownResidualEvent]] = {}
    for event in events:
        by_reader.setdefault(event.reader_id, []).append(event)

    profile_by_scope: dict[str, list[UnknownLoadProfile]] = {}
    for profile in profiles:
        profile_by_scope.setdefault(profile.scope_id, []).append(profile)

    cells: list[DeviceReaderMappingCell] = []
    scopes: set[str] = set()
    for reader_id in sorted(by_reader):
        reader_events = sorted(by_reader[reader_id], key=lambda e: (e.window_start, e.event_id))
        values = [
            value
            for event in reader_events
            for value in (_finite(event.dynamic_w), _finite(event.residual_w))
            if value is not None
        ]
        estimate = _rounded(max(values, key=abs)) if values else None
        matching = profile_by_scope.get(reader_id, [])
        profile_id = matching[0].profile_id if matching else None
        sources = [_source("residual", "unexplained_scope")]
        if profile_id is not None:
            sources.append(_source("unknown_load_profile", "observed_profile"))
        scopes.update(str(event.scope_id) for event in reader_events)
        cells.append(
            DeviceReaderMappingCell(
                device_id=f"{_UNKNOWN_ID_PREFIX}{reader_id}",
                reader_id=reader_id,
                reader_configuration_version=reader_configuration_version,
                observability=MappingObservability.DERIVED,
                estimated_contribution_w=estimate,
                min_contribution_w=None,
                max_contribution_w=None,
                confidence=ConfidenceVector(method=METHOD),
                evidence_count=len(reader_events),
                last_validated_at=None,
                status=MappingCellStatus.NON_IDENTIFIABLE,
                basis=MeasurementBasis.UNKNOWN,
                relation_type=None,
                sources_used=_sorted_sources(sources),
                model_version=None,
                topology_version=topology_version,
                is_unknown_load=True,
                unknown_load_profile_id=profile_id,
            )
        )
    return cells, tuple(sorted(scopes))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def build_mapping_matrix(
    topology: Topology,
    *,
    models: Mapping[str, ModelView | None] | None = None,
    evidence: MappingEvidence | None = None,
    reader_configuration_version: str,
    topology_version: str | None = None,
    device_sources: Sequence[Sequence[Any]] = (),
) -> MappingMatrix:
    """Derive the device/reader mapping matrix from topology, models, evidence.

    Pure and deterministic: no clock, no sample, no storage.  Passing the same
    inputs twice yields a byte-identical :meth:`MappingMatrix.stable_json`.

    ``reader_configuration_version`` is an explicit input because every cell
    requires one and the projection must not invent it.  ``topology_version``
    is the opaque version id (when known) and is otherwise ``None``.

    ``device_sources`` is optional and additive: a sequence of iterables of
    real-device records (HA registry devices and/or DeviceConfiguration-backed
    devices) used to build the device/row axis.  CT auto-circuits are excluded
    from that axis regardless.  Existing callers that pass nothing keep the
    cell envelope unchanged; the returned matrix additionally exposes the
    ``devices`` and ``readers`` axes for device-major consumers.
    """
    models = models or {}
    evidence = evidence or MappingEvidence()
    graph = build_graph(topology)

    channels = topology.channel_map()
    # CT-derived auto-circuits (``dev_<ct_id>``) describe a measured conductor,
    # not an appliance.  CTs belong on the reader axis, so exclude these rows
    # from the device axis, the cells and the coverage alike.  Real devices
    # (config-backed or HA appliances) stay.
    ct_circuit_ids = {
        device.device_id
        for device in topology.devices
        if _is_ct_auto_circuit(device, channels)
    }
    known_devices = set(graph.device_ids) - ct_circuit_ids
    known_readers = set(graph.channel_ids)

    topology_assocs = _association_pairs(topology.relations, known_devices, known_readers)
    evidence_assocs = _association_pairs(evidence.associations, known_devices, known_readers)
    assoc_by_pair = {**topology_assocs, **evidence_assocs}

    declared = {
        (device_id, reader_id)
        for device_id, reader_id in _declared_pairs(topology)
        if device_id not in ct_circuit_ids
    }
    all_pairs = declared | set(assoc_by_pair)

    device_readers: dict[str, set[str]] = {}
    reader_devices: dict[str, set[str]] = {}
    for device_id, reader_id in all_pairs:
        device_readers.setdefault(device_id, set()).add(reader_id)
        reader_devices.setdefault(reader_id, set()).add(device_id)

    # A reader whose cells would double-count one device: it observes the same
    # device through more than one overlapping path (1:N from the device side).
    non_summing = tuple(sorted(reader for reader, devs in reader_devices.items() if len(devs) > 1))

    cells: list[DeviceReaderMappingCell] = []
    attributed_readers: set[str] = set()
    for device_id, reader_id in sorted(all_pairs):
        relation = assoc_by_pair.get((device_id, reader_id))
        model = models.get(device_id)
        channel = graph.channel(reader_id)
        owner = None if channel is None else channel.device_id
        attribution = set(graph.attribution_ct_ids(device_id))
        enclosing = set(graph.device_channels(device_id))
        has_association = relation is not None

        observability, identifiable = _observability(
            reader_is_owner=owner == device_id,
            is_attribution_ct=reader_id in attribution,
            is_enclosing=reader_id in enclosing,
            has_association=has_association,
        )

        sources = [_source("topology", "declared_pair" if (device_id, reader_id) in declared else "relation")]
        if channel is not None:
            sources.append(_source("topology", "channel_map"))
        if relation is not None:
            verdict = str(_field(relation.evidence, "verdict", "") or "")
            sources.append(_source("association", f"verdict:{verdict}" if verdict else "relation"))
        if model is not None and model.version is not None:
            sources.append(_source("model", "estimated_power"))

        relation_type = _cardinality(len(device_readers[device_id]), len(reader_devices[reader_id]))
        basis = _basis(
            relation,
            model,
            MeasurementBasis.UNKNOWN
            if channel is None
            else MeasurementBasis(str(getattr(channel.measurement_basis, "value", channel.measurement_basis))),
        )
        cell = _device_cell(
            device_id=device_id,
            reader_id=reader_id,
            relation=relation,
            model=model,
            relation_type=relation_type,
            observability=observability,
            identifiable=identifiable,
            basis=basis,
            sources=sources,
            reader_configuration_version=reader_configuration_version,
            topology_version=topology_version,
        )
        cells.append(cell)
        if not cell.is_unknown_load:
            attributed_readers.add(reader_id)

    unknown_cells, unexplained_scopes = _unknown_cells(
        profiles=evidence.unknown_load_profiles,
        events=evidence.unknown_residual_events,
        reader_configuration_version=reader_configuration_version,
        topology_version=topology_version,
    )
    cells.extend(unknown_cells)

    cells.sort(key=lambda c: (c.device_id, c.reader_id, c.is_unknown_load))
    unobserved_devices = tuple(sorted(known_devices - set(device_readers)))
    readers_without = tuple(sorted(known_readers - attributed_readers))
    coverage = MappingCoverage(
        unobserved_devices=unobserved_devices,
        unexplained_scopes=unexplained_scopes,
        readers_without_attributable_device=readers_without,
        non_summing_readers=non_summing,
    )
    return MappingMatrix(
        cells=tuple(cells),
        coverage=coverage,
        reader_configuration_version=reader_configuration_version,
        topology_version=topology_version,
        devices=_real_device_ids(topology, device_sources=device_sources),
        readers=_reader_ids(topology),
    )


# --------------------------------------------------------------------------- #
# Reconciliation after a topology version change (REQ-TOPO-009,
# REQ-CHANGE-004/007, REQ-MAPPING-005; gap G-08)
# --------------------------------------------------------------------------- #
#
# A confirmed topology change changes the configuration that downstream
# inferences and mapping cells were computed against.  Reconciliation answers
# exactly one question -- "what does this change invalidate?" -- without
# mutating any historical observation.  It is a *pure projection*: given the new
# topology and a snapshot of existing records/cells it returns (a) the matrix
# re-derived from the new topology via B3a's :func:`build_mapping_matrix` and
# (b) the identity of every record/cell still stamped with the old version.
# Nothing is deleted, rewritten or promoted.


@dataclass(frozen=True)
class StaleRecord:
    """One downstream artefact whose assumptions a version change invalidated.

    ``record_id`` names an ``InferenceRecord`` (the DB read path filters the
    existing ``topology_version`` integer column); ``cell_key`` names a mapping
    cell as ``device_id|reader_id``.  Exactly one of the two is set.  The stored
    value itself is never touched -- this is a verdict about a record, not a
    rewrite of it.
    """

    record_id: str | None = None
    cell_key: str | None = None
    topology_version: int | str | None = None
    reason: str = "topology_version_superseded"

    @property
    def identity(self) -> str:
        """A stable, human-legible identity for the affected artefact."""
        return self.record_id if self.record_id is not None else str(self.cell_key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "cell_key": self.cell_key,
            "topology_version": self.topology_version,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """The complete, read-only outcome of reconciling a version change.

    ``matrix`` is the deterministic re-derivation under ``new_version``;
    ``stale`` lists every artefact that still carries ``old_version`` (or any
    version other than ``new_version`` when ``strict_new_only`` is off).  The
    result is a value: calling reconciliation twice with the same inputs yields
    two equal results (idempotence), and it never changes the inputs.
    """

    old_version: int | str | None
    new_version: int | str | None
    matrix: MappingMatrix
    stale: tuple[StaleRecord, ...]

    @property
    def affected_record_ids(self) -> tuple[str, ...]:
        return tuple(item.record_id for item in self.stale if item.record_id is not None)

    @property
    def affected_cell_keys(self) -> tuple[str, ...]:
        return tuple(item.cell_key for item in self.stale if item.cell_key is not None)

    def as_dict(self) -> dict[str, Any]:
        """Audit-evidence view; byte-stable for equal inputs."""
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "matrix": self.matrix.as_dict(),
            "stale": [item.as_dict() for item in self.stale],
            "affected_record_ids": list(self.affected_record_ids),
            "affected_cell_keys": list(self.affected_cell_keys),
        }


def _record_version(value: Any) -> int | str | None:
    """Read a record's ``topology_version`` off a model or a decoded dict."""
    return _field(value, "topology_version")


def _record_id(value: Any) -> str | None:
    return _field(value, "record_id")


def reconcile_topology_change(
    *,
    old_version: int | str | None,
    new_version: int | str | None,
    new_topology: Topology,
    reader_configuration_version: str,
    inference_records: Sequence[Any] = (),
    current_matrix: MappingMatrix | None = None,
    models: Mapping[str, ModelView | None] | None = None,
    evidence: MappingEvidence | None = None,
    strict_new_only: bool = False,
) -> ReconciliationResult:
    """Re-derive the mapping matrix and flag history invalidated by a change.

    Pure and deterministic: it never writes storage, never changes a clock and
    never modifies ``inference_records`` or ``current_matrix``.  The matrix is
    re-derived from ``new_topology`` by the B3a projection, so it is exactly the
    matrix the new configuration would produce (REQ-MAPPING-005).  A record or
    cell is flagged stale when the version it carries is no longer the version
    in force:

    * when ``strict_new_only`` is ``False`` (the default), anything that is not
      the new version is stale -- this is the honest "old version" answer even
      when the caller cannot name the exact predecessor;
    * when ``True``, only artefacts carrying ``old_version`` are flagged, which
      is the narrow old→new answer when both endpoints are known.

    Because the check is a comparison against the new version, running twice is
    identical (idempotence) and no stored observation is rewritten.  This is the
    reconciliation half of a *confirmed* version change; detection and promotion
    remain separate, evidence-gated acts (no auto-promotion here).
    """
    new_token = version_token(new_version)
    old_token = version_token(old_version)
    # Mapping cells carry the opaque string spelling of the version; inference
    # records carry the integer.  One token keeps the re-derived matrix and the
    # stale verdicts talking about the same version.
    matrix_version = new_token if new_token is not None else (
        None if new_version is None else str(new_version)
    )
    matrix = build_mapping_matrix(
        new_topology,
        models=models,
        evidence=evidence,
        reader_configuration_version=reader_configuration_version,
        topology_version=matrix_version,
    )

    stale: list[StaleRecord] = []
    for record in inference_records:
        record_token = version_token(_record_version(record))
        is_stale = (
            record_token == old_token
            if strict_new_only and old_token is not None
            else record_token != new_token
        )
        if is_stale:
            stale.append(
                StaleRecord(
                    record_id=str(_record_id(record)),
                    topology_version=_record_version(record),
                )
            )

    if current_matrix is not None:
        for cell in current_matrix.cells:
            cell_token = version_token(cell.topology_version)
            is_stale = (
                cell_token == old_token
                if strict_new_only and old_token is not None
                else cell_token != new_token
            )
            if is_stale:
                stale.append(
                    StaleRecord(
                        cell_key=f"{cell.device_id}|{cell.reader_id}",
                        topology_version=cell.topology_version,
                    )
                )

    stale.sort(key=lambda item: (item.record_id is None, item.identity))
    return ReconciliationResult(
        old_version=old_version,
        new_version=new_version,
        matrix=matrix,
        stale=tuple(stale),
    )
