"""The residual as a first-class, decomposed object (WP7).

Why this module exists
----------------------
``Reconciliation.unknown_residual_w`` is already exact::

    accounted + unknown_residual_w == main_power_w
    unmonitored_w + meter_disagreement_w == unknown_residual_w

What it cannot answer is *"what kind of number is this, and how has it moved?"*
The residual had no sign convention of its own, no resolution state, no age, no
baseline/dynamic split and no history a reader could look at.  This module adds
that layer **without touching the engine's balance**:

* :class:`ResidualScope` describes *what was measured*: one conductor, one
  measurement basis, how many raw CTs observe it, whether it sits under another
  scope, and which device contributions are known.
* :class:`ResidualAnalyzer` computes the signed residual, refuses when the
  arithmetic would be dishonest, and decomposes it into a robust baseline plus a
  dynamic part.
* :func:`persist` writes results through the existing
  ``Database.insert_residual_power`` / ``Database.residual_history`` helpers.

The core identity
-----------------
::

    Residual(t) = CircuitPower(t) - Sum(KnownLoadContributions(t))

It is evaluated **only** when all of the following hold:

1. the scope names exactly one conductor after alias resolution;
2. the scope's basis is established and every contribution's basis is
   *comparable* to it (or an explicit conversion multiplier is recorded);
3. no two contributions are observations of the same conductor (an alias or a
   duplicated CT must be counted once), and no contribution is already enclosed
   by another contribution's CT (that would subtract it twice).

Anything else raises :class:`ResidualNotComputed` (a typed family) rather than
returning a plausible-looking number.  :meth:`ResidualAnalyzer.try_compute` is
the non-raising form and carries the same reason code plus its evidence.

Never clamped
-------------
``_signed_residual`` is the single place the difference is formed and it applies
no ``max(0.0, ...)``, no ``abs()`` and no epsilon.  A negative residual is a real
observation (accounted power exceeding the measured total) and keeps its sign;
``kind`` is always :func:`wattmosaic_engine.contracts.derived_residual_kind`, so
there is exactly one definition of "positive/negative/near-zero" in the codebase.

Baseline vs dynamic
-------------------
``baseline_w`` is the **median** of the recent residual observations in
``ResidualScope.window_w`` (oldest first), and ``dynamic_w`` is
``residual_w - baseline_w``, so the two components always add back to the total.
The median is used because the residual window routinely contains a minority of
large excursions (a compressor start, a meter glitch) that would move a mean by
kilowatts while leaving the median untouched; the split is meant to say "this
much is always there, this much is moving", and a robust central tendency is the
only estimator that does not turn one spike into a permanent baseline.  With no
window there is no dynamic claim: ``baseline_w = residual_w`` and
``dynamic_w = 0.0``.

Resolution
----------
``resolved=False`` is a legitimate, permanent state: an unexplained residual may
stay unexplained forever, and it is never rendered as ``0 W``.
:class:`ResidualDecomposition` and :class:`ResidualTrend` both report the signed
number, the resolution state and its age, so a consumer cannot mistake
"unresolved" for "nothing there".

This module deliberately does **not** classify circuits (WP6) and does not
re-derive the engine's balance; it is a *view* over the same arithmetic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Any

from ..contracts import (
    MeasurementBasis,
    QualityStatus,
    ResidualKind,
    ResidualPower,
    derived_residual_kind,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph flat
    from ..electrical.topology import TopologyGraph

__all__ = [
    "IncompatibleBasisError",
    "ResidualAnalyzer",
    "ResidualAttempt",
    "ResidualDecomposition",
    "ResidualDecompositionConflict",
    "ResidualNotComputed",
    "ResidualScope",
    "ResidualTrend",
    "bases_are_compatible",
    "persist",
]

#: Tolerance for "explained completely".  A residual is a float difference of
#: floats; only this comparison may look like a tolerance, and it is never used
#: to move a value towards zero.
ZERO_TOL_W = 1e-9


# --------------------------------------------------------------------------- #
# Typed refusals
# --------------------------------------------------------------------------- #
class ResidualNotComputed(RuntimeError):
    """The residual was not computed, with a machine-readable reason.

    Returning a number because a number was asked for is how a residual becomes
    a fabrication.  ``reason_code`` names *why* it was refused and ``evidence``
    carries the inputs that made the decision checkable.
    """

    def __init__(
        self,
        reason_code: str,
        detail: str = "",
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.reason_code = str(reason_code)
        self.evidence: dict[str, Any] = dict(evidence or {})
        self.detail = str(detail)
        super().__init__(
            f"residual not computed: {self.reason_code}"
            + (f" ({self.detail})" if self.detail else "")
        )


class IncompatibleBasisError(ResidualNotComputed):
    """A physical measurement and a differently-based value were to be mixed.

    Subtracting a factor-adjusted whole-appliance total from a raw conductor
    reading (or an estimate from a measurement) is not arithmetic, it is two
    different quantities in one subtraction.  An explicit conversion -- the
    recorded multiplier -- is the only thing that makes them comparable.
    """


class ResidualDecompositionConflict(ResidualNotComputed):
    """The candidate contributions contradict the residual itself."""


#: ``MeasurementBasis`` values that describe a physical, whole-load observation
#: of the conductor they are clamped to.
_PHYSICAL: frozenset[MeasurementBasis] = frozenset(
    {MeasurementBasis.CT_MEASURED, MeasurementBasis.MONITOR_REPORTED}
)
#: Values whose published number already claims the whole appliance.
_WHOLE_APPLIANCE: frozenset[MeasurementBasis] = frozenset(
    {MeasurementBasis.MONITOR_REPORTED, MeasurementBasis.FACTOR_ADJUSTED}
)


def bases_are_compatible(scope: MeasurementBasis, contribution: MeasurementBasis) -> bool:
    """Whether two watt values may appear in one subtraction.

    Same basis is always comparable.  Beyond that only two equivalences hold,
    and both are physical statements rather than conveniences:

    * ``CT_MEASURED`` / ``MONITOR_REPORTED`` -- a CT reading and a monitor that
      publishes the whole load itself are both measurements of that conductor;
    * ``MONITOR_REPORTED`` / ``FACTOR_ADJUSTED`` -- a monitor that already
      applied the factor *is* the factor-adjusted case (``factors._derive``
      resolves it to ``MONITOR_REPORTED`` for exactly this reason).

    ``FACTOR_ADJUSTED`` against ``CT_MEASURED`` is **not** comparable: one side
    claims an appliance total, the other a leg, and only the recorded multiplier
    can bridge them.  ``ESTIMATED_APPLIANCE_TOTAL`` is never comparable to a
    measurement; an estimate may be reported, not subtracted from a reading.
    """
    left = MeasurementBasis(scope)
    right = MeasurementBasis(contribution)
    if left is right:
        return True
    pair = {left, right}
    return pair <= _PHYSICAL or pair <= _WHOLE_APPLIANCE


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ResidualScope:
    """One measurement scope: a single conductor and the loads known on it.

    ``ct_id`` is the declared scope channel; ``ct_ids`` (when supplied) lists
    every raw CT that contributed to the reading, so a duplicated or aliased
    pair is *described* rather than silently summed.  ``multiplicity`` states how
    many raw CTs map to this scope (2 for a second monitor clamped on one wire, 1
    for a plain CT) and must agree with ``ct_ids`` when both are given.
    """

    scope_id: str
    ct_id: str
    basis: MeasurementBasis = MeasurementBasis.UNKNOWN
    multiplicity: int = 1
    is_nested_under: str | None = None
    device_contributions: Mapping[str, float] = field(default_factory=dict)
    """Known loads to subtract, in W, keyed by device id."""

    ct_ids: tuple[str, ...] = ()
    """Every raw CT the scope reading was assembled from."""

    readings: Mapping[str, float] = field(default_factory=dict)
    """Raw per-CT readings (W) of the *same* conductor, used only as evidence."""

    contribution_bases: Mapping[str, MeasurementBasis] = field(default_factory=dict)
    """Basis of a contribution when it differs from the scope's own basis."""

    contribution_cts: Mapping[str, str] = field(default_factory=dict)
    """The CT that observed each contribution, for duplicate/nesting checks."""

    conversions: Mapping[str, float] = field(default_factory=dict)
    """Explicit recorded multiplier (e.g. ``2.0``) for one contribution.

    This is the only thing that makes an otherwise incompatible basis
    comparable, and it is applied exactly once.
    """

    explained_by_device_w: Mapping[str, float] = field(default_factory=dict)
    """Parts of the residual already explained before this cycle."""

    quality_status: QualityStatus = QualityStatus.GOOD
    window_w: tuple[float, ...] = ()
    """Recent **residual** observations (W), oldest first.

    These are residual values, not circuit totals: the baseline is a statement
    about the residual, so mixing the two would make the split meaningless.
    """

    unresolved_since: datetime | None = None
    """When this scope's residual was first seen unexplained."""

    def __post_init__(self) -> None:
        if not str(self.scope_id).strip():
            raise ValueError("scope_id must be non-empty")
        if not str(self.ct_id).strip():
            raise ValueError("ct_id must be non-empty")
        if isinstance(self.multiplicity, bool) or int(self.multiplicity) < 1:
            raise ValueError("multiplicity must be an integer >= 1")
        object.__setattr__(self, "multiplicity", int(self.multiplicity))
        object.__setattr__(self, "basis", MeasurementBasis(self.basis))
        object.__setattr__(self, "quality_status", QualityStatus(self.quality_status))
        object.__setattr__(self, "ct_ids", tuple(str(cid) for cid in self.ct_ids))
        object.__setattr__(
            self, "window_w", tuple(float(value) for value in self.window_w)
        )
        object.__setattr__(self, "device_contributions", dict(self.device_contributions))
        object.__setattr__(self, "readings", dict(self.readings))
        object.__setattr__(
            self,
            "contribution_bases",
            {str(k): MeasurementBasis(v) for k, v in self.contribution_bases.items()},
        )
        object.__setattr__(
            self,
            "contribution_cts",
            {str(k): str(v) for k, v in self.contribution_cts.items()},
        )
        object.__setattr__(
            self, "conversions", {str(k): float(v) for k, v in self.conversions.items()}
        )
        object.__setattr__(self, "explained_by_device_w", dict(self.explained_by_device_w))
        if self.ct_ids and len(self.ct_ids) != self.multiplicity:
            raise ValueError(
                "ct_ids must list every raw CT of the scope: "
                f"got {len(self.ct_ids)} for multiplicity={self.multiplicity}"
            )
        if not self.ct_ids and self.multiplicity != 1:
            raise ValueError(
                "multiplicity > 1 must name the CTs it counts (ct_ids), so a "
                "duplicate cannot be asserted without being described"
            )

    @property
    def channels(self) -> tuple[str, ...]:
        """Every raw CT of this scope, the declared one included."""
        if self.ct_ids:
            return self.ct_ids
        return (self.ct_id,)


