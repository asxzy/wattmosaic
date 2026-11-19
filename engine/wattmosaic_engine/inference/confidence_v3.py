"""Seven-component estimate confidence (I5, HB §9.6, DEC-24, ADR-0007).

**This is not a probability.**  Each component is an uncalibrated engineering
score in ``[0, 1]`` (or ``None`` when the input it needs is absent).  The scores
are not calibrated, do not sum to one, and must never be read as likelihoods or
blended into a single number for display (DEC-24 retires evidence tiers in
favour of ``EstimateStatus × IdentifiabilityStatus``).  Consumers must name the
component they mean.

Each component has exactly one input family (design §7 / task I5 step 3):

======================  =========================================================
component               input
======================  =========================================================
measurement_quality     reader measurement quality scores (weakest link)
event_association       match history / match states (I6; a fake before then)
model_fit               model residual against its own sigma
topology_consistency    weakest edge lifecycle on the subject's path
identifiability         null-space share from I4
sample_coverage         independent-event count (HB §10.3: samples != events)
freshness               age of the newest contributing reading
======================  =========================================================

The module is pure: no database, no wall clock (age is injected), no globals
that change between calls.  Absent inputs give ``None``, never ``0.0`` (HB §18),
and a missing measurement is never treated as a perfect score.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from wattmosaic_engine.contracts import ConfidenceVectorV3

__all__: list[str] = [
    "DEFAULT_COVERAGE_EVENTS",
    "DEFAULT_FRESH_TAU_S",
    "EDGE_LIFECYCLE_SCORE",
    "MATCH_STATE_SCORE",
    "ConfidenceInputs",
    "compute_confidence",
    "component_event_association",
    "component_freshness",
    "component_identifiability",
    "component_measurement_quality",
    "component_model_fit",
    "component_sample_coverage",
    "component_topology_consistency",
]

#: Independent events that give full sample coverage (design §7).
DEFAULT_COVERAGE_EVENTS = 5
#: Exponential time constant, in seconds, of the freshness decay.
DEFAULT_FRESH_TAU_S = 300.0

#: Uncalibrated score of each topology edge lifecycle.  A weaker edge drags the
#: whole path down: the path is only as consistent as its weakest link.
EDGE_LIFECYCLE_SCORE: dict[str, float] = {
    "confirmed": 1.0,
    "provisional": 0.6,
    "suspected_change": 0.4,
    "candidate": 0.3,
    "conflicted": 0.2,
    "rejected": 0.0,
    "retired": 0.0,
}

#: Uncalibrated score of a match state (I6's ``MatchState`` vocabulary).
MATCH_STATE_SCORE: dict[str, float] = {
    "confirmed": 1.0,
    "provisional": 0.6,
    "proposed": 0.3,
    "unmatched": 0.0,
    "ambiguous": 0.3,
    "conflicted": 0.1,
    "rejected": 0.0,
    "expired": 0.0,
}


def _unit(value: Any) -> float | None:
    """Coerce an input score to ``[0, 1]``; ``None`` for absent/non-finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return min(1.0, max(0.0, out))


def _name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    """Everything the seven components need, all optional and all injected.

    A component whose inputs are empty returns ``None``.  In particular a
    single-subject endpoint with a direct reading has no topology edge and thus
    no ``topology_consistency`` score; that is honest absence, not a perfect 1.0.
    """

    #: Quality score in ``[0, 1]`` of each contributing reader (or None).
    reader_quality: Mapping[str, float | None] = field(default_factory=dict)
    #: Match states from the (I6) match store / history for this subject.
    match_states: Sequence[Any] = ()
    #: Number of independent matches; used when explicit states are absent.
    match_history_count: int | None = None
    #: Absolute fit residual and the model's own scale, both in watts.
    model_residual_w: float | None = None
    model_sigma_w: float | None = None
    #: Edge lifecycles on the subject's path (any nesting/order).
    edge_lifecycles: Sequence[Any] = ()
    #: I4 null-space share of the subject column.
    null_space_share: float | None = None
    #: Independent events covering this subject (HB §10.3).
    independent_event_count: int | None = None
    #: Age of the newest contributing reading, in seconds (injected).
    age_s: float | None = None

    #: Per-component overrides (e.g. I4's identifiability score).  A listed
    #: component is returned verbatim instead of being recomputed.
    overrides: Mapping[str, float | None] = field(default_factory=dict)


