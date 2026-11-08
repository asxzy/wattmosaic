"""Evidence tiers for a device power value (ADR-0013).

Why tiers exist
---------------
The numeric ``confidence`` score blends six components into a continuous
heuristic.  That is good for ranking two devices but a poor *label*: ``0.62``
does not tell a user whether the number came from a dedicated CT or from an
unvalidated model.  The tier answers exactly that question, and it is
deliberately **coarse** — five ordered grades.

The tier is a *grade*, never a probability.  ``confidence_is_calibrated`` stays
``False``.

What the tier orders, and what it does not
------------------------------------------
The tier orders **evidence kind**, which is the question a user asks when they
see a watt value: *"did something measure this, or is it a guess?"*

===============================  ============================================
tier                             evidence
===============================  ============================================
``tier_0_unknown``               nothing supports a device-level value
``tier_1_modelled``              a configured prior or a model only
``tier_2_inferred``              separated from CT rows, uncorroborated
``tier_3_measured``              a CT measured it (or a corroborated share)
``tier_4_verified``              measured *and* independently corroborated
===============================  ============================================

It does **not** restate the score, and the score is never rewritten to fit the
tier.  The two disagree in one specific, intended case: a *stale* dedicated CT
keeps its evidence grade (a CT did measure it) while its score collapses,
because the number is old.  That is not a contradiction — the tier says *how*
the value was obtained, the score says *how much to trust the number right now*.
When the two diverge, a ``tier_score_divergence`` reason is attached so the
disagreement is visible rather than hidden.  Do not read the tier as a bucket of
the score; read it as the evidence label, and read the score for magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass

from wattmosaic_engine.contracts import (
    TIER_RANK,
    ConfidenceTier,
    MonitoringLevel,
    PowerBehavior,
    Source,
)

#: Documented *typical* score range per tier, used only to flag divergence.  Not
#: enforced: the score is a blend of six components and a narrow range here would
#: destroy its resolution (a repeated-update sample legitimately scores slightly
#: lower than a fresh one inside the same tier).
TIER_TYPICAL_SCORE: dict[ConfidenceTier, tuple[float, float]] = {
    ConfidenceTier.TIER_0_UNKNOWN: (0.0, 0.25),
    ConfidenceTier.TIER_1_MODELLED: (0.20, 0.65),
    ConfidenceTier.TIER_2_INFERRED: (0.45, 0.85),
    ConfidenceTier.TIER_3_MEASURED: (0.30, 0.97),
    ConfidenceTier.TIER_4_VERIFIED: (0.60, 1.00),
}

#: Human-readable meaning, surfaced verbatim by the API so clients do not have to
#: hard-code their own interpretation.
TIER_DESCRIPTION: dict[ConfidenceTier, str] = {
    ConfidenceTier.TIER_0_UNKNOWN: (
        "no evidence supports a device-level value; the power is left in the "
        "unknown residual"
    ),
    ConfidenceTier.TIER_1_MODELLED: (
        "value comes from a validated model or a configured prior only; no live "
        "measurement independently confirms it"
    ),
    ConfidenceTier.TIER_2_INFERRED: (
        "separated from shared or bulk CT rows; no independent corroboration"
    ),
    ConfidenceTier.TIER_3_MEASURED: (
        "a CT measured it, or a declared load signal corroborates a "
        "shared-channel estimate"
    ),
    ConfidenceTier.TIER_4_VERIFIED: (
        "measured and independently corroborated by a declared load signal or a "
        "validated model agreeing within tolerance, on a fresh sample"
    ),
}


@dataclass(slots=True, frozen=True)
class TierInputs:
    """Everything :func:`grade` is allowed to look at.

    Keeping this a flat, explicit dataclass (rather than passing the engine's
    whole state) is what makes the tier auditable and unit-testable.
    """

    source: Source
    monitoring: MonitoringLevel = MonitoringLevel.UNKNOWN
    behavior: PowerBehavior = PowerBehavior.UNKNOWN
    #: The device's declared load signal says it is running.
    state_says_on: bool = False
    #: The device's declared load signal says it is definitely not running.
    state_says_off: bool = False
    #: A load entity is actually declared for this device.  Without one, a state
    #: value arriving in ``device_states`` is not evidence of anything: it may be
    #: a stale key or an unlinked entity, and treating it as corroboration would
    #: promote a device to "verified" on no evidence at all.
    has_declared_load_signal: bool = False
    #: A validated model exists and agrees with the value within tolerance.
    model_corroborates: bool = False
    #: The value came from a *confirmed* controller-state -> watts mapping.
    configured_prior: bool = False
    #: Which configured prior produced the value, used verbatim as the reason.
    prior_reason: str = ""
    #: Sample is old; a measured value that is stale cannot be "verified".
    stale: bool = False
    #: Rows were missing, the solve was ambiguous, or the row is a shared upper
    #: bound.  Never promotes; only blocks verification.
    degraded: bool = False


def evidence_tier(inp: TierInputs) -> tuple[ConfidenceTier, list[str]]:
    """Classify the evidence behind one attribution."""
    reasons: list[str] = []

    # ---- Tier 0: nothing supports a device-level number ------------------
    if inp.source == Source.UNKNOWN:
        reasons.append("source_unknown")
        if inp.monitoring == MonitoringLevel.HOME_TOTAL_ONLY:
            reasons.append("only_visible_in_home_total")
        elif inp.monitoring == MonitoringLevel.UNMONITORED:
            reasons.append("no_ct_observes_device")
        return ConfidenceTier.TIER_0_UNKNOWN, reasons

    # ---- Tier 1: a model or a configured prior, nothing live ------------
    if inp.source == Source.MODEL_ESTIMATED:
        reasons.append("model_only")
        if inp.stale:
            reasons.append("stale")
        return ConfidenceTier.TIER_1_MODELLED, reasons

    if inp.source == Source.CONFIGURED_PRIOR:
        reasons.append(inp.prior_reason or "configured_prior_only")
        if inp.stale:
            reasons.append("stale")
        return ConfidenceTier.TIER_1_MODELLED, reasons

    corroborated = inp.model_corroborates or (
        inp.state_says_on and inp.has_declared_load_signal
    )

    # ---- Tier 3/4: a CT actually measured this device -------------------
    if inp.source == Source.DIRECT_MEASURED:
        reasons.append("dedicated_ct_measurement")
        if inp.stale:
            reasons.append("dedicated_ct_stale:not_verifiable")
            return ConfidenceTier.TIER_3_MEASURED, reasons
        if inp.state_says_off:
            # The CT carries power but the device claims to be off.  The value is
            # measured; the *attribution* is what is in question.
            reasons.append("dedicated_ct_disagrees_with_state:not_verifiable")
            return ConfidenceTier.TIER_3_MEASURED, reasons
        if corroborated and not inp.degraded:
            reasons.append(
                "model_corroborates" if inp.model_corroborates else "load_signal_corroborates"
            )
            return ConfidenceTier.TIER_4_VERIFIED, reasons
        if corroborated:
            reasons.append("degraded_measurements")
        return ConfidenceTier.TIER_3_MEASURED, reasons

    # ---- Tier 2/3: separated from rows ---------------------------------
    if inp.source == Source.INFERRED:
        reasons.append("constrained_separation")
        if inp.stale:
            reasons.append("inferred_stale")
            return ConfidenceTier.TIER_2_INFERRED, reasons
        if inp.monitoring == MonitoringLevel.SHARED:
            reasons.append("shared_circuit_separation")
        if corroborated and not inp.degraded:
            reasons.append(
                "model_corroborates" if inp.model_corroborates else "load_signal_corroborates"
            )
            return ConfidenceTier.TIER_3_MEASURED, reasons
        if inp.degraded:
            reasons.append("degraded_measurements")
        return ConfidenceTier.TIER_2_INFERRED, reasons

    reasons.append("unhandled_source")  # pragma: no cover - Source is a closed enum
    return ConfidenceTier.TIER_0_UNKNOWN, reasons


def grade(inp: TierInputs, score: float) -> tuple[ConfidenceTier, float, list[str]]:
    """Return ``(tier, score, reasons)`` for one attribution.

    The score is returned **unchanged**: grading evidence must not rewrite a
    measurement's trustworthiness, and clamping it into a per-tier band would
    erase the difference between a fresh and a repeated sample within one tier.
    """
    tier, reasons = evidence_tier(inp)
    low, high = TIER_TYPICAL_SCORE[tier]
    if score < low or score > high:
        reasons.append(
            f"tier_score_divergence:{score:.2f}_outside_typical_{low:.2f}_{high:.2f}"
        )
    return tier, float(score), reasons


def tier_rank(tier: ConfidenceTier | str) -> int:
    """Numeric rank for comparisons; unknown strings sort lowest."""
    if isinstance(tier, ConfidenceTier):
        return TIER_RANK[tier]
    try:
        return TIER_RANK[ConfidenceTier(str(tier))]
    except ValueError:
        return 0


def describe(tier: ConfidenceTier) -> str:
    return TIER_DESCRIPTION[tier]


__all__ = [
    "TIER_DESCRIPTION",
    "TIER_TYPICAL_SCORE",
    "TierInputs",
    "describe",
    "evidence_tier",
    "grade",
    "tier_rank",
]