# --------------------------------------------------------------------------- #
# Arithmetic: one place, no clamp
# --------------------------------------------------------------------------- #
def _signed_residual(circuit_power_w: float, known_load_w: float) -> float:
    """``CircuitPower - KnownLoads``, sign preserved.

    This is the only subtraction in the module.  It must never be wrapped in
    ``max(0.0, ...)``, ``abs(...)`` or a near-zero epsilon: a negative residual
    means accounted power exceeds the measured total, which is information.
    """
    return float(circuit_power_w) - float(known_load_w)


def _median(values: Sequence[float]) -> float:
    return float(median(values))


def _strict_float(value: Any) -> float:
    """Convert a watt value without accepting a numeric string.

    ``float("1000")`` succeeds, and a string in a watt slot is a caller bug:
    coercing it silently would turn ``"1000"`` and ``1000`` into the same
    arithmetic while hiding the type error.  An ``int``/``float`` (and a numpy
    scalar) is accepted; everything else is refused.
    """
    if isinstance(value, bool | str):
        raise TypeError(f"not a watt value: {value!r}")
    if not isinstance(value, int | float):
        number = getattr(value, "__float__", None)
        if number is None:
            raise TypeError(f"not a watt value: {value!r}")
        return float(number())
    return float(value)


# --------------------------------------------------------------------------- #
# The analyzer
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ResidualAttempt:
    """The outcome of :meth:`ResidualAnalyzer.try_compute`.

    Exactly one of ``residual`` / ``refused`` is set.  ``evidence`` is the
    "not computed" marker: the frozen :class:`ResidualPower` contract has no
    evidence field, so the refusal lives here (and in
    :class:`ResidualDecomposition` / :class:`ResidualTrend`) instead of being
    smuggled into a power value.
    """

    residual: ResidualPower | None
    refused: bool
    reason_code: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "residual": None if self.residual is None else self.residual.model_dump(mode="json"),
            "refused": self.refused,
            "reason_code": self.reason_code,
            "evidence": dict(self.evidence),
        }


