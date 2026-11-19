"""Measurement-basis and factor semantics for a CT reading.

Why this module exists
----------------------
A Refoss-style monitor can clamp a CT around **one conductor** of a 240 V
appliance.  That conductor carries only one leg's current, so the published
number is either

* a genuine single-leg reading (a physical measurement of that conductor), or
* a *monitor-computed estimate of the whole appliance*, because the monitor
  already multiplied the leg by a factor, or
* a value the *user* still expects WattMosaic to multiply, because the
  multiplication was declared here and never applied by the device.

Those three cases produce the **same digits** and demand **different
arithmetic**.  A bare ``float`` cannot tell them apart, so the previous
single-scalar design could apply a factor correctly and still be unable to say
whether it should have.

Design rules, and the reason for each
-------------------------------------
1. **``applied_by`` decides the arithmetic, not the number.**

   * ``"monitor"`` -- the published value already includes the factor.  The
     engine applies **nothing**; multiplying again would double count.  Basis
     ``MONITOR_REPORTED``.
   * ``"user"`` -- the declaration has not been applied yet.  The engine applies
     the multiplier **exactly once**.  Basis ``FACTOR_ADJUSTED``.
   * ``"unknown"`` -- the semantics cannot be established.  Basis ``UNKNOWN``,
     confidence below 1, and no guess at all: neither multiplying nor silently
     "not multiplying as if we knew" is permitted.

2. **A factor is resolved against the interval that covers the sample.**  A
   reading is never scaled with today's declaration: a factor change is
   reconstructable and never retroactive.  A gap is *no metadata* -- it is not
   an implicit x1, because the existence of a history proves the channel is
   factor-managed.

3. **The basis is never inferred from ``phase``, ``ct_type``, ``name`` or
   ``nominal_voltage``.**  A double-pole 240 V channel is the exact case where
   inference would double count, so only explicit evidence decides.

4. **Every scaling site takes a raw value and applies the factor once.**
   :func:`scale_reading` is the one scaler: it multiplies a raw reading by
   :meth:`FactorRegistry.basis_for`'s ``applied_multiplier`` exactly once.

   The registry never mutates anything, so a value cannot be scaled twice by
   composition.  A caller that *chooses* to hand an already-scaled number to :func:`scale_reading` is
   violating the function's raw-input contract; ``applied_multiplier`` being
   the only multiplication factor is what makes double application detectable
   (it is recorded on every artifact).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from wattmosaic_engine.contracts import (
    MeasurementBasis,
    MeasurementFactor,
    validate_factor_intervals,
)

# --------------------------------------------------------------------------- #
# Confidence constants
# --------------------------------------------------------------------------- #

#: Confidence multiplier for "semantics established from explicit evidence".
ESTABLISHED_CONFIDENCE = 1.0

#: Confidence multiplier when ownership/interval semantics cannot be established
#: (``applied_by="unknown"``, missing metadata on a channel that needs it, or a
#: gap in a history).  Low but non-zero: the *reading* may still be a valid
#: conductor value, it is the interpretation that is unverified.
UNKNOWN_CONFIDENCE = 0.25

#: Confidence multiplier for self-contradictory metadata.  Zero, because no
#: arithmetic branch is supportable: applying the factor and skipping it both
#: report a wrong total.  Consumers must refuse rather than pick one.
CONFLICT_CONFIDENCE = 0.0

AppliedBy = Literal["monitor", "user", "unknown"]

#: Reason codes.  Short machine strings, stable for logs and audit records.
NO_FACTOR_DECLARED = "no_factor_declared"
FACTOR_MISSING = "factor_missing"
FACTOR_GAP = "factor_gap"
FACTOR_NOT_YET_DECLARED = "factor_not_yet_declared"
FACTOR_UNKNOWN_OWNERSHIP = "factor_unknown_ownership"
FACTOR_CONFLICT = "factor_conflict"
FACTOR_APPLIED_BY_USER = "factor_applied_by_user"
FACTOR_APPLIED_BY_MONITOR = "factor_applied_by_monitor"
FACTOR_DECLARED_ONE = "factor_declared_one"
FACTOR_ESTIMATED = "estimated_appliance_total"


def _aware(value: datetime) -> datetime:
    """Comparable datetime; a naive value is UTC, matching the contracts."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


