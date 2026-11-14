"""Refoss ×1/×2 factor semantics for a 240 V appliance metered on one leg.

The physical situation this module models
-----------------------------------------
A Refoss-style split-phase monitor clamps a CT around **one conductor** of a
240 V appliance.  That conductor carries only *that leg's* current: for a
balanced 240 V load the leg reading is half the appliance's total power.

Some monitors (and some users, in the WattMosaic UI) apply a **×2 factor** so
the published number is an *estimate of the whole appliance* rather than a
single-leg reading.  Three things can then be true at the same time and they
must never be conflated:

``conductor_w``
    What the CT physically sees on its one leg — ground truth, produced by the
    physics, never by the factor.
``reported_w``
    What the integration publishes.  It equals ``conductor_w`` multiplied by
    the *publish* multiplier in force at that instant: ×1 for a raw leg
    reading, ×2 when the monitor already doubled it.
``appliance_total_truth_w``
    The true two-leg appliance power.  It is a **separate number** that the
    factor does not get to define.  When the legs are unbalanced (or the factor
    is wrong) it legitimately disagrees with any factor-derived estimate.

So a factor is not "a float on a channel".  It is a claim, made by somebody
(``monitor``, ``user``, or nobody), with a validity interval, and with an
independent ground truth to disagree with.  ``FactorEpoch`` is that claim and
``FactorTimeline`` is the history of claims, which is what makes a
mid-history factor change reconstructable instead of retroactive.

Design rules enforced here
--------------------------
* A sample is resolved against the epoch that *covers its timestamp*.  Today's
  factor is never applied to a past reading (no epoch covering ``t`` ⇒ no
  declared factor at ``t``).
* ``published=True`` records that the monitor already multiplied the reading.
  Multiplying it again is a double application, expressible
  (``double_applied``) precisely so tests can prove it is refused.
* ``declared=False`` is "no factor metadata exists".  It is *not* the same as a
  deliberate ×1, and ``estimated_appliance_total_w`` returns ``None`` rather
  than inventing a total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from wattmosaic_engine.contracts import MeasurementFactor, Phase


class FactorOwner(str, Enum):
    """Who applied (or owns) the factor in force.

    ``MONITOR`` means the published value already includes the factor, so it
    must never be multiplied again.  ``USER`` means WattMosaic holds a user
    declaration that has *not* been applied to the published value yet.
    ``NONE`` means no factor exists — deliberately ×1 or simply unknown, told
    apart by :attr:`FactorEpoch.declared`.
    """

    MONITOR = "monitor"
    USER = "user"
    NONE = "none"


_OWNER_TO_CONTRACT = {
    FactorOwner.MONITOR: "monitor",
    FactorOwner.USER: "user",
    FactorOwner.NONE: "unknown",
}


@dataclass(frozen=True, slots=True)
class FactorEpoch:
    """One interval during which a factor was in force on one CT.

    ``start_s`` / ``end_s`` are simulated seconds, half-open
    (``[start_s, end_s)``) so a change at ``t`` is exactly expressible: the old
    epoch ends where the new one begins.  ``end_s=None`` means "still in force".
    """

    multiplier: float = 1.0
    """The declared factor.  Strictly positive and finite."""

    start_s: float = 0.0
    """Inclusive start of the interval, simulated seconds."""

    end_s: float | None = None
    """Exclusive end; ``None`` means open-ended."""

    applied_by: FactorOwner = FactorOwner.NONE
    """Who owns the factor in this interval."""

    published: bool = False
    """Whether the *monitor* already multiplied the conductor reading before
    publishing.  Independent of ownership: a user declaration layered on top of
    an already-published total is exactly the "incorrect metadata" case."""

    declared: bool = True
    """Whether any factor metadata exists at all.  ``False`` means unknown
    basis — not a deliberate ×1."""

    note: str = ""
    """Free text, e.g. ``"Refoss publishes the whole load"``."""

    def __post_init__(self) -> None:
        value = float(self.multiplier)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"factor multiplier must be finite and > 0, got {self.multiplier!r}")
        if self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError(
                f"factor interval must be non-empty: {self.start_s}..{self.end_s}"
            )

    def contains(self, t: float) -> bool:
        if t < self.start_s:
            return False
        return self.end_s is None or t < self.end_s

    @property
    def interval(self) -> tuple[float, float | None]:
        return (self.start_s, self.end_s)

    def as_measurement_factor(self, ct_id: str, start_time: datetime) -> MeasurementFactor:
        """Export this epoch as the engine contract's ``MeasurementFactor``.

        The bridge exists so a fixture can be fed to a production consumer
        without re-declaring the factor by hand.  ``published`` has no contract
        field of its own: a monitor-published total is expressed as
        ``applied_by="monitor"``, which is the contract's way of saying
        "already applied, do not multiply again".
        """
        effective_from = start_time + timedelta(seconds=float(self.start_s))
        effective_to = (
            None if self.end_s is None else start_time + timedelta(seconds=float(self.end_s))
        )
        return MeasurementFactor(
            ct_id=ct_id,
            multiplier=float(self.multiplier),
            effective_from=effective_from,
            effective_to=effective_to,
            applied_by=_OWNER_TO_CONTRACT[self.applied_by],
            note=self.note or None,
        )

    def as_metadata(self) -> dict[str, Any]:
        return {
            "multiplier": float(self.multiplier),
            "start_s": float(self.start_s),
            "end_s": None if self.end_s is None else float(self.end_s),
            "applied_by": self.applied_by.value,
            "published": bool(self.published),
            "declared": bool(self.declared),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class FactorTimeline:
    """The ordered history of factor claims for one CT.

    Epochs must not overlap.  A gap is meaningful: no metadata covers that
    instant, which is the "missing factor" case rather than a default of ×1.
    """

    epochs: tuple[FactorEpoch, ...] = ()

    def __post_init__(self) -> None:
        ordered = sorted(self.epochs, key=lambda e: (e.start_s, e.end_s is None))
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.end_s is None or later.start_s < earlier.end_s:
                raise ValueError(
                    "factor epochs must not overlap: "
                    f"{earlier.start_s}..{earlier.end_s} and {later.start_s}..{later.end_s}"
                )

    def epoch_at(self, t: float) -> FactorEpoch | None:
        for epoch in self.epochs:
            if epoch.contains(t):
                return epoch
        return None

    def factor_at(self, t: float) -> float:
        epoch = self.epoch_at(t)
        return 1.0 if epoch is None else float(epoch.multiplier)

    def applied_by_at(self, t: float) -> FactorOwner:
        epoch = self.epoch_at(t)
        return FactorOwner.NONE if epoch is None else epoch.applied_by

    def is_declared_at(self, t: float) -> bool:
        epoch = self.epoch_at(t)
        return bool(epoch is not None and epoch.declared)

    def published_multiplier_at(self, t: float) -> float:
        """The multiplier actually baked into the *published* value."""
        epoch = self.epoch_at(t)
        if epoch is None or not epoch.published:
            return 1.0
        return float(epoch.multiplier)

    def intervals(self) -> tuple[tuple[float, float | None], ...]:
        return tuple(e.interval for e in self.epochs)

    def as_metadata(self) -> list[dict[str, Any]]:
        return [e.as_metadata() for e in self.epochs]

    @classmethod
    def constant(
        cls,
        multiplier: float = 1.0,
        *,
        applied_by: FactorOwner = FactorOwner.NONE,
        published: bool = False,
        declared: bool = True,
        note: str = "",
    ) -> FactorTimeline:
        return cls(
            epochs=(
                FactorEpoch(
                    multiplier=multiplier,
                    start_s=0.0,
                    end_s=None,
                    applied_by=applied_by,
                    published=published,
                    declared=declared,
                    note=note,
                ),
            )
        )

    @classmethod
    def step(
        cls,
        change_at: float,
        before: FactorEpoch,
        after: FactorEpoch,
    ) -> FactorTimeline:
        """Two epochs split at ``change_at``; ``before`` is closed at the split."""
        if before.start_s != 0.0:
            raise ValueError("the first epoch of a step timeline must start at 0 s")
        if change_at <= before.start_s:
            raise ValueError("change_at must be after the first epoch's start")
        closed = FactorEpoch(
            multiplier=before.multiplier,
            start_s=before.start_s,
            end_s=change_at,
            applied_by=before.applied_by,
            published=before.published,
            declared=before.declared,
            note=before.note,
        )
        opened = FactorEpoch(
            multiplier=after.multiplier,
            start_s=change_at,
            end_s=after.end_s,
            applied_by=after.applied_by,
            published=after.published,
            declared=after.declared,
            note=after.note,
        )
        return cls(epochs=(closed, opened))


@dataclass(frozen=True, slots=True)
class RefossFactorChannel:
    """One CT on a single leg of a 240 V appliance, plus its factor history.

    The class deliberately keeps three quantities apart:

    * the **conductor reading** (physical, passed in — it is ground truth),
    * the **reported value** (``conductor_w * published multiplier``),
    * the **appliance total** (never derived here; supplied by the caller, so
      the estimate and the truth are free to disagree).

    ``leg_share`` is the fraction of the appliance total carried by this
    conductor: ``0.5`` for one leg of a balanced 240 V load.  It is used only by
    :meth:`appliance_leg_w`, i.e. when a caller wants a truth-shaped leg value
    from a device total; it never feeds :meth:`reported_w`.
    """

    ct_id: str
    """The CT measuring the leg."""

    device_id: str
    """The appliance the CT is associated with."""

    metered_leg: Phase
    """The phase conductor the CT is clamped on (``L1`` or ``L2``)."""

    timeline: FactorTimeline = field(default_factory=FactorTimeline)
    """The factor history for this CT."""

    leg_share: float = 0.5
    """Fraction of the appliance total carried by this conductor."""

    note: str = ""
    """Free text explaining the wiring, e.g. ``"only L1 is metered"``."""

    # -- factor resolution ------------------------------------------------- #
    def epoch_at(self, t: float) -> FactorEpoch | None:
        return self.timeline.epoch_at(t)

    def factor_at(self, t: float) -> float:
        return self.timeline.factor_at(t)

    def applied_by_at(self, t: float) -> FactorOwner:
        return self.timeline.applied_by_at(t)

    def is_declared_at(self, t: float) -> bool:
        return self.timeline.is_declared_at(t)

    def reported_multiplier_at(self, t: float) -> float:
        return self.timeline.published_multiplier_at(t)

    def double_applied_at(self, t: float) -> bool:
        """True when a user factor sits on top of a monitor-published total."""
        epoch = self.epoch_at(t)
        return bool(epoch is not None and epoch.published and epoch.applied_by is FactorOwner.USER)

    # -- values ------------------------------------------------------------ #
    def appliance_leg_w(self, appliance_total_w: float) -> float:
        """The appliance's own contribution to this conductor (truth-shaped)."""
        return float(appliance_total_w) * self.leg_share

    def reported_w(self, conductor_w: float, t: float) -> float:
        """What the integration publishes for this sample."""
        return float(conductor_w) * self.reported_multiplier_at(t)

    def estimated_appliance_total_w(self, conductor_w: float, t: float) -> float | None:
        """The two-leg total the declared metadata implies, or ``None``.

        ``None`` means "no stated basis": either no factor was ever declared
        (the missing-metadata case) or the reading is a bare leg measurement
        that cannot be promoted to an appliance total without a claim.
        """
        epoch = self.epoch_at(t)
        if epoch is None or not epoch.declared:
            return None
        reported = self.reported_w(conductor_w, t)
        if epoch.applied_by is FactorOwner.MONITOR and epoch.published:
            return reported  # the monitor already published the whole load
        return reported * float(epoch.multiplier)

    # -- frames ------------------------------------------------------------ #
    def observation(self, conductor_w: float, t: float, *, start_time: datetime | None = None) -> dict[str, Any]:
        """The observable frame: published value plus declared metadata."""
        epoch = self.epoch_at(t)
        frame: dict[str, Any] = {
            "ct_id": self.ct_id,
            "device_id": self.device_id,
            "metered_leg": self.metered_leg.value,
            "reported_w": round(self.reported_w(conductor_w, t), 6),
            "factor": self.factor_at(t),
            "applied_by": self.applied_by_at(t).value,
            "published": bool(epoch is not None and epoch.published),
            "declared": self.is_declared_at(t),
            "double_applied": self.double_applied_at(t),
            "interval_s": list(epoch.interval) if epoch is not None else None,
            "t_s": round(float(t), 4),
        }
        if start_time is not None:
            frame["state_timestamp"] = (
                start_time + timedelta(seconds=float(t))
            ).isoformat()
        return frame

    def truth(
        self,
        conductor_w: float,
        appliance_total_w: float,
        t: float,
    ) -> dict[str, Any]:
        """The ground-truth frame.  Never merged into :meth:`observation`."""
        return {
            "ct_id": self.ct_id,
            "conductor_truth_w": round(float(conductor_w), 6),
            "appliance_leg_truth_w": round(self.appliance_leg_w(appliance_total_w), 6),
            "appliance_total_truth_w": round(float(appliance_total_w), 6),
            "t_s": round(float(t), 4),
        }

    # -- interop ----------------------------------------------------------- #
    def as_measurement_factors(self, start_time: datetime) -> list[MeasurementFactor]:
        """The whole timeline as contract ``MeasurementFactor`` rows."""
        return [epoch.as_measurement_factor(self.ct_id, start_time) for epoch in self.timeline.epochs]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "ct_id": self.ct_id,
            "device_id": self.device_id,
            "metered_leg": self.metered_leg.value,
            "leg_share": float(self.leg_share),
            "note": self.note,
            "factor_intervals": self.timeline.as_metadata(),
        }


__all__ = [
    "FactorEpoch",
    "FactorOwner",
    "FactorTimeline",
    "RefossFactorChannel",
]