class ResidualAnalyzer:
    """Compute and decompose signed residuals for a measurement scope.

    ``topology`` (a :class:`~wattmosaic_engine.electrical.topology.TopologyGraph`)
    is optional but strongly preferred: it supplies ``canonical_channel_id`` (so
    an alias cannot be counted twice) and ``ancestors`` (so a contribution
    already enclosed by another is not subtracted again).  Without it the
    analyzer can only verify what the scope itself declares, and it says so in
    its evidence.
    """

    def __init__(self, topology: TopologyGraph | None = None) -> None:
        self.topology = topology
        self.last_evidence: dict[str, Any] = {}

    # -- compute ----------------------------------------------------------- #
    def compute(
        self, scope: ResidualScope, circuit_power_w: float, *, now: datetime | None = None
    ) -> ResidualPower:
        """Return the signed residual for one scope.

        The residual is only computed when the bases are comparable and every
        conductor is counted once; otherwise a typed
        :class:`ResidualNotComputed` is raised.  The result never clamps: a
        negative residual stays negative and keeps ``NEGATIVE`` as its kind, and
        ``resolved=False`` is preserved with its ``unresolved_since`` age.
        """
        attempt = self.try_compute(scope, circuit_power_w, now=now)
        if attempt.refused or attempt.residual is None:
            code = attempt.reason_code or "not_computed"
            error = (
                IncompatibleBasisError
                if code.startswith("incompatible_basis") or code.startswith("unknown_scope_basis")
                else ResidualNotComputed
            )
            raise error(code, detail=str(attempt.evidence.get("detail", "")), evidence=attempt.evidence)
        return attempt.residual

    def try_compute(
        self, scope: ResidualScope, circuit_power_w: float, *, now: datetime | None = None
    ) -> ResidualAttempt:
        """The non-raising form of :meth:`compute`; marks refusals explicitly."""
        evidence: dict[str, Any] = {
            "scope_id": scope.scope_id,
            "scope_basis": MeasurementBasis(scope.basis).value,
            "multiplicity": scope.multiplicity,
            "canonicalization": "topology" if self.topology is not None else "declared_only",
        }
        self.last_evidence = evidence

        try:
            circuit = _strict_float(circuit_power_w)
        except (TypeError, ValueError):
            evidence["circuit_power_w"] = repr(circuit_power_w)
            return self._refuse("non_finite_circuit_power", evidence)
        evidence["circuit_power_w"] = circuit
        if not math.isfinite(circuit):
            return self._refuse("non_finite_circuit_power", evidence)

        refusal = self._canonicalise(scope, evidence)
        if refusal is not None:
            return refusal

        basis_refusal = self._check_bases(scope, evidence)
        if basis_refusal is not None:
            return basis_refusal

        counted, excluded, duplicates = self._counted_contributions(scope, evidence)
        evidence["excluded_contributions"] = excluded
        evidence["duplicate_contributions"] = duplicates
        evidence["subtracted_contributions"] = dict(counted)

        known = sum(counted.values())
        residual_w = _signed_residual(circuit, known)
        if not math.isfinite(residual_w):
            return self._refuse("non_finite_residual", evidence)

        baseline_w, dynamic_w = self._split(residual_w, scope.window_w, evidence)
        resolved = abs(residual_w) <= ZERO_TOL_W
        unresolved_since = None if resolved else (scope.unresolved_since or now)

        residual = ResidualPower(
            residual_w=residual_w,
            kind=derived_residual_kind(residual_w),
            resolved=resolved,
            unresolved_since=unresolved_since,
            explained_by_device_w=dict(scope.explained_by_device_w),
            quality_status=scope.quality_status,
            basis=scope.basis,
            baseline_w=baseline_w,
            dynamic_w=dynamic_w,
        )
        evidence["known_load_w"] = known
        evidence["residual_w"] = residual_w
        evidence["resolved"] = resolved
        return ResidualAttempt(residual=residual, refused=False, evidence=evidence)

    # -- trend & decomposition --------------------------------------------- #
    def decompose(
        self,
        residual_history: Iterable[Mapping[str, Any] | ResidualPower],
        candidate_contributions: Mapping[str, float],
        *,
        now: datetime | None = None,
    ) -> ResidualDecomposition:
        """Explain part of the most recent residual with new device candidates.

        Rules:

        * candidates are subtracted from the **latest** residual only; the
          before/after pair is kept in evidence so a shrink is visible;
        * a candidate may explain *part* of the residual, which leaves the rest
          unresolved (``resolved=False``) with its reason code;
        * if no candidate applies the residual simply stays unresolved -- nothing
          is invented;
        * if the candidates sum to more than the residual (or the residual is
          negative, or a candidate is not a load) the decomposition is a
          **conflict**: flagged, explanatory fields emptied, resolution refused.
        """
        rows = self._ordered_history(residual_history)
        evidence: dict[str, Any] = {"history_rows": len(rows)}
        if not rows:
            # No residual was measured.  Reporting ``0 W`` here would be a
            # fabricated measurement -- and one that reads as "nothing is
            # unexplained", the most misleading possible answer.
            return ResidualDecomposition(
                timestamp=None,
                before_w=None,
                unexplained_w=None,
                explained_by_device_w={},
                resolved=False,
                conflict=False,
                reason_code="no_residual_history",
                detail="no residual history exists, so there is no residual to decompose",
                basis=MeasurementBasis.UNKNOWN,
                quality_status=QualityStatus.MISSING,
                evidence={**evidence, "measured": False},
            )

        latest = rows[-1]
        before_w = float(latest["residual_w"])
        timestamp = latest.get("timestamp")
        basis = MeasurementBasis(latest.get("basis") or MeasurementBasis.UNKNOWN)
        quality = QualityStatus(latest.get("quality_status") or QualityStatus.MISSING)
        prior_explained = dict(latest.get("explained_by_device_w") or {})
        evidence.update({"before_w": before_w, "candidate_contributions": dict(candidate_contributions)})

        def conflict(reason_code: str, detail: str = "") -> ResidualDecomposition:
            return ResidualDecomposition(
                timestamp=timestamp,
                before_w=before_w,
                unexplained_w=before_w,
                explained_by_device_w={},
                resolved=False,
                conflict=True,
                reason_code=reason_code,
                detail=detail,
                basis=basis,
                quality_status=quality,
                evidence={**evidence, "after_w": before_w},
            )

        if before_w < 0.0:
            # Accounted power exceeds the measured total: a positive candidate
            # cannot "explain" a deficit, so any claim here is a contradiction.
            return conflict(
                "residual_is_negative",
                "a negative residual is a surplus of accounted power, not load",
            )

        accepted: dict[str, float] = {}
        excluded: list[dict[str, Any]] = []
        for device_id in sorted(candidate_contributions):
            value = candidate_contributions[device_id]
            try:
                watts = _strict_float(value)
            except (TypeError, ValueError):
                excluded.append({"device_id": device_id, "value": value, "reason": "not_a_number"})
                continue
            if not math.isfinite(watts) or watts <= 0.0:
                excluded.append(
                    {"device_id": device_id, "value": watts, "reason": "not_a_load"}
                )
                continue
            accepted[device_id] = watts
        if excluded:
            evidence["excluded"] = excluded

        if not accepted:
            return ResidualDecomposition(
                timestamp=timestamp,
                before_w=before_w,
                unexplained_w=before_w,
                explained_by_device_w={},
                resolved=False,
                conflict=False,
                reason_code="residual_not_disaggregable",
                detail="no candidate explains any part of this residual",
                basis=basis,
                quality_status=quality,
                evidence={**evidence, "after_w": before_w},
            )

        total = sum(accepted.values())
        if total > before_w + ZERO_TOL_W:
            return conflict(
                "candidates_over_explain_residual",
                f"candidates total {total:.3f} W against a {before_w:.3f} W residual",
            )

        after_w = before_w - total
        resolved = abs(after_w) <= ZERO_TOL_W
        explained = {**prior_explained, **accepted}
        evidence.update(
            {
                "before_w": before_w,
                "after_w": after_w,
                "explained_total_w": total,
                "resolved": resolved,
            }
        )
        return ResidualDecomposition(
            timestamp=timestamp,
            before_w=before_w,
            unexplained_w=after_w,
            explained_by_device_w=explained,
            resolved=resolved,
            conflict=False,
            reason_code="residual_resolved" if resolved else "residual_partially_explained",
            basis=basis,
            quality_status=quality,
            evidence=evidence,
        )

    def trend(
        self,
        residual_history: Iterable[Mapping[str, Any] | ResidualPower],
        *,
        decompositions: Sequence[ResidualDecomposition] = (),
    ) -> ResidualTrend:
        """Describe the residual's history: current, typical, curve, confidence.

        The curve is ascending in time even though ``Database.residual_history``
        returns newest first, so a reader can plot it without reasoning about
        storage order.  ``confidence`` states how much history supports the
        typical value (``n / (n + 8)``, capped at 1.0): it rises monotonically
        with sample count and is ``None`` when there is no history at all --
        never a fabricated 0.0 or 1.0.
        """
        rows = self._ordered_history(residual_history)
        if not rows:
            return ResidualTrend(
                current_w=None,
                typical_w=None,
                baseline_w=None,
                dynamic_w=None,
                confidence=None,
                curve=(),
                decompositions=tuple(decompositions),
                resolved=False,
                unresolved_since=None,
                basis=MeasurementBasis.UNKNOWN,
                quality_status=QualityStatus.MISSING,
                sample_count=0,
                reason_code="no_residual_history",
                evidence={"sample_count": 0, "measured": False},
            )

        values = [float(row["residual_w"]) for row in rows]
        latest = rows[-1]
        current = values[-1]
        typical = _median(values)
        confidence = min(1.0, len(values) / (len(values) + 8.0))
        unresolved_since = _optional_datetime(latest.get("unresolved_since"))
        return ResidualTrend(
            current_w=current,
            typical_w=typical,
            baseline_w=typical,
            dynamic_w=current - typical,
            confidence=confidence,
            curve=tuple((row["timestamp"], float(row["residual_w"])) for row in rows),
            decompositions=tuple(decompositions),
            resolved=bool(latest.get("resolved", False)),
            unresolved_since=unresolved_since,
            basis=MeasurementBasis(latest.get("basis") or MeasurementBasis.UNKNOWN),
            quality_status=QualityStatus(latest.get("quality_status") or QualityStatus.MISSING),
            sample_count=len(values),
            reason_code="residual_history",
            evidence={
                "sample_count": len(values),
                "cause_counts": _cause_counts(values),
                "first_at": rows[0]["timestamp"].isoformat() if rows else None,
                "last_at": latest["timestamp"].isoformat(),
                "sign_changes": sum(
                    1
                    for left, right in zip(values, values[1:], strict=False)
                    if derived_residual_kind(left) is not derived_residual_kind(right)
                ),
            },
        )

    # -- internals --------------------------------------------------------- #
    @staticmethod
    def _refuse(reason_code: str, evidence: dict[str, Any]) -> ResidualAttempt:
        return ResidualAttempt(
            residual=None, refused=True, reason_code=reason_code, evidence=dict(evidence)
        )

    def _canonicalise(
        self, scope: ResidualScope, evidence: dict[str, Any]
    ) -> ResidualAttempt | None:
        """Collapse alias chains; refuse a scope that spans two conductors."""
        canonical: dict[str, str] = {}
        for channel in scope.channels:
            canonical[channel] = self._canonical_id(channel)
        groups: dict[str, list[str]] = {}
        for channel, owner in canonical.items():
            groups.setdefault(owner, []).append(channel)
        evidence["collapsed_aliases"] = {
            owner: tuple(sorted(members)) for owner, members in groups.items() if len(members) > 1
        }
        evidence["counted_channels"] = sorted(groups)

        if len(groups) != 1:
            evidence["scope_channels"] = dict(canonical)
            return self._refuse("scope_spans_multiple_channels", evidence)

        owner = next(iter(groups))
        evidence["canonical_channel_id"] = owner
        declared_owner = self._canonical_id(scope.ct_id)
        if declared_owner != owner:
            evidence["declared_channel_canonical_id"] = declared_owner

        # Per-CT readings of one conductor: a spread is a meter diagnostic, not
        # extra load, so it is reported and never added to the total.
        values = [
            float(scope.readings[channel])
            for channel in groups[owner]
            if channel in scope.readings
            and isinstance(scope.readings[channel], (int, float))
            and math.isfinite(float(scope.readings[channel]))
        ]
        if len(values) > 1:
            evidence["alias_spread_w"] = max(values) - min(values)
        evidence["scope_is_nested"] = scope.is_nested_under is not None
        return None

    def _canonical_id(self, channel: str) -> str:
        if self.topology is None:
            return channel
        canonical = self.topology.canonical_channel_id(channel)
        return canonical or channel

    def _check_bases(
        self, scope: ResidualScope, evidence: dict[str, Any]
    ) -> ResidualAttempt | None:
        scope_basis = MeasurementBasis(scope.basis)
        if scope_basis is MeasurementBasis.UNKNOWN:
            return self._refuse("unknown_scope_basis", evidence)

        declared: dict[str, str] = {}
        incompatible: dict[str, str] = {}
        conversions: dict[str, float] = {}
        for device_id in sorted(scope.device_contributions):
            raw = scope.contribution_bases.get(device_id)
            basis = scope_basis if raw is None else MeasurementBasis(raw)
            if basis is MeasurementBasis.UNKNOWN:
                # The contribution never stated a basis: it inherits the scope's
                # one.  This is an absence of a distinct claim, not a claim that
                # the value is dimensionless.
                basis = scope_basis
            declared[device_id] = basis.value
            conversion = scope.conversions.get(device_id)
            if conversion is not None:
                if not math.isfinite(float(conversion)) or float(conversion) <= 0.0:
                    return self._refuse("invalid_conversion", evidence)
                conversions[device_id] = float(conversion)
                continue
            if not bases_are_compatible(scope_basis, basis):
                incompatible[device_id] = basis.value

        evidence["contribution_basis"] = declared
        if conversions:
            evidence["conversions"] = conversions
        if incompatible:
            evidence["incompatible_contributions"] = incompatible
            evidence["detail"] = ", ".join(
                f"{device_id} ({basis})" for device_id, basis in sorted(incompatible.items())
            )
            return self._refuse("incompatible_basis", evidence)
        return None

    def _counted_contributions(
        self, scope: ResidualScope, evidence: dict[str, Any]
    ) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
        """Contributions to subtract, after dropping duplicates and enclosures.

        Two rules, both structural rather than heuristic:

        * **same conductor** -- two contributions observed by CTs that resolve to
          one canonical channel are one observation, not two loads;
        * **enclosure** -- a contribution whose CT sits under another
          contribution's CT is already inside that total (the
          ``effective_feeds`` rule), so subtracting both counts it twice.

        Both rules are decided on the *whole* candidate set first, so the answer
        does not depend on the order the contributions were declared in.
        """
        candidates: dict[str, tuple[float, str]] = {}
        excluded: dict[str, str] = {}
        for device_id in sorted(scope.device_contributions):
            value = scope.device_contributions[device_id]
            try:
                watts = _strict_float(value)
            except (TypeError, ValueError):
                excluded[device_id] = "not_a_number"
                continue
            if not math.isfinite(watts):
                excluded[device_id] = "not_finite"
                continue
            conversion = scope.conversions.get(device_id)
            if conversion is not None:
                # Applied exactly once, and recorded so a reader can check it.
                watts *= float(conversion)
            declared_ct = scope.contribution_cts.get(device_id)
            canonical = self._canonical_id(declared_ct) if declared_ct else ""
            candidates[device_id] = (watts, canonical)

        duplicates: dict[str, str] = {}
        # Keep one contribution per conductor.  Prefer the one declared on the
        # canonical channel itself (an alias is the shadow copy), then the
        # lexicographically first device, so the choice is deterministic.
        by_conductor: dict[str, list[str]] = {}
        for device_id, (_, canonical) in candidates.items():
            if canonical:
                by_conductor.setdefault(canonical, []).append(device_id)
        for canonical, members in by_conductor.items():
            if len(members) < 2:
                continue
            keep = min(
                members,
                key=lambda device_id: (
                    scope.contribution_cts.get(device_id, "") != canonical,
                    device_id,
                ),
            )
            for device_id in members:
                if device_id != keep:
                    duplicates[device_id] = f"same_conductor_as:{keep}"
                    excluded[device_id] = f"duplicate_of:{keep}"

        kept: dict[str, str] = {
            device_id: canonical
            for device_id, (_, canonical) in candidates.items()
            if device_id not in duplicates and canonical
        }
        enclosed: dict[str, str] = {}
        for device_id, canonical in kept.items():
            enclosing = next(
                (
                    other
                    for other, other_ct in kept.items()
                    if other != device_id and self._is_ancestor(other_ct, canonical)
                ),
                None,
            )
            if enclosing is not None:
                enclosed[device_id] = f"enclosed_by:{enclosing}"
                excluded[device_id] = f"enclosed_by:{enclosing}"

        counted = {
            device_id: watts
            for device_id, (watts, _) in candidates.items()
            if device_id not in excluded
        }
        evidence["known_load_w"] = sum(counted.values())
        return counted, excluded, duplicates

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if self.topology is None or ancestor == descendant:
            return False
        return bool(self.topology.is_ancestor(ancestor, descendant))

    @staticmethod
    def _split(
        residual_w: float, window: Sequence[float], evidence: dict[str, Any]
    ) -> tuple[float, float]:
        """Robust baseline plus the moving part; they always add to the total."""
        values = [float(value) for value in window if math.isfinite(float(value))]
        if not values:
            evidence["baseline_estimator"] = "no_window_current_only"
            evidence["window_samples"] = 0
            return residual_w, 0.0
        baseline = _median(values)
        evidence["baseline_estimator"] = "median_of_window"
        evidence["window_samples"] = len(values)
        evidence["window_span_w"] = max(values) - min(values)
        return baseline, residual_w - baseline

    @staticmethod
    def _ordered_history(
        history: Iterable[Mapping[str, Any] | ResidualPower]
    ) -> list[dict[str, Any]]:
        """Normalise DB rows / models into an ascending time series.

        ``ResidualPower`` carries no timestamp of its own, so a model in a
        bare iterable is given one second's worth of synthetic ordering at the
        position it was supplied -- it is never placed at "now", which would
        reorder history behind the caller's back.  The sort is stable, so equal
        timestamps keep their supplied order.
        """
        rows: list[dict[str, Any]] = []
        synthetic = datetime(1970, 1, 1, tzinfo=UTC)
        for index, item in enumerate(history):
            if isinstance(item, ResidualPower):
                rows.append(
                    {
                        "timestamp": synthetic + timedelta(seconds=index),
                        "residual_w": float(item.residual_w),
                        "kind": ResidualKind(item.kind).value,
                        "resolved": bool(item.resolved),
                        "unresolved_since": _optional_datetime(item.unresolved_since),
                        "explained_by_device_w": dict(item.explained_by_device_w),
                        "quality_status": QualityStatus(item.quality_status).value,
                        "basis": MeasurementBasis(item.basis).value,
                    }
                )
                continue
            row = dict(item)
            rows.append(
                {
                    **row,
                    "timestamp": _as_datetime(row.get("timestamp")),
                    "unresolved_since": _optional_datetime(row.get("unresolved_since")),
                    "residual_w": float(row.get("residual_w", 0.0)),
                }
            )
        rows.sort(key=lambda row: row["timestamp"])
        return rows


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return _as_datetime(value)


