"""Matching evidence A–H for one candidate over an event window (HB §7.4, I2).

Each term is a log-likelihood contribution in **nats**, clipped to
``[clip_min_nats, clip_max_nats]`` from the versioned ``evidence_weights.json``.
Terms C, D and E all derive from the same ΔY vector; their sum is multiplied by
the dependence-correction factor (``1 / sqrt(3)``) so one reader's evidence is
never counted three times.  The score is a *prior penalty* for the joint solve,
never a winner-take-all choice (design §7).

The module is pure: no database, no wall clock.  All time comes from the
supplied event group and window samples; all model numbers come from the
supplied ports.  A missing ΔY is ``None`` (HB §18), never zero.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    DeltaPrior,
    EdgeLifecycleV3,
    EventGroup,
    HAEvent,
    Observability,
    PredictionStatus,
    ReaderDelta,
)
from wattmosaic_engine.runtime.interfaces import (
    ModelSetView,
    ObservationOperator,
    PredictionContext,
)

from .temporal import ResponseWindow, expected_response, onset_offsets

__all__ = [
    "WEIGHTS_VERSION",
    "EVIDENCE_TERMS",
    "EvidenceInputs",
    "EvidenceResult",
    "load_weights",
    "score_candidate",
    "score_candidates",
    "score_and_record",
    "evidence_window",
    "temporal_fit",
    "version_as_float",
]

TERMS: tuple[str, ...] = tuple("ABCDEFGH")
EVIDENCE_TERMS: tuple[str, ...] = TERMS
WEIGHTS_PATH = Path(__file__).with_name("evidence_weights.json")

# Loaded once, deterministically, at import time (versioned config file).
_CONFIG = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
WEIGHTS_VERSION: str = str(_CONFIG["version"])
_CLIP_MIN: float = float(_CONFIG["clip_min_nats"])
_CLIP_MAX: float = float(_CONFIG["clip_max_nats"])
_WEIGHTS: Mapping[str, float] = {
    term: float(_CONFIG["weights"].get(term, 1.0)) for term in TERMS
}
_CORRECTION_GROUP: tuple[str, ...] = tuple(
    _CONFIG.get("dependence_correction", {}).get("group", ["C", "D", "E"])
)
_CORRECTION_FACTOR: float = float(
    _CONFIG.get("dependence_correction", {}).get("factor", 1.0 / math.sqrt(3.0))
)

# Edge/observability confidences used by term E.  PROVISIONAL is soft (0.5).
_EDGE_CONFIDENCE: Mapping[str, float] = {
    EdgeLifecycleV3.CONFIRMED.value: 1.0,
    EdgeLifecycleV3.PROVISIONAL.value: 0.5,
    EdgeLifecycleV3.CANDIDATE.value: 0.25,
}
_OBSERVABILITY_CONFIDENCE: Mapping[str, float] = {
    Observability.DIRECT.value: 1.0,
    Observability.EXPECTED.value: 0.8,
    Observability.INFERRED.value: 0.5,
    Observability.PARTIAL.value: 0.4,
    Observability.UNKNOWN.value: 0.0,
    Observability.NOT_OBSERVABLE.value: 0.0,
    Observability.CONFLICTED.value: 0.0,
}

# Neutral term value (log 0.5 = -ln 2): "no information" is a small penalty,
# never a fabricated zero of evidence.
_NEUTRAL: float = -math.log(2.0)
_MIN_SIGMA = 1e-3


def load_weights(path: str | Path | None = None) -> dict[str, Any]:
    """Return the evidence-weights config (versioned).

    With no argument the module's packaged config is returned.  A path may be
    supplied by a caller that owns a different config revision; the result is
    always a fresh dictionary.
    """
    source = Path(path) if path is not None else WEIGHTS_PATH
    return json.loads(source.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@dataclass(frozen=True)
class EvidenceInputs:
    """Everything I2 needs to score candidates, supplied by the caller.

    ``samples_by_reader`` holds the immutable event-window samples
    ``[(observed_at, power_w | None), ...]`` per reader (P4's ``EventEvidence``
    samples).  It is an explicit input so this module stays pure and can be
    tested against the K08 fakes without any storage access.
    """

    group: EventGroup
    operator: ObservationOperator
    models: ModelSetView
    samples_by_reader: Mapping[str, Sequence[tuple[datetime, float | None]]] | None = None
    attrs: Mapping[str, Any] | None = None
    profiles: Mapping[str, Any] | None = None
    ha_events: Mapping[str, HAEvent] | None = None


@dataclass(frozen=True)
class EvidenceResult:
    """The structured score for one candidate."""

    candidate_id: str
    subject_id: str
    terms: dict[str, float]  # keys A..H, each clipped to [clip_min, clip_max]
    score: float  # weighted, dependence-corrected sum
    temporal_fit: float  # normalised [0, 1] window fit (unclipped comparator)
    rejected: bool  # hard rejection (confirmed consumer, clear ON sign clash)
    rejection_reason: str | None
    weights_version: str


# --------------------------------------------------------------------------- #
# Small pure helpers.
# --------------------------------------------------------------------------- #


def _clip(value: float) -> float:
    """Clip one term to the configured ``[min, max]`` nats range."""
    if not math.isfinite(value):
        return _CLIP_MIN
    return max(_CLIP_MIN, min(_CLIP_MAX, value))


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _delta(rd: ReaderDelta) -> float | None:
    """A missing ΔY stays ``None``; it is never treated as zero."""
    return _finite(rd.delta_w)


def _sigma(rd: ReaderDelta) -> float | None:
    value = _finite(rd.sigma_w)
    return value if value is not None and value > 0.0 else None


def _subject_index(operator: ObservationOperator) -> dict[tuple[str, str], int]:
    return {subject: i for i, subject in enumerate(operator.subjects)}


def _columns_for(
    candidate: CandidateHypothesis, operator: ObservationOperator
) -> tuple[int, ...]:
    """Operator column indices for this candidate's subject (possibly a group)."""
    index = _subject_index(operator)
    subject = (candidate.subject_kind, candidate.subject_id)
    if subject in index:
        return (index[subject],)
    if _enum_value(candidate.subject_kind) == "group":
        members = set(candidate.subject_id.split(","))
        return tuple(i for (kind, sid), i in index.items() if sid in members)
    return ()


