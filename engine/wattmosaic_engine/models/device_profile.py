"""The device power profile: standby, idle, active and transient.

A device's power is not one number.  ``ExpectedPower`` (``contracts.py``) is
the contract's *declared envelope*; this module is the engine-side object that
turns it into one explicit profile and answers the question a published value
needs answered: **which level is this?**

Design rules (spec ``06-device-and-load-model-spec.md`` §5):

* Every level is optional.  ``None`` means *not stated*, never zero.
* A published value names its level via :class:`PowerLevelV3`.  ``UNKNOWN`` is
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
    AdjustableSpec,
    ExpectedPower,
    FixedSpec,
    LearningObservation,
    LevelStat,
    LoadShape,
    ModelType,
    PowerLevelV3,
)
from .base import _linear_interp, enforce_monotonic, finite

#: The four levels in their natural order: lowest (off but connected) to
#: highest (doing work), with the transient last because it is not a level.
LEVEL_ORDER: tuple[PowerLevelV3, ...] = (
    PowerLevelV3.STANDBY,
    PowerLevelV3.IDLE,
    PowerLevelV3.ACTIVE,
    PowerLevelV3.TRANSIENT,
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
        samples: Sequence[LearningObservation],
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
            if sample.status != "confirmed":
                continue
            variable = str(sample.attributes.get("control_variable") or "").strip().lower()
            if variable != desired:
                continue
            x = finite(sample.attributes.get("control_value"))
            y = finite(sample.level_w if sample.basis == "ABSOLUTE_ANCHOR" else sample.delta_w)
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

    def as_parameters(self) -> AdjustableSpec:
        """The declared curve as a v3 :class:`AdjustableSpec`."""
        return AdjustableSpec(
            control_variable=self.control_variable,
            knots_x=[x for x, _ in self.points],
            knots_w=[y for _, y in self.points],
            monotone=self.monotonic,
        )

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
    #: A declared response delay in seconds (OD-47).  ``None`` means "no delay
    #: declared", the neutral default that keeps the shipped immediate-response
    #: behaviour.  A positive value defers the electrical response to a state
    #: change.  This is orthogonal to the power law / load shape.
    response_delay_s: float | None = None
    #: A declared standing load with no OFF state (OD-46).  ``False`` is the
    #: neutral default (a normal device with a standby/off transition).  This is
    #: orthogonal to the power law / load shape.
    always_on: bool = False

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
            response_delay_s=_opt_float(data.get("response_delay_s")),
            always_on=bool(data.get("always_on", False)),
        )

    # -- level lookup ----------------------------------------------------- #
    def level_for(self, power_w: float | None) -> PowerLevelV3:
        """Name the level a value sits at, or ``UNKNOWN`` when it matches none.

        The comparison uses the profile's tolerance and prefers the **lowest**
        matching level, so a 5 W reading against a profile with a 5 W standby
        and a 200 W active is ``STANDBY`` rather than ``ACTIVE``.  A value that
        matches nothing is ``UNKNOWN`` -- the honest answer, not a guess.
        """
        if power_w is None:
            return PowerLevelV3.UNKNOWN
        for level in LEVEL_ORDER:
            stated = self.level_w(level)
            if stated is not None and abs(float(power_w) - stated) <= self.tolerance_w:
                return level
        return PowerLevelV3.UNKNOWN

    def level_w(self, level: PowerLevelV3) -> float | None:
        """The stated watts for one level, or ``None`` when not stated."""
        if level is PowerLevelV3.STANDBY:
            return self.standby_w
        if level is PowerLevelV3.IDLE:
            return self.idle_w
        if level is PowerLevelV3.ACTIVE:
            return self.active_w
        if level is PowerLevelV3.TRANSIENT:
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
        return level is not PowerLevelV3.TRANSIENT and level is not PowerLevelV3.UNKNOWN

    # -- adjustable response ---------------------------------------------- #
    def with_adjustable_response(self, response: AdjustableResponse) -> DevicePowerProfile:
        """Return a copy of this profile carrying a declared adjustable response.

        A pure, additive attachment: the standby/idle/active/transient levels and
        all existing lookups are unchanged.  The copy keeps the profile frozen
        and leaves every other caller's profile untouched.
        """
        return replace(self, adjustable_response=response)

    def with_response_delay(self, delay_s: float | None) -> DevicePowerProfile:
        """Return a copy declaring a response delay (OD-47), orthogonally.

        Adding or changing the delay never alters the power levels, the load
        shape or any other caller's profile.  ``None`` (or a non-positive value)
        is the neutral "no delay" declaration.
        """
        return replace(self, response_delay_s=_non_negative_delay(delay_s))

    def with_always_on(self, always_on: bool = True) -> DevicePowerProfile:
        """Return a copy declaring an always-on standing load (OD-46).

        Orthogonal to the power levels and load shape; ``False`` restores the
        normal off-transition behaviour.  Other callers' profiles are untouched.
        """
        return replace(self, always_on=bool(always_on))

    # -- orthogonal response ---------------------------------------------- #
    def activation_power_w(self, elapsed_s: float | None) -> float | None:
        """Power after an ON transition, suppressed until ``response_delay_s``.

        Before the delay the device still draws standby; after it, active (with
        idle/operating fallbacks).  With the default zero delay this is active
        immediately -- the shipped behaviour.
        """
        return power_after(
            self.standby_w,
            self.operating_w(),
            delay_s=self.response_delay_s,
            elapsed_s=elapsed_s,
        )

    def deactivation_power_w(self, elapsed_s: float | None) -> float | None:
        """Power after an OFF transition, suppressed until ``response_delay_s``.

        Before the delay the device still draws its operating level; after it,
        the value from :meth:`off_transition_w` (standby for a normal device, the
        standing load for an always-on one).
        """
        return power_after(
            self.operating_w(),
            self.off_transition_w(),
            delay_s=self.response_delay_s,
            elapsed_s=elapsed_s,
        )

    def off_transition_w(self) -> float | None:
        """Watts on an OFF transition: standby, or the standing load if always on.

        An always-on device never falls to its standby/off level here; it keeps
        its declared active draw (falling back to standby when none is stated).
        """
        return off_w(self.standby_w, self.active_w, always_on=self.always_on)

    def is_always_on(self) -> bool:
        """True when this profile declares no off-state."""
        return self.always_on

    def power_at_level(self, level: float | None) -> float | None:
        """Expected watts at a control level, or ``None`` when not declared."""
        if self.adjustable_response is None:
            return None
        return self.adjustable_response.power_at(level)

    def is_adjustable(self) -> bool:
        """True when a declared adjustable response is attached."""
        return self.adjustable_response is not None

    # -- declared priors on the same v3 specs (DEC-33) ------------------------
    def as_fixed_spec(self) -> FixedSpec:
        """The declared levels as a :class:`FixedSpec` prior (DEC-33).

        Every field is taken from the declaration only -- no device name, no
        learning output.  An undeclared level stays absent (``None`` median),
        never a fabricated 0 W.  Learning seeds from this spec but never
        overwrites it (a >3σ discrepancy is a diagnostic, not a rewrite).
        """
        levels: dict[PowerLevelV3, LevelStat] = {}
        mapping = (
            (PowerLevelV3.STANDBY, self.standby_w),
            (PowerLevelV3.IDLE, self.idle_w),
            (PowerLevelV3.ACTIVE, self.active_w),
            (PowerLevelV3.TRANSIENT, self.transient_w),
        )
        for level, value in mapping:
            if value is not None:
                levels[level] = LevelStat(median_w=float(value))
        return FixedSpec(levels=levels)

    def as_adjustable_spec(self) -> AdjustableSpec | None:
        """The declared curve as an :class:`AdjustableSpec` prior, when declared.

        ``None`` when no adjustable response was declared, so a FIXED device is
        never silently given an adjustable prior.  The control variable is the
        one declared on :class:`AdjustableResponse` -- there is no hard-coded
        vocabulary here either.
        """
        response = self.adjustable_response
        if response is None:
            return None
        knots_x = [x for x, _ in response.points]
        knots_w = [y for _, y in response.points]
        standby = self.standby_w
        return AdjustableSpec(
            control_variable=response.control_variable,
            knots_x=knots_x,
            knots_w=knots_w,
            monotone=bool(response.monotonic),
            x_coverage=(knots_x[0], knots_x[-1]) if knots_x else None,
            standby=LevelStat(median_w=float(standby)) if standby is not None else LevelStat(),
        )

    def as_spec_priors(self) -> dict[PowerLevelV3, LevelStat]:
        """Declared level values as a prior map keyed by power level.

        This is the shape L2's fitters accept as ``declared_priors``; it lets a
        declaration seed the learned spec without ever being replaced by it.
        """
        priors: dict[PowerLevelV3, LevelStat] = {}
        for level, stat in self.as_fixed_spec().levels.items():
            priors[level] = stat
        return priors

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
        # Non-neutral declarations only: a default profile serialises exactly as
        # it did before these orthogonal parameters existed.
        if self.response_delay_s is not None:
            data["response_delay_s"] = self.response_delay_s
        if self.always_on:
            data["always_on"] = self.always_on
        return data


def _non_negative_delay(value: float | None) -> float:
    """Normalise a declared delay to a finite, non-negative float.

    ``None`` (no declaration) and any non-finite/negative value fall back to
    ``0.0`` -- "no delay" -- so a malformed declaration can never suppress a
    response.  This is the neutral default that keeps existing payloads and
    behavior unchanged.
    """
    if value is None:
        return 0.0
    cleaned = finite(value)
    return cleaned if cleaned is not None and cleaned >= 0.0 else 0.0


def power_after(
    before_w: float | None,
    after_w: float | None,
    *,
    delay_s: float | None,
    elapsed_s: float | None,
) -> float | None:
    """Electrical power following a state change that may be delayed.

    This is the pure, time-only core of a delayed response.  For every
    ``elapsed_s < delay_s`` the device still draws ``before_w`` (the response is
    suppressed); at or after the declared delay it draws ``after_w``.  Passing
    ``(standby_w, active_w)`` models activation; ``(active_w, standby_w)`` models
    deactivation.  With the neutral default ``delay_s = 0.0`` the new value
    applies immediately, which is exactly the shipped behaviour.

    ``delay_s`` is an *orthogonal parameter*: the shape of the power law is
    untouched, only its timing is.  A non-finite or negative delay is treated as
    zero (see :func:`_non_negative_delay`).  A missing or non-finite
    ``elapsed_s`` is answered with ``None`` -- an unmeasured time is not "zero
    elapsed" and must not fabricate a response.
    """
    elapsed = finite(elapsed_s)
    if elapsed is None:
        return None
    return before_w if elapsed < _non_negative_delay(delay_s) else after_w


def off_w(
    standby_w: float | None,
    active_w: float | None,
    *,
    always_on: bool,
) -> float | None:
    """The watts a device falls back to on an OFF transition.

    For a normal device this is ``standby_w`` -- the standby (possibly zero)
    transition a binary load shows.  An ``always_on`` device has no off-state,
    so an OFF transition does **not** drop it to standby: it keeps its declared
    standing load (``active_w``, falling back to ``standby_w`` when no active
    level was stated).  ``always_on`` defaults to ``False`` so the neutral case
    is the shipped binary behaviour.

    ``always_on`` is an *orthogonal parameter*: it is the absence of a
    transition, not a different power law, so it works with any load shape.
    """
    if always_on:
        return active_w if active_w is not None else standby_w
    return standby_w


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0.0 else None


#: The transient level, named once so a caller cannot forget to exclude it.
TRANSIENT_LEVEL = PowerLevelV3.TRANSIENT


__all__ = [
    "LEVEL_ORDER",
    "TRANSIENT_LEVEL",
    "AdjustableResponse",
    "DevicePowerProfile",
    "off_w",
    "power_after",
]