def _cause_counts(values: Sequence[float]) -> dict[str, int]:
    counts = {"positive": 0, "negative": 0, "near_zero": 0}
    for value in values:
        counts[derived_residual_kind(value).value] += 1
    return counts


# --------------------------------------------------------------------------- #
# Decomposition result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ResidualDecomposition:
    """What a set of candidate devices does (or does not) explain.

    ``before_w`` and ``unexplained_w`` are both kept: the history of the
    residual is the point, so "it shrank by 250 W" must be visible rather than
    replaced by a new number.  ``conflict`` means the candidates contradict the
    residual (they over-explain it, or a positive claim is made against a
    negative residual), in which case nothing is explained and nothing is
    resolved.

    ``before_w`` is ``None`` when there was no residual history at all: "we did
    not measure a residual" is not "the residual was 0 W", and a consumer that
    renders ``0.0`` for unknown would publish a fabricated measurement.  The
    reason code says which case it is.
    """

    timestamp: datetime | None
    before_w: float | None
    unexplained_w: float | None
    explained_by_device_w: Mapping[str, float] = field(default_factory=dict)
    resolved: bool = False
    conflict: bool = False
    reason_code: str = "residual_not_disaggregable"
    detail: str = ""
    basis: MeasurementBasis = MeasurementBasis.UNKNOWN
    quality_status: QualityStatus = QualityStatus.MISSING
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", MeasurementBasis(self.basis))
        object.__setattr__(self, "quality_status", QualityStatus(self.quality_status))
        object.__setattr__(self, "explained_by_device_w", dict(self.explained_by_device_w))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def measured(self) -> bool:
        """Whether a residual was actually present in the history."""
        return self.before_w is not None

    @property
    def after_w(self) -> float | None:
        """The residual that remains after the explained part: never clamped."""
        return self.unexplained_w

    @property
    def explained_total_w(self) -> float:
        return float(sum(self.explained_by_device_w.values()))

    def to_residual_power(self, *, now: datetime | None = None) -> ResidualPower:
        """Carry the decomposition into the frozen residual contract.

        A decomposition with no measured residual cannot become a
        ``ResidualPower``: the contract carries a number, and ``0.0`` would be
        a fabricated measurement rather than "unknown".
        """
        if self.unexplained_w is None:
            raise ResidualNotComputed(
                self.reason_code or "residual_not_measured",
                detail="no residual was measured, so there is nothing to carry",
                evidence=self.evidence,
            )
        unresolved_since = None if self.resolved else now
        return ResidualPower(
            residual_w=self.unexplained_w,
            kind=derived_residual_kind(self.unexplained_w),
            resolved=self.resolved,
            unresolved_since=unresolved_since,
            explained_by_device_w=dict(self.explained_by_device_w),
            quality_status=self.quality_status,
            basis=self.basis,
        )

    def require_resolved(self) -> ResidualPower:
        """Return the residual, or refuse if the decomposition is a conflict."""
        if self.conflict or not self.resolved:
            raise ResidualDecompositionConflict(
                self.reason_code, detail=self.detail, evidence=self.evidence
            )
        return self.to_residual_power()

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": None if self.timestamp is None else self.timestamp.isoformat(),
            "residual_w": self.unexplained_w,
            "before_w": self.before_w,
            "after_w": self.after_w,
            "measured": self.measured,
            "explained_by_device_w": dict(self.explained_by_device_w),
            "resolved": self.resolved,
            "conflict": self.conflict,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "basis": MeasurementBasis(self.basis).value,
            "quality_status": QualityStatus(self.quality_status).value,
            "evidence": dict(self.evidence),
        }

    @property
    def explanation(self) -> list[str]:
        before = "unknown" if self.before_w is None else f"{self.before_w:.1f}W"
        after = "unknown" if self.after_w is None else f"{self.after_w:.1f}W"
        notes = [f"residual_before:{before}", f"residual_after:{after}"]
        if self.explained_by_device_w:
            for device_id, watts in sorted(self.explained_by_device_w.items()):
                notes.append(f"explained_by:{device_id}={watts:.1f}W")
        if self.conflict:
            notes.append(f"residual_decomposition_conflict:{self.reason_code}")
        elif self.before_w is None:
            notes.append(f"residual_not_measured:{self.reason_code}")
        elif not self.resolved:
            notes.append(f"residual_unresolved:{self.reason_code}")
        else:
            notes.append("residual_resolved")
        return notes