def _window_details(group: EventGroup) -> tuple[float, list[float]]:
    """Return the group duration (s) and per-sub-event onset uncertainties (s)."""
    duration = (group.closed_at - group.opened_at).total_seconds() if group.closed_at else 0.0
    uncertainties: list[float] = []
    for sub in group.sub_events:
        value = _finite(sub.timestamp_uncertainty_s)
        if value is not None and value >= 0.0:
            uncertainties.append(value)
    return max(0.0, duration), uncertainties


def evidence_window(group: EventGroup) -> tuple[Any, ...]:
    """A deterministic summary tuple ``(opened_at, closed_at, duration_s)``."""
    duration = (group.closed_at - group.opened_at).total_seconds() if group.closed_at else 0.0
    return (group.opened_at, group.closed_at, max(0.0, duration))


def _sample_evidence(
    inputs: EvidenceInputs,
    reader_id: str,
) -> tuple[np.ndarray, np.ndarray, float | None]:
    """Observed ΔY samples relative to the pre-event baseline.

    Returns ``(tau_s, deltas_w, sigma_w)``.  Samples are taken from the explicit
    ``samples_by_reader`` input; a reader with no samples contributes no
    dimensions.  ``sigma_w`` is ``None`` when the reader declares no usable
    measurement uncertainty.
    """
    group = inputs.group
    samples = (inputs.samples_by_reader or {}).get(reader_id, ())
    pre: list[float] = []
    post: list[tuple[float, float]] = []
    for at, value in samples:
        value_f = _finite(value)
        if value_f is None:
            continue
        tau = (at - group.opened_at).total_seconds()
        if tau < 0.0:
            pre.append(value_f)
        else:
            post.append((tau, value_f))
    if not post:
        return np.empty(0, dtype=float), np.empty(0, dtype=float), None
    tau = np.array([t for t, _ in post], dtype=float)
    baseline = float(np.median(pre)) if pre else 0.0
    deltas = np.array([v - baseline for _, v in post], dtype=float)
    sigma: float | None = None
    for rd in group.reader_deltas:
        if rd.reader_id == reader_id:
            value = _sigma(rd)
            if value is not None:
                sigma = value
    return tau, deltas, sigma


