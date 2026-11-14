"""WP8 -- CT hierarchy discovery: the conservative parent/child relation.

Why this module exists
----------------------
``CTChannel.parent_ct_id`` is a wiring fact.  A wrong edge silently authorises a
wrong total, and a wrong total is indistinguishable from a correct one once it
is stored, so parenthood is inferred **only** when measurement scope itself can
prove containment.  Four relation kinds stay separate and non-implying:

``DEVICE_CT_ASSOCIATION`` (WP5, consumed)
    "this CT observes this device".  Never a parent.
``CT_MEASUREMENT_OVERLAP`` (emitted by :meth:`HierarchyFinder.overlaps`)
    "two CTs respond to the same device".  An overlap, *not* parenthood: the
    two legs of one load respond identically and are still siblings.
``CIRCUIT_MEMBERSHIP`` (WP6, consumed)
    "this device/CT is on this circuit".  Emitted elsewhere; untouched here.
``ELECTRICAL_PARENT_CHILD`` (emitted by :meth:`HierarchyFinder.infer`)
    "this CT's conductors are fed through that CT".

The evidence bar
----------------
A response is not a scope.  Correlation is not topography.  A bigger reading is
not upstream.  So an edge requires **all** of the following, each of which can
be measured and can fail:

scope containment
    The child never exceeds the parent beyond a small tolerance
    (``max_power_violation_frac``) over every comparable instant.  Skipped
    instants are those where neither reading carries meaningful power; an
    instant where the child exceeds an active parent is a *violation*, never a
    skipped sample.
independence
    At least ``min_independent_devices`` distinct devices must respond through
    both CTs.  One device seen twice is the Hot Tub case: two legs of one load.
    A single independent device can never establish parenthood, no matter how
    perfectly the series nest.
operating conditions
    The containment must hold across at least two distinct load levels
    (``single_operating_condition`` otherwise).  One flat regime proves
    nothing: an idle circuit is contained by everything.
response consistency
    The fraction of operating conditions whose containment holds must be at
    least ``min_response_consistency``.
not one conductor
    A constant ratio between the two series is one wire with a factor
    (``scaled_copy_of_one_conductor``), not two nested feeds.
basis
    An ``UNKNOWN`` measurement basis on either CT blocks the comparison
    (``basis_unestablished``).  A factor-adjusted reading and a raw reading are
    not the same quantity.

What is intentionally *not* here
--------------------------------
``validate`` never drops a bad edge: a self edge, an alias self edge, an unknown
CT, a cycle, or an edge contradicting a declared fact comes back with
``status=REJECTED`` and ``evidence['reason']``.  Incomplete topology is a valid
output: an empty result with a report reason is preferred to a guess.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from wattmosaic_engine.contracts import (
    CTChannel,
    MeasurementBasis,
    RelationKind,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.electrical.factors import FactorRegistry

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from wattmosaic_engine.storage.database import Database

#: Instants either reading must reach for a comparison to say anything.
MIN_PAIR_ACTIVE_W = 200.0

#: Watts of slack, so CT resolution and sampling skew cannot fake a violation.
VIOLATION_TOL_W = 30.0

#: Fewer comparable instants than this is no measurement-scope evidence at all.
MIN_COMPARABLE_INSTANTS = 30

#: An operating condition must be observed this many times to be counted.
MIN_CONDITION_INSTANTS = 5

#: Load levels closer than this fraction of the observed peak are one
#: condition, not two.  Coarse on purpose: this counts *distinct regimes*, not
#: samples.
CONDITION_QUANTUM_FRAC = 0.05

#: A parent reading below this cannot place a ratio meaningfully.
RATIO_MIN_PARENT_W = 200.0

#: Relative spread of the per-condition child/parent ratio below which the two
#: series are one conductor measured twice (a circuit multiplier), not a nest.
SCALED_COPY_RATIO_SPREAD_MAX = 0.02

_PARENT_CHILD = RelationKind.ELECTRICAL_PARENT_CHILD
_OVERLAP = RelationKind.CT_MEASUREMENT_OVERLAP
_ASSOCIATION = RelationKind.DEVICE_CT_ASSOCIATION


@dataclass(frozen=True, slots=True)
class HierarchyConfig:
    """Thresholds of the evidence bar.  Each one is checkable, none is a prior."""

    min_independent_devices: int = 2
    """Distinct devices that must respond through both CTs."""

    min_response_consistency: float = 0.9
    """Fraction of operating conditions whose containment must hold."""

    max_power_violation_frac: float = 0.02
    """Fraction of comparable instants where the child may exceed the parent."""

    require_scope_evidence: bool = True
    """When true an ``UNKNOWN`` measurement basis blocks inference."""

    algorithm_version: str = "wp8-hierarchy-1.0.0"
    """Recorded on every emitted relation."""

    def __post_init__(self) -> None:
        if int(self.min_independent_devices) < 2:
            raise ValueError(
                "min_independent_devices must be at least 2: one device observed "
                "twice is an overlap, never parenthood"
            )
        if not 0.0 <= float(self.max_power_violation_frac) < 1.0:
            raise ValueError(
                "max_power_violation_frac must be in [0, 1): a child that exceeds "
                "the parent half the time is not contained by it"
            )
        if not 0.0 < float(self.min_response_consistency) <= 1.0:
            raise ValueError(
                "min_response_consistency must be in (0, 1]: zero conditions "
                "would accept an unmeasured topology"
            )
        if not str(self.algorithm_version).strip():
            raise ValueError("algorithm_version must not be empty")

    def as_metadata(self) -> dict[str, Any]:
        return {
            "min_independent_devices": int(self.min_independent_devices),
            "min_response_consistency": float(self.min_response_consistency),
            "max_power_violation_frac": float(self.max_power_violation_frac),
            "require_scope_evidence": bool(self.require_scope_evidence),
            "algorithm_version": str(self.algorithm_version),
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Valid and rejected edges, both retained.

    ``rejected`` is part of the answer, not a discarded error: a caller must be
    able to show *why* a proposed parent was refused.
    """

    valid: tuple[TopologyRelation, ...] = ()
    rejected: tuple[TopologyRelation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(frozen=True, slots=True)
class _ScopeGeometry:
    """What the aligned series say about one candidate direction."""

    parent_ct_id: str
    child_ct_id: str
    comparable: int
    violation_frac: float
    conditions: int
    consistency: float
    ratio_spread: float
    median_remainder_w: float
    median_parent_w: float
    median_child_w: float
    child_levels: int


# --------------------------------------------------------------------------- #
# Series helpers
# --------------------------------------------------------------------------- #
def _as_array(values: Sequence[float] | np.ndarray | None) -> np.ndarray | None:
    """Strict ``float64`` view; a value that is not a number becomes ``nan``."""
    if values is None:
        return None
    try:
        array = np.asarray(values)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    if array.ndim != 1:
        return None
    if array.dtype == np.float64:
        return array
    if array.dtype.kind in "iub":
        return array.astype(np.float64)
    out = np.full(array.shape[0], math.nan, dtype=np.float64)
    for index, value in enumerate(values):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[index] = float(value)
    return out


def _level_count(values: np.ndarray) -> int:
    """Number of distinct coarse load levels in ``values`` (regimes, not ticks)."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0
    scale = float(np.abs(finite).max()) or 1.0
    quantum = max(1.0, CONDITION_QUANTUM_FRAC * scale)
    return int(np.unique(np.floor(finite / quantum)).size)


def _scope_geometry(
    parent: np.ndarray,
    child: np.ndarray,
    parent_ct_id: str,
    child_ct_id: str,
    config: HierarchyConfig,
) -> _ScopeGeometry | None:
    """Measure containment of ``child`` inside ``parent`` for one direction."""
    if parent.shape != child.shape or parent.size == 0:
        return None
    finite = np.isfinite(parent) & np.isfinite(child)
    comparable = finite & (
        np.maximum(np.abs(parent), np.abs(child)) >= MIN_PAIR_ACTIVE_W
    )
    if int(np.count_nonzero(comparable)) < MIN_COMPARABLE_INSTANTS:
        return None
    parent_values = parent[comparable]
    child_values = child[comparable]
    violation = child_values > parent_values + VIOLATION_TOL_W
    violation_frac = float(np.count_nonzero(violation)) / int(parent_values.size)

    scale = float(np.abs(parent_values).max()) or 1.0
    quantum = max(1.0, CONDITION_QUANTUM_FRAC * scale)
    bins = np.floor(parent_values / quantum)

    condition_violations: list[float] = []
    condition_ratios: list[float] = []
    for level in np.unique(bins):
        mask = bins == level
        count = int(np.count_nonzero(mask))
        if count < MIN_CONDITION_INSTANTS:
            continue
        condition_violations.append(
            float(np.count_nonzero(violation[mask])) / count
        )
        usable = mask & (parent_values >= RATIO_MIN_PARENT_W)
        if int(np.count_nonzero(usable)) >= MIN_CONDITION_INSTANTS:
            condition_ratios.append(
                float(np.median(child_values[usable] / parent_values[usable]))
            )

    conditions = len(condition_violations)
    if conditions:
        consistent = sum(
            1
            for value in condition_violations
            if value <= config.max_power_violation_frac
        )
        consistency = consistent / conditions
    else:
        consistency = 0.0

    if len(condition_ratios) >= 2:
        ordered = sorted(condition_ratios)
        center = float(np.median(ordered))
        spread = (ordered[-1] - ordered[0]) / max(abs(center), 1e-9)
    else:
        spread = 1.0

    return _ScopeGeometry(
        parent_ct_id=parent_ct_id,
        child_ct_id=child_ct_id,
        comparable=int(parent_values.size),
        violation_frac=violation_frac,
        conditions=conditions,
        consistency=consistency,
        ratio_spread=spread,
        median_remainder_w=float(np.median(parent_values - child_values)),
        median_parent_w=float(np.median(parent_values)),
        median_child_w=float(np.median(child_values)),
        child_levels=_level_count(child_values),
    )


# --------------------------------------------------------------------------- #
# Channel / relation helpers
# --------------------------------------------------------------------------- #
def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(getattr(value, "value", value))


def _normalise_channels(
    channels: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, dict[str, Any]]:
    """Accept contract channels, raw dicts, a mapping or a sequence of either."""
    out: dict[str, dict[str, Any]] = {}
    if channels is None:
        return out
    items: Any = (
        channels.items() if isinstance(channels, Mapping) else ((None, item) for item in channels)
    )
    for ct_id, meta in items:
        if isinstance(meta, CTChannel):
            key = str(meta.ct_id)
            out[key] = {
                "ct_id": key,
                "ct_type": _coerce_text(meta.ct_type),
                "alias_of_ct_id": meta.alias_of_ct_id,
                "parent_ct_id": meta.parent_ct_id,
                "topology_status": _coerce_text(meta.topology_status),
                "name": meta.name,
            }
        elif isinstance(meta, Mapping):
            payload = dict(meta)
            key = str(ct_id if ct_id is not None else payload.get("ct_id") or "")
            if not key:
                continue
            payload.setdefault("ct_id", key)
            out[key] = payload
        else:  # pragma: no cover - defensive
            key = str(ct_id or "")
            if key:
                out[key] = {"ct_id": key}
    return out


def _resolve_alias(ct_id: str, channels: Mapping[str, Mapping[str, Any]]) -> str:
    """Follow ``alias_of_ct_id`` to the channel that owns the wire."""
    seen: set[str] = set()
    current = ct_id
    while current not in seen:
        seen.add(current)
        target = channels.get(current, {}).get("alias_of_ct_id")
        target_id = str(target) if target else ""
        if not target_id or target_id not in channels:
            return current
        current = target_id
    return current


def _relation_kind(value: Any) -> RelationKind:
    return RelationKind(_coerce_text(value))


def _relation_status(value: Any) -> TopologyStatus:
    return TopologyStatus(_coerce_text(value))


def _payload(item: Any) -> dict[str, Any]:
    if isinstance(item, TopologyRelation):
        return {
            "relation_id": item.relation_id,
            "kind": _coerce_text(item.kind),
            "subject_id": item.subject_id,
            "object_id": item.object_id,
            "status": _coerce_text(item.status),
            "declared_by": item.declared_by,
        }
    if isinstance(item, Mapping):
        return dict(item)
    raise TypeError(f"unsupported relation payload: {type(item)!r}")


def _coerce_relation(item: Any) -> TopologyRelation:
    """Validate an item into a relation (raising on a self edge, as the contract does)."""
    if isinstance(item, TopologyRelation):
        return item
    return TopologyRelation.model_validate(_payload(item))


def _construct_rejected(payload: dict[str, Any], evidence: dict[str, Any]) -> TopologyRelation:
    """Build a rejected edge even when the contract would refuse to construct it.

    A self edge is impossible to *express* through ``TopologyRelation`` and must
    still be reportable, so the pathological shape is constructed without
    re-validating the fields.  Every normal rejection is a ``model_copy``.
    """
    return TopologyRelation.model_construct(
        relation_id=str(payload.get("relation_id") or "rejected"),
        kind=_relation_kind(payload.get("kind", _PARENT_CHILD.value)),
        subject_id=str(payload.get("subject_id") or ""),
        object_id=str(payload.get("object_id") or ""),
        status=TopologyStatus.REJECTED,
        confidence=None,
        evidence=dict(evidence),
        requires_confirmation=True,
        declared_by="inference",
        created_at=datetime.now(UTC),
        valid_from=None,
        valid_to=None,
        algorithm_version=None,
    )


def _reject(relation: TopologyRelation, evidence: dict[str, Any]) -> TopologyRelation:
    payload = dict(relation.evidence)
    payload.update(evidence)
    return relation.model_copy(
        update={"status": TopologyStatus.REJECTED, "evidence": payload}
    )


def _parent_cycle(
    parent_of: Mapping[str, str], child: str, parent: str
) -> list[str] | None:
    """Return the closed path ``[child, parent, ..., child]`` when it would cycle."""
    path = [child]
    current: str | None = parent
    seen: set[str] = set()
    while current is not None:
        path.append(current)
        if current == child:
            return path
        if current in seen:  # pragma: no cover - a stored graph is already acyclic
            return None
        seen.add(current)
        current = parent_of.get(current)
    return None


def parent_cycle(
    edges: Iterable[tuple[str, str]], child: str, parent: str
) -> list[str] | None:
    """Public form of :func:`_parent_cycle`, for a persistence guard.

    ``edges`` are ``(child, parent)`` pairs already accepted.  The returned path
    is ``[child, parent, ..., child]`` when adding ``child -> parent`` would
    close a containment loop, otherwise ``None``.  One implementation is shared
    by ``merge``, ``validate`` and the storage guard so the three can never
    disagree about what a cycle is.
    """
    return _parent_cycle(dict(edges), str(child), str(parent))


def _declared_parents(channels: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    declared: dict[str, str] = {}
    for ct_id, meta in channels.items():
        parent = str(meta.get("parent_ct_id") or "")
        if parent:
            declared[ct_id] = parent
    return declared


def _declared_pairs(declared: Sequence[Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for item in declared:
        payload = _payload(item)
        if _relation_kind(payload.get("kind")) is not _PARENT_CHILD:
            continue
        pairs.add((str(payload["subject_id"]), str(payload["object_id"])))
    return pairs


def _association_devices(relations: Sequence[Any]) -> dict[str, list[str]]:
    """Device -> CTs that respond to it, from WP5 association relations only."""
    by_device: dict[str, list[str]] = {}
    for item in relations:
        payload = _payload(item)
        if _relation_kind(payload.get("kind")) is not _ASSOCIATION:
            continue
        device = str(payload["subject_id"])
        ct_id = str(payload["object_id"])
        by_device.setdefault(device, [])
        if ct_id not in by_device[device]:
            by_device[device].append(ct_id)
    return by_device


# --------------------------------------------------------------------------- #
# The finder
# --------------------------------------------------------------------------- #
class HierarchyFinder:
    """Conservative parent/child discovery, overlap reporting and validation."""

    def __init__(
        self,
        config: HierarchyConfig | None = None,
        *,
        declared: Sequence[Any] = (),
    ) -> None:
        self.config = config or HierarchyConfig()
        self._declared = tuple(declared)
        self._report: dict[str, Any] = self._empty_report()

    # -- reporting ------------------------------------------------------- #
    @staticmethod
    def _empty_report() -> dict[str, Any]:
        return {
            "refused": {},
            "undeterminable": [],
            "superseded": [],
            "declared_wins": [],
            "reason": None,
            "explanation": "",
            "algorithm_version": HierarchyConfig().algorithm_version,
        }

    @property
    def report(self) -> dict[str, Any]:
        """What was considered, refused or left unknown by the last ``infer``."""
        return self._report

    # -- overlap --------------------------------------------------------- #
    def overlaps(
        self,
        relations: Sequence[Any],
        *,
        registry: FactorRegistry | None = None,
    ) -> list[TopologyRelation]:
        """Emit ``CT_MEASUREMENT_OVERLAP`` for every pair sharing a device.

        ``registry`` is accepted so a caller can pass the same factor context it
        gave WP5; overlap itself needs no basis arithmetic and no series.
        """
        del registry  # overlap is a statement about responses, not scope
        by_device = _association_devices(relations)
        pair_devices: dict[tuple[str, str], list[str]] = {}
        for device_id in sorted(by_device):
            cts = sorted(set(by_device[device_id]))
            for index, first in enumerate(cts):
                for second in cts[index + 1 :]:
                    pair_devices.setdefault((first, second), []).append(device_id)

        out: list[TopologyRelation] = []
        for (first, second), devices in sorted(pair_devices.items()):
            out.append(
                TopologyRelation(
                    relation_id=f"overlap:{first}|{second}",
                    kind=_OVERLAP,
                    subject_id=first,
                    object_id=second,
                    status=TopologyStatus.CANDIDATE,
                    evidence={
                        "implies_parenthood": False,
                        "devices": sorted(devices),
                        "independent_devices": len(set(devices)),
                        "reason": (
                            "two CTs respond to the same device(s); a measurement "
                            "overlap is not parenthood and never authorises a sum"
                        ),
                        "algorithm_version": self.config.algorithm_version,
                    },
                    requires_confirmation=True,
                    declared_by="inference",
                    algorithm_version=self.config.algorithm_version,
                )
            )
        return out

    # -- inference ------------------------------------------------------- #
    def infer(
        self,
        relations: Sequence[Any],
        circuits: Sequence[Any],
        residual_history: Sequence[Any] = (),
        *,
        registry: FactorRegistry | None = None,
        declared: Sequence[Any] = (),
        now: datetime | None = None,
        series: Mapping[str, Sequence[float]] | None = None,
        channels: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> list[TopologyRelation]:
        """Infer parent/child edges, or return nothing with a stated reason.

        ``circuits`` and ``residual_history`` are accepted for interface
        stability; neither can establish containment.  ``series`` maps ct-id to
        an equally indexed watts sequence: measurement scope is the only
        evidence that can place a level, so without it nothing is inferred.
        """
        del circuits, residual_history
        report = self._empty_report()
        report["algorithm_version"] = self.config.algorithm_version
        self._report = report

        by_device = _association_devices(relations)
        if not by_device:
            report["reason"] = "no_device_evidence"
            report["explanation"] = (
                "no DEVICE_CT_ASSOCIATION relation was supplied, so no device is "
                "known to respond through any CT; topology stays unknown"
            )
            return []

        series_map: dict[str, np.ndarray] = {}
        for ct_id in series or {}:
            array = _as_array((series or {})[ct_id])
            if array is not None:
                series_map[str(ct_id)] = array
        if not series_map:
            report["reason"] = "no_measurement_scope_evidence"
            report["explanation"] = (
                "device responses say which CTs observe a device, not which "
                "conductor feeds which; no aligned measurement series was "
                "supplied, so parenthood stays unknown"
            )
            return []

        channel_map = _normalise_channels(channels)
        for ct_id in series_map:
            channel_map.setdefault(ct_id, {"ct_id": ct_id})

        declared_pairs = set(_declared_pairs(declared)) | set(
            _declared_pairs(self._declared)
        )

        pair_devices: dict[tuple[str, str], list[str]] = {}
        for device_id in sorted(by_device):
            cts = sorted(set(by_device[device_id]))
            for index, first in enumerate(cts):
                for second in cts[index + 1 :]:
                    bucket = pair_devices.setdefault((first, second), [])
                    if device_id not in bucket:
                        bucket.append(device_id)

        moment = now or datetime.now(UTC)
        accepted: dict[str, list[tuple[_ScopeGeometry, list[str]]]] = {}
        for (first, second), devices in sorted(pair_devices.items()):
            key = f"{first}|{second}"
            if first not in series_map or second not in series_map:
                report["undeterminable"].append(
                    {"ct_ids": [first, second], "reason": "no_series"}
                )
                continue

            canonical_first = _resolve_alias(first, channel_map)
            canonical_second = _resolve_alias(second, channel_map)
            if canonical_first == canonical_second:
                report["refused"][key] = "alias_of_same_conductor"
                continue

            if self.config.require_scope_evidence and self._basis_unknown(
                (canonical_first, canonical_second), registry, moment
            ):
                report["refused"][key] = "basis_unestablished"
                continue

            independent = sorted(set(devices))
            if len(independent) < self.config.min_independent_devices:
                report["refused"][key] = "insufficient_independent_devices"
                continue

            chosen = self._choose_direction(
                first,
                second,
                series_map,
                report,
                key,
            )
            if chosen is None:
                continue

            refusal = self._refusal_for_scope(chosen, report)
            if refusal is not None:
                report["refused"][key] = refusal
                continue

            accepted.setdefault(chosen.child_ct_id, []).append((chosen, independent))

        return self._emit(accepted, declared_pairs, channel_map, report)

    # -- inference internals --------------------------------------------- #
    def _basis_unknown(
        self,
        ct_ids: Sequence[str],
        registry: FactorRegistry | None,
        moment: datetime,
    ) -> bool:
        if registry is None:
            return False
        for ct_id in ct_ids:
            basis, _multiplier, _confidence = registry.basis_for(ct_id, moment)
            if MeasurementBasis(_coerce_text(basis)) is MeasurementBasis.UNKNOWN:
                return True
        return False

    def _choose_direction(
        self,
        first: str,
        second: str,
        series_map: Mapping[str, np.ndarray],
        report: dict[str, Any],
        key: str,
    ) -> _ScopeGeometry | None:
        """Pick which CT contains the other, or record why neither does.

        Both directions acceptable means the two series are the same conductor
        (or unresolved); neither means no containment evidence at all.  Either
        way the pair is undeterminable, never guessed.
        """
        forward = _scope_geometry(
            series_map[second], series_map[first], second, first, self.config
        )
        backward = _scope_geometry(
            series_map[first], series_map[second], first, second, self.config
        )
        limit = self.config.max_power_violation_frac
        forward_ok = forward is not None and forward.violation_frac <= limit
        backward_ok = backward is not None and backward.violation_frac <= limit

        if forward is None or backward is None:
            report["undeterminable"].append(
                {"ct_ids": [first, second], "reason": "insufficient_comparable_instants"}
            )
            return None
        if forward_ok and backward_ok:
            # Mutual containment: one conductor measured twice, or two series
            # that cannot separate.  Let the stricter checks name the refusal,
            # and never invent a direction.
            report["undeterminable"].append(
                {"ct_ids": [first, second], "reason": "mutual_containment"}
            )
            chosen = forward if forward.median_parent_w >= backward.median_parent_w else backward
            return chosen
        if forward_ok:
            return forward
        if backward_ok:
            return backward

        report["refused"][key] = "no_scope_containment"
        report["undeterminable"].append(
            {"ct_ids": [first, second], "reason": "no_scope_containment"}
        )
        return None

    def _refusal_for_scope(
        self, geometry: _ScopeGeometry, report: dict[str, Any]
    ) -> str | None:
        """Name the first check the candidate direction fails, if any."""
        if geometry.conditions < 2 or geometry.child_levels < 2:
            return "single_operating_condition"
        if geometry.consistency < self.config.min_response_consistency:
            return "inconsistent_response_across_conditions"
        if geometry.ratio_spread <= SCALED_COPY_RATIO_SPREAD_MAX:
            return "scaled_copy_of_one_conductor"
        if any(
            entry.get("reason") == "mutual_containment"
            and set(entry.get("ct_ids", []))
            == {geometry.parent_ct_id, geometry.child_ct_id}
            for entry in report["undeterminable"]
        ):
            return "ambiguous_scope_containment"
        return None

    def _emit(
        self,
        accepted: Mapping[str, list[tuple[_ScopeGeometry, list[str]]]],
        declared_pairs: set[tuple[str, str]],
        channel_map: Mapping[str, Mapping[str, Any]],
        report: dict[str, Any],
    ) -> list[TopologyRelation]:
        proposed: list[TopologyRelation] = []
        for child in sorted(accepted):
            candidates = accepted[child]
            # The immediate parent is the tightest container: the candidate
            # that leaves the smallest unexplained remainder.  A grandparent
            # still contains the child -- it is simply not the wiring edge.
            candidates = sorted(
                candidates,
                key=lambda row: (
                    row[0].median_remainder_w,
                    row[0].parent_ct_id,
                ),
            )
            geometry, devices = candidates[0]
            for _other, _other_devices in candidates[1:]:
                report["superseded"].append(
                    {
                        "child_ct_id": child,
                        "parent_ct_id": _other.parent_ct_id,
                        "reason": "not_the_immediate_parent",
                    }
                )

            pair = (child, geometry.parent_ct_id)
            if pair in declared_pairs:
                report["declared_wins"].append(
                    f"{pair[0]}->{pair[1]}"
                )
                continue
            if (pair[1], pair[0]) in declared_pairs:
                proposed.append(
                    _construct_rejected(
                        {
                            "relation_id": f"parent_child:{pair[0]}->{pair[1]}",
                            "kind": _PARENT_CHILD.value,
                            "subject_id": pair[0],
                            "object_id": pair[1],
                        },
                        {
                            "reason": "contradicts_declared",
                            "declared_parent_ct_id": pair[0],
                            "algorithm_version": self.config.algorithm_version,
                        },
                    )
                )
                report["declared_wins"].append(f"{pair[1]}->{pair[0]}")
                continue

            proposed.append(self._parent_child_relation(geometry, devices))

        result = self.validate(proposed, channel_map)
        found = [*result.valid, *result.rejected]
        found.sort(key=lambda relation: (relation.subject_id, relation.object_id))
        report["found"] = len(result.valid)
        if not report["explanation"]:
            report["explanation"] = self._explanation(found, report)
        if not found and report["reason"] is None and report["refused"]:
            report["reason"] = "insufficient_evidence"
            report["explanation"] = (
                "device responses exist, but no pair met the measurement-scope "
                "evidence bar; topology stays unknown"
            )
        return found

    def _parent_child_relation(
        self, geometry: _ScopeGeometry, devices: Sequence[str]
    ) -> TopologyRelation:
        violation_pct = round(100.0 * geometry.violation_frac, 3)
        consistency_pct = round(100.0 * geometry.consistency, 2)
        return TopologyRelation(
            relation_id=(
                f"parent_child:{geometry.child_ct_id}->{geometry.parent_ct_id}"
            ),
            kind=_PARENT_CHILD,
            subject_id=geometry.child_ct_id,
            object_id=geometry.parent_ct_id,
            status=TopologyStatus.CANDIDATE,
            evidence={
                "devices": list(devices),
                "independent_devices": len(devices),
                "comparable_instants": geometry.comparable,
                "conditions": geometry.conditions,
                "response_consistency": round(geometry.consistency, 4),
                "response_consistency_pct": consistency_pct,
                "power_violation_frac": round(geometry.violation_frac, 6),
                "power_violation_pct": violation_pct,
                "median_remainder_w": round(geometry.median_remainder_w, 1),
                "median_parent_w": round(geometry.median_parent_w, 1),
                "median_child_w": round(geometry.median_child_w, 1),
                "child_to_parent_ratio_spread": round(geometry.ratio_spread, 4),
                "accounting": {
                    "adds_totals": False,
                    "note": (
                        "a parent already contains this child; adding the parent "
                        "row and the child row together would double count the "
                        "child's power"
                    ),
                },
                "reason": (
                    "the child stays inside the parent across independent "
                    "devices and operating conditions; this is containment, not "
                    "a sum and not a claim that the readings may be added"
                ),
                "algorithm_version": self.config.algorithm_version,
                "config": self.config.as_metadata(),
            },
            requires_confirmation=True,
            declared_by="inference",
            algorithm_version=self.config.algorithm_version,
        )

    @staticmethod
    def _explanation(
        found: Sequence[TopologyRelation], report: Mapping[str, Any]
    ) -> str:
        if found:
            return (
                f"{len(found)} parent/child candidate(s) met the measurement-scope "
                "evidence bar; every one is a candidate that the user must confirm"
            )
        refused = report.get("refused") or {}
        if refused:
            return (
                "no parent/child edge met the evidence bar; refused: "
                + ", ".join(f"{key} ({reason})" for key, reason in sorted(refused.items()))
            )
        return "topology is undetermined; no edge is proposed"

    # -- validation ------------------------------------------------------ #
    def validate(
        self,
        relations: Sequence[Any],
        channels: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> ValidationResult:
        """Reject impossible edges **with a reason**; never drop them.

        Rules, in order: self edge, alias self edge (canonical ids first),
        unknown CT, contradiction of a declared ``parent_ct_id``, duplicate of
        an already accepted edge, and a transitive cycle.  Non-parent/child
        relations pass through untouched: this method never reclassifies a kind.
        """
        channel_map = _normalise_channels(channels)
        declared = _declared_parents(channel_map)

        valid: list[TopologyRelation] = []
        rejected: list[TopologyRelation] = []
        seen: set[tuple[str, str]] = set()
        parent_of: dict[str, str] = {}

        for item in relations:
            payload = _payload(item)
            kind = _relation_kind(payload.get("kind"))
            if kind is not _PARENT_CHILD:
                if isinstance(item, TopologyRelation):
                    valid.append(item)
                continue

            subject = str(payload["subject_id"])
            object_id = str(payload["object_id"])

            if subject == object_id:
                # The contract refuses to *construct* a self edge, so it is
                # reported from the raw description rather than lost.
                rejected.append(
                    _construct_rejected(payload, {"reason": "self_edge"})
                )
                continue

            relation = (
                item if isinstance(item, TopologyRelation) else _coerce_relation(item)
            )
            if channel_map:
                unknown = sorted(
                    ct_id for ct_id in (subject, object_id) if ct_id not in channel_map
                )
                if unknown:
                    rejected.append(
                        _reject(
                            relation,
                            {"reason": "unknown_ct", "unknown_ct_ids": unknown},
                        )
                    )
                    continue
                subject_key = _resolve_alias(subject, channel_map)
                object_key = _resolve_alias(object_id, channel_map)
                if subject_key == object_key:
                    rejected.append(
                        _reject(
                            relation,
                            {
                                "reason": "alias_self_edge",
                                "canonical_subject_id": subject_key,
                                "canonical_object_id": object_key,
                            },
                        )
                    )
                    continue
            else:
                subject_key, object_key = subject, object_id

            declared_parent = declared.get(subject_key) or declared.get(subject)
            reverse_declared = (
                declared.get(object_key) or declared.get(object_id)
            )
            if reverse_declared in (subject_key, subject):
                # The object is *declared* to sit under the subject, so the
                # subject cannot be its child.  A declared fact wins.
                rejected.append(
                    _reject(
                        relation,
                        {
                            "reason": "contradicts_declared",
                            "declared_parent_ct_id": object_key,
                        },
                    )
                )
                continue
            if declared_parent is not None and declared_parent != object_key:
                rejected.append(
                    _reject(
                        relation,
                        {
                            "reason": "contradicts_declared",
                            "declared_parent_ct_id": declared_parent,
                        },
                    )
                )
                continue

            if (subject_key, object_key) in seen:
                rejected.append(_reject(relation, {"reason": "duplicate_edge"}))
                continue

            cycle = _parent_cycle(parent_of, subject_key, object_key)
            if cycle is not None:
                rejected.append(
                    _reject(
                        relation,
                        {"reason": "cycle: " + " -> ".join(cycle), "cycle": cycle},
                    )
                )
                continue

            seen.add((subject_key, object_key))
            parent_of[subject_key] = object_key
            valid.append(relation)

        return ValidationResult(valid=tuple(valid), rejected=tuple(rejected))

    # -- merge ----------------------------------------------------------- #
    def merge(
        self,
        declared: Sequence[Any],
        inferred: Sequence[Any],
    ) -> list[TopologyRelation]:
        """Declared facts first, inferred edges after; declared always wins.

        The *union* is validated, not just the new edge: a cycle can be created
        by a pair of individually reasonable edges (``a -> b`` and ``b -> c``
        accepted earlier, then ``c -> a`` proposed).  Rejecting only a
        duplicated edge would persist a containment loop, and a loop makes
        "which CT contains which" unanswerable forever.

        A **declared** set that is itself cyclic is a user fact that contradicts
        itself.  It is not dropped silently: the edge that closes the loop is
        returned with ``status=REJECTED`` and ``evidence['reason']`` naming the
        cycle, so the caller can show it.
        """
        out: list[TopologyRelation] = []
        declared_pairs: set[tuple[str, str]] = set()
        seen: set[tuple[str, str]] = set()
        parent_of: dict[str, str] = {}

        for item in declared:
            relation = _coerce_relation(item)
            if _relation_kind(relation.kind) is not _PARENT_CHILD:
                out.append(relation)
                continue
            pair = (relation.subject_id, relation.object_id)
            cycle = _parent_cycle(parent_of, pair[0], pair[1])
            if cycle is not None:
                # A user declaration cannot be honoured: two declared facts
                # contradict each other.  Keep it visible rather than picking
                # one silently, and name the closed path.
                out.append(
                    _reject(
                        relation,
                        {
                            "reason": "declared_cycle: " + " -> ".join(cycle),
                            "cycle": cycle,
                        },
                    )
                )
                continue
            declared_pairs.add(pair)
            parent_of[pair[0]] = pair[1]
            out.append(relation)

        for item in inferred:
            relation = _coerce_relation(item)
            if _relation_kind(relation.kind) is not _PARENT_CHILD:
                out.append(relation)
                continue
            pair = (relation.subject_id, relation.object_id)
            if pair in declared_pairs:
                continue
            if (pair[1], pair[0]) in declared_pairs:
                out.append(
                    _reject(
                        relation,
                        {
                            "reason": "contradicts_declared",
                            "declared_parent_ct_id": pair[0],
                        },
                    )
                )
                continue
            if pair in seen:
                continue
            cycle = _parent_cycle(parent_of, pair[0], pair[1])
            if cycle is not None:
                out.append(
                    _reject(
                        relation,
                        {
                            "reason": "cycle: " + " -> ".join(cycle),
                            "cycle": cycle,
                        },
                    )
                )
                continue
            seen.add(pair)
            parent_of[pair[0]] = pair[1]
            out.append(relation)
        return out

    # -- explanation ----------------------------------------------------- #
    def explain(self, relation: TopologyRelation) -> str:
        return explain(relation)


# --------------------------------------------------------------------------- #
# Module-level surface
# --------------------------------------------------------------------------- #
def explain(relation: TopologyRelation) -> str:
    """Human-readable reason for one relation, citing only its own evidence."""
    kind = _relation_kind(relation.kind)
    evidence = relation.evidence or {}
    if kind is _OVERLAP:
        devices = evidence.get("devices") or []
        return (
            f"{relation.subject_id} and {relation.object_id} both respond to "
            f"{', '.join(str(device) for device in devices) or 'the same device'}. "
            "This is a measurement overlap; it does not prove parenthood and no "
            "totals may be added because of it."
        )
    if kind is _PARENT_CHILD:
        devices = evidence.get("devices") or []
        conditions = evidence.get("conditions", 0)
        consistency = evidence.get("response_consistency_pct")
        violation = evidence.get("power_violation_pct")
        remainder = evidence.get("median_remainder_w")
        parts = [
            f"{relation.subject_id} is contained by {relation.object_id}",
            "on measurement-scope evidence: both CTs respond to "
            + (", ".join(str(device) for device in devices) or "no named device"),
            f"across {conditions} operating condition(s)",
        ]
        if consistency is not None:
            parts.append(f"with {consistency}% condition consistency")
        if violation is not None:
            parts.append(f"and a {violation}% power-violation fraction")
        if remainder is not None:
            parts.append(f"(median remainder {remainder} W)")
        parts.append(
            "the parent contains the child, so the readings must never be summed"
        )
        return ", ".join(parts) + "."
    if kind is _ASSOCIATION:
        return (
            f"{relation.subject_id} is observed through {relation.object_id}; "
            "an association says nothing about which conductor feeds which."
        )
    return (
        f"{relation.subject_id} relates to {relation.object_id} as "
        f"{_coerce_text(relation.kind)}; no parenthood is implied."
    )


def persist(relations: Sequence[TopologyRelation], db: Database) -> int:
    """Upsert relations by ``relation_id``; returns how many were written.

    The relation id is the identity, so calling this twice writes the same rows
    with the same content instead of accumulating duplicates.

    A parent/child row that would close a cycle against what is **already
    stored** is refused here as a last line of defence: ``merge`` validates the
    union, but a caller can still hand a hand-built edge straight to the store.
    The refusal is raised, not silently skipped -- a persisted containment loop
    would make every later "who contains whom" answer depend on traversal order.
    The whole batch is validated before the first row is written, so a refused
    write leaves the store exactly as it was.
    """
    written = 0
    stored: dict[str, str] = {}
    for row in db.relations_of_kind(RelationKind.ELECTRICAL_PARENT_CHILD):
        if str(row.get("status")) == TopologyStatus.REJECTED.value:
            continue
        subject = str(row.get("subject_id") or "")
        parent = str(row.get("object_id") or "")
        if subject and parent:
            stored[subject] = parent
    batch = list(relations)
    # Validate the whole batch before writing anything: a guard that commits the
    # first half of a cyclic batch leaves the caller with a silently partial
    # write, and "which of the two landed?" would then depend on iteration order.
    pending = dict(stored)
    for relation in batch:
        if _relation_kind(relation.kind) is not _PARENT_CHILD:
            continue
        if TopologyStatus(_coerce_text(relation.status)) is TopologyStatus.REJECTED:
            continue
        subject = str(relation.subject_id)
        parent = str(relation.object_id)
        # The stored graph plus the batch so far, i.e. the union this write
        # would create -- not just the new edge.
        cycle = _parent_cycle(pending, subject, parent)
        if cycle is not None:
            raise ValueError(
                "refusing to persist a parent/child cycle: " + " -> ".join(cycle)
            )
        pending[subject] = parent
    for relation in batch:
        db.insert_topology_relation(relation)
        written += 1
    return written


__all__ = [
    "CONDITION_QUANTUM_FRAC",
    "HierarchyConfig",
    "HierarchyFinder",
    "MIN_COMPARABLE_INSTANTS",
    "MIN_CONDITION_INSTANTS",
    "MIN_PAIR_ACTIVE_W",
    "RATIO_MIN_PARENT_W",
    "SCALED_COPY_RATIO_SPREAD_MAX",
    "ValidationResult",
    "VIOLATION_TOL_W",
    "explain",
    "parent_cycle",
    "persist",
]