# --------------------------------------------------------------------------- #
# Trend result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ResidualTrend:
    """Current vs typical residual plus the curve and its decomposition history.

    With no history, ``current_w``/``typical_w``/``baseline_w``/``dynamic_w``
    are ``None`` and ``kind`` is ``None``: "not measured" is a distinct state
    from "measured as 0 W".  A consumer that needs a number must handle the
    absent case explicitly instead of rendering a fabricated zero.
    """

    current_w: float | None
    typical_w: float | None
    baseline_w: float | None
    dynamic_w: float | None
    confidence: float | None = None
    curve: tuple[tuple[datetime, float], ...] = ()
    decompositions: tuple[ResidualDecomposition, ...] = ()
    resolved: bool = False
    unresolved_since: datetime | None = None
    basis: MeasurementBasis = MeasurementBasis.UNKNOWN
    quality_status: QualityStatus = QualityStatus.MISSING
    sample_count: int = 0
    reason_code: str = "residual_history"
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", MeasurementBasis(self.basis))
        object.__setattr__(self, "quality_status", QualityStatus(self.quality_status))
        object.__setattr__(self, "curve", tuple(tuple(pair) for pair in self.curve))
        object.__setattr__(self, "decompositions", tuple(self.decompositions))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def measured(self) -> bool:
        """Whether any residual was measured at all."""
        return self.current_w is not None

    @property
    def kind(self) -> ResidualKind | None:
        """The sign of the current residual, or ``None`` when unmeasured."""
        return None if self.current_w is None else derived_residual_kind(self.current_w)

    def as_dict(self) -> dict[str, Any]:
        return {
            "current_w": self.current_w,
            "typical_w": self.typical_w,
            "baseline_w": self.baseline_w,
            "dynamic_w": self.dynamic_w,
            "kind": None if self.kind is None else self.kind.value,
            "measured": self.measured,
            "confidence": self.confidence,
            "resolved": self.resolved,
            "unresolved_since": (
                None if self.unresolved_since is None else self.unresolved_since.isoformat()
            ),
            "basis": MeasurementBasis(self.basis).value,
            "quality_status": QualityStatus(self.quality_status).value,
            "sample_count": self.sample_count,
            "reason_code": self.reason_code,
            "curve": [
                {"timestamp": instant.isoformat(), "residual_w": value}
                for instant, value in self.curve
            ],
            "decompositions": [item.as_dict() for item in self.decompositions],
            "evidence": dict(self.evidence),
        }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def persist(
    residuals: ResidualPower | Iterable[ResidualPower],
    db: Any,
    *,
    timestamp: datetime | None = None,
    reconciliation_id: int | None = None,
) -> int:
    """Write residuals through ``Database.insert_residual_power``.

    Returns the number of rows written.  The sign, the derived kind and the
    resolution state are written as computed -- nothing is rewritten on the way
    into storage.
    """
    items = [residuals] if isinstance(residuals, ResidualPower) else list(residuals)
    written = 0
    for residual in items:
        db.insert_residual_power(
            residual,
            timestamp=timestamp,
            reconciliation_id=reconciliation_id,
        )
        written += 1
    return written