# --------------------------------------------------------------------------- #
# One resolved sample
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FactorResolution:
    """What the metadata says about **one** reading, and what to do with it.

    ``applied_multiplier`` is the multiplier the *engine* still owes the raw
    value; ``reported_multiplier`` is the multiplier already baked into the
    value by the monitor.  They are never both non-1.0, which is what makes
    double application structurally impossible: only ``applied_multiplier`` is
    ever used by :func:`scale_reading`.
    """

    ct_id: str
    timestamp: datetime
    basis: MeasurementBasis
    factor: MeasurementFactor | None = None
    applied_multiplier: float = 1.0
    reported_multiplier: float = 1.0
    confidence: float = ESTABLISHED_CONFIDENCE
    reason_code: str = NO_FACTOR_DECLARED
    reason: str = ""
    has_metadata: bool = False
    conflict: bool = False
    applied_by: AppliedBy = "unknown"
    declared_by: str = "default"
    note: str | None = None

    @property
    def multiplier(self) -> float:
        """The declared multiplier, or 1.0 when no interval covers the sample."""
        return 1.0 if self.factor is None else float(self.factor.multiplier)

    @property
    def interval(self) -> tuple[datetime, datetime | None] | None:
        if self.factor is None:
            return None
        return (self.factor.effective_from, self.factor.effective_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ct_id": self.ct_id,
            "timestamp": self.timestamp.isoformat(),
            "basis": self.basis.value,
            "multiplier": self.multiplier,
            "applied_multiplier": self.applied_multiplier,
            "reported_multiplier": self.reported_multiplier,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "has_metadata": self.has_metadata,
            "conflict": self.conflict,
            "applied_by": self.applied_by,
            "declared_by": self.declared_by,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BasisSample:
    """The basis in force for one historical sample.

    Returned by :meth:`FactorRegistry.basis_history` so a caller can prove that
    a factor change is *reconstructed* per sample rather than applied
    retroactively.
    """

    timestamp: datetime
    basis: MeasurementBasis
    applied_multiplier: float = 1.0
    reported_multiplier: float = 1.0
    confidence: float = ESTABLISHED_CONFIDENCE
    reason_code: str = NO_FACTOR_DECLARED

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "basis": self.basis.value,
            "applied_multiplier": self.applied_multiplier,
            "reported_multiplier": self.reported_multiplier,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }


# --------------------------------------------------------------------------- #
# classify_basis: the only place a basis is derived
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Derivation:
    """The single derivation behind both :func:`classify_basis` and ``resolve``.

    Private on purpose: one function decides the basis *and* the arithmetic, so
    the two can never disagree.  ``classify_basis`` is the public projection
    that returns only the basis; ``FactorRegistry.resolve`` adds the CT, the
    timestamp, the interval and the explanation.
    """

    basis: MeasurementBasis
    applied_multiplier: float = 1.0
    reported_multiplier: float = 1.0
    confidence: float = ESTABLISHED_CONFIDENCE
    reason_code: str = NO_FACTOR_DECLARED
    reason: str = ""
    has_metadata: bool = False
    conflict: bool = False