def component_measurement_quality(inputs: ConfidenceInputs) -> float | None:
    """Weakest contributing-reader quality; ``None`` when none is available."""
    if "measurement_quality" in inputs.overrides:
        return _unit(inputs.overrides["measurement_quality"])
    scores = [
        score
        for score in (_unit(value) for value in inputs.reader_quality.values())
        if score is not None
    ]
    return min(scores) if scores else None


def component_event_association(inputs: ConfidenceInputs) -> float | None:
    """Match-history strength: strongest confirmed state, else event count."""
    if "event_association" in inputs.overrides:
        return _unit(inputs.overrides["event_association"])
    states = [_name(state) for state in inputs.match_states]
    scores = [
        MATCH_STATE_SCORE[state] for state in states if state in MATCH_STATE_SCORE
    ]
    if scores:
        return max(scores)
    count = inputs.match_history_count
    if count is None or count < 0:
        return None
    return min(1.0, count / DEFAULT_COVERAGE_EVENTS)


def component_model_fit(inputs: ConfidenceInputs) -> float | None:
    """Fit quality from the residual relative to the model's own scale.

    ``1 / (1 + (|r| / sigma)^2)`` with ``sigma`` defaulting to a documented
    1 W reference when the model supplies no scale.  ``None`` when there is no
    residual: an absent fit is not a perfect fit.
    """
    if "model_fit" in inputs.overrides:
        return _unit(inputs.overrides["model_fit"])
    residual = inputs.model_residual_w
    if residual is None:
        return None
    scale = inputs.model_sigma_w if inputs.model_sigma_w is not None else 1.0
    if not math.isfinite(scale) or scale <= 0.0:
        scale = 1.0
    return 1.0 / (1.0 + (abs(float(residual)) / scale) ** 2)


def component_topology_consistency(inputs: ConfidenceInputs) -> float | None:
    """Weakest edge lifecycle on the path; ``None`` for a direct reading."""
    if "topology_consistency" in inputs.overrides:
        return _unit(inputs.overrides["topology_consistency"])
    scores = [
        EDGE_LIFECYCLE_SCORE[life]
        for life in (_name(value) for value in inputs.edge_lifecycles)
        if life in EDGE_LIFECYCLE_SCORE
    ]
    return min(scores) if scores else None


def component_identifiability(inputs: ConfidenceInputs) -> float | None:
    """One minus the null-space share; ``None`` when I4 gave no share."""
    if "identifiability" in inputs.overrides:
        return _unit(inputs.overrides["identifiability"])
    share = _unit(inputs.null_space_share)
    return None if share is None else 1.0 - share


def component_sample_coverage(inputs: ConfidenceInputs) -> float | None:
    """Independent-event count saturating at :data:`DEFAULT_COVERAGE_EVENTS`."""
    if "sample_coverage" in inputs.overrides:
        return _unit(inputs.overrides["sample_coverage"])
    count = inputs.independent_event_count
    if count is None or count < 0:
        return None
    return min(1.0, count / DEFAULT_COVERAGE_EVENTS)


def component_freshness(inputs: ConfidenceInputs) -> float | None:
    """Exponential decay ``exp(-age / tau)``; ``None`` when age is absent."""
    if "freshness" in inputs.overrides:
        return _unit(inputs.overrides["freshness"])
    age = inputs.age_s
    if age is None or not math.isfinite(age):
        return None
    return _unit(math.exp(-max(0.0, float(age)) / DEFAULT_FRESH_TAU_S))


_COMPONENTS = {
    "measurement_quality": component_measurement_quality,
    "event_association": component_event_association,
    "model_fit": component_model_fit,
    "topology_consistency": component_topology_consistency,
    "identifiability": component_identifiability,
    "sample_coverage": component_sample_coverage,
    "freshness": component_freshness,
}


def compute_confidence(inputs: ConfidenceInputs | None = None) -> ConfidenceVectorV3:
    """Compute all seven components independently.  **Not a probability.**

    There is deliberately no weighted sum, ``__float__`` or arithmetic on the
    result: a caller reads the named component it needs (DEC-24).
    """
    inputs = inputs or ConfidenceInputs()
    return ConfidenceVectorV3(
        **{name: fn(inputs) for name, fn in _COMPONENTS.items()}
    )
