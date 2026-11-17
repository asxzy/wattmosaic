"""The device power profile: standby, idle, active and transient.

A device's power is not one number.  ``ExpectedPower`` (``contracts.py``) is
the contract's *declared envelope*; this module is the engine-side object that
turns it into one explicit profile and answers the question a published value
needs answered: **which level is this?**

Design rules (spec ``06-device-and-load-model-spec.md`` §5):

* Every level is optional.  ``None`` means *not stated*, never zero.
* A published value names its level via :class:`PowerLevel`.  ``UNKNOWN`` is
  the neutral answer, so a profile that cannot place a value does not fabricate
  one.
* A transient (start-up inrush / shutdown decay) is **not** an operating level
  and is never averaged into a steady-state fit or the energy integral.  The
  profile exposes :meth:`DevicePowerProfile.operating_w` so a caller can pick
  a steady-state number without silently including it.
* The profile is built from a declaration only.  Nothing here reads a device
  name, an entity id or any other naming surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import ExpectedPower, PowerLevel

#: The four levels in their natural order: lowest (off but connected) to
#: highest (doing work), with the transient last because it is not a level.
LEVEL_ORDER: tuple[PowerLevel, ...] = (
    PowerLevel.STANDBY,
    PowerLevel.IDLE,
    PowerLevel.ACTIVE,
    PowerLevel.TRANSIENT,
)


@dataclass(slots=True, frozen=True)
class DevicePowerProfile:
    """Standby, idle, active and transient power for one device.

    Sourced from :class:`~wattmosaic_engine.contracts.ExpectedPower`.  Every
    field is ``float | None``; ``None`` means the declaration did not state it,
    which is distinct from ``0.0`` (a stated zero draw).

    ``transient_w`` is the start-up inrush bound (``startup_w``) when stated,
    else the shutdown decay bound (``shutdown_w``).  It is kept separate from
    the three operating levels precisely so it can be excluded from a fit or an
    integral.
    """

    device_id: str
    standby_w: float | None = None
    idle_w: float | None = None
    active_w: float | None = None
    transient_w: float | None = None
    #: Absolute tolerance for matching a measurement to ``active_w``.
    tolerance_w: float = 2.0

    # -- constructors ----------------------------------------------------- #
    @classmethod
    def from_expected(cls, device_id: str, expected: ExpectedPower) -> DevicePowerProfile:
        """Build a profile from a declared ``ExpectedPower`` envelope.

        The source is the contract's declaration, never an inference: this
        constructor reads only the numbers the user (or an import) stated.
        """
        return cls(
            device_id=device_id,
            standby_w=expected.standby_w,
            idle_w=expected.idle_w,
            active_w=expected.on_w,
            transient_w=cls._transient(expected),
            tolerance_w=expected.tolerance(),
        )

    @staticmethod
    def _transient(expected: ExpectedPower) -> float | None:
        """The transient bound: the start-up inrush, else the shutdown decay."""
        if expected.startup_w is not None:
            return expected.startup_w
        return expected.shutdown_w

    @classmethod
    def from_mapping(cls, device_id: str, data: Mapping[str, Any]) -> DevicePowerProfile:
        """Build a profile from a plain mapping (e.g. a persisted record).

        Unknown keys are ignored and every value is optional, so a partial
        record yields a partial profile rather than a failure.
        """
        return cls(
            device_id=device_id,
            standby_w=_opt_float(data.get("standby_w")),
            idle_w=_opt_float(data.get("idle_w")),
            active_w=_opt_float(data.get("active_w", data.get("on_w"))),
            transient_w=_opt_float(
                data.get("transient_w", data.get("startup_w", data.get("shutdown_w")))
            ),
            tolerance_w=float(data.get("tolerance_w") or 2.0),
        )

    # -- level lookup ----------------------------------------------------- #
    def level_for(self, power_w: float | None) -> PowerLevel:
        """Name the level a value sits at, or ``UNKNOWN`` when it matches none.

        The comparison uses the profile's tolerance and prefers the **lowest**
        matching level, so a 5 W reading against a profile with a 5 W standby
        and a 200 W active is ``STANDBY`` rather than ``ACTIVE``.  A value that
        matches nothing is ``UNKNOWN`` -- the honest answer, not a guess.
        """
        if power_w is None:
            return PowerLevel.UNKNOWN
        for level in LEVEL_ORDER:
            stated = self.level_w(level)
            if stated is not None and abs(float(power_w) - stated) <= self.tolerance_w:
                return level
        return PowerLevel.UNKNOWN

    def level_w(self, level: PowerLevel) -> float | None:
        """The stated watts for one level, or ``None`` when not stated."""
        if level is PowerLevel.STANDBY:
            return self.standby_w
        if level is PowerLevel.IDLE:
            return self.idle_w
        if level is PowerLevel.ACTIVE:
            return self.active_w
        if level is PowerLevel.TRANSIENT:
            return self.transient_w
        return None

    # -- steady state ----------------------------------------------------- #
    def operating_w(self) -> float | None:
        """A steady-state operating level: active, else idle, else standby.

        The transient is deliberately excluded: averaging an inrush into a
        steady-state number is the defect this method exists to prevent.  The
        fallback order is the device's own hierarchy -- an idle device is
        closer to its idle draw than to its standby draw.
        """
        for value in (self.active_w, self.idle_w, self.standby_w):
            if value is not None:
                return float(value)
        return None

    def minimum_w(self) -> float | None:
        """The lowest stated draw (standby when declared), never invented."""
        if self.standby_w is not None:
            return float(self.standby_w)
        if self.idle_w is not None:
            return float(self.idle_w)
        return None

    def maximum_w(self) -> float | None:
        """The largest stated bound, including the transient.

        This is an upper bound on what the device may draw, so the inrush is
        in scope here even though it is out of scope for ``operating_w``.
        """
        candidates = [
            float(v) for v in (self.active_w, self.idle_w, self.transient_w) if v is not None
        ]
        return max(candidates) if candidates else None

    def is_steady_state(self, power_w: float | None) -> bool:
        """True when ``power_w`` is a steady-state level, never a transient."""
        level = self.level_for(power_w)
        return level is not PowerLevel.TRANSIENT and level is not PowerLevel.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "standby_w": self.standby_w,
            "idle_w": self.idle_w,
            "active_w": self.active_w,
            "transient_w": self.transient_w,
            "tolerance_w": self.tolerance_w,
        }


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0.0 else None


#: The transient level, named once so a caller cannot forget to exclude it.
TRANSIENT_LEVEL = PowerLevel.TRANSIENT


__all__ = [
    "LEVEL_ORDER",
    "TRANSIENT_LEVEL",
    "DevicePowerProfile",
]