def _derive(
    factor: MeasurementFactor | None,
    *,
    requires_factor: bool = False,
    conflict: str | None = None,
    estimated_appliance_total: bool = False,
    has_history: bool = False,
    before_history: bool = False,
) -> _Derivation:
    if conflict:
        return _Derivation(
            basis=MeasurementBasis.UNKNOWN,
            confidence=CONFLICT_CONFIDENCE,
            reason_code=FACTOR_CONFLICT,
            reason=(
                "factor metadata is self-contradictory "
                f"({conflict}); applying it and skipping it would each report "
                "a different, unsupportable total"
            ),
            conflict=True,
        )
    if factor is None:
        if estimated_appliance_total:
            return _Derivation(
                basis=MeasurementBasis.ESTIMATED_APPLIANCE_TOTAL,
                reason_code=FACTOR_ESTIMATED,
                reason="the value is an explicit model/prior appliance total, not a reading",
            )
        if requires_factor:
            # The channel is known to need a factor and no interval covers the
            # sample.  "Unknown" is the only honest answer: x1 would silently
            # halve a split load.
            if has_history and not before_history:
                return _Derivation(
                    basis=MeasurementBasis.UNKNOWN,
                    confidence=UNKNOWN_CONFIDENCE,
                    reason_code=FACTOR_GAP,
                    reason=(
                        "no factor interval covers this instant; the gap is "
                        "'no metadata', not a deliberate x1"
                    ),
                )
            if has_history:
                return _Derivation(
                    basis=MeasurementBasis.UNKNOWN,
                    confidence=UNKNOWN_CONFIDENCE,
                    reason_code=FACTOR_NOT_YET_DECLARED,
                    reason=(
                        "the sample predates the first factor interval, so no "
                        "declaration covers it"
                    ),
                )
            return _Derivation(
                basis=MeasurementBasis.UNKNOWN,
                confidence=UNKNOWN_CONFIDENCE,
                reason_code=FACTOR_MISSING,
                reason=(
                    "this CT is declared to need a factor (one leg of a split "
                    "load) but no factor interval exists; x1 would silently "
                    "report half the appliance"
                ),
            )
        if has_history and not before_history:
            # A history exists, so the channel *is* factor-managed; an instant
            # between two intervals is "no metadata", never an implicit x1.
            return _Derivation(
                basis=MeasurementBasis.UNKNOWN,
                confidence=UNKNOWN_CONFIDENCE,
                reason_code=FACTOR_GAP,
                reason=(
                    "no factor interval covers this instant; the gap is 'no "
                    "metadata', not a deliberate x1"
                ),
            )
        if has_history:
            return _Derivation(
                basis=MeasurementBasis.UNKNOWN,
                confidence=UNKNOWN_CONFIDENCE,
                reason_code=FACTOR_NOT_YET_DECLARED,
                reason=(
                    "the sample predates the first factor interval, so no "
                    "declaration covers it"
                ),
            )
        return _Derivation(
            basis=MeasurementBasis.CT_MEASURED,
            reason_code=NO_FACTOR_DECLARED,
            reason=(
                "no factor has ever been declared for this CT, so the reading "
                "is the conductor value as reported"
            ),
        )

    applied_by = str(factor.applied_by)
    multiplier = float(factor.multiplier)
    if applied_by == "unknown":
        return _Derivation(
            basis=MeasurementBasis.UNKNOWN,
            confidence=UNKNOWN_CONFIDENCE,
            reason_code=FACTOR_UNKNOWN_OWNERSHIP,
            reason=(
                "the factor interval does not record who applied it; "
                "multiplying and not multiplying both guess"
            ),
            has_metadata=True,
        )
    if applied_by == "monitor":
        # The monitor already published the whole load.  This holds even for a
        # declared x1, where the two answers coincide numerically.
        return _Derivation(
            basis=MeasurementBasis.MONITOR_REPORTED,
            applied_multiplier=1.0,
            reported_multiplier=multiplier,
            reason_code=FACTOR_APPLIED_BY_MONITOR,
            reason=(
                "the monitor already applied the factor and publishes the whole "
                "load; applying it again would double count"
            ),
            has_metadata=True,
        )
    if multiplier == 1.0:
        return _Derivation(
            basis=MeasurementBasis.CT_MEASURED,
            reason_code=FACTOR_DECLARED_ONE,
            reason="a factor is declared as x1: the reading is the conductor value",
            has_metadata=True,
        )
    return _Derivation(
        basis=MeasurementBasis.FACTOR_ADJUSTED,
        applied_multiplier=multiplier,
        reported_multiplier=1.0,
        reason_code=FACTOR_APPLIED_BY_USER,
        reason=(
            "a user declaration is in force and has not been applied to the "
            "published value, so the engine applies it exactly once"
        ),
        has_metadata=True,
    )