# --------------------------------------------------------------------------- #
# Terms A–H (raw nats before clipping).
# --------------------------------------------------------------------------- #


def _term_a(
    inputs: EvidenceInputs,
    response: ResponseWindow,
    onset_uncertainty_s: float,
) -> float:
    """Temporal agreement: Gaussian log-likelihood over the whole window.

    Every reader's ΔY samples are compared with the kernel-predicted mean and
    σ over the *whole* window, marginalised over a uniform grid of onset
    offsets in ``[-u, +u]``.  A step response is evaluated analytically, so a
    90 s compressor step is *matched*, not dropped as unanchored.
    """
    if response.mean_w is None:
        return _NEUTRAL
    grid = onset_offsets(onset_uncertainty_s)
    per_offset = np.zeros(len(grid), dtype=float)
    used = False
    for row in inputs.operator.rows:
        tau, deltas, obs_sigma = _sample_evidence(inputs, row.reader_id)
        if tau.size == 0:
            continue
        for k, offset in enumerate(grid):
            mean, model_sigma = response.evaluate(tau, float(offset))
            if mean is None:
                continue
            if model_sigma is not None:
                sigma = np.asarray(model_sigma, dtype=float)
            else:
                sigma = np.full(tau.size, _MIN_SIGMA, dtype=float)
            if obs_sigma is not None:
                sigma = np.maximum(sigma, obs_sigma)
            sigma = np.maximum(sigma, _MIN_SIGMA)
            resid = (deltas - mean) / sigma
            per_offset[k] += float(
                np.sum(-0.5 * resid * resid - np.log(sigma * math.sqrt(2.0 * math.pi)))
            )
            used = True
    if not used:
        return _NEUTRAL
    finite = per_offset[np.isfinite(per_offset)]
    if finite.size == 0:
        return _NEUTRAL
    peak = float(np.max(finite))
    return float(peak + math.log(float(np.mean(np.exp(finite - peak)))))


def temporal_fit(
    inputs: EvidenceInputs,
    response: ResponseWindow,
    onset_uncertainty_s: float = 0.0,
) -> float:
    """Normalised window-fit quality in ``[0, 1]`` (1 = exact match).

    This is the unclipped, scale-free comparator for term A.  Raw Gaussian
    log-likelihoods grow with the number of window samples and clip to
    ``clip_min_nats`` for *every* wrong candidate, so they cannot rank two
    delayed kernels against each other.  I3 consumes this as the
    ``(1 - temporal_fit)`` temporal penalty (design §7) and AT-05 uses it to
    assert the correct delay wins.
    """
    if response.mean_w is None:
        return 0.0
    offsets = onset_offsets(onset_uncertainty_s)
    residuals: list[float] = []
    observed: list[float] = []
    for row in inputs.operator.rows:
        tau, deltas, obs_sigma = _sample_evidence(inputs, row.reader_id)
        if tau.size == 0:
            continue
        observed.extend(float(v) for v in deltas)
        # Marginalise the onset over the uncertainty grid: the best-fitting
        # offset contributes the fit, matching term A's marginalisation.
        best_chi2 = float("inf")
        for offset in offsets:
            mean, model_sigma = response.evaluate(tau, float(offset))
            if mean is None:
                continue
            scale = np.asarray(model_sigma, dtype=float) if model_sigma is not None else None
            if obs_sigma is not None:
                scale = np.full(tau.size, obs_sigma) if scale is None else np.maximum(scale, obs_sigma)
            if scale is None:
                scale = np.ones(tau.size, dtype=float)
            scale = np.maximum(np.asarray(scale, dtype=float), _MIN_SIGMA)
            chi2 = float(np.mean(np.square((deltas - mean) / scale)))
            best_chi2 = min(best_chi2, chi2)
        if best_chi2 < float("inf"):
            residuals.append(math.sqrt(best_chi2))
    if not observed or not residuals:
        return 0.0
    # Normalised RMSE in [0, 1], with 1 = exact fit and 0 = no fit.  The
    # denominator combines the observed energy and the noise floor so a
    # constant window (e.g. an instantaneous light step observed only post-
    # onset) still discriminates a correct step from a delayed one: a 90 s
    # delayed kernel leaves the early samples residual=amplitude/σ for many
    # samples, while the correct onset leaves no residual at all.
    obs = np.asarray(observed, dtype=float)
    energy = float(np.mean(obs * obs))
    noise_floor = 0.25  # a residual of ±0.5σ is already a mediocre fit
    nrmse = float(np.mean(np.asarray(residuals, dtype=float)))
    value = 1.0 / (1.0 + nrmse / (math.sqrt(energy) + noise_floor))
    return float(max(0.0, min(1.0, value)))


