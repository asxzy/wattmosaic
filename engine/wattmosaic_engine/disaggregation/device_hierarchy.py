"""Per-device inference hierarchy (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as
module-level functions that take engine state explicitly.  ``finish`` (the
evidence-grade attacher nested inside ``_infer_device``) is now a module-level
helper that takes the engine configuration it closes over as explicit
arguments, so the logic is byte-for-byte unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    InferenceResult,
    LoadShape,
    MeasurementBasis,
    MonitoringLevel,
    PowerBehavior,
    PowerLevel,
    PowerState,
    QualityStatus,
    Source,
    power_state_for,
)
from wattmosaic_engine.disaggregation._shared import (
    MODEL_MATCH_REL_TOL,
    _is_off,
    _is_on,
    _measured_level,
    _quality_from_age,
    _stage_for,
    _state_power,
    _used_support,
    _worst,
)
from wattmosaic_engine.disaggregation.confidence import (
    ConfidenceInputs,
    compute_confidence,
    event_isolation_score,
)
from wattmosaic_engine.disaggregation.configuration import (
    EffectiveConfig,
    configured_power,
)
from wattmosaic_engine.disaggregation.events import StepChange
from wattmosaic_engine.disaggregation.tiers import TierInputs, grade
from wattmosaic_engine.disaggregation.transitions import (
    _row_siblings,
    _Transition,
)
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


def _support_sigma(
    graph: TopologyGraph, channels: Sequence[str], *, noise_floor_w: float
) -> float:
    """Per-channel resolution/reliability combined into one sigma (public helper)."""
    import numpy as np

    vals = []
    for cid in channels:
        ch = graph.channels.get(cid)
        if ch is None:
            continue
        res = ch.resolution_w or 0.0
        rel = ch.reliability or 1.0
        vals.append(max(noise_floor_w, res / 2.0) / max(rel, 0.05))
    return float(np.sqrt(sum(v * v for v in vals))) if vals else noise_floor_w


def _unknown_result(
    device_id: str,
    at: datetime,
    confidence: float,
    quality: QualityStatus,
    supporting: Sequence[str],
    explanation: Sequence[str],
    *,
    provenance: Mapping[str, Any] | None = None,
) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at,
        power_w=0.0,
        source=Source.UNKNOWN,
        confidence=min(max(confidence, 0.0), 0.25),
        quality_status=quality,
        supporting_ct_channels=list(supporting),
        explanation=list(dict.fromkeys([*explanation, "power_left_unattributed"])),
        provenance={
            **dict(provenance or {}),
            "power_w_is_placeholder": True,
            "actual_power_accounted_in": "Reconciliation.unknown_residual_w",
        },
        confidence_is_calibrated=False,
        power_state=PowerState.UNKNOWN,
    )


def _fallback_result(device_id: str, at: datetime, exc: Exception) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at,
        power_w=0.0,
        source=Source.UNKNOWN,
        confidence=0.05,
        quality_status=QualityStatus.BAD,
        explanation=["device_inference_error", "power_left_unattributed"],
        provenance={
            "error": type(exc).__name__,
            "power_w_is_placeholder": True,
        },
        confidence_is_calibrated=False,
        power_state=PowerState.UNKNOWN,
    )


def finish(
    result: InferenceResult,
    *,
    tier_inputs: TierInputs,
    monitoring: MonitoringLevel,
    behavior: PowerBehavior,
    load_shape: LoadShape,
    profile: Any,
    config_notes: Sequence[str],
    max_measurement_age_s: float,
    model_corroborates: bool = False,
    configured_prior: bool = False,
    prior_reason: str = "",
    measured_step: bool = False,
    degraded: bool = False,
) -> InferenceResult:
    """Attach the evidence grade to a result without changing its score."""
    tier, score, reasons = grade(
        tier_inputs,
        result.confidence,
    )
    result.confidence = score
    # ``use_enum_values`` only coerces on validation, not on assignment, so
    # assign the plain value to keep every downstream consumer (API JSON,
    # SQLite, comparisons) seeing the same string form.
    result.confidence_tier = (
        tier.value if hasattr(tier, "value") else str(tier)
    )
    result.monitoring_level = (
        monitoring.value if hasattr(monitoring, "value") else str(monitoring)
    )
    result.power_behavior = (
        behavior.value if hasattr(behavior, "value") else str(behavior)
    )
    # The declared load-shape class, published with the value so a consumer can
    # read the axis without re-reading the configuration.
    # A declaration only; never derived from the device name.
    result.load_shape = (
        load_shape.value  # type: ignore[assignment]
        if hasattr(load_shape, "value")
        else str(load_shape)
    )
    # The operating level of the value, derived from the device's declared
    # power profile.  ``UNKNOWN`` is the honest fallback: a device with no
    # declaration (or a value matching none of its stated levels) names no
    # level rather than guessing one.  This is a separate axis from
    # ``power_state`` below, which is provenance.
    level = PowerLevel.UNKNOWN
    if profile is not None:
        level = profile.level_for(result.power_w)
    result.power_level = (
        level.value  # type: ignore[assignment]
        if hasattr(level, "value")
        else str(level)
    )
    # Derived, never independently settable: the label follows the
    # source and the basis, so it cannot disagree with how the value
    # was obtained.  ``use_enum_values`` coerces only on validation,
    # so re-read both axes through the house ``Enum(...)`` idiom before
    # deriving, and assign the member (``model_dump`` emits the string).
    result.power_state = power_state_for(
        Source(result.source), MeasurementBasis(result.measurement_basis)
    )
    result.tier_reasons = [*config_notes, *reasons]
    return result


def _infer_device(
    device_id: str,
    *,
    graph: TopologyGraph,
    window: CoercedWindow,
    model: ModelView | None,
    state: str | None,
    transition: _Transition | None,
    step: StepChange | None,
    attribution_step: StepChange | None = None,
    siblings_stable: bool = False,
    row_is_shared: bool = False,
    fit_value: float | None,
    identifiable: bool,
    ambiguous: bool,
    fit_at_ceiling: bool = False,
    declared_ceiling_hit: bool = False,
    state_overrides: Mapping[str, str] | None = None,
    config: EffectiveConfig | None = None,
    max_measurement_age_s: float,
    off_tolerance_w: float,
    noise_floor_w: float,
) -> InferenceResult:
    discipline = graph.resolve(device_id)
    topology_status = str(discipline.topology_status)
    now = window.at
    own_event = transition is not None and transition.direction is not None
    concurrent_events = len(window.state_transitions)
    monitoring = config.monitoring if config is not None else MonitoringLevel.UNKNOWN
    behavior = config.behavior if config is not None else PowerBehavior.UNKNOWN
    load_shape = config.load_shape if config is not None else LoadShape.UNKNOWN
    # The device's declared standby/idle/active/transient envelope.  A pure
    # computed property of ``EffectiveConfig`` (no ``__dict__`` on a frozen
    # ``slots`` dataclass), so it is rebuilt here rather than cached.
    profile = config.power_profile if config is not None else None
    config_notes = list(config.notes) if config is not None else []
    # A state value is only evidence when the topology declares a load entity
    # for this device; an undeclared key in ``device_states`` proves nothing.
    has_load_signal = bool(config is not None and config.load_entity_ids)
    state_on = has_load_signal and _is_on(state, state_overrides)
    state_off = has_load_signal and _is_off(state, state_overrides)
    # Measured dimmer output for *this* device, when the control entity
    # reported one.  ``None`` means "not reported", never "full brightness":
    # a light that is merely ``on`` must not be read as 100 % output.
    level = _measured_level(window.metadata, device_id)

    def _finish(
        result: InferenceResult,
        *,
        model_corroborates: bool = False,
        configured_prior: bool = False,
        prior_reason: str = "",
        measured_step: bool = False,
        degraded: bool = False,
    ) -> InferenceResult:
        """Attach the evidence grade to a result without changing its score."""
        return finish(
            result,
            tier_inputs=TierInputs(
                source=Source(result.source),
                monitoring=monitoring,
                behavior=behavior,
                state_says_on=state_on,
                state_says_off=state_off,
                has_declared_load_signal=has_load_signal,
                model_corroborates=model_corroborates,
                configured_prior=configured_prior,
                prior_reason=prior_reason,
                measured_step=measured_step,
                stale=result.measurement_age_seconds > max_measurement_age_s,
                degraded=degraded,
            ),
            monitoring=monitoring,
            behavior=behavior,
            load_shape=load_shape,
            profile=profile,
            config_notes=config_notes,
            max_measurement_age_s=max_measurement_age_s,
            model_corroborates=model_corroborates,
            configured_prior=configured_prior,
            prior_reason=prior_reason,
            measured_step=measured_step,
            degraded=degraded,
        )

    # ---------------- Level 2/3/4: nothing measured this device directly ----
    explanation: list[str] = []
    if own_event:
        explanation.append(
            "ha_state_transition_activation"
            if transition and transition.direction == "activation"
            else "ha_state_transition_deactivation"
        )
    if step is not None and own_event and transition is not None:
        agrees = (
            transition.direction == "activation" and step.delta_w > 0
        ) or (transition.direction == "deactivation" and step.delta_w < 0)
        if agrees:
            explanation.append(
                "branch_power_increase" if step.delta_w > 0 else "branch_power_decrease"
            )
    if ambiguous:
        explanation.append("ambiguous_concurrent_events")

    supporting = [cid for cid in graph.device_channels(device_id)]
    sigma = _support_sigma(graph, supporting, noise_floor_w=noise_floor_w)
    ages = [
        window.readings[cid].age_s
        for cid in supporting
        if cid in window.readings
    ]
    qualities = [
        window.readings[cid].quality
        for cid in supporting
        if cid in window.readings
    ]
    if not qualities:
        qualities = [QualityStatus.MISSING]
    if not ages:
        ages = [float("inf")]
    stale = any(a > max_measurement_age_s for a in ages)
    repeated = any(
        window.readings[cid].repeated
        for cid in supporting
        if cid in window.readings
    )
    quality = _quality_from_age(max(ages), _worst(qualities))
    isolation = event_isolation_score(
        own_event=own_event,
        concurrent_events=concurrent_events,
        ambiguous=ambiguous,
    )

    # Ambiguity wins over every estimate.
    if ambiguous:
        confidence = compute_confidence(
            ConfidenceInputs(
                source=Source.UNKNOWN,
                measurement_qualities=qualities,
                measurement_ages_s=ages,
                supporting_ct_count=len(supporting),
                model_quality=0.0 if model is None else model.quality,
                event_isolation=isolation,
                topology_status=topology_status,
                residual_w=0.0,
                residual_sigma_w=sigma,
                stale=stale,
                ambiguous=True,
            )
        )
        return _finish(
            _unknown_result(
                device_id,
                now,
                confidence.score,
                quality,
                supporting,
                explanation,
                provenance={
                    "ambiguity": "concurrent_events_not_separable",
                    "confidence": confidence.as_dict(),
                },
            ),
            degraded=True,
        )

    fit = 0.0 if fit_value is None else max(float(fit_value), 0.0)
    model_power = _state_power(model, state)
    standby = 0.0
    if model is not None and model.params.standby_power_w is not None:
        standby = abs(float(model.params.standby_power_w))
    off_tolerance = max(off_tolerance_w, 2.0 * standby)

    # ---------------- Level 2a: measured step (ADR-0026) --------------
    # A shared row cannot be split by linear algebra, but a *step* on it can
    # be read: the row moved by exactly this many watts while this device
    # changed state, and no other device on the row changed.  That movement
    # is the device's draw, measured.
    #
    # This is deliberately weaker than ADR-0023's "the single ON claimant
    # owns the row", which required every sibling to be positively OFF and
    # therefore credited the whole circuit to one lamp whenever the fixture
    # list was incomplete.  Here a sibling may be running; it only has to
    # have held steady across the transition.
    #
    # Only an *activation* is attributed.  On a deactivation the step tells
    # us what the device drew a moment ago, not what it draws now -- it is
    # off, and reporting the step as its current power would both be wrong
    # for the device page and double count against the row that already
    # dropped.  The deactivation is marked in the explanation so the evidence
    # is visible without being reported as present power.
    step_w = 0.0
    if (
        attribution_step is not None
        and row_is_shared
        and siblings_stable
        and has_load_signal
        and transition is not None
        and transition.direction == "activation"
        and attribution_step.delta_w > 0.0
        and not stale
        and not state_off
    ):
        step_w = float(attribution_step.delta_w)
        row_now = window.readings.get(attribution_step.channel_id)
        row_value = None if row_now is None else row_now.signed_w()
        if row_value is not None:
            # The step is a change *on* the row, so it cannot be larger than
            # the row itself.  Clamping here is a physics guard against a
            # mis-scaled or mis-signed reading, not an estimate.
            step_w = min(step_w, abs(float(row_value)))
        # A stated wattage is a non-relaxable upper bound (ADR-0025), and it
        # applies to a measured step exactly as it applies to a fit.  A
        # 400 W step on a row whose lamp declares 50 W is not that lamp's
        # 50 W lamp being measured; the surplus belongs to something else on
        # the circuit, so the step is not this device's draw at all.
        ceiling = None if config is None else config.config_ceiling_w
        if ceiling is not None and step_w > float(ceiling):
            explanation.append("step_exceeds_declared_wattage")
            step_w = 0.0
        if step_w > 0.0:
            explanation.append("shared_circuit_step_measured")
            if attribution_step.source == "previous_snapshot":
                explanation.append("step_from_previous_snapshot")
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.INFERRED,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=len(
                        _used_support(graph, device_id, supporting)
                    ),
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=0.0,
                    residual_sigma_w=float(attribution_step.noise_w),
                    repeated=repeated,
                )
            )
            stage, stage_status = _stage_for(model, step_w)
            return _finish(
                InferenceResult(
                    device_id=device_id,
                    timestamp=now,
                    power_w=step_w,
                    source=Source.INFERRED,
                    confidence=confidence.score,
                    model_id=None if model is None else model.model_id,
                    model_version=None if model is None else model.version,
                    operating_stage=stage,
                    stage_status=stage_status,
                    sample_count=0 if model is None else model.sample_count,
                    quality_status=quality,
                    measurement_age_seconds=max(ages) if ages else 0.0,
                    supporting_ct_channels=[attribution_step.channel_id],
                    explanation=explanation,
                    provenance={
                        "measured_step_w": step_w,
                        "step_channel_id": attribution_step.channel_id,
                        "step_before_w": attribution_step.before_w,
                        "step_after_w": attribution_step.after_w,
                        "step_threshold_w": attribution_step.threshold_w,
                        "step_source": attribution_step.source,
                        "siblings_stable": True,
                        "siblings": list(_row_siblings(graph, device_id)),
                        "confidence": confidence.as_dict(),
                    },
                    confidence_is_calibrated=False,
                ),
                measured_step=True,
            )
    if (
        attribution_step is not None
        and row_is_shared
        and siblings_stable
        and attribution_step.delta_w < 0.0
        and transition is not None
        and transition.direction == "deactivation"
    ):
        # Keep the measurement as evidence without reporting it as current
        # power: the device is off, and the row that dropped has already
        # dropped.  Echoing the step now would double count it against the
        # row and claim the device is drawing when it is not.  The magnitude
        # is recorded so learning can use it.
        explanation.append("shared_circuit_step_measured_on_deactivation")

    # ---------------- Level 2: inferred ------------------------------
    # A row the user declared shared carries power that belongs to siblings we
    # do not model.  The solver already protects the *sum* with a slack
    # column, but a device within it must not absorb the remainder: that would
    # report a lights circuit's whole draw as one lamp.  Such a value is only
    # ever a bounded prior at Level 3b.  When the siblings *are* modelled the
    # row is legitimately separable and inference is allowed.
    shared_attribution_blocked = config is not None and config.shared_unmodelled
    if shared_attribution_blocked:
        explanation.append("shared_circuit_not_individually_measured")
    # A fit pinned at the device's *declared* wattage is not a measurement of
    # the device.  The solver reached the user's own number because the row
    # carried more power than the declaration allows, so the surplus is
    # undeclared load and the honest answer is the declaration itself, graded
    # as a configured prior -- never "a CT measured it".
    declared_bound_blocked = declared_ceiling_hit and config is not None
    if declared_bound_blocked:
        explanation.append("declared_wattage_bounds_row_attribution")
    if (
        identifiable
        and not stale
        and fit > 0.0
        and not shared_attribution_blocked
        and not declared_bound_blocked
    ):
        if _is_off(state, state_overrides) and (fit_at_ceiling or fit > off_tolerance):
            # The circuit carries power, but this device says OFF.  Do not
            # invent an attribution; preserve it as unknown residual.
            explanation.append("unexplained_load_on_device_circuit")
            confidence = compute_confidence(
                ConfidenceInputs(
                    source=Source.UNKNOWN,
                    measurement_qualities=qualities,
                    measurement_ages_s=ages,
                    supporting_ct_count=len(supporting),
                    model_quality=0.0 if model is None else model.quality,
                    event_isolation=isolation,
                    topology_status=topology_status,
                    residual_w=fit,
                    residual_sigma_w=sigma,
                    stale=stale,
                )
            )
            return _finish(
                _unknown_result(
                    device_id,
                    now,
                    confidence.score,
                    quality,
                    supporting,
                    explanation,
                    provenance={
                        "unexplained_power_w": fit,
                        "reason": "device_reported_off",
                        "confidence": confidence.as_dict(),
                    },
                ),
                degraded=True,
            )
        explanation.append("constrained_nnls_fit")
        if repeated:
            explanation.append("repeated_state_update")
        if model is not None and model_power is not None:
            tolerance = max(MODEL_MATCH_REL_TOL * max(abs(model_power), 1.0), 25.0)
            if abs(fit - model_power) <= tolerance:
                explanation.append("historical_model_match")
            else:
                explanation.append("measurement_disagrees_with_model")
        confidence = compute_confidence(
            ConfidenceInputs(
                source=Source.INFERRED,
                measurement_qualities=qualities,
                measurement_ages_s=ages,
                supporting_ct_count=len(_used_support(graph, device_id, supporting)),
                model_quality=0.0 if model is None else model.quality,
                event_isolation=isolation,
                topology_status=topology_status,
                residual_w=0.0,
                residual_sigma_w=sigma,
                repeated=repeated,
            )
        )
        stage, stage_status = _stage_for(model, fit)
        agrees = (
            model is not None
            and model_power is not None
            and abs(fit - model_power)
            <= max(MODEL_MATCH_REL_TOL * max(abs(model_power), 1.0), 25.0)
        )
        return _finish(
            InferenceResult(
                device_id=device_id,
                timestamp=now,
                power_w=fit,
                source=Source.INFERRED,
                confidence=confidence.score,
                model_id=None if model is None else model.model_id,
                model_version=None if model is None else model.version,
                operating_stage=stage,
                stage_status=stage_status,
                sample_count=0 if model is None else model.sample_count,
                quality_status=quality,
                measurement_age_seconds=max(ages) if ages else 0.0,
                supporting_ct_channels=supporting,
                explanation=explanation,
                provenance={
                    "fit_w": fit,
                    "identifiable": True,
                    "confidence": confidence.as_dict(),
                },
                confidence_is_calibrated=False,
            ),
            model_corroborates=agrees,
            degraded=repeated or (config.shared_unmodelled if config else False),
        )

    # ---------------- Level 3: model estimate ------------------------
    model_usable = (
        model is not None
        and model_power is not None
        and (not _is_off(state, state_overrides) or model_power > 0.0)
    )
    if model_usable:
        explanation.append("historical_model_match")
        if not identifiable:
            explanation.append("measurements_do_not_identify_device")
        if stale:
            explanation.append("stale_measurement")
        if fit > 0.0 and identifiable:
            explanation.append("model_below_detection_or_zero_fit")
        confidence = compute_confidence(
            ConfidenceInputs(
                source=Source.MODEL_ESTIMATED,
                measurement_qualities=qualities,
                measurement_ages_s=ages,
                supporting_ct_count=len(supporting),
                model_quality=model.quality,
                event_isolation=isolation,
                topology_status=topology_status,
                residual_w=abs(fit - float(model_power)) if fit else 0.0,
                residual_sigma_w=sigma,
                stale=stale,
                missing_rows=not supporting,
                ambiguous=False,
                repeated=repeated,
            )
        )
        stage, stage_status = _stage_for(model, float(model_power))
        return _finish(
            InferenceResult(
                device_id=device_id,
                timestamp=now,
                power_w=float(model_power),
                source=Source.MODEL_ESTIMATED,
                confidence=confidence.score,
                model_id=model.model_id,
                model_version=model.version,
                operating_stage=stage,
                stage_status=stage_status,
                sample_count=model.sample_count,
                quality_status=quality,
                measurement_age_seconds=0.0 if not ages else max(ages),
                supporting_ct_channels=supporting,
                explanation=explanation,
                provenance={
                    "model_power_w": float(model_power),
                    "identifiable": False,
                    "fit_w": fit,
                    "confidence": confidence.as_dict(),
                },
                confidence_is_calibrated=False,
            )
        )

    # ---------- Level 3b: configured prior (ADR-0011) ----------------
    # Nothing measured or modelled this device, but the user has stated a
    # watt value and the device's own control signal says it is running.
    # This is a *bounded prior*, weaker than any measurement: it may explain
    # part of the residual but it never claims to have observed anything.
    # A shared circuit with unmodelled siblings is exactly the case this
    # exists for: the CT row includes neighbours, so it cannot be attributed,
    # yet a declared ``on_w`` is honest knowledge worth reporting.
    prior_power = None
    prior_reason = ""
    if config is not None and not state_off:
        resolved = configured_power(
            config, state, state_is_on=state_on, level=level
        )
        if resolved is not None:
            prior_power, prior_reason = resolved
    if prior_power is not None and prior_power > 0.0:
        explanation.append(prior_reason)
        if config is not None and config.shared_unmodelled:
            explanation.append("shared_circuit_not_attributable_from_ct")
        if config is not None and config.expected_standby_w:
            prior_power = max(float(prior_power), float(config.expected_standby_w))
        confidence = compute_confidence(
            ConfidenceInputs(
                source=Source.CONFIGURED_PRIOR,
                measurement_qualities=qualities,
                measurement_ages_s=ages,
                supporting_ct_count=0,
                model_quality=0.0 if model is None else model.quality,
                event_isolation=isolation,
                topology_status=topology_status,
                residual_w=0.0,
                residual_sigma_w=sigma,
                stale=stale,
                missing_rows=not supporting,
                repeated=repeated,
            )
        )
        return _finish(
            InferenceResult(
                device_id=device_id,
                timestamp=now,
                power_w=float(prior_power),
                source=Source.CONFIGURED_PRIOR,
                confidence=confidence.score,
                sample_count=0 if model is None else model.sample_count,
                quality_status=quality,
                measurement_age_seconds=0.0 if not ages else max(ages),
                supporting_ct_channels=supporting,
                explanation=explanation,
                provenance={
                    "configured_power_w": float(prior_power),
                    "configuration_source": prior_reason,
                    "fit_w": fit,
                    "confidence": confidence.as_dict(),
                },
                confidence_is_calibrated=False,
            ),
            configured_prior=True,
            prior_reason=prior_reason,
            degraded=stale,
        )

    # ---------------- Level 4: unknown -------------------------------
    if identifiable and stale:
        explanation.append("stale_measurement")
    elif not identifiable:
        explanation.append("insufficient_independent_measurements")
    if config is not None and config.mode_entity_ids:
        explanation.append("only_mode_signals_declared")
    if not explanation:
        explanation.append("insufficient_evidence")
    confidence = compute_confidence(
        ConfidenceInputs(
            source=Source.UNKNOWN,
            measurement_qualities=qualities,
            measurement_ages_s=ages,
            supporting_ct_count=len(supporting),
            model_quality=0.0 if model is None else model.quality,
            event_isolation=isolation,
            topology_status=topology_status,
            residual_w=0.0,
            residual_sigma_w=sigma,
            stale=stale,
            repeated=repeated,
        )
    )
    return _finish(
        _unknown_result(
            device_id,
            now,
            confidence.score,
            quality,
            supporting,
            explanation,
            provenance={"fit_w": fit, "confidence": confidence.as_dict()},
        ),
        degraded=stale or repeated,
    )