def classify_basis(
    factor: MeasurementFactor | None,
    *,
    requires_factor: bool = False,
    conflict: str | None = None,
    estimated_appliance_total: bool = False,
    has_history: bool = False,
    before_history: bool = False,
) -> MeasurementBasis:
    """Derive a :class:`MeasurementBasis` from **explicit evidence only**.

    Arguments are facts, never hints: ``phase``, ``ct_type``, ``name`` and
    ``nominal_voltage`` are deliberately absent from the signature so a caller
    cannot smuggle in an inference.

    ``factor``
        The interval covering the sample, or ``None``.  ``None`` with no other
        evidence means "plain conductor reading" -- ``CT_MEASURED``.
    ``requires_factor``
        The channel is *declared* to need a factor (e.g. a CT on one leg of a
        split load).  ``None`` factor then means "metadata missing", not "x1".
    ``conflict``
        A named self-contradiction (e.g. the same factor declared user-applied
        *and* already published).  Any conflict is ``UNKNOWN``.
    ``estimated_appliance_total``
        The value is an explicit model/prior appliance total, not a reading.
    ``has_history``
        The channel has *some* declared interval history, even though none
        covers this sample.  ``factor=None`` then means "gap", not "x1", which
        is exactly what :meth:`FactorRegistry.resolve` concludes.
    ``before_history``
        The sample precedes the first interval.  Only meaningful with
        ``has_history``; it selects the ``factor_not_yet_declared`` reason.

    This is a projection of the private ``_derive``, so it can never disagree
    with the arithmetic in :meth:`FactorRegistry.resolve` -- provided the caller
    passes the same facts.  :meth:`FactorRegistry.classify` exists so a caller
    holding a registry does not have to restate them by hand.
    """
    return _derive(
        factor,
        requires_factor=requires_factor,
        conflict=conflict,
        estimated_appliance_total=estimated_appliance_total,
        has_history=has_history,
        before_history=before_history,
    ).basis



# --------------------------------------------------------------------------- #
# FactorRegistry
# --------------------------------------------------------------------------- #


def _coerce_factor(item: MeasurementFactor | Mapping[str, Any]) -> MeasurementFactor:
    """Accept a contract model or a raw DB row; never invent a value."""
    if isinstance(item, MeasurementFactor):
        return item
    from_row = dict(item)
    effective_from = from_row["effective_from"]
    effective_to = from_row.get("effective_to")
    return MeasurementFactor(
        ct_id=str(from_row["ct_id"]),
        multiplier=float(from_row["multiplier"]),
        effective_from=(
            effective_from
            if isinstance(effective_from, datetime)
            else datetime.fromisoformat(str(effective_from))
        ),
        effective_to=(
            None
            if effective_to is None
            else (effective_to if isinstance(effective_to, datetime) else datetime.fromisoformat(str(effective_to)))
        ),
        applied_by=from_row.get("applied_by", "unknown"),
        declared_by=from_row.get("declared_by", "user"),
        note=from_row.get("note"),
    )


