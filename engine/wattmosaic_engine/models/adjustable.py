"""Adjustable model fitted on device-attributed delta power (L2, HB §11.2).

This unifies the three previous adjustable representations -- the dimmable
light, the variable-speed fan and the declared ``AdjustableResponse`` -- into
one spec-driven model on the declared control variable.  There is no
hard-coded control vocabulary and no ``_MAX_RPM`` normalisation: the control
variable is declared by the caller and matched against the observation's
``attributes`` (C8, analysis F2).

P(x) is learned from the deltas between levels, so a non-linear curve is
recovered without assuming P = k·x.  PAVA (pool-adjacent-violators) is applied
only when monotonicity is declared or validated; a genuinely non-monotonic
device keeps its measured shape.  ``x_coverage`` records the observed control
range and a lookup outside it is ``None`` / ``UNKNOWN`` (never an extrapolated
number, HB §18).

Declared priors (DEC-33) seed ``standby`` and are never overwritten; a
discrepancy beyond 3σ is surfaced as a diagnostic.  The class comes from the
declared ``LoadShape`` (DEC-34); learning never switches a device silently.

Pure: no DB, no wall clock, no runtime fakes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    AdjustableSpec,
    EvidenceRef,
    LearningObservation,
    LevelStat,
    LoadShape,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
)
from .base import _linear_interp, enforce_monotonic, finite, median, robust_sigma
from .fixed import model_class_for_load_shape

__all__ = [
    "AdjustableModel",
    "fit_adjustable_spec",
    "model_class_for_load_shape",
]


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float | None:
    return _linear_interp(x, list(zip(xs, ys, strict=False)))


def fit_adjustable_spec(
    observations: Sequence[LearningObservation],
    *,
    control_variable: str,
    monotonic: bool = False,
    declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
) -> tuple[AdjustableSpec, list[str]]:
    """Fit an :class:`AdjustableSpec` from confirmed observations.

    Returns ``(spec, diagnostics)``.  Only observations whose declared control
    variable matches are used, so a partially-labelled dataset yields a curve
    for the requested axis rather than a blended guess.
    """
    desired = str(control_variable).strip().lower()
    points: list[tuple[float, float]] = []
    anchor_levels: list[float] = []
    for obs in observations:
        if obs.status != "confirmed":
            continue
        variable = str(obs.attributes.get("control_variable") or "").strip().lower()
        if variable and variable != desired:
            continue
        x = finite(obs.attributes.get(desired))
        if x is None:
            # A caller may pass the value under the generic key; honour it only
            # when it is explicitly labelled as this control variable.
            generic_var = str(obs.attributes.get("control_variable") or "").strip().lower()
            if generic_var == desired:
                x = finite(obs.attributes.get("control_value"))
        delta = finite(obs.delta_w)
        if x is None or delta is None:
            continue
        points.append((float(x), float(delta)))
        if str(getattr(obs.basis, 'value', obs.basis)) == ModelBasis.ABSOLUTE_ANCHOR.value and obs.level_w is not None:
            anchor = finite(obs.level_w)
            if anchor is not None:
                anchor_levels.append(float(anchor))

    points.sort(key=lambda pair: pair[0])
    collapsed: dict[float, list[float]] = {}
    for x, y in points:
        collapsed.setdefault(x, []).append(y)
    curve = [(x, median(values) or 0.0) for x, values in sorted(collapsed.items())]
    if monotonic and curve:
        curve = enforce_monotonic(curve)

    knots_x = [x for x, _ in curve]
    knots_w = [y for _, y in curve]
    coverage = (knots_x[0], knots_x[-1]) if knots_x else None

    standby_stat = _stat(anchor_levels)
    diagnostics: list[str] = []
    if declared_priors:
        observed = _stat(anchor_levels)
        for member, prior in declared_priors.items():
            if member is PowerLevelV3.STANDBY or member == "standby":
                standby_stat = prior  # a declared prior is never overwritten
                learned_standby = observed if anchor_levels else (
                    LevelStat(median_w=min(knots_w), n=len(knots_w)) if knots_w else LevelStat()
                )
                if _beyond_three_sigma(prior, learned_standby):
                    diagnostics.append("declared standby prior differs from learned by more than 3sigma")
            elif curve:
                # An ACTIVE prior disagrees when the fitted curve's span is far
                # from the declared operating point (beyond 3σ of the prior).
                learned = LevelStat(
                    median_w=median(knots_w),
                    mad_w=robust_sigma(knots_w, default=None),
                    n=len(knots_w),
                )
                if _beyond_three_sigma(prior, learned):
                    diagnostics.append(
                        f"declared {getattr(member, 'value', member)} prior differs from "
                        "learned curve by more than 3sigma"
                    )

    spec = AdjustableSpec(
        control_variable=str(control_variable),
        knots_x=knots_x,
        knots_w=knots_w,
        monotone=bool(monotonic),
        x_coverage=coverage,
        standby=standby_stat,
    )
    return spec, diagnostics


def _stat(values: Sequence[float]) -> LevelStat:
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if not clean:
        return LevelStat()
    return LevelStat(
        median_w=median(clean),
        mad_w=robust_sigma(clean, default=None),
        n=len(clean),
    )


def _beyond_three_sigma(prior: LevelStat, observed: LevelStat) -> bool:
    if prior.median_w is None or observed.median_w is None:
        return False
    # The declared MAD is the prior's own uncertainty and sets the scale.  A
    # tiny or absent MAD gets a relative floor so a single observation cannot
    # flag a harmless rounding difference as a 3σ change.
    sigma = finite(prior.mad_w)
    if sigma is None or sigma <= 0:
        sigma = max(0.5, abs(prior.median_w) * 0.05)
    return abs(float(observed.median_w) - float(prior.median_w)) > 3.0 * float(sigma)


class AdjustableModel:
    """ADJUSTABLE model: a spec over a declared control variable."""

    model_class: ModelClass = ModelClass.ADJUSTABLE
    minimum_samples: int = 2

    def __init__(
        self,
        spec: AdjustableSpec | None = None,
        *,
        control_variable: str = "level",
        load_shape: LoadShape | str | None = LoadShape.ADJUSTABLE,
        declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
        diagnostics: Sequence[str] | None = None,
    ) -> None:
        self.spec = spec or AdjustableSpec(control_variable=control_variable)
        self.control_variable = control_variable
        self.load_shape = load_shape or LoadShape.ADJUSTABLE
        self.declared_priors = dict(declared_priors or {})
        self._diagnostics = list(diagnostics or [])

    @property
    def kind(self) -> str:
        return "adjustable"

    def fit(
        self,
        observations: Sequence[LearningObservation],
        *,
        control_variable: str | None = None,
        monotonic: bool | None = None,
        load_shape: LoadShape | str | None = None,
        declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
        **_kwargs: Any,
    ) -> AdjustableModel:
        """Fit from device-attributed observations; return ``self`` (fluent)."""
        variable = control_variable or self.control_variable
        shape = load_shape if load_shape is not None else self.load_shape
        priors = declared_priors if declared_priors is not None else self.declared_priors
        mono = bool(monotonic) if monotonic is not None else self.spec.monotone
        spec, diagnostics = fit_adjustable_spec(
            observations,
            control_variable=variable,
            monotonic=mono,
            declared_priors=priors,
        )
        self.spec = spec
        self.control_variable = variable
        self.load_shape = shape
        self.declared_priors = dict(priors)
        self._diagnostics = diagnostics
        return self

    @property
    def x_coverage(self) -> tuple[float, float] | None:
        return self.spec.x_coverage

    def predict(
        self,
        *,
        level: PowerLevelV3 | str | None = None,
        attributes: Mapping[str, Any] | None = None,
        **_kwargs: Any,
    ) -> PowerPrediction:
        """Predict the device's power at its declared control value.

        A missing or out-of-coverage control value is ``UNKNOWN`` with ``None``
        -- never an extrapolated or fabricated number (HB §18).
        """
        member = level if isinstance(level, PowerLevelV3) else PowerLevelV3.UNKNOWN
        if member is PowerLevelV3.STANDBY:
            stat = self.spec.standby
            if stat.median_w is None:
                return PowerPrediction(
                    level=member,
                    mean_w=None,
                    sigma_w=None,
                    status=PredictionStatus.UNKNOWN,
                    model_version_id=None,
                )
            mad = finite(stat.mad_w)
            return PowerPrediction(
                level=member,
                mean_w=float(stat.median_w),
                sigma_w=float(mad) if mad is not None else None,
                status=PredictionStatus.KNOWN,
                model_version_id=None,
            )
        x = finite(attributes.get(self.spec.control_variable)) if isinstance(attributes, Mapping) else None
        if x is None or not self.spec.knots_x:
            return PowerPrediction(
                level=member,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        coverage = self.spec.x_coverage
        if coverage is not None and not (coverage[0] <= x <= coverage[1]):
            return PowerPrediction(
                level=member,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        mean = _interp(x, self.spec.knots_x, self.spec.knots_w)
        if mean is None:
            return PowerPrediction(
                level=member,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        mad = finite(self.spec.standby.mad_w)
        return PowerPrediction(
            level=member,
            mean_w=float(mean),
            sigma_w=float(mad) if mad is not None else None,
            status=PredictionStatus.KNOWN,
            model_version_id=None,
        )

    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def build_version(
        self,
        *,
        subject_id: str,
        number: int,
        trainer_version: str,
        version_id: str | None = None,
        subject_kind: str = "device",
        evidence_refs: Sequence[EvidenceRef] | None = None,
        created_at: datetime | None = None,
    ) -> PowerModelVersion:
        """Wrap the fitted spec in a v3 :class:`PowerModelVersion`."""
        return PowerModelVersion(
            version_id=version_id or f"mdl-{subject_id}-{number}",
            subject_kind="device" if subject_kind == "device" else "component",
            subject_id=subject_id,
            number=int(number),
            model_class=ModelClass.ADJUSTABLE,
            basis=ModelBasis.DELTA,
            spec=self.spec,
            evidence_refs=list(evidence_refs or []),
            trainer_version=trainer_version,
            created_at=created_at or datetime.now(UTC),
        )
