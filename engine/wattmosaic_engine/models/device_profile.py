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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..contracts import (
    DeviceModelParameters,
    ExpectedPower,
    LearningSample,
    LoadShape,
    ModelType,
    PowerLevel,
)
from .base import _linear_interp, enforce_monotonic, finite

#: The four levels in their natural order: lowest (off but connected) to
#: highest (doing work), with the transient last because it is not a level.
LEVEL_ORDER: tuple[PowerLevel, ...] = (
    PowerLevel.STANDBY,
    PowerLevel.IDLE,
    PowerLevel.ACTIVE,
    PowerLevel.TRANSIENT,
)


@dataclass(slots=True, frozen=True)
class AdjustableResponse:
    """A declared ``control level -> watts`` curve for any adjustable load.

    This is the generic form of the dimmable-light response curve.  It is *not*
    limited to lighting: any device whose load shape is
    :attr:`~wattmosaic_engine.contracts.LoadShape.ADJUSTABLE` and whose power
    follows a continuous control value (a speed, a position, a duty cycle, a
    valve opening, ...) can declare a set of measured ``(level, watts)`` points
    through this model.

    The class reuses the very same machinery the dimmable model is built on --
    :func:`~wattmosaic_engine.models.base.enforce_monotonic` (PAVA) and
    :func:`~wattmosaic_engine.models.base._linear_interp` -- so a non-linear
    response is represented without duplicating that logic and without any
    dimmable-light-specific attribute names.  The control variable is a plain
    declared string, matched against a context attribute of the same name (or
    the generic ``control_value``/``control_variable`` pair).  No device or
    entity name is ever consulted.

    Design rules:

    * ``points`` is a *declared* curve.  It is sorted and made monotonic only
      when ``monotonic=True`` is declared; otherwise the declared shape is kept
      exactly (a non-monotonic device is legitimate and must not be
      "corrected" into a false claim).
    * :meth:`power_at` interpolates linearly between declared points and clamps
      beyond the range -- it never extrapolates.
    * All fields are neutral by default: an empty curve answers ``None``, and a
      lookup that cannot be answered returns ``None`` rather than a fabricated
      number.
    """

    points: tuple[tuple[float, float], ...] = ()
    control_variable: str = "level"
    monotonic: bool = True
    #: A declared label for the curve, carried through to diagnostics.
    name: str = "adjustable"

    @classmethod
    def from_declared(
        cls,
        points: Sequence[tuple[float, float]],
        *,
        control_variable: str = "level",
        monotonic: bool = True,
        name: str = "adjustable",
    ) -> AdjustableResponse:
        """Build a response from declared ``(level, watts)`` points.

        A declared curve is taken at face value: only finite points are kept,
        duplicates are collapsed to their mean, and points are sorted by level.
        Monotonicity is enforced only when the declaration says the device is
        monotonic; a non-monotonic device keeps its declared (non-linear) shape.
        """
        clean = [
            (float(x), float(y))
            for x, y in points
            if finite(x) is not None and finite(y) is not None
        ]
        clean.sort(key=lambda pair: pair[0])
        collapsed: list[tuple[float, float]] = []
        for x, y in clean:
            if collapsed and abs(collapsed[-1][0] - x) < 1e-9:
                px, py = collapsed[-1]
                collapsed[-1] = (px, (py + y) / 2.0)
            else:
                collapsed.append((x, y))
        curve = enforce_monotonic(collapsed) if monotonic else collapsed
        return cls(
            points=tuple(curve),
            control_variable=control_variable,
            monotonic=monotonic,
            name=name,
        )

    @classmethod
    def from_samples(
        cls,
        samples: Sequence[LearningSample],
        *,
        control_variable: str = "level",
        monotonic: bool = True,
        name: str = "adjustable",
    ) -> AdjustableResponse:
        """Build a response from accepted learning samples for one control axis.

        Samples whose control variable does not match (or that carry no usable
        control value / power) are ignored, so a partially-labelled dataset
        yields a curve from just the declared axis rather than a blended guess.
        """
        desired = control_variable.strip().lower()
        points: list[tuple[float, float]] = []
        for sample in samples:
            if not sample.accepted:
                continue
            variable = (sample.control_variable or "").strip().lower()
            if variable != desired:
                continue
            x, y = finite(sample.control_value), finite(sample.event_power_w)
            if x is not None and y is not None:
                points.append((x, y))
        return cls.from_declared(
            points,
            control_variable=control_variable,
            monotonic=monotonic,
            name=name,
        )

    @property
    def model_type(self) -> ModelType:
        """The model implementation axis this response belongs to."""
        return ModelType.DIMMABLE

    def as_parameters(self) -> DeviceModelParameters:
        """The curve in the contract's shared ``DeviceModelParameters`` shape."""
        return DeviceModelParameters(curve=[(x, y) for x, y in self.points])

    def power_at(self, level: float | None) -> float | None:
        """Interpolate the declared curve at ``level`` (clamped, never extrapolated).

        ``None`` (no level reported) is answered with ``None``: a level that was
        not reported is not "full scale", and reporting a number would fabricate
        one.
        """
        x = finite(level)
        if x is None or not self.points:
            return None
        predicted = _linear_interp(x, self.points)
        return float(predicted) if predicted is not None else None

    def is_monotonic(self) -> bool:
        """True when the declared response is non-decreasing across its points."""
        watts = [y for _, y in self.points]
        return all(watts[i] <= watts[i + 1] + 1e-9 for i in range(len(watts) - 1))

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_type": self.model_type.value,
            "load_shape": LoadShape.ADJUSTABLE.value,
            "control_variable": self.control_variable,
            "monotonic_declared": self.monotonic,
            "monotonic": self.is_monotonic(),
            "curve_points": len(self.points),
            "curve": [[x, y] for x, y in self.points],
        }


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
    #: An optional declared adjustable response curve.  ``None`` means the
    #: device declared no control-to-power relationship; the profile then
    #: answers no adjustable question, exactly as before this field existed.
    #: Attaching one never changes the standby/idle/active/transient lookups.
    adjustable_response: AdjustableResponse | None = None

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
    def for_adjustable(
        cls,
        device_id: str,
        load_shape: LoadShape,
        points: Sequence[tuple[float, float]],
        *,
        expected: ExpectedPower | None = None,
        control_variable: str = "level",
        monotonic: bool = True,
    ) -> DevicePowerProfile:
        """Build a profile with a declared adjustable response.

        The response is attached **only** when the load shape is explicitly
        :attr:`LoadShape.ADJUSTABLE`.  That is the declaration gate: a device
        whose shape is undeclared (``UNKNOWN``) or any other shape keeps a
        neutral profile with no response, even if its name suggests otherwise.
        Nothing here reads a device or entity name.
        """
        profile = (
            cls.from_expected(device_id, expected)
            if expected is not None
            else cls(device_id=device_id)
        )
        if load_shape != LoadShape.ADJUSTABLE:
            return profile
        response = AdjustableResponse.from_declared(
            points,
            control_variable=control_variable,
            monotonic=monotonic,
        )
        return profile.with_adjustable_response(response)

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

    # -- adjustable response ---------------------------------------------- #
    def with_adjustable_response(self, response: AdjustableResponse) -> DevicePowerProfile:
        """Return a copy of this profile carrying a declared adjustable response.

        A pure, additive attachment: the standby/idle/active/transient levels and
        all existing lookups are unchanged.  The copy keeps the profile frozen
        and leaves every other caller's profile untouched.
        """
        return replace(self, adjustable_response=response)

    def power_at_level(self, level: float | None) -> float | None:
        """Expected watts at a control level, or ``None`` when not declared."""
        if self.adjustable_response is None:
            return None
        return self.adjustable_response.power_at(level)

    def is_adjustable(self) -> bool:
        """True when a declared adjustable response is attached."""
        return self.adjustable_response is not None

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "device_id": self.device_id,
            "standby_w": self.standby_w,
            "idle_w": self.idle_w,
            "active_w": self.active_w,
            "transient_w": self.transient_w,
            "tolerance_w": self.tolerance_w,
        }
        if self.adjustable_response is not None:
            data["adjustable_response"] = self.adjustable_response.describe()
        return data


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
    "AdjustableResponse",
    "DevicePowerProfile",
]
