"""Circuit classification: dedicated / shared / partially understood / unknown.

The gap this closes
-------------------
``MonitoringLevel`` answers *"how closely does the tree observe a device?"* and
``CTType`` answers *"what kind of sensor is clamped here?"*.  Neither answers
*"what is the electrical scope of this circuit?"*, and neither can distinguish
the two materially different facts:

* a breaker carrying two known devices (``SHARED``), and
* a breaker carrying two known devices **plus** load nobody can name
  (``PARTIALLY_UNDERSTOOD``).

This module derives that answer once per *canonical CT scope*, from evidence the
association layer already produced, and it carries three separate things:

``classification``
    the scope class, derived from measurement evidence only;
``classification_status``
    ``CANDIDATE`` until every evidence bar is met, then ``CONFIRMED``.  A
    classification is never born confirmed, and an incoming ``CONFIRMED`` is
    re-derived rather than trusted;
``classification_confidence``
    a **circuit-classification** confidence in its own right, also exposed as a
    :class:`~wattmosaic_engine.contracts.ConfidenceVector` with exactly that
    dimension populated.  It is never blended with power, topology or residual
    confidence -- a reader that wants one number must say which dimension.

The evidence bars
-----------------
``DEDICATED`` requires **all** of:

1. ``min_isolated_events`` isolated events on the scope -- one coincidence is
   not a circuit (a single event leaves the class ``UNKNOWN``);
2. ``min_stability`` -- the CT response to repeated events is consistent;
3. the device's own power explains the CT behaviour (its attributed share of
   the scope is complete, whether that is one conductor or two legs that are
   explicitly summable);
4. **no independent contributions from other identified devices** -- a second
   device with its own isolated evidence makes the scope ``SHARED``;
5. **no other identified device at all** -- a co-toggling load whose own share
   was never isolated is still a device on the wire, so it blocks ``DEDICATED``
   even though it contributes no independent evidence;
6. ``dedicated_min_observation_s`` of observation span -- a short burst cannot
   show that a circuit is quiet the rest of the time;
7. **no residual contradiction** -- a persistent unexplained residual larger
   than ``max_residual_contradiction_w`` means something else is on the
   conductor, so the circuit is not dedicated.

Bar 7 is deliberately split in two: *checked and clean* satisfies it, *never
checked* also satisfies it (absence of a contradiction is not a contradiction),
but only the checked case may reach ``CONFIRMED``.  That is what makes
"dedicated, candidate" an honest state rather than a contradiction in terms.

``SHARED`` requires ``>= 2`` **independently evidenced** devices: each must have
its own isolated (solo) event.  Two devices that only ever switch together have
no independent evidence -- the association verdict for them is ``ambiguous`` --
so the answer is ``UNKNOWN``, never ``SHARED`` with invented certainty.

``PARTIALLY_UNDERSTOOD`` requires ``>= 2`` known devices **and** unattributable
load above the contradiction threshold.  One known device plus unexplained load
is *not* partially understood (there is nothing shared to understand): it is
``UNKNOWN``, because claiming either ``DEDICATED`` or ``SHARED`` would overstate
what is known.

``UNKNOWN`` is therefore reachable in four distinct ways: no relations at all,
too few isolated events, one device whose power does not account for the scope,
and two devices that only ever moved together.

Standby is not a second load
----------------------------
A device that draws a standing wattage while "off" still produces a clean step
from ``off`` to ``on``; the CT's non-zero *level* is the device's own standby.
Nothing in the rules treats a standing level as another device, so a standby
load stays ``DEDICATED``.  ``stats["standby_explained"]`` records that reading.

Never name-derived
------------------
No rule reads ``name``, ``panel_id`` or any label.  ``circuit_id`` is a hash of
the canonical CT ids and the algorithm version, so relabelling a CT or a device
cannot change the classification or the identity.

Nothing here re-implements association, residual arithmetic, factor resolution
or channel canonicalisation: it consumes
:mod:`wattmosaic_engine.disaggregation.association`,
:mod:`wattmosaic_engine.models.residual_power`,
:mod:`wattmosaic_engine.electrical.factors` and
:mod:`wattmosaic_engine.electrical.topology`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from wattmosaic_engine.contracts import (
    Circuit,
    CircuitClass,
    ConfidenceVector,
    MeasurementBasis,
    RelationKind,
    ResidualPower,
    TopologyRelation,
    TopologyStatus,
    utcnow,
)
from wattmosaic_engine.disaggregation.association import (
    VERDICT_AMBIGUOUS,
    VERDICT_SINGLE_EVENT,
    VERDICT_SPLIT,
    VERDICT_SUPPORTED,
)
from wattmosaic_engine.electrical.factors import FactorRegistry

__all__ = [
    "CircuitClassifier",
    "CircuitConfig",
    "explain",
    "persist",
]

#: A classification is never presented as certain: confirmation is a user act,
#: so even overwhelming evidence stops here.
MAX_CONFIDENCE = 0.99

#: An ``UNKNOWN`` scope may never look better than a coin flip.  If the evidence
#: were stronger than this the classifier would have named a class.
UNKNOWN_CAP = 0.5

#: Verdicts that carry at least one *solo* event, i.e. evidence that can be
#: attributed to one device.  ``ambiguous`` is deliberately absent: it means the
#: device was only ever seen moving together with another one.
INDEPENDENT_VERDICTS = frozenset({VERDICT_SUPPORTED, VERDICT_SPLIT, VERDICT_SINGLE_EVENT})

#: Basis preference for a scope.  A factor-adjusted (or monitor-reported) leg
#: already claims the whole appliance, so it outranks a raw conductor reading.
_BASIS_RANK = {
    MeasurementBasis.FACTOR_ADJUSTED: 4,
    MeasurementBasis.MONITOR_REPORTED: 3,
    MeasurementBasis.CT_MEASURED: 2,
    MeasurementBasis.ESTIMATED_APPLIANCE_TOTAL: 1,
    MeasurementBasis.UNKNOWN: 0,
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CircuitConfig:
    """Every threshold the classification depends on, in one place.

    ``min_isolated_events``
        Solo (isolated) events required on a scope before a class may be named.
        Three is the smallest count that distinguishes "repeats" from "twice",
        and one is never enough: a single switching cannot separate a dedicated
        circuit from a coincidence.
    ``min_stability``
        Minimum ``1 - MAD(share) / median(share)`` the CT response must show.
        Below it the conductor does not respond consistently to the device, so
        "the device explains the CT" is not established.
    ``max_residual_contradiction_w``
        Largest unexplained residual (W, absolute) tolerated on the scope.  A
        persistent residual above it means unidentified load is present, which
        contradicts ``DEDICATED`` and turns two known devices into
        ``PARTIALLY_UNDERSTOOD``.
    ``dedicated_min_observation_s``
        Observation span (s) the isolated events must cover before a dedicated
        claim is credible.  900 s (15 min) is the engine's ordinary CT cadence
        times a handful of samples: enough to show the scope is quiet between
        events rather than merely unobserved.
    ``power_explains_tolerance``
        Fractional slack allowed on "the device's power explains the scope".
        The CT may carry the whole device step plus measurement error.
    ``algorithm_version``
        Travels onto every circuit and every confidence vector so a stored
        classification can be explained after an upgrade.
    """

    min_isolated_events: int = 3
    min_stability: float = 0.70
    max_residual_contradiction_w: float = 150.0
    dedicated_min_observation_s: float = 900.0
    power_explains_tolerance: float = 0.20
    algorithm_version: str = "circuits@1.0"

    def __post_init__(self) -> None:
        if isinstance(self.min_isolated_events, bool) or int(self.min_isolated_events) < 1:
            raise ValueError("min_isolated_events must be an integer >= 1")
        stability = float(self.min_stability)
        if not math.isfinite(stability) or not 0.0 < stability <= 1.0:
            raise ValueError("min_stability must be finite and in (0, 1]")
        threshold = float(self.max_residual_contradiction_w)
        if not math.isfinite(threshold) or threshold < 0.0:
            raise ValueError("max_residual_contradiction_w must be finite and non-negative")
        span = float(self.dedicated_min_observation_s)
        if not math.isfinite(span) or span <= 0.0:
            raise ValueError("dedicated_min_observation_s must be finite and positive")
        tolerance = float(self.power_explains_tolerance)
        if not math.isfinite(tolerance) or not 0.0 <= tolerance < 1.0:
            raise ValueError("power_explains_tolerance must be finite and in [0, 1)")
        object.__setattr__(self, "min_isolated_events", int(self.min_isolated_events))
        object.__setattr__(self, "min_stability", stability)
        object.__setattr__(self, "max_residual_contradiction_w", threshold)
        object.__setattr__(self, "dedicated_min_observation_s", span)
        object.__setattr__(self, "power_explains_tolerance", tolerance)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "min_isolated_events": self.min_isolated_events,
            "min_stability": self.min_stability,
            "max_residual_contradiction_w": self.max_residual_contradiction_w,
            "dedicated_min_observation_s": self.dedicated_min_observation_s,
            "power_explains_tolerance": self.power_explains_tolerance,
            "algorithm_version": self.algorithm_version,
        }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _circuit_id(canonical_ct_ids: Sequence[str], algorithm_version: str) -> str:
    """Deterministic, name-free identity for one CT scope.

    Only the canonical conductors and the algorithm version participate, so
    ``persist`` is idempotent and a display label cannot create a second circuit
    for the same wire.
    """
    digest = hashlib.sha256(
        ("circuit|" + "|".join(sorted(canonical_ct_ids)) + f"|{algorithm_version}").encode()
    ).hexdigest()
    return f"cir_{digest[:24]}"


def _scope_basis(bases: Iterable[MeasurementBasis]) -> MeasurementBasis:
    """The strongest basis present, because it claims the most about the scope."""
    resolved = [MeasurementBasis(_value(item)) for item in bases]
    if not resolved:
        return MeasurementBasis.UNKNOWN
    return max(resolved, key=lambda item: _BASIS_RANK.get(item, 0))


# --------------------------------------------------------------------------- #
# One device's evidence on one scope
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _DeviceEvidence:
    device_id: str
    relations: tuple[TopologyRelation, ...]
    supporting: tuple[dict[str, Any], ...]
    conflicting: tuple[dict[str, Any], ...]
    verdict: str
    stability: float
    isolated_events: int
    explained_fraction: float
    first_at: datetime | None
    last_at: datetime | None

    @property
    def independent(self) -> bool:
        """Whether this device has at least one event that is *its own*."""
        return self.verdict in INDEPENDENT_VERDICTS and self.isolated_events >= 1

    @property
    def span_s(self) -> float:
        if self.first_at is None or self.last_at is None:
            return 0.0
        return max(0.0, (self.last_at - self.first_at).total_seconds())


def _explained_fraction(
    relation: TopologyRelation, scope: frozenset[str]
) -> float:
    """How much of the scope's measured step this device accounts for.

    ``overlap.contributions`` is keyed by CT and already factor-scaled, so the
    answer is arithmetic on association evidence, not a new estimate.  Two legs
    that are explicitly summable add up; a factor-adjusted total that already
    claims the appliance takes the maximum instead (summing would double count).
    """
    overlap = relation.evidence.get("overlap") or {}
    contributions = {
        str(ct_id): float(value)
        for ct_id, value in (overlap.get("contributions") or {}).items()
        if str(ct_id) in scope
    }
    if not contributions:
        return 0.0
    if bool(overlap.get("summing_allowed")):
        return sum(max(0.0, value) for value in contributions.values())
    return max(max(0.0, value) for value in contributions.values())


def _device_evidence(
    device_id: str, relations: Sequence[TopologyRelation], scope: frozenset[str]
) -> _DeviceEvidence:
    supporting: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []
    stamps: list[datetime] = []
    event_ids: set[str] = set()
    anonymous = 0
    for relation in relations:
        evidence = relation.evidence
        supporting.extend(dict(row) for row in evidence.get("supporting") or ())
        conflicting.extend(dict(row) for row in evidence.get("conflicting") or ())
        for row in evidence.get("supporting") or ():
            stamp = _stamp(row.get("occurred_at"))
            if stamp is not None:
                stamps.append(stamp)
            # One event can appear on several conductors (two legs, or a
            # factor-adjusted total).  It is still *one* isolated event.
            event_id = str(row.get("event_id") or "")
            if event_id:
                event_ids.add(event_id)
            else:
                anonymous += 1
    verdict = str(relations[0].evidence.get("verdict") or "")
    # A device seen on several CTs of one scope keeps its worst verdict: if any
    # relation is ambiguous, the *device* is not independently evidenced.
    for relation in relations:
        candidate = str(relation.evidence.get("verdict") or "")
        if candidate == VERDICT_AMBIGUOUS:
            verdict = VERDICT_AMBIGUOUS
    stability = min(
        (
            float((relation.evidence.get("stats") or {}).get("stability") or 0.0)
            for relation in relations
        ),
        default=0.0,
    )
    fraction = max((_explained_fraction(relation, scope) for relation in relations), default=0.0)
    return _DeviceEvidence(
        device_id=device_id,
        relations=tuple(relations),
        supporting=tuple(supporting),
        conflicting=tuple(conflicting),
        verdict=verdict,
        stability=stability,
        isolated_events=len(event_ids) + anonymous,
        explained_fraction=fraction,
        first_at=min(stamps) if stamps else None,
        last_at=max(stamps) if stamps else None,
    )


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #
class CircuitClassifier:
    """Derive ``Circuit`` candidates and classify their electrical scope."""

    def __init__(self, config: CircuitConfig | None = None) -> None:
        self.config = config or CircuitConfig()

    # ------------------------------------------------------------------ #
    # 1. candidates
    # ------------------------------------------------------------------ #
    def candidates(
        self,
        relations: Iterable[TopologyRelation],
        *,
        registry: FactorRegistry | None = None,
        now: datetime | None = None,
        topology: Any | None = None,
    ) -> list[Circuit]:
        """One candidate circuit per canonical CT scope.

        ``topology`` is an optional
        :class:`~wattmosaic_engine.electrical.topology.TopologyGraph`.  When it
        is supplied, alias chains are collapsed with its ``canonical_channel_id``
        so two clamps on one wire produce one circuit, not two.  Without it the
        ids are taken as declared, and the alias map is empty.

        Candidates are *unclassified*: ``UNKNOWN`` / ``CANDIDATE``.  Naming a
        class is :meth:`classify`'s job, and it needs evidence this method does
        not interpret.
        """
        factor_registry = registry if registry is not None else FactorRegistry()
        rows = [
            relation
            for relation in relations
            if RelationKind(_value(relation.kind)) is RelationKind.DEVICE_CT_ASSOCIATION
        ]

        # One scope is one *connected set* of conductors.  A device observed by
        # two legs (or two clamps on one wire) reports them together in
        # ``overlap.ct_ids``, so those conductors are the same scope.  Grouping
        # by an exact tuple instead would split the hot tub into two circuits
        # whenever a second device sits on just one of its legs -- the D2 defect.
        parent: dict[str, str] = {}

        def find(ct_id: str) -> str:
            parent.setdefault(ct_id, ct_id)
            while parent[ct_id] != ct_id:
                parent[ct_id] = parent[parent[ct_id]]
                ct_id = parent[ct_id]
            return ct_id

        def union(first: str, second: str) -> None:
            left, right = find(first), find(second)
            if left != right:
                parent[max(left, right)] = min(left, right)

        members: dict[str, list[TopologyRelation]] = {}
        aliases: dict[str, str] = {}
        for relation in rows:
            declared = [
                str(ct_id)
                for ct_id in ((relation.evidence.get("overlap") or {}).get("ct_ids") or ())
            ] or [str(relation.object_id)]
            canonical_ids = [self._canonical(ct_id, topology) for ct_id in declared]
            for ct_id, canonical in zip(declared, canonical_ids, strict=True):
                if canonical != ct_id:
                    aliases[ct_id] = canonical
            for canonical in canonical_ids[1:]:
                union(canonical_ids[0], canonical)
            for canonical in canonical_ids:
                members.setdefault(canonical, []).append(relation)

        groups: dict[str, dict[str, Any]] = {}
        for canonical, relations_here in members.items():
            group = groups.setdefault(
                find(canonical),
                {"cts": set(), "devices": set(), "moments": []},
            )
            group["cts"].add(canonical)
            for relation in relations_here:
                group["devices"].add(str(relation.subject_id))
                for row in relation.evidence.get("supporting") or ():
                    stamp = _stamp(row.get("occurred_at"))
                    if stamp is not None:
                        group["moments"].append(stamp)

        circuits: list[Circuit] = []
        for group in groups.values():
            ct_ids = sorted(group["cts"])
            moments: list[datetime] = list(group["moments"])
            moment = now or (max(moments) if moments else utcnow())
            bases = [
                factor_registry.basis_for(ct_id, moment)[0] for ct_id in ct_ids
            ]
            circuits.append(
                Circuit(
                    circuit_id=_circuit_id(ct_ids, self.config.algorithm_version),
                    name=None,
                    panel_id=None,
                    classification=CircuitClass.UNKNOWN,
                    classification_status=TopologyStatus.CANDIDATE,
                    parent_circuit_id=None,
                    ct_ids=ct_ids,
                    device_ids=sorted(group["devices"]),
                    measurement_basis=_scope_basis(bases),
                    metadata={
                        "ct_alias_map": {
                            alias: canonical
                            for alias, canonical in sorted(aliases.items())
                            if canonical in group["cts"]
                        },
                        "algorithm_version": self.config.algorithm_version,
                        "config": self.config.as_metadata(),
                    },
                )
            )
        circuits.sort(key=lambda circuit: circuit.circuit_id)
        return circuits

    def _canonical(self, ct_id: str, topology: Any | None) -> str:
        if topology is None:
            return ct_id
        canonical = topology.canonical_channel_id(ct_id)
        return str(canonical or ct_id)

    # ------------------------------------------------------------------ #
    # 2. classify
    # ------------------------------------------------------------------ #
    def classify(
        self,
        circuit: Circuit,
        relations: Iterable[TopologyRelation],
        *,
        residual: ResidualPower | None = None,
        observations: Iterable[Any] | None = None,
        now: datetime | None = None,
    ) -> Circuit:
        """Decide the scope class from evidence, never from the incoming class.

        The incoming ``classification``/``classification_status`` are treated as
        a previous *proposal*: they are re-derived, so a stale ``CONFIRMED`` can
        never survive evidence that no longer supports it.
        """
        alias_map = {
            str(alias): str(canonical)
            for alias, canonical in (circuit.metadata.get("ct_alias_map") or {}).items()
        }
        scope = frozenset(str(ct_id) for ct_id in circuit.ct_ids)

        def canonical(ct_id: str) -> str:
            return alias_map.get(str(ct_id), str(ct_id))

        scoped = [
            relation
            for relation in relations
            if RelationKind(_value(relation.kind)) is RelationKind.DEVICE_CT_ASSOCIATION
            and canonical(relation.object_id) in scope
        ]

        by_device: dict[str, list[TopologyRelation]] = {}
        for relation in scoped:
            by_device.setdefault(str(relation.subject_id), []).append(relation)
        evidence = {
            device_id: _device_evidence(device_id, rows, scope)
            for device_id, rows in sorted(by_device.items())
        }

        independent = [item for item in evidence.values() if item.independent]
        isolated_events = sum(item.isolated_events for item in evidence.values())
        stability = min((item.stability for item in evidence.values()), default=0.0)
        span_s = max((item.span_s for item in evidence.values()), default=0.0)
        primary_fraction = max(
            (item.explained_fraction for item in independent), default=0.0
        )
        # The residual is **signed**.  A positive number is unaccounted *load*
        # (something else is on the conductor).  A negative number is the
        # opposite fact: the known devices already claim more power than the
        # scope measures, i.e. over-attribution or a measurement conflict.  The
        # two must never be published as the same thing -- ``abs()`` here used
        # to turn a -400 W surplus into "400 W of unattributed load".
        signed_residual_w = (
            None if residual is None else float(residual.residual_w)
        )
        contradiction_w = 0.0 if signed_residual_w is None else abs(signed_residual_w)
        residual_measured = residual is not None
        residual_contradicts = (
            signed_residual_w is not None
            and contradiction_w > self.config.max_residual_contradiction_w
        )
        unattributed_load_w: float | None = None
        residual_conflict_w: float | None = None
        if signed_residual_w is not None and residual_contradicts:
            # Only the sign decides which fact this is.
            if signed_residual_w > 0.0:
                unattributed_load_w = signed_residual_w
            elif signed_residual_w < 0.0:
                residual_conflict_w = signed_residual_w

        explains = primary_fraction >= 1.0 - self.config.power_explains_tolerance
        bars = {
            "min_isolated_events": isolated_events >= self.config.min_isolated_events,
            "min_stability": stability >= self.config.min_stability,
            "device_power_explains": explains,
            "no_independent_contributions": len(independent) == 1,
            # "Dedicated" means the conductor serves one device.  A second
            # identified device -- even one whose share was never isolated, so it
            # has no *independent contribution* -- makes that claim false, and
            # naming only the first device would hide it.
            "no_other_identified_devices": len(evidence) == 1,
            "dedicated_min_observation_s": (
                span_s >= self.config.dedicated_min_observation_s
            ),
            "residual_checked": residual_measured,
            "no_residual_contradiction": not residual_contradicts,
            "min_independent_devices": len(independent) >= 2,
        }

        dedicated_bars = (
            "min_isolated_events",
            "min_stability",
            "device_power_explains",
            "no_independent_contributions",
            "no_other_identified_devices",
            "dedicated_min_observation_s",
            "no_residual_contradiction",
        )
        is_dedicated = all(bars[name] for name in dedicated_bars)
        # Shared-ness is decided by *device* evidence alone.  A residual does not
        # make two independently evidenced devices less shared; it decides
        # whether what they sit on is fully understood.  "Independently
        # evidenced" is already the bar: ``independent`` means the device has at
        # least one solo event, so a second bar would only restate it.
        shared_bars = ("min_independent_devices",)
        is_shared = all(bars[name] for name in shared_bars)

        if is_dedicated:
            classification = CircuitClass.DEDICATED
            confirmed = residual_measured and not residual_contradicts
        elif is_shared and unattributed_load_w is not None:
            # Only a *positive* residual is unattributed load.  A negative one
            # is a conflict, not a class: it says the accounting is wrong, not
            # that there is an extra device to discover.
            classification = CircuitClass.PARTIALLY_UNDERSTOOD
            confirmed = True
        elif is_shared:
            classification = CircuitClass.SHARED
            confirmed = residual_measured and not residual_contradicts
        else:
            classification = CircuitClass.UNKNOWN
            confirmed = False

        confidence = self._confidence(
            classification=classification,
            evidence=list(evidence.values()),
            independent=independent,
            isolated_events=isolated_events,
            stability=stability,
            span_s=span_s,
            fraction=primary_fraction,
            residual_measured=residual_measured,
            residual_contradicts=residual_contradicts,
        )

        if classification is CircuitClass.DEDICATED:
            device_ids = [independent[0].device_id]
        elif classification in (CircuitClass.SHARED, CircuitClass.PARTIALLY_UNDERSTOOD):
            device_ids = sorted(item.device_id for item in independent)
        else:
            device_ids = sorted(evidence)

        metadata: dict[str, Any] = {
            **circuit.metadata,
            "algorithm_version": self.config.algorithm_version,
            "config": self.config.as_metadata(),
            "classification_confidence": confidence,
            "confidence": ConfidenceVector(
                circuit_classification=confidence,
                method=self.config.algorithm_version,
            ),
            "evidence": {
                "supporting": [
                    {**row, "device_id": item.device_id}
                    for item in evidence.values()
                    for row in item.supporting
                ],
                "conflicting": [
                    {**row, "device_id": item.device_id}
                    for item in evidence.values()
                    for row in item.conflicting
                ],
                "stats": {
                    "isolated_events": isolated_events,
                    "independent_devices": len(independent),
                    "device_count": len(evidence),
                    "devices": sorted(evidence),
                    "stability": round(stability, 6),
                    "span_s": round(span_s, 3),
                    "explained_fraction": round(primary_fraction, 6),
                    "residual_w": signed_residual_w,
                    "residual_contradiction_w": contradiction_w,
                    "unattributed_load_w": unattributed_load_w,
                    "residual_conflict_w": residual_conflict_w,
                    "standby_explained": bool(explains and not residual_contradicts),
                    "n_conflicting": sum(len(item.conflicting) for item in evidence.values()),
                    "observed_devices": self._observed_devices(observations),
                },
                "bars": bars,
                "residual": None if residual is None else self._residual_record(residual),
                "algorithm_version": self.config.algorithm_version,
            },
        }
        if classification is CircuitClass.PARTIALLY_UNDERSTOOD and unattributed_load_w:
            metadata["unattributed_load_w"] = unattributed_load_w
            metadata["unattributed_note"] = (
                f"{unattributed_load_w:.1f} W on this scope is not attributable to any "
                "identified device, so 'shared' would overstate what is known"
            )
        if residual_conflict_w is not None:
            # A surplus is a contradiction of the *accounting*, never invented
            # load.  It is published with its sign so a reader can see which
            # direction the books are wrong in, and it never promotes a class.
            metadata["residual_conflict_w"] = residual_conflict_w
            metadata["residual_conflict_note"] = (
                f"the scope measures {abs(residual_conflict_w):.1f} W less than the "
                "identified devices claim: accounted power exceeds the measured "
                "total, so the attribution is wrong (a surplus is not unattributed "
                "load)"
            )

        return Circuit(
            circuit_id=circuit.circuit_id,
            name=circuit.name,
            panel_id=circuit.panel_id,
            classification=classification,
            classification_status=(
                TopologyStatus.CONFIRMED if confirmed else TopologyStatus.CANDIDATE
            ),
            parent_circuit_id=circuit.parent_circuit_id,
            ct_ids=list(circuit.ct_ids),
            device_ids=device_ids,
            measurement_basis=circuit.measurement_basis,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # 3. explain
    # ------------------------------------------------------------------ #
    def explain(self, circuit: Circuit) -> str:
        """Human-readable reason citing the evidence the class was derived from."""
        return explain(circuit)

    # -- internals ------------------------------------------------------ #
    @staticmethod
    def _observed_devices(observations: Iterable[Any] | None) -> list[str]:
        if observations is None:
            return []
        seen: set[str] = set()
        for row in observations:
            device_id = getattr(row, "device_id", None)
            if device_id:
                seen.add(str(device_id))
        return sorted(seen)

    @staticmethod
    def _residual_record(residual: ResidualPower) -> dict[str, Any]:
        return {
            "residual_w": float(residual.residual_w),
            "kind": _value(residual.kind),
            "resolved": bool(residual.resolved),
            "basis": _value(residual.basis),
            "baseline_w": residual.baseline_w,
            "dynamic_w": residual.dynamic_w,
            "explained_by_device_w": dict(residual.explained_by_device_w),
        }

    def _confidence(
        self,
        *,
        classification: CircuitClass,
        evidence: Sequence[_DeviceEvidence],
        independent: Sequence[_DeviceEvidence],
        isolated_events: int,
        stability: float,
        span_s: float,
        fraction: float,
        residual_measured: bool,
        residual_contradicts: bool,
    ) -> float:
        """A circuit-classification score built only from classification terms.

        Every factor answers one of the questions the class itself depends on,
        so the number can be recomputed from ``metadata["evidence"]``:

        ``repeat``    ``1 - 0.5 ** isolated_events`` -- one event is 0.5, four 0.94
        ``stability`` the CT response consistency
        ``fraction``  how much of the scope the device(s) account for
        ``span``      observed span against the dedicated requirement
        ``breadth``   ``1 - 0.5 ** independent_devices`` -- one is not shared
        ``residual``  1.0 checked-and-clean, 0.9 unchecked, 0.5 contradictory

        A ``PARTIALLY_UNDERSTOOD`` circuit is penalised by the *measured* size of
        the residual, not by the fact that it exists: the residual is what makes
        the class true, so treating it as failure would make the honest verdict
        less credible than the over-claim it replaces.  Confidence there falls
        with how much of the scope is unexplained.
        """
        repeat = 1.0 - 0.5 ** max(0, isolated_events)
        fraction_term = max(0.0, min(1.0, fraction))
        span_term = max(0.0, min(1.0, span_s / self.config.dedicated_min_observation_s))
        if classification is CircuitClass.PARTIALLY_UNDERSTOOD:
            # Known devices are the explained part; the rest is honestly unknown.
            residual_term = max(0.1, min(1.0, fraction_term))
        elif residual_contradicts:
            residual_term = 0.5
        elif residual_measured:
            residual_term = 1.0
        else:
            residual_term = 0.9

        if classification is CircuitClass.UNKNOWN:
            return max(0.0, min(UNKNOWN_CAP, UNKNOWN_CAP * repeat * stability * fraction_term))

        if classification is CircuitClass.DEDICATED:
            score = repeat * stability * fraction_term * span_term * residual_term
            return max(0.0, min(MAX_CONFIDENCE, score))

        if not independent:
            return 0.0
        device_repeat = min(
            1.0 - 0.5 ** max(0, item.isolated_events) for item in independent
        )
        device_stability = min(item.stability for item in independent)
        device_fraction = min(max(0.0, min(1.0, item.explained_fraction)) for item in independent)
        breadth = 1.0 - 0.5 ** len(independent)
        score = device_repeat * device_stability * device_fraction * breadth * residual_term
        return max(0.0, min(MAX_CONFIDENCE, score))


# --------------------------------------------------------------------------- #
# Explanation
# --------------------------------------------------------------------------- #
def _bars_for(classification: str) -> tuple[str, ...]:
    """The bars that actually gate this class, in a stable order.

    ``min_independent_devices`` is *false* for every dedicated circuit by
    construction, so quoting it as an unmet bar would slander a confirmed
    verdict.  A reader must see only the bars the class was judged on.
    """
    if classification == CircuitClass.DEDICATED.value:
        return (
            "min_isolated_events",
            "min_stability",
            "device_power_explains",
            "no_independent_contributions",
            "no_other_identified_devices",
            "dedicated_min_observation_s",
            "residual_checked",
            "no_residual_contradiction",
        )
    if classification in (
        CircuitClass.SHARED.value,
        CircuitClass.PARTIALLY_UNDERSTOOD.value,
    ):
        return ("min_independent_devices", "residual_checked")
    return ("min_isolated_events", "min_stability", "device_power_explains")


def explain(circuit: Circuit) -> str:
    """Render the classification and the evidence behind it as one paragraph."""
    metadata = circuit.metadata or {}
    evidence = metadata.get("evidence") or {}
    stats = evidence.get("stats") or {}
    bars = evidence.get("bars") or {}
    classification = _value(circuit.classification)
    status = _value(circuit.classification_status)
    confidence = float(metadata.get("classification_confidence") or 0.0)
    devices = ", ".join(str(item) for item in circuit.device_ids) or "none identified"
    isolated = stats.get("isolated_events", 0)
    span = float(stats.get("span_s") or 0.0)
    stability = float(stats.get("stability") or 0.0)
    fraction = float(stats.get("explained_fraction") or 0.0)

    if evidence.get("residual") is None:
        residual_text = "no residual was measured, so the class stays a candidate"
    else:
        residual = evidence["residual"]
        residual_text = (
            f"residual {float(residual['residual_w']):.1f} W "
            f"({residual['kind']}, resolved={residual['resolved']})"
        )

    relevant = [name for name in _bars_for(classification) if name in bars]
    failed = [name for name in relevant if bars[name] is False]
    failed_text = (
        f"; unmet bars: {', '.join(failed)}" if failed else "; every bar met"
    )
    return (
        f"Circuit {circuit.circuit_id} is {classification} ({status}) with "
        f"classification confidence {confidence:.2f}: {isolated} isolated events "
        f"spanning {span:.0f} s at stability {stability:.2f}, explaining "
        f"{fraction * 100:.0f}% of the measured step; devices: {devices}; "
        f"{residual_text}{failed_text}."
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Make circuit metadata storable without losing structure."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return value


def persist(circuits: Iterable[Circuit], db: Any, *, now: datetime | None = None) -> int:
    """Store circuits through ``upsert_circuit``; idempotent on ``circuit_id``.

    History is append-only.  Each write compares the incoming class *and*
    status with the stored one and, only when either changed, appends
    ``{at, from, to, from_status, to_status, reason}`` to
    ``metadata["history"]``.  Nothing is ever overwritten silently, and
    re-persisting an unchanged circuit adds no row.

    ``now`` must be supplied by any caller that needs a reproducible trail
    (a replay, a test, a backfill).  ``utcnow()`` is the production default only
    because the live loop has no better instant; it is never used inside the
    pure analysis in this module, so a seeded replay with an explicit clock is
    byte-identical.
    """
    existing = {str(row["circuit_id"]): row for row in db.circuits_all()}
    written = 0
    moment = _aware(now) if now is not None else utcnow()
    for circuit in circuits:
        previous = existing.get(str(circuit.circuit_id))
        metadata = dict(circuit.metadata or {})
        # History is seeded from the *stored* row, so a re-run that carries no
        # history of its own still appends to what is already recorded.
        stored = dict((previous or {}).get("metadata") or {})
        incoming_history = [dict(item) for item in (metadata.get("history") or [])]
        history = incoming_history or [
            dict(item) for item in (stored.get("history") or [])
        ]
        to_class = _value(circuit.classification)
        to_status = _value(circuit.classification_status)
        if previous is None:
            from_class: str | None = None
            from_status: str | None = None
            changed = True
        else:
            from_class = _value(previous.get("classification"))
            from_status = _value(previous.get("classification_status"))
            changed = (from_class, from_status) != (to_class, to_status)
        if changed:
            history.append(
                {
                    "at": moment.isoformat(),
                    "from": from_class,
                    "to": to_class,
                    "from_status": from_status,
                    "to_status": to_status,
                    "reason": explain(circuit),
                }
            )
        metadata["history"] = history
        db.upsert_circuit(
            Circuit(
                circuit_id=circuit.circuit_id,
                name=circuit.name,
                panel_id=circuit.panel_id,
                classification=circuit.classification,
                classification_status=circuit.classification_status,
                parent_circuit_id=circuit.parent_circuit_id,
                ct_ids=list(circuit.ct_ids),
                device_ids=list(circuit.device_ids),
                measurement_basis=circuit.measurement_basis,
                metadata=_jsonable(metadata),
            )
        )
        existing[str(circuit.circuit_id)] = {
            "classification": to_class,
            "classification_status": to_status,
        }
        written += 1
    return written