def _transition_sign(candidate: CandidateHypothesis) -> str:
    transition = (candidate.transition or "").lower()
    if any(tok in transition for tok in ("off", "down", "close", "stop", "decrease")):
        return "neg"
    if any(tok in transition for tok in ("on", "open", "start", "up", "increase")):
        return "pos"
    return "any"


def _term_b(
    group: EventGroup, candidate: CandidateHypothesis, prior: DeltaPrior | None
) -> tuple[float, bool, str | None]:
    """Sign agreement.  Returns ``(nats, hard_reject, reason)``.

    A sign disagreement is a *hard rejection* only for a confirmed consumer
    with an ON transition and a ΔY clearly negative beyond 3σ (I2 step 4).
    Every other disagreement is a penalty.
    """
    sign = prior.sign if prior is not None else _transition_sign(candidate)
    signed = [
        (rd, d)
        for rd in group.reader_deltas
        if (d := _delta(rd)) is not None and abs(d) > 1e-9
    ]
    if not signed or sign == "any":
        return _NEUTRAL, False, None
    expected = 1.0 if sign == "pos" else -1.0
    agreeing = [abs(d) for _, d in signed if d * expected > 0]
    disagreeing = [(rd, d) for rd, d in signed if d * expected < 0]
    if not disagreeing:
        return _CLIP_MAX, False, None
    if not agreeing:
        base = -2.0
    else:
        total = sum(agreeing) + sum(abs(d) for _, d in disagreeing)
        base = 2.0 * (sum(agreeing) / total - 0.5) if total > 0 else _NEUTRAL

    sources = {_enum_value(s) for s in candidate.sources}
    # "Confirmed consumer" means an established mapping/plug/topology source,
    # not merely being a device candidate (an unconfirmed HA state is a device
    # candidate too).  Only those established sources can hard-reject.
    confirmed_sources = {"prior_mapping", "plug", "topology_visible", "device_group"}
    consumer = bool(sources & confirmed_sources)
    on_transition = (candidate.transition or "").lower() in ("on", "start", "open")
    clearly_negative = False
    for rd, d in disagreeing:
        sigma = _sigma(rd)
        if d < 0.0 and sigma is not None and d < -3.0 * sigma:
            clearly_negative = True
            break
    if consumer and on_transition and clearly_negative:
        return _CLIP_MIN, True, "sign_disagreement_confirmed_consumer_on"
    return base, False, None


def _term_c(prior: DeltaPrior | None, deltas: list[float]) -> float:
    """Amplitude agreement with ``DeltaPrior`` (Gaussian log-density)."""
    if prior is None or not deltas:
        return _NEUTRAL
    mean = _finite(prior.mean_w)
    sigma = _finite(prior.sigma_w)
    if mean is None or sigma is None or sigma <= 0.0:
        return _NEUTRAL
    terms = []
    for d in deltas:
        resid = (abs(d) - abs(mean)) / sigma
        terms.append(-0.5 * resid * resid - math.log(sigma * math.sqrt(2.0 * math.pi)))
    return float(np.mean(terms))


