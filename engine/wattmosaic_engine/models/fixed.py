"""Fixed-power model fitted on device-attributed delta power (L2, HB §11.1).

A FIXED device draws one of a small set of discrete levels (standby, idle,
active, transient).  This module learns each level as a robust ``LevelStat``
(median + 1.4826·MAD + count) and learns named transition deltas on the same
basis.  It is the v3, delta-basis successor to the legacy ``binary.py`` model:
it never fits on an absolute circuit row, never collapses idle/standby into an
"off" state, and never fabricates 0 W (HB §18, C1, C5, DEC-33/34).

Standby is the subtle part (analysis F2):

* on a single-subject endpoint (ABSOLUTE_ANCHOR basis, e.g. a smart plug) the
  reported ``level_w`` is an absolute level and is used directly;
* on a shared circuit standby comes from a chain of deltas: an OFF→STANDBY
  step plus a residual-baseline step (the circuit's baseline after other
  devices have been removed), exactly the ΔP + residual-baseline construction
  AT-03 requires.

Declared priors (DEC-33) seed the spec and are never overwritten by learning;
a discrepancy beyond 3σ is surfaced as a diagnostic item.  The model class is
the declared ``LoadShape`` (default FIXED, DEC-34); learning may *propose* a
change but never switches the spec silently.

Pure: no DB, no wall clock, no runtime fakes.  All inputs are explicit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    DeltaPrior,
    EvidenceRef,
    FixedSpec,
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
from .base import finite, median, robust_sigma

__all__ = [
    "FixedPowerModel",
    "fit_fixed_spec",
    "model_class_for_load_shape",
    "suggest_model_class",
]

#: Transitions that carry the residual-baseline step used to derive standby.
_RESIDUAL_TRANSITIONS = frozenset(
    {"residual_baseline", "baseline_step", "residual_step", "standby_baseline"}
)


def _level(value: Any) -> PowerLevelV3 | None:
    """Normalise an observation level to a ``PowerLevelV3`` member."""
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    for member in PowerLevelV3:
        if member.value == text:
            return member
    return None


def _stat(values: Sequence[float]) -> LevelStat:
    """A robust statistic, with ``None`` (never 0.0) for absent values."""
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if not clean:
        return LevelStat()
    med = median(clean)
    mad = robust_sigma(clean, default=None)
    return LevelStat(median_w=med, mad_w=mad, n=len(clean))


def model_class_for_load_shape(
    shape: LoadShape | str | None,
    *,
    device_type: str | None = None,  # noqa: ARG001 - deliberately ignored (DEC-34)
    attributes: Mapping[str, Any] | None = None,  # noqa: ARG001
) -> ModelClass:
    """The declared load shape picks the model class; no naming surface is read.

    ``UNKNOWN`` (or no declaration) defaults to FIXED.  ``device_type`` and
    ``attributes`` are accepted only so callers can pass a whole device record;
    they are never consulted, which is what makes keyword-based inference
    impossible in the v3 path.
    """
    value = str(getattr(shape, "value", shape) or "").strip().lower()
    return ModelClass.ADJUSTABLE if value == "adjustable" else ModelClass.FIXED


def suggest_model_class(
    shape: LoadShape | str | None,
    *,
    observed_nonlinear: bool = False,
) -> ModelClass | None:
    """Return a *suggested* class change, or ``None`` when nothing is suggested.

    This is an L5-facing hint only.  It never mutates a spec or an active
    version: DEC-34 says learning proposes, never switches silently.
    """
    current = model_class_for_load_shape(shape)
    if observed_nonlinear and current is ModelClass.FIXED:
        return ModelClass.ADJUSTABLE
    return None


def _derive_standby(
    observations: Sequence[LearningObservation],
    learned: dict[PowerLevelV3, LevelStat],
) -> LevelStat | None:
    """Derive standby from an anchor level or a ΔP + residual-baseline chain.

    An ABSOLUTE_ANCHOR level wins (single-subject endpoint).  Otherwise a
    residual-baseline step is combined with the OFF→STANDBY delta: if other
    devices are removed from the circuit (residual step of ``r``) and the device
    then turns OFF with delta ``-d``, its standby draw is ``r - d``.  With no
    chain at all there is no honest standby estimate, so ``None`` is returned.
    """
    anchor = [
        float(obs.level_w)
        for obs in observations
        if str(getattr(obs.basis, 'value', obs.basis)) == ModelBasis.ABSOLUTE_ANCHOR.value
        and obs.level_w is not None
        and finite(obs.level_w) is not None
    ]
    if anchor:
        return _stat(anchor)

    residuals: list[float] = []
    for obs in observations:
        # T3 residual-baseline steps carry the circuit's post-removal baseline
        # as a level in ``residual_step_w``.  That baseline *is* the device's
        # standby draw on a shared circuit (AT-03), not a step to be combined
        # with the OFF delta -- the OFF delta only confirms active - standby.
        step = finite(obs.attributes.get("residual_step_w"))
        if step is not None:
            residuals.append(float(step))
            continue
        if (
            finite(obs.delta_w) is not None
            and str(obs.transition).strip().lower() in _RESIDUAL_TRANSITIONS
        ):
            # No explicit level: a residual transition's |ΔP| is the drop to
            # the new baseline, i.e. the standby draw.
            residuals.append(abs(float(obs.delta_w)))
    if residuals:
        candidate = _stat(residuals)
        if candidate.median_w is not None:
            return candidate

    # An explicit STANDBY level learned from deltas is still better than a guess.
    existing = learned.get(PowerLevelV3.STANDBY)
    if existing is not None and existing.median_w is not None:
        return existing
    return None


def fit_fixed_spec(
    observations: Sequence[LearningObservation],
    *,
    declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
) -> tuple[FixedSpec, dict[PowerLevelV3, LevelStat], list[str]]:
    """Fit a :class:`FixedSpec` from confirmed observations.

    Returns ``(spec, learned_levels, diagnostics)``.  ``learned_levels`` keeps
    the raw learned statistics even when a declared prior was seeded into the
    spec, so a >3σ discrepancy can be reported without overwriting the prior.
    """
    levels: dict[PowerLevelV3, list[float]] = {
        PowerLevelV3.STANDBY: [],
        PowerLevelV3.IDLE: [],
        PowerLevelV3.ACTIVE: [],
        PowerLevelV3.TRANSIENT: [],
    }
    transitions: dict[str, list[float]] = {}
    anchor_levels: dict[PowerLevelV3, list[float]] = {}
    #: Transition names that name the level the device is at after the event.
    level_transitions: dict[str, PowerLevelV3] = {
        "on": PowerLevelV3.ACTIVE,
        "start": PowerLevelV3.ACTIVE,
        "activate": PowerLevelV3.ACTIVE,
        "off": PowerLevelV3.STANDBY,
        "standby": PowerLevelV3.STANDBY,
        "idle": PowerLevelV3.IDLE,
        "transient": PowerLevelV3.TRANSIENT,
        "startup": PowerLevelV3.TRANSIENT,
    }

    for obs in observations:
        if obs.status != "confirmed":
            continue
        level = _level(obs.attributes.get("level"))
        if level is None:
            level = level_transitions.get(str(obs.transition).strip().lower())
        if str(getattr(obs.basis, 'value', obs.basis)) == ModelBasis.ABSOLUTE_ANCHOR.value and obs.level_w is not None:
            target = level or PowerLevelV3.STANDBY
            anchor_levels.setdefault(target, []).append(float(obs.level_w))
        delta = finite(obs.delta_w)
        if delta is None:
            continue
        if level in levels:
            levels[level].append(float(delta))
        elif obs.transition:
            transitions.setdefault(str(obs.transition), []).append(float(delta))

    learned: dict[PowerLevelV3, LevelStat] = {}
    for member, values in levels.items():
        stat = _stat(values)
        if anchor_levels.get(member):
            stat = _stat(anchor_levels[member])
        if stat.median_w is not None:
            learned[member] = stat

    standby = _derive_standby(observations, learned)
    if standby is not None and standby.median_w is not None:
        learned[PowerLevelV3.STANDBY] = standby

    # A positive ON transition reveals ACTIVE; a negative OFF transition feeds
    # the signed transition_delta the inference layer consumes.
    for member, name in (
        (PowerLevelV3.ACTIVE, "on"),
        (PowerLevelV3.STANDBY, "off"),
    ):
        if member in learned and name not in transitions:
            value = learned[member].median_w
            if value is not None:
                transitions[name] = [float(value) if name == "on" else -float(value)]

    transition_delta = {name: _stat(values) for name, values in transitions.items()}

    diagnostics: list[str] = []
    spec_levels: dict[PowerLevelV3, LevelStat] = dict(learned)
    for member, prior in (declared_priors or {}).items():
        spec_levels[member] = prior  # a declaration is never overwritten
        observed = learned.get(member)
        if observed is not None and _beyond_three_sigma(prior, observed):
            diagnostics.append(
                f"declared {member.value} prior {prior.median_w} differs from learned "
                f"{observed.median_w} by more than 3sigma"
            )
    return FixedSpec(levels=spec_levels, transition_delta=transition_delta), learned, diagnostics


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


class FixedPowerModel:
    """FIXED model: a spec plus its fit provenance, used to build a v3 version."""

    model_class: ModelClass = ModelClass.FIXED
    minimum_samples: int = 1

    def __init__(
        self,
        spec: FixedSpec | None = None,
        *,
        control_variable: str | None = None,  # noqa: ARG002 - FIXED has none
        load_shape: LoadShape | str | None = None,
        declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
        diagnostics: Sequence[str] | None = None,
        learned_levels: Mapping[PowerLevelV3, LevelStat] | None = None,
        suggested_class: ModelClass | None = None,
    ) -> None:
        self.spec = spec or FixedSpec()
        self.load_shape = load_shape or LoadShape.FIXED
        self.declared_priors = dict(declared_priors or {})
        self._diagnostics = list(diagnostics or [])
        self.learned_levels: dict[PowerLevelV3, LevelStat] = dict(learned_levels or {})
        self.suggested_class = suggested_class
        self.basis = ModelBasis.DELTA

    @property
    def kind(self) -> str:
        return "fixed"

    def fit(
        self,
        observations: Sequence[LearningObservation],
        *,
        load_shape: LoadShape | str | None = None,
        declared_priors: Mapping[PowerLevelV3, LevelStat] | None = None,
        suggested_class: ModelClass | None = None,
        **_kwargs: Any,
    ) -> FixedPowerModel:
        """Fit from device-attributed observations; return ``self`` (fluent)."""
        shape = load_shape if load_shape is not None else self.load_shape
        priors = declared_priors if declared_priors is not None else self.declared_priors
        spec, learned, diagnostics = fit_fixed_spec(observations, declared_priors=priors)
        has_anchor = any(str(getattr(obs.basis, 'value', obs.basis)) == ModelBasis.ABSOLUTE_ANCHOR.value for obs in observations)
        self.spec = spec
        self.load_shape = shape
        self.declared_priors = dict(priors)
        self.learned_levels = learned
        self._diagnostics = diagnostics
        self.basis = ModelBasis.ABSOLUTE_ANCHOR if has_anchor else ModelBasis.DELTA
        self.suggested_class = suggest_model_class(shape) if suggested_class is None else suggested_class
        return self

    def predict(
        self,
        *,
        level: PowerLevelV3 | str | None = None,
        after_transition: str | None = None,  # noqa: ARG002 - level is authoritative
        attributes: Mapping[str, Any] | None = None,  # noqa: ARG002
    ) -> PowerPrediction:
        """Predict one level; an unknown level is ``UNKNOWN`` with ``None``."""
        member = _level(level)
        unknown_level = member if member is not None else PowerLevelV3.UNKNOWN
        stat = self.spec.levels.get(member) if member is not None else None
        if stat is None or stat.median_w is None:
            return PowerPrediction(
                level=unknown_level,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        return PowerPrediction(
            level=unknown_level,
            mean_w=float(stat.median_w),
            sigma_w=float(stat.mad_w) if stat.mad_w is not None else None,
            status=PredictionStatus.KNOWN,
            model_version_id=None,
        )

    def transition_prior(self, transition: str) -> DeltaPrior | None:
        stat = self.spec.transition_delta.get(transition) or self.spec.transition_delta.get(
            str(transition).strip().lower()
        )
        if stat is None or stat.median_w is None or stat.mad_w is None:
            return None
        mean = float(stat.median_w)
        return DeltaPrior(
            mean_w=mean,
            sigma_w=float(stat.mad_w),
            sign="pos" if mean > 0 else "neg" if mean < 0 else "any",
            model_version_id="",
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
            model_class=model_class_for_load_shape(self.load_shape),
            basis=self.basis,
            spec=self.spec,
            evidence_refs=list(evidence_refs or []),
            trainer_version=trainer_version,
            created_at=created_at or datetime.now(UTC),
        )
