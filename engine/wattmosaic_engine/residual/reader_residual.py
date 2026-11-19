"""Per-reader residual engine and learned unknown-load prior (T3, design §6).

The module is pure: it reads no clock and no database.  ``update`` takes the
joint-inference result, the held readings and the observation operator, and
returns :class:`ReaderResidualPoint` contracts keyed by reader; persistence is
an explicit, injected repository call (S19's batch ``put_points``).  The
background prior job is likewise a pure function of an injected clock and the
points supplied to it, so it can run in an executor (P6) without touching the
event loop or the wall clock.

Three objects stay strictly distinct (rule 5):

* measurement noise ``σ_r`` — produced by P2 and read from an injected
  ``noise_sigma_w`` lookup; never re-estimated here;
* the signed residual series ``R_r`` — computed here and persisted per reader
  and topology version (fixes DEF-05);
* the learned unknown-load prior ``UnknownPrior`` — a *penalty* summary of
  unexplained residual steps, published for I3 to consume.

Absent values are ``None``, never 0 W (HB §18).  Residuals are signed and never
clamped.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Protocol

from wattmosaic_engine.contracts import (
    JointInferenceResult,
    MeasurementBasis,
    ReaderResidualPoint,
)
from wattmosaic_engine.models.residual_power import baseline_and_dynamic, signed_residual_w
from wattmosaic_engine.runtime.interfaces import (
    HeldReading,
    ObservationOperator,
    UnknownPrior,
)

__all__: list[str] = [
    "DEFAULT_PENALTY_SCALE",
    "INITIAL_PENALTY_SCALE_FRACTION",
    "MAE_WINDOW_DAYS",
    "PRIOR_WINDOW_DAYS",
    "ROLLING_SCALE_DAYS",
    "ReaderResidualEngine",
    "UnknownPriorModel",
    "estimate_unknown_prior",
]

#: ``WM_UNKNOWN_PENALTY_SCALE`` (DEC-23).  The caller supplies this scale; T3
#: never reads the environment or a settings singleton.  Default 1.0.
DEFAULT_PENALTY_SCALE = 1.0

#: The initial operating point favours *residual over attribution*: before
#: enough unexplained steps have been observed, λ_u starts low so the unknown
#: competitor is cheap (DEC-23).
INITIAL_PENALTY_SCALE_FRACTION = 0.25

#: Look-back for the learned prior (design §6: "the last 7 days").
PRIOR_WINDOW_DAYS = 7.0

#: Look-back for the per-day over/under-assignment diagnostic (design §6).
MAE_WINDOW_DAYS = 7.0

#: Window used to turn a reader's |ΔY| step history into a σ scaling factor.
ROLLING_SCALE_DAYS = 7.0

#: The initial σ_U is a *penalty scale*, not a measurement (I3 uses the same
#: fallback).  A wide σ keeps the unknown column cheap until data characterises
#: the scope, which is DEC-23's "favour residual" operating point.
INITIAL_UNKNOWN_SIGMA_W = 200.0

#: 1.4826·MAD is the consistent estimator of σ for a normal distribution.
_MAD_TO_SIGMA = 1.4826

_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Optional repository seam (S19) and noise lookup (P2).
# --------------------------------------------------------------------------- #


class ResidualPointSink(Protocol):
    """The S19 batch writer T3 needs (``ResidualRepository.put_points``)."""

    def put_points(self, batch: Sequence[ReaderResidualPoint]) -> int: ...


NoiseLookup = Mapping[str, float | None]


# --------------------------------------------------------------------------- #
# Measurement equation — one reader at a time, signed, no clamp.
# --------------------------------------------------------------------------- #


def _observability_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _direct_subject_total(
    operator: ObservationOperator,
    result: JointInferenceResult,
    row_index: int,
) -> float:
    """``Σ_i H_ri·P̂_i`` for subjects observed DIRECTLY on reader ``r``.

    Only direct entries are summed here.  A subject that enters a parent row as
    EXPECTED belongs to a child reader and is accounted for by that child's
    measured reading in the child term, so counting it here too would double
    count it.  Identified subjects and group sums count once: a member of a
    group contributes only through its group's identified sum.
    """
    groups = {g.group_id: g for g in result.group_estimates}
    group_of = {
        (e.subject_kind, e.subject_id): e.group_id
        for e in result.device_estimates
        if e.group_id is not None
    }
    points = {
        (e.subject_kind, e.subject_id): e.point_w for e in result.device_estimates
    }

    counted_groups: set[str] = set()
    contribution = 0.0
    for column, subject in enumerate(operator.subjects):
        entry = operator.entries.get((row_index, column))
        if entry is None or _observability_value(entry.observability) != "direct":
            continue
        coefficient = float(entry.coefficient)
        if coefficient == 0.0 or not math.isfinite(coefficient):
            continue
        group_id = group_of.get(subject)
        if group_id is not None:
            if group_id in counted_groups:
                continue
            counted_groups.add(group_id)
            group = groups.get(group_id)
            point = None if group is None else group.point_w
        else:
            point = points.get(subject)
        if point is not None and math.isfinite(float(point)):
            contribution += coefficient * float(point)
    return contribution


def _child_contributions(
    operator: ObservationOperator,
    held: Mapping[str, HeldReading],
    reader_id: str,
) -> float:
    """``Σ_c β·Y_c``: held readings of child readers, weighted by path β.

    A reader ``c`` is a child of ``r`` when some subject is observed DIRECTLY on
    ``c`` and EXPECTED on ``r`` along a confirmed path.  ``β`` is the ratio of
    the two operator coefficients (unity for an uncalibrated edge, faithfully
    carried through for a recorded factor or estimated β).  Children with no
    usable reading contribute nothing — their load stays in the residual rather
    than becoming a fabricated 0 W measurement (HB §18, DEC-12).
    """
    rows = list(operator.rows)
    row_index = next(i for i, meta in enumerate(rows) if meta.reader_id == reader_id)
    child_beta: dict[str, float] = {}
    for column in range(len(operator.subjects)):
        parent_entry = operator.entries.get((row_index, column))
        if parent_entry is None or not parent_entry.edge_ids:
            continue
        if _observability_value(parent_entry.observability) == "direct":
            continue
        for r, meta in enumerate(rows):
            if r == row_index:
                continue
            entry = operator.entries.get((r, column))
            if entry is None or _observability_value(entry.observability) != "direct":
                continue
            if entry.coefficient == 0.0:
                continue
            beta = parent_entry.coefficient / entry.coefficient
            if math.isfinite(beta):
                child_beta[meta.reader_id] = beta

    total = 0.0
    for child in sorted(child_beta):
        reading = held.get(child)
        if reading is None or reading.value is None:
            continue
        total += child_beta[child] * float(reading.value)
    return total


# --------------------------------------------------------------------------- #
# The engine.
# --------------------------------------------------------------------------- #


@dataclass
class ReaderResidualEngine:
    """Compute, publish and persist per-reader residuals.

    ``noise_sigma_w`` is an injected P2 lookup (reader → σ); production code
    never re-estimates noise.  ``penalty_scale`` is ``WM_UNKNOWN_PENALTY_SCALE``.
    """

    noise_sigma_w: NoiseLookup | None = None
    penalty_scale: float = DEFAULT_PENALTY_SCALE

    def update(
        self,
        result: JointInferenceResult,
        held: Mapping[str, HeldReading],
        operator: ObservationOperator,
        *,
        windows: Mapping[str, Sequence[float]] | None = None,
        sink: ResidualPointSink | None = None,
    ) -> tuple[ReaderResidualPoint, ...]:
        """Return one signed residual point per operator row.

        ``R_r = Y_r − Σ_c β·Y_c − Σ_i H_ri·P̂_i``.  A reader whose ``held``
        reading is absent or has ``value is None`` yields a point with
        ``residual_w=None`` (never 0 W).  When ``sink`` is supplied the points
        are persisted in one S19 batch call.
        """
        windows = windows or {}
        points: list[ReaderResidualPoint] = []
        for row_index, meta in enumerate(operator.rows):
            reading = held.get(meta.reader_id)
            y_r = None if reading is None else reading.value
            if y_r is None:
                residual_w = None
            else:
                child_sum = _child_contributions(operator, held, meta.reader_id)
                subject_sum = _direct_subject_total(operator, result, row_index)
                residual_w = signed_residual_w(float(y_r), child_sum + subject_sum)
            baseline_w, dynamic_w = baseline_and_dynamic(
                residual_w, windows.get(meta.reader_id, ())
            )
            sigma = None if self.noise_sigma_w is None else self.noise_sigma_w.get(meta.reader_id)
            if sigma is None and reading is not None:
                sigma = getattr(reading, "noise_sigma_w", None)
            points.append(
                ReaderResidualPoint(
                    reader_id=meta.reader_id,
                    at=operator.at,
                    residual_w=residual_w,
                    baseline_w=baseline_w,
                    dynamic_w=dynamic_w,
                    noise_sigma_w=sigma,
                    topology_version_id=operator.topology_version_id,
                    basis=self._basis(reading),
                )
            )
        if sink is not None:
            sink.put_points(points)
        return tuple(points)

    @staticmethod
    def _basis(reading: HeldReading | None) -> MeasurementBasis:
        if reading is not None:
            basis = getattr(reading, "basis", None)
            if basis is not None:
                return MeasurementBasis(basis)
        return MeasurementBasis.UNKNOWN


# --------------------------------------------------------------------------- #
# Diagnostics: over/under-assignment to residual vs subjects.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssignmentDiagnostics:
    """Per reader/day |ΔY| shares: ΔU (unexplained) vs subjects vs residual."""

    scope_reader_id: str
    day: datetime
    unexplained_share: float | None
    subject_share: float | None
    residual_share: float | None


def assignment_diagnostics(
    points: Sequence[ReaderResidualPoint],
    *,
    day: datetime | None = None,
) -> tuple[AssignmentDiagnostics, ...]:
    """Summarise per reader per day how |ΔY| splits among ΔU, subjects, residual.

    The dynamic part (``dynamic_w``) is the unexplained step; ``residual_w``
    minus the dynamic part is the steady residual; the remainder of |ΔY| is the
    part assigned to identified subjects.  Each share is ``None`` when there is
    no observation to divide — absent is not zero (HB §18).
    """
    by_reader: dict[str, list[ReaderResidualPoint]] = defaultdict(list)
    for point in points:
        if point.residual_w is None:
            continue
        by_reader[point.reader_id].append(point)
    out: list[AssignmentDiagnostics] = []
    for reader in sorted(by_reader):
        day_stamp = day or by_reader[reader][-1].at
        total = sum(abs(float(p.residual_w)) for p in by_reader[reader])
        unexplained = sum(
            abs(float(p.dynamic_w)) for p in by_reader[reader] if p.dynamic_w is not None
        )
        steady = sum(
            abs(float(p.residual_w) - float(p.dynamic_w))
            for p in by_reader[reader]
            if p.dynamic_w is not None
        )
        if total <= _EPS:
            shares: tuple[float | None, float | None, float | None] = (None, None, None)
        else:
            shares = (
                min(1.0, unexplained / total),
                min(1.0, max(0.0, 1.0 - (unexplained + steady) / total)),
                min(1.0, steady / total),
            )
        out.append(AssignmentDiagnostics(reader, day_stamp, shares[0], shares[1], shares[2]))
    return tuple(out)


# --------------------------------------------------------------------------- #
# The learned unknown-load prior.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnexplainedStep:
    """One unexplained residual step in a scope (from the event ledger)."""

    at: datetime
    delta_w: float
    delta_y_w: float | None = None


@dataclass
class UnknownPriorModel:
    """In-memory ``ResidualModelView`` for one or more scopes (T3 → I3).

    ``update`` replaces a scope's prior atomically; ``unknown_prior`` answers
    the protocol's time-scoped query.  With no learned prior for a scope the
    *initial* penalty is returned, so I3 always has a cheap unknown competitor
    rather than a fabricated zero (DEC-23).
    """

    penalty_scale: float = DEFAULT_PENALTY_SCALE
    _priors: dict[str, UnknownPrior] | None = None

    def __post_init__(self) -> None:
        if self._priors is None:
            object.__setattr__(self, "_priors", {})
        if not math.isfinite(self.penalty_scale) or self.penalty_scale < 0.0:
            raise ValueError("penalty_scale must be finite and non-negative")

    @property
    def initial_lambda_u(self) -> float:
        return INITIAL_PENALTY_SCALE_FRACTION * self.penalty_scale

    def initial_prior(self, scope_reader_id: str) -> UnknownPrior:
        return UnknownPrior(
            scope_reader_id=scope_reader_id,
            mu_w=0.0,
            sigma_w=INITIAL_UNKNOWN_SIGMA_W,
            lambda_u=self.initial_lambda_u,
            step_rate_per_h=None,
        )

    def update(self, prior: UnknownPrior) -> None:
        assert self._priors is not None
        self._priors[prior.scope_reader_id] = prior

    def replace(self, priors: Mapping[str, UnknownPrior]) -> None:
        assert self._priors is not None
        self._priors.clear()
        self._priors.update(priors)

    def unknown_prior(self, scope_reader_id: str, at: datetime) -> UnknownPrior | None:
        assert self._priors is not None
        prior = self._priors.get(scope_reader_id)
        return self.initial_prior(scope_reader_id) if prior is None else prior


def _lambda_u(penalty_scale: float, step_rate_per_h: float) -> float:
    """``scale × 1/(1 + step_rate_per_h)`` (design §6).

    A scope with frequent unexplained activity makes the unknown competitor
    cheap (smaller λ_u); a quiet scope raises it.  A missing or negative rate
    falls back to the initial operating point.
    """
    if not math.isfinite(step_rate_per_h) or step_rate_per_h < 0.0:
        return INITIAL_PENALTY_SCALE_FRACTION * penalty_scale
    return float(penalty_scale) / (1.0 + float(step_rate_per_h))


def estimate_unknown_prior(
    scope_reader_id: str,
    steps: Sequence[UnexplainedStep],
    *,
    at: datetime,
    penalty_scale: float = DEFAULT_PENALTY_SCALE,
    window_days: float = PRIOR_WINDOW_DAYS,
) -> UnknownPrior:
    """Learn ``(μ_U, σ_U, λ_u, step_rate_per_h)`` for one scope.

    Only steps in ``[at - window_days, at]`` count.  ``μ_U`` is the median of
    the step deltas, ``σ_U = 1.4826·MAD`` (never a fabricated 0.0 when the MAD
    is zero), and ``step_rate_per_h`` is the count divided by the window hours.
    With no steps the initial, residual-favouring penalty is returned.
    """
    start = at - timedelta(days=float(window_days))
    rates = [
        float(s.delta_w)
        for s in steps
        if s.delta_w is not None and start <= s.at <= at
    ]
    hours = max(float(window_days) * 24.0, _EPS)
    step_rate = len(rates) / hours
    if not rates:
        return UnknownPrior(
            scope_reader_id=scope_reader_id,
            mu_w=0.0,
            sigma_w=INITIAL_UNKNOWN_SIGMA_W,
            lambda_u=INITIAL_PENALTY_SCALE_FRACTION * penalty_scale,
            step_rate_per_h=0.0,
        )
    mu = float(median(rates))
    mad = float(median([abs(value - mu) for value in rates]))
    sigma = _MAD_TO_SIGMA * mad
    if not math.isfinite(sigma) or sigma <= 0.0:
        # A zero MAD is not "no uncertainty"; it is a degenerate spread, so we
        # fall back to a finite penalty scale rather than dividing by zero.
        sigma = INITIAL_UNKNOWN_SIGMA_W
    return UnknownPrior(
        scope_reader_id=scope_reader_id,
        mu_w=mu,
        sigma_w=sigma,
        lambda_u=_lambda_u(penalty_scale, step_rate),
        step_rate_per_h=step_rate,
    )


class ResidualPriorJob:
    """The background (P6 executor) recompute of every scope's UnknownPrior.

    It is pure given ``steps_by_scope`` and ``clock``; the caller runs it in an
    executor and feeds the result to :class:`UnknownPriorModel`.  No wall clock,
    no DB, no event loop I/O happens here.
    """

    def __init__(
        self,
        model: UnknownPriorModel,
        *,
        clock: Any,
        window_days: float = PRIOR_WINDOW_DAYS,
    ) -> None:
        self.model = model
        self.clock = clock
        self.window_days = float(window_days)

    def run(
        self, steps_by_scope: Mapping[str, Sequence[UnexplainedStep]]
    ) -> dict[str, UnknownPrior]:
        at = self.clock()
        priors = {
            scope: estimate_unknown_prior(
                scope,
                steps,
                at=at,
                penalty_scale=self.model.penalty_scale,
                window_days=self.window_days,
            )
            for scope, steps in steps_by_scope.items()
        }
        self.model.replace(priors)
        return priors


# --------------------------------------------------------------------------- #
# The diagnostic contract used by /residuals (T3 owns this routing).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResidualAssignmentReport:
    """The v3 ``/residuals?reader_id=`` payload: points + prior + diagnostic."""

    points: tuple[ReaderResidualPoint, ...]
    unknown_prior: UnknownPrior | None
    diagnostics: tuple[AssignmentDiagnostics, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "points": [p.model_dump(mode="json") for p in self.points],
            "unknown_prior": (
                None
                if self.unknown_prior is None
                else {
                    "scope_reader_id": self.unknown_prior.scope_reader_id,
                    "mu_w": self.unknown_prior.mu_w,
                    "sigma_w": self.unknown_prior.sigma_w,
                    "lambda_u": self.unknown_prior.lambda_u,
                    "step_rate_per_h": self.unknown_prior.step_rate_per_h,
                }
            ),
            "diagnostics": [
                {
                    "scope_reader_id": d.scope_reader_id,
                    "day": d.day.isoformat(),
                    "unexplained_share": d.unexplained_share,
                    "subject_share": d.subject_share,
                    "residual_share": d.residual_share,
                }
                for d in self.diagnostics
            ],
        }