def _term_d(
    group: EventGroup, operator: ObservationOperator, columns: tuple[int, ...]
) -> float:
    """Reader-signature agreement (cosine of an H column and the signed ΔY)."""
    delta_by_reader: dict[str, float] = {
        rd.reader_id: d for rd in group.reader_deltas if (d := _delta(rd)) is not None
    }
    if not columns or not delta_by_reader:
        return _NEUTRAL
    rows = operator.rows
    deltas = np.array([delta_by_reader.get(row.reader_id, 0.0) for row in rows], dtype=float)
    H = np.asarray(operator.H, dtype=float)
    best = _NEUTRAL
    for col in columns:
        h = H[:, col].astype(float)
        denom = float(np.linalg.norm(h) * np.linalg.norm(deltas))
        if denom <= 0.0:
            continue
        best = max(best, float(np.dot(h, deltas) / denom))
    return best


def _term_e(
    group: EventGroup,
    operator: ObservationOperator,
    columns: tuple[int, ...],
    candidate: CandidateHypothesis,
) -> float:
    """Topology / mapping consistency (provisional edges are soft)."""
    if not columns:
        return _NEUTRAL if _enum_value(candidate.subject_kind) == "unknown_scope" else -1.0
    affected = {rd.reader_id for rd in group.reader_deltas if _delta(rd) is not None}
    H = np.asarray(operator.H, dtype=float)
    total = 0.0
    for col in columns:
        matching = [
            i
            for i, row in enumerate(operator.rows)
            if row.reader_id in affected and abs(H[i, col]) > 0.0
        ]
        if not matching:
            # No affected reader sees this subject: topology disagrees.
            confidences = [0.0]
            total += 0.0
            continue
        confidences: list[float] = []
        for i in matching:
            entry = operator.entries.get((i, col))
            if entry is None:
                confidences.append(0.7)
                continue
            lifecycle = _EDGE_CONFIDENCE.get(_enum_value(entry.edge_lifecycle), 0.5)
            if entry.edge_lifecycle is None:
                lifecycle = 1.0
            obs = _OBSERVABILITY_CONFIDENCE.get(_enum_value(entry.observability), 0.5)
            confidences.append(min(lifecycle, obs))
        total += max(confidences) if confidences else 0.5
    score = total / len(columns)
    return math.log(score) if score > 0.0 else -5.0


def _term_f(
    models: ModelSetView, candidate: CandidateHypothesis, at: datetime
) -> float:
    """Model agreement: a learned model with a known level supports the match."""
    try:
        prediction = models.predict(candidate.subject_id, PredictionContext(at=at))
    except Exception:
        return _NEUTRAL
    if prediction.status == PredictionStatus.UNKNOWN or prediction.mean_w is None:
        return _NEUTRAL
    return 1.0 if prediction.sigma_w is not None else 0.5


def _term_g(rd_list: list[ReaderDelta]) -> float:
    """Measurement quality for the readers that carry a ΔY."""
    scores: list[float] = []
    for rd in rd_list:
        if _delta(rd) is None:
            continue
        scores.append(0.8 if _sigma(rd) is not None else 0.5)
    if not scores:
        return _NEUTRAL
    return math.log(max(float(np.mean(scores)), _MIN_SIGMA))


def _term_h(
    group: EventGroup,
    candidate: CandidateHypothesis,
    ha_events: Mapping[str, HAEvent] | None,
) -> float:
    """HA context lineage: linked sub-events and candidate HA sources."""
    sources = {_enum_value(s) for s in candidate.sources}
    ha_source = bool(sources & {"ha_state", "ha_attribute"})
    linked = any(
        _enum_value(sub.kind) in {"ha_state", "ha_attribute"}
        and ha_events is not None
        and (ev := ha_events.get(sub.ref.id)) is not None
        and ev.linked
        for sub in group.sub_events
    )
    if linked:
        score = 0.9
    elif ha_source:
        score = 0.7
    else:
        score = 0.4
    if ha_source:
        score = min(1.0, score + 0.1)
    return math.log(max(score, _MIN_SIGMA))


# --------------------------------------------------------------------------- #
# Public scoring entry points.
# --------------------------------------------------------------------------- #


