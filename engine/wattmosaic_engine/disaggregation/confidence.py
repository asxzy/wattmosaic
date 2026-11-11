"""Engineering confidence score (never a calibrated probability).

The score is a documented, bounded heuristic in ``[0, 1]``.  It answers the
engineering question *"how much should a human trust this number?"*, not
*"what is the probability this number is within 1 W?"*.  Every
``InferenceResult`` therefore keeps ``confidence_is_calibrated = False``.

Source ceilings
---------------
=================  ======  ==========================================
source             base    meaning
=================  ======  ==========================================
direct_measured    0.97    a verified dedicated CT measured it
inferred           0.88    solved from CT rows that uniquely identify it
model_estimated    0.60    only a validated model supports the value
configured_prior   0.42    only a user-stated watt value supports it
unknown            0.15    nothing supports a device-level value
=================  ======  ==========================================

Component blend (weights sum to 1)
----------------------------------
``measurement_quality``  0.30  quality + age of the supporting CTs
``ct_support``           0.15  min(1, number of supporting CTs / 2)
``model_quality``        0.20  validation quality of the active model
``event_isolation``      0.15  1 isolated, 0.6 quiet/identified, 0.2 concurrent
``topology_certainty``   0.10  1 confirmed, 0.6 candidate, 0.2 rejected
``residual_quality``     0.10  1 / (1 + (|row residual| / (4 sigma))^2)

``confidence = base_source * blend``.  Multiplicative penalties are then
applied for stale data (x0.55), missing rows (x0.5), ambiguity (x0.5) and
repeated-but-not-fresh samples (x0.9).  The result is clamped to ``[0, 1]``.

The formula is intentionally simple and auditable.  It is *not* fitted to data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from wattmosaic_engine.contracts import QualityStatus, Source

SOURCE_BASE: dict[Source, float] = {
    Source.DIRECT_MEASURED: 0.97,
    Source.INFERRED: 0.88,
    Source.MODEL_ESTIMATED: 0.60,
    Source.CONFIGURED_PRIOR: 0.42,
    Source.UNKNOWN: 0.15,
}

QUALITY_SCORE: dict[QualityStatus, float] = {
    QualityStatus.GOOD: 1.0,
    QualityStatus.DEGRADED: 0.70,
    QualityStatus.STALE: 0.30,
    QualityStatus.MISSING: 0.0,
    QualityStatus.BAD: 0.0,
}

TOPOLOGY_SCORE = {"confirmed": 1.0, "candidate": 0.60, "rejected": 0.20}

WEIGHTS = {
    "measurement_quality": 0.30,
    "ct_support": 0.15,
    "model_quality": 0.20,
    "event_isolation": 0.15,
    "topology_certainty": 0.10,
    "residual_quality": 0.10,
}

#: Age at which the measurement-quality component starts to decay.
AGE_SOFT_LIMIT_S = 30.0
#: Age at which the measurement-quality component reaches zero.
AGE_HARD_LIMIT_S = 180.0

#: Floor for ``direct_measured`` only.  A healthy dedicated CT is a real
#: observation of the load, so its score is never allowed to fall below this
#: even when the age/residual components are imperfect (and ``0.30`` when
#: stale).
#:
#: It is **not** the 55.0 % seen on every inferred device in production.  That
#: value is the analytic fixed point of a degenerate input set — no models, no
#: corroboration, so ``blend == 0.625`` and ``0.88 * 0.625 == 0.55``.  Two
#: unrelated constants that happen to print the same number; do not "fix" the
#: inferred score by touching this one.
DIRECT_MEASURED_FLOOR = 0.55
#: The same floor when the measurement is stale: the CT proves nothing current.
DIRECT_MEASURED_STALE_FLOOR = 0.30


@dataclass(slots=True)
class ConfidenceInputs:
    source: Source
    measurement_qualities: Sequence[QualityStatus] = ()
    measurement_ages_s: Sequence[float] = ()
    supporting_ct_count: int = 0
    model_quality: float = 0.0
    event_isolation: float = 0.6
    topology_status: str = "candidate"
    residual_w: float = 0.0
    residual_sigma_w: float = 1.0
    stale: bool = False
    missing_rows: bool = False
    ambiguous: bool = False
    repeated: bool = False


@dataclass(slots=True)
class ConfidenceBreakdown:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "components": dict(self.components),
            "penalties": dict(self.penalties),
            "notes": list(self.notes),
            "calibrated": False,
        }


def age_factor(age_s: float) -> float:
    """1.0 below ``AGE_SOFT_LIMIT_S``, linearly to 0 at ``AGE_HARD_LIMIT_S``."""
    if age_s <= AGE_SOFT_LIMIT_S:
        return 1.0
    if age_s >= AGE_HARD_LIMIT_S:
        return 0.0
    span = AGE_HARD_LIMIT_S - AGE_SOFT_LIMIT_S
    return 1.0 - (age_s - AGE_SOFT_LIMIT_S) / span


def compute_confidence(inp: ConfidenceInputs) -> ConfidenceBreakdown:
    base = SOURCE_BASE.get(inp.source, 0.15)
    notes: list[str] = []

    qualities = list(inp.measurement_qualities) or [QualityStatus.MISSING]
    ages = list(inp.measurement_ages_s) or [0.0]
    quality_values = [QUALITY_SCORE.get(q, 0.0) for q in qualities]
    mean_quality = sum(quality_values) / len(quality_values)
    mean_age_factor = sum(age_factor(a) for a in ages) / len(ages)
    measurement_quality = mean_quality * mean_age_factor
    if mean_age_factor < 0.999:
        notes.append("measurement_age_penalty")

    ct_support = min(1.0, inp.supporting_ct_count / 2.0)
    if inp.supporting_ct_count == 0:
        notes.append("no_supporting_ct")

    model_quality = min(max(inp.model_quality, 0.0), 1.0)
    event_isolation = min(max(inp.event_isolation, 0.0), 1.0)
    topology = TOPOLOGY_SCORE.get(str(inp.topology_status), 0.60)
    sigma = max(float(inp.residual_sigma_w), 1e-9)
    ratio = inp.residual_w / (4.0 * sigma)
    residual_quality = 1.0 / (1.0 + ratio * ratio)

    components = {
        "measurement_quality": measurement_quality,
        "ct_support": ct_support,
        "model_quality": model_quality,
        "event_isolation": event_isolation,
        "topology_certainty": topology,
        "residual_quality": residual_quality,
    }
    blend = sum(WEIGHTS[k] * components[k] for k in WEIGHTS)

    penalties: dict[str, float] = {}
    if inp.stale:
        penalties["stale"] = 0.55
    if inp.missing_rows:
        penalties["missing_rows"] = 0.50
    if inp.ambiguous:
        penalties["ambiguous"] = 0.50
    if inp.repeated:
        penalties["repeated"] = 0.90

    score = base * blend
    for factor in penalties.values():
        score *= factor
    score = min(max(score, 0.0), 1.0)

    if inp.source == Source.DIRECT_MEASURED:
        # A healthy dedicated CT must stay clearly above every estimate even
        # when its age/residual terms are imperfect.
        score = max(
            score,
            DIRECT_MEASURED_STALE_FLOOR if inp.stale else DIRECT_MEASURED_FLOOR,
        )
    if inp.source == Source.UNKNOWN:
        score = min(score, 0.25)

    return ConfidenceBreakdown(
        score=round(score, 4),
        components={k: round(v, 4) for k, v in components.items()},
        penalties=penalties,
        notes=notes,
    )


def event_isolation_score(
    *,
    own_event: bool,
    concurrent_events: int,
    ambiguous: bool,
) -> float:
    """Map the event picture onto the ``event_isolation`` component.

    * no state event, but the device is independently identified -> 0.60
    * exactly this device changed -> 1.0
    * this device plus others changed, resolvable -> 0.45
    * unresolvable concurrent group -> 0.15
    """
    if ambiguous:
        return 0.15
    if own_event and concurrent_events <= 1:
        return 1.0
    if own_event:
        return 0.45
    return 0.60


def clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(float(value), 0.0), 1.0)