@dataclass(slots=True)
class FactorRegistry:
    """The factor intervals of every CT, indexed for as-of-timestamp lookups.

    Build from an iterable of :class:`MeasurementFactor` (or the dicts returned
    by ``Database.factors_for_ct``).  Overlapping intervals for one CT are
    refused at construction: with two covering intervals a lookup would have to
    pick one arbitrarily, and a historical reconstruction would then be a
    guess.

    ``requires_factor`` names channels that are *declared* to need a factor
    (a CT on one leg of a split load).  For those, a timestamp with no covering
    interval is ``UNKNOWN`` rather than a silent x1.

    ``conflicts`` maps a CT to a named self-contradiction in its metadata.  A
    conflicting interval is never used arithmetically.
    """

    factors: Sequence[MeasurementFactor] = field(default_factory=tuple)
    requires_factor: frozenset[str] = frozenset()
    conflicts: dict[str, str] = field(default_factory=dict)
    _by_ct: dict[str, tuple[MeasurementFactor, ...]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        rows = [_coerce_factor(item) for item in self.factors]
        validate_factor_intervals(rows)
        by_ct: dict[str, list[MeasurementFactor]] = {}
        for row in rows:
            by_ct.setdefault(str(row.ct_id), []).append(row)
        for items in by_ct.values():
            items.sort(key=lambda f: _aware(f.effective_from))
        self.factors = tuple(rows)
        self.requires_factor = frozenset(str(ct) for ct in self.requires_factor)
        self.conflicts = {str(ct): str(reason) for ct, reason in dict(self.conflicts).items()}
        self._by_ct = {ct: tuple(items) for ct, items in by_ct.items()}

    # -- construction ------------------------------------------------------ #
    @classmethod
    def from_database(cls, database: Any, ct_ids: Iterable[str]) -> FactorRegistry:
        """Read every interval for ``ct_ids`` through the storage helper.

        The database is only a source of rows: the interval invariant is
        re-validated by the constructor, so a corrupt store still cannot make
        the registry answer with an overlapping interval.
        """
        rows: list[MeasurementFactor] = []
        for ct_id in ct_ids:
            rows.extend(_coerce_factor(row) for row in database.factors_for_ct(str(ct_id)))
        return cls(rows)

    # -- queries ----------------------------------------------------------- #
    def has_history(self, ct_id: str) -> bool:
        return bool(self._by_ct.get(str(ct_id)))

    def factor_at(self, ct_id: str, timestamp: datetime) -> MeasurementFactor | None:
        """The interval covering ``timestamp``, or ``None``.

        ``None`` means "no metadata at this instant" -- never "the next
        interval" and never an implicit x1.  This is the single guarantee that
        makes historical reconstruction honest.
        """
        moment = _aware(timestamp)
        for factor in self._by_ct.get(str(ct_id), ()):
            start = _aware(factor.effective_from)
            end = None if factor.effective_to is None else _aware(factor.effective_to)
            if moment < start:
                # Ordered by start: nothing later can cover an earlier instant.
                return None
            if end is None or moment < end:
                return factor
        return None

    def resolve(self, ct_id: str, timestamp: datetime) -> FactorResolution:
        """Everything the metadata says about one sample, including the *why*.

        The basis and the arithmetic both come from the single private
        ``_derive`` used by :func:`classify_basis`, so the two can never
        disagree about whether a factor was applied.
        """
        ct = str(ct_id)
        moment = _aware(timestamp)
        conflict_reason = self.conflicts.get(ct)
        factor = None if conflict_reason else self.factor_at(ct, moment)
        requires = ct in self.requires_factor
        history = self._by_ct.get(ct, ())
        before_history = bool(
            factor is None and history and moment < _aware(history[0].effective_from)
        )
        derivation = _derive(
            factor,
            requires_factor=requires,
            conflict=conflict_reason,
            has_history=bool(history),
            before_history=before_history,
        )
        applied_by: AppliedBy = (
            "unknown" if factor is None else str(factor.applied_by)  # type: ignore[assignment]
        )
        return FactorResolution(
            ct_id=ct,
            timestamp=moment,
            basis=derivation.basis,
            factor=factor,
            applied_multiplier=derivation.applied_multiplier,
            reported_multiplier=derivation.reported_multiplier,
            confidence=derivation.confidence,
            reason_code=derivation.reason_code,
            reason=derivation.reason,
            has_metadata=derivation.has_metadata,
            conflict=derivation.conflict,
            applied_by=applied_by,
            declared_by="default" if factor is None else str(factor.declared_by),
            note=None if factor is None else factor.note,
        )


    def basis_at(self, ct_id: str, timestamp: datetime) -> MeasurementBasis:
        return self.resolve(ct_id, timestamp).basis

    def facts_at(self, ct_id: str, timestamp: datetime) -> dict[str, Any]:
        """The evidence :func:`classify_basis` needs for this CT and instant.

        The point of this accessor is that a caller *cannot* restate the facts
        differently from :meth:`resolve`: the dictionary is built from the same
        lookup, so ``classify_basis(**registry.facts_at(...))`` is the same
        arithmetic by construction.  A gap and a pre-history sample both carry
        ``factor=None`` with ``has_history=True``, which is what turns them into
        ``UNKNOWN`` instead of a silent x1.
        """
        ct = str(ct_id)
        moment = _aware(timestamp)
        conflict_reason = self.conflicts.get(ct)
        history = self._by_ct.get(ct, ())
        factor = None if conflict_reason else self.factor_at(ct, moment)
        return {
            "factor": factor,
            "requires_factor": ct in self.requires_factor,
            "conflict": conflict_reason,
            "estimated_appliance_total": False,
            "has_history": bool(history),
            "before_history": bool(
                factor is None and history and moment < _aware(history[0].effective_from)
            ),
        }

    def classify(self, ct_id: str, timestamp: datetime) -> MeasurementBasis:
        """``classify_basis`` specialised to this registry, with its facts."""
        return classify_basis(**self.facts_at(ct_id, timestamp))

    def applied_multiplier_at(self, ct_id: str, timestamp: datetime) -> float:
        """The multiplier the **engine** must apply (1.0 when it applies none)."""
        return self.resolve(ct_id, timestamp).applied_multiplier

    def reported_multiplier_at(self, ct_id: str, timestamp: datetime) -> float:
        """The multiplier already reflected in the **reported** value."""
        return self.resolve(ct_id, timestamp).reported_multiplier

    def confidence_factor_at(self, ct_id: str, timestamp: datetime) -> float:
        return self.resolve(ct_id, timestamp).confidence

    def basis_for(
        self, ct_id: str, timestamp: datetime
    ) -> tuple[MeasurementBasis, float, float]:
        """``(basis, applied_multiplier, confidence_factor)`` -- WP5/WP7 contract.

        The frozen interface returns the multiplier the *engine* still owes, so
        a consumer that multiplies by it can never re-apply a monitor factor.
        """
        resolution = self.resolve(ct_id, timestamp)
        return (resolution.basis, resolution.applied_multiplier, resolution.confidence)

    def describe(self, ct_id: str, timestamp: datetime) -> dict[str, Any]:
        """A complete, explainable record of one resolution."""
        resolution = self.resolve(ct_id, timestamp)
        interval = resolution.interval
        return {
            **resolution.as_dict(),
            "interval": (
                None
                if interval is None
                else {"from": interval[0].isoformat(), "to": None if interval[1] is None else interval[1].isoformat()}
            ),
            "requires_factor": resolution.ct_id in self.requires_factor,
            "why": resolution.reason,
        }

    def basis_history(
        self, ct_id: str, timestamps: Iterable[datetime]
    ) -> list[BasisSample]:
        """The basis in force at each sample, in the order supplied.

        Reconstructing a history therefore uses the interval that covered each
        sample: a later factor change cannot rewrite an earlier reading.
        """
        out: list[BasisSample] = []
        for timestamp in timestamps:
            resolution = self.resolve(ct_id, timestamp)
            out.append(
                BasisSample(
                    timestamp=resolution.timestamp,
                    basis=resolution.basis,
                    applied_multiplier=resolution.applied_multiplier,
                    reported_multiplier=resolution.reported_multiplier,
                    confidence=resolution.confidence,
                    reason_code=resolution.reason_code,
                )
            )
        return out


# --------------------------------------------------------------------------- #
# scale_reading: the single scaler
# --------------------------------------------------------------------------- #


def scale_reading(
    reading_value: float | None,
    ct_id: str,
    timestamp: datetime,
    registry: FactorRegistry,
) -> tuple[float | None, float, MeasurementBasis]:
    """Scale one **raw** reading and report what was applied.

    This is the only place a factor changes a value.  The function takes the
    raw reading (never a previously scaled one) and returns
    ``(scaled_value, multiplier_applied, basis)``.

    Idempotence is structural rather than defensive: the registry is immutable,
    nothing is written back, and only ``applied_multiplier`` -- which is always
    ``1.0`` when the monitor already applied the factor -- is ever used.  A
    caller that passed the result back in would be passing a value it already
    treated as published; the engine instead always hands in the raw sample.
    """
    resolution = registry.resolve(ct_id, timestamp)
    if reading_value is None:
        return (None, 1.0, resolution.basis)
    applied = resolution.applied_multiplier
    scaled = reading_value if applied == 1.0 else float(reading_value) * applied
    return (scaled, applied, resolution.basis)


__all__ = [
    "CONFLICT_CONFIDENCE",
    "ESTABLISHED_CONFIDENCE",
    "UNKNOWN_CONFIDENCE",
    "BasisSample",
    "FactorRegistry",
    "FactorResolution",
    "classify_basis",
    "scale_reading",
]