def score_candidate(candidate: CandidateHypothesis, inputs: EvidenceInputs) -> EvidenceResult:
    """Compute evidence A–H for one candidate and its dependence-corrected score."""
    group = inputs.group
    operator = inputs.operator
    models = inputs.models
    attrs: Mapping[str, Any] = inputs.attrs if inputs.attrs is not None else {}
    transition = candidate.transition

    duration, uncertainties = _window_details(group)
    grid = np.linspace(0.0, max(duration, 1.0), 64)
    profile = None
    if inputs.profiles is not None:
        profile = inputs.profiles.get(candidate.subject_id)
    response = expected_response(
        candidate, transition, attrs, grid, models=models, profile=profile
    )
    onset_u = max(uncertainties) if uncertainties else 0.0

    columns = _columns_for(candidate, operator)
    prior = models.transition_prior(candidate.subject_id, transition, attrs) if transition else None
    deltas = [d for rd in group.reader_deltas if (d := _delta(rd)) is not None]

    term_b, hard_reject, reason = _term_b(group, candidate, prior)
    fit = 0.0 if hard_reject else temporal_fit(inputs, response, onset_u)
    raw: dict[str, float] = {
        "A": _NEUTRAL if hard_reject else _term_a(inputs, response, onset_u),
        "B": term_b,
        "C": _term_c(prior, deltas),
        "D": _term_d(group, operator, columns),
        "E": _term_e(group, operator, columns, candidate),
        "F": _term_f(models, candidate, group.opened_at),
        "G": _term_g(group.reader_deltas),
        "H": _term_h(group, candidate, inputs.ha_events),
    }
    clipped = {term: _clip(raw[term]) for term in TERMS}

    score = 0.0
    for term in TERMS:
        contribution = _WEIGHTS[term] * clipped[term]
        if term in _CORRECTION_GROUP:
            contribution *= _CORRECTION_FACTOR
        score += contribution

    return EvidenceResult(
        candidate_id=candidate.candidate_id,
        subject_id=candidate.subject_id,
        terms=clipped,
        score=score,
        temporal_fit=fit,
        rejected=hard_reject,
        rejection_reason=reason,
        weights_version=WEIGHTS_VERSION,
    )


def score_candidates(
    candidates: list[CandidateHypothesis], inputs: EvidenceInputs
) -> dict[str, EvidenceResult]:
    """Score all candidates, returning ``candidate_id -> EvidenceResult``."""
    return {c.candidate_id: score_candidate(c, inputs) for c in candidates}


def version_as_float(version: str) -> float:
    """Encode a semver string as a unique, monotonic float.

    ``"1.0.0" -> 1.0``, ``"1.2.3" -> 1.0203``.  The result is recorded under
    the reserved ``_weights_version`` key of the float-only evidence dict.
    """
    parts = version.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 1
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        major, minor, patch = 1, 0, 0
    return float(f"{major}.{minor:02d}{patch:02d}")


def _apply_evidence(candidate: CandidateHypothesis, result: EvidenceResult) -> CandidateHypothesis:
    """Write clipped terms and the version onto a (new) candidate.

    ``CandidateHypothesis.evidence_terms`` is ``dict[str, float]``, so the
    version is stored as a float under the reserved ``_weights_version`` key.
    The authoritative string is on :class:`EvidenceResult` and
    :data:`WEIGHTS_VERSION`.
    """
    terms = dict(result.terms)
    terms["_weights_version"] = version_as_float(WEIGHTS_VERSION)
    updates: dict[str, Any] = {"evidence_terms": terms}
    if result.rejected:
        updates["filtered"] = True
        updates["filter_reason"] = f"evidence:{result.rejection_reason}"
    return candidate.model_copy(update=updates)


def score_and_record(
    candidates: list[CandidateHypothesis], inputs: EvidenceInputs
) -> tuple[list[CandidateHypothesis], dict[str, EvidenceResult]]:
    """Score all candidates and return them with evidence terms populated.

    The returned candidates are *copies*; inputs are never mutated.  This is a
    convenience wrapper for callers (I3/I7) that want the scored candidates and
    the full :class:`EvidenceResult` detail in one step.
    """
    results = score_candidates(candidates, inputs)
    scored = [_apply_evidence(c, results[c.candidate_id]) for c in candidates]
    return scored, results
