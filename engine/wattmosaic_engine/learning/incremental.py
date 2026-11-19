"""Per-class incremental power-model learners (L5, design §9, analysis F5).

Each model class gets its own incremental update, fed by device-attributed
:class:`~wattmosaic_engine.contracts.LearningObservation` objects:

* FIXED — windowed robust median/MAD plus a P² quantile estimator;
* ADJUSTABLE — bin-wise PAVA (monotone isotonic regression) on the control
  variable;
* DYNAMIC — running, state-conditioned distributions and transition counts;
* RESPONSE_KERNEL — robust running phase (delay/amplitude/duration) stats;
* COMPONENT_SUM — via the declared component ids (no numeric learning here).

The module is pure: no database and no wall clock.  An absent statistic is
``None``, never ``0.0`` (HB §18).  ``fit_incremental`` and ``fit_batch`` use the
same grouping; the only difference is that the incremental path accumulates
one observation at a time while the batch path collects first.  They therefore
agree to well within the 2 % acceptance tolerance.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wattmosaic_engine.contracts import (
    AdjustableSpec,
    ComponentSumSpec,
    DynamicSpec,
    FixedSpec,
    KernelPhase,
    LearningObservation,
    LevelStat,
    ModelClass,
    PowerLevelV3,
    ResponseKernelSpec,
)

__all__ = [
    "P2Quantile",
    "RobustStats",
    "IncrementalAccumulator",
    "fit_incremental",
    "fit_batch",
    "predict_spec",
]

#: Bounded retention window for the exact robust statistics.  Bounding it keeps
#: the per-subject worker memory-flat; a larger window only improves accuracy.
DEFAULT_WINDOW = 4096
#: Number of bins used by the bin-wise PAVA adjustable updater.
DEFAULT_BINS = 12

_ON_LEVELS = frozenset({"on", "open", "running", "active", "heating", "cooling", "true", "1"})
_OFF_LEVELS = frozenset({"off", "closed", "false", "0"})


def _name(value: Any) -> str:
    return str(getattr(value, "value", value)) if value is not None else ""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _exact_mad(values: Sequence[float]) -> float | None:
    if not values:
        return None
    med = statistics.median(values)
    return float(statistics.median([abs(v - med) for v in values]))


# --------------------------------------------------------------------------- #
# Robust primitives.
# --------------------------------------------------------------------------- #


class P2Quantile:
    """The P² (Jain–Chlamtac) single-pass quantile estimator.

    Constant memory and O(1) per update.  Used as the fast O(1) median estimate;
    :class:`RobustStats` additionally retains a bounded window so its published
    median/MAD are exact for the retained samples.
    """

    __slots__ = ("_desired", "_marker", "_n", "_p", "_positions", "_q", "_sorted")

    def __init__(self, p: float = 0.5) -> None:
        if not 0 < p < 1:
            raise ValueError("p must be strictly between 0 and 1")
        self._p = float(p)
        self._desired = [0.0, self._p / 2.0, self._p, (1.0 + self._p) / 2.0, 1.0]
        self._marker = [1.0, 1.0 + 2 * self._p, 1.0 + 4 * self._p, 3.0 + 2 * self._p, 5.0]
        self._positions = list(range(1, 6))
        self._q: list[float] = []
        self._sorted = False
        self._n = 0

    @property
    def count(self) -> int:
        return self._n

    def _find_k(self, x: float) -> int:
        if x < self._q[0]:
            self._q[0] = x
            return 1
        for i in range(1, 5):
            if self._q[i - 1] <= x < self._q[i]:
                return i
        self._q[4] = x
        return 4

    @staticmethod
    def _parabolic(qp1: float, q: float, qm1: float, d: float, np1: float, n: float, nm1: float) -> float:
        d, n, np1, nm1 = float(d), float(n), float(np1), float(nm1)
        outer = d / (np1 - nm1)
        inner_left = (n - nm1 + d) * (qp1 - q) / (np1 - n)
        inner_right = (np1 - n - d) * (q - qm1) / (n - nm1)
        return q + outer * (inner_left + inner_right)

    def _adjust(self) -> None:
        import math

        for i in (1, 2, 3):
            n = self._positions[i]
            q = self._q[i]
            d = self._marker[i] - n
            if (d >= 1 and self._positions[i + 1] - n > 1) or (
                d <= -1 and self._positions[i - 1] - n < -1
            ):
                d = int(math.copysign(1, d))
                qn = self._parabolic(
                    self._q[i + 1], q, self._q[i - 1], d,
                    self._positions[i + 1], n, self._positions[i - 1],
                )
                if self._q[i - 1] < qn < self._q[i + 1]:
                    self._q[i] = qn
                else:
                    self._q[i] = q + d * (self._q[i + d] - q) / (self._positions[i + d] - n)
                self._positions[i] = n + d

    def add(self, x: float) -> None:
        x = float(x)
        self._n += 1
        if len(self._q) != 5:
            self._q.append(x)
            return
        if not self._sorted:
            self._q.sort()
            self._sorted = True
        k = self._find_k(x)
        self._positions = [j if i < k else j + 1 for i, j in enumerate(self._positions)]
        self._marker = [a + b for a, b in zip(self._marker, self._desired, strict=False)]
        self._adjust()

    def median(self) -> float | None:
        if not self._q:
            return None
        if self._sorted:
            return float(self._q[2])
        ordered = sorted(self._q)
        index = int(min(max(len(ordered) - 1, 0), len(ordered) * self._p))
        return float(ordered[index])

    def quantile(self) -> float | None:
        return self.median()


@dataclass(slots=True)
class RobustStats:
    """Windowed robust median/MAD with a parallel O(1) P² median estimate."""

    window: int = DEFAULT_WINDOW
    _values: deque[float] = field(default_factory=deque)
    _p2: P2Quantile = field(default_factory=lambda: P2Quantile(0.5))

    @property
    def count(self) -> int:
        return self._p2.count

    def add(self, value: float | None) -> None:
        number = _finite(value)
        if number is None:
            return
        self._values.append(number)
        if len(self._values) > self.window:
            self._values.popleft()
        self._p2.add(number)

    def median(self) -> float | None:
        if not self._values:
            return None
        return float(statistics.median(self._values))

    def mad(self) -> float | None:
        return _exact_mad(list(self._values))

    def p2_median(self) -> float | None:
        return self._p2.median()

    def stat(self) -> LevelStat:
        return LevelStat(median_w=self.median(), mad_w=self.mad(), n=self.count)


# --------------------------------------------------------------------------- #
# Observation grouping helpers.
# --------------------------------------------------------------------------- #


def _level_for(observation: LearningObservation) -> PowerLevelV3 | None:
    transition = _name(observation.transition).lower()
    if transition in _ON_LEVELS:
        return PowerLevelV3.ACTIVE
    if transition in _OFF_LEVELS:
        return PowerLevelV3.STANDBY
    if transition in {"idle"}:
        return PowerLevelV3.IDLE
    if transition in {"transient"}:
        return PowerLevelV3.TRANSIENT
    if transition in {"standby"}:
        return PowerLevelV3.STANDBY
    return None


def _control(observation: LearningObservation) -> float | None:
    attrs: Mapping[str, Any] = observation.attributes or {}
    return _finite(attrs.get("control_value", attrs.get("control", attrs.get("level"))))


def _state(observation: LearningObservation) -> str:
    attrs: Mapping[str, Any] = observation.attributes or {}
    state = _name(attrs.get("state"))
    return state or _name(observation.transition) or "unknown"


def _attr_float(observation: LearningObservation, key: str) -> float | None:
    attrs: Mapping[str, Any] = observation.attributes or {}
    return _finite(attrs.get(key))


# --------------------------------------------------------------------------- #
# Batch fits (exact, order-independent reference implementation).
# --------------------------------------------------------------------------- #


def _batch_fixed(observations: Iterable[LearningObservation]) -> FixedSpec:
    levels: dict[str, list[float]] = {}
    transitions: dict[str, list[float]] = {}
    for obs in observations:
        value = _finite(obs.delta_w)
        if value is None:
            continue
        level = _level_for(obs)
        if level is not None:
            levels.setdefault(_name(level), []).append(value)
        transitions.setdefault(_name(obs.transition), []).append(value)
    return FixedSpec(
        levels={
            PowerLevelV3(key): _stat(values) for key, values in levels.items()
        },
        transition_delta={key: _stat(values) for key, values in transitions.items()},
    )


def _stat(values: Sequence[float]) -> LevelStat:
    if not values:
        return LevelStat()
    med = statistics.median(values)
    mad = _exact_mad(values)
    return LevelStat(median_w=float(med), mad_w=mad, n=len(values))


def _bins(observations: Iterable[LearningObservation], n_bins: int) -> list[list[float]]:
    pairs: list[tuple[float, float]] = []
    for obs in observations:
        x = _control(obs)
        y = _finite(obs.delta_w)
        if x is None or y is None:
            continue
        pairs.append((x, y))
    if not pairs:
        return []
    xs = [x for x, _ in pairs]
    low, high = min(xs), max(xs)
    bins: list[list[float]] = [[] for _ in range(max(1, n_bins))]
    span = high - low
    for x, y in pairs:
        idx = 0 if span == 0 else min(len(bins) - 1, int((x - low) / span * len(bins)))
        bins[idx].append(y)
    return bins


def _pava_points(bins: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    """One (x, robust-median-y) point per non-empty bin, made monotone by PAVA."""
    from ..models.base import enforce_monotonic

    points: list[tuple[float, float]] = []
    for idx, values in enumerate(bins):
        if values:
            med = statistics.median(values)
            points.append((float(idx), float(med)))
    return enforce_monotonic(points)


def _batch_adjustable(observations: Iterable[LearningObservation], n_bins: int) -> AdjustableSpec:
    obs = list(observations)
    points = _pava_points(_bins(obs, n_bins))
    knots_x = [float(x) for x, _ in points]
    knots_w = [float(y) for _, y in points]
    standby_values = [
        value
        for o in obs
        if _control(o) is None and (value := _finite(o.delta_w)) is not None
    ]
    covered = [x for o in obs if (x := _control(o)) is not None]
    coverage = (min(covered), max(covered)) if covered else None
    return AdjustableSpec(
        control_variable="control_value",
        knots_x=knots_x,
        knots_w=knots_w,
        monotone=True,
        x_coverage=coverage,
        standby=_stat(standby_values),
    )


def _batch_dynamic(observations: Iterable[LearningObservation]) -> DynamicSpec:
    states: dict[str, list[float]] = {}
    durations: dict[str, list[float]] = {}
    transitions: dict[str, dict[str, int]] = {}
    previous: str | None = None
    for obs in observations:
        state = _state(obs)
        value = _finite(obs.delta_w)
        if value is not None:
            states.setdefault(state, []).append(value)
        duration = _attr_float(obs, "duration_s")
        if duration is not None:
            durations.setdefault(state, []).append(duration)
        if previous is not None:
            counts = transitions.setdefault(previous, {})
            counts[state] = counts.get(state, 0) + 1
        previous = state
    return DynamicSpec(
        states={key: _stat(values) for key, values in states.items()},
        duration_s={key: _stat(values) for key, values in durations.items()},
        transition_counts=transitions,
    )


_PHASES = ("startup", "active", "steady", "shutdown", "standby")


def _batch_kernel(observations: Iterable[LearningObservation]) -> ResponseKernelSpec:
    grouped: dict[str, dict[str, dict[str, list[float]]]] = {}
    for obs in observations:
        transition = _name(obs.transition) or "unknown"
        by_phase = grouped.setdefault(
            transition,
            {phase: {"delay": [], "amplitude": [], "duration": []} for phase in _PHASES},
        )
        delay = _attr_float(obs, "delay_s")
        amplitude = _attr_float(obs, "amplitude_w")
        if amplitude is None:
            amplitude = _finite(obs.delta_w)
        duration = _attr_float(obs, "duration_s")
        for phase in _PHASES:
            if delay is not None:
                by_phase[phase]["delay"].append(delay)
            if amplitude is not None:
                by_phase[phase]["amplitude"].append(amplitude)
            if duration is not None:
                by_phase[phase]["duration"].append(duration)
    return ResponseKernelSpec(
        by_transition={
            transition: [
                KernelPhase(
                    phase=phase,  # type: ignore[arg-type]
                    delay_s=_stat(by_phase[phase]["delay"]),
                    amplitude_w=_stat(by_phase[phase]["amplitude"]),
                    duration_s=_stat(by_phase[phase]["duration"]),
                )
                for phase in _PHASES
            ]
            for transition, by_phase in grouped.items()
        }
    )


def _batch_component_sum(observations: Iterable[LearningObservation]) -> ComponentSumSpec:
    ids: list[str] = []
    for obs in observations:
        attrs: Mapping[str, Any] = obs.attributes or {}
        value = attrs.get("component_id")
        if value is not None and str(value) not in ids:
            ids.append(str(value))
    return ComponentSumSpec(component_ids=ids)


# --------------------------------------------------------------------------- #
# Incremental accumulator.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class IncrementalAccumulator:
    """Accumulates observations one at a time into a per-class spec.

    ``merge`` combines two accumulators (useful for the background worker's
    partition-then-merge), and ``spec`` materialises the current model.  All
    operations are pure and deterministic.
    """

    model_class: ModelClass | str
    window: int = DEFAULT_WINDOW
    bins: int = DEFAULT_BINS
    _levels: dict[str, RobustStats] = field(default_factory=dict)
    _transitions: dict[str, RobustStats] = field(default_factory=dict)
    _states: dict[str, RobustStats] = field(default_factory=dict)
    _durations: dict[str, RobustStats] = field(default_factory=dict)
    _counts: dict[str, dict[str, int]] = field(default_factory=dict)
    _control_points: dict[float, RobustStats] = field(default_factory=dict)
    _standby: RobustStats = field(default_factory=RobustStats)
    _kernel: dict[str, dict[str, dict[str, RobustStats]]] = field(default_factory=dict)
    _component_ids: list[str] = field(default_factory=list)
    _previous_state: str | None = None
    _size: int = 0

    def _new_stats(self) -> RobustStats:
        return RobustStats(window=self.window)

    def update(self, observation: LearningObservation) -> None:
        value = _finite(observation.delta_w)
        self._size += 1
        model_class = _name(self.model_class)

        if model_class == ModelClass.FIXED.value:
            if value is not None:
                level = _level_for(observation)
                if level is not None:
                    self._levels.setdefault(_name(level), self._new_stats()).add(value)
                self._transitions.setdefault(_name(observation.transition), self._new_stats()).add(value)
            return

        if model_class == ModelClass.ADJUSTABLE.value:
            control = _control(observation)
            if control is None or value is None:
                if value is not None:
                    self._standby.add(value)
            else:
                self._control_points.setdefault(control, self._new_stats()).add(value)
            return

        if model_class == ModelClass.DYNAMIC.value:
            state = _state(observation)
            if value is not None:
                self._states.setdefault(state, self._new_stats()).add(value)
            duration = _attr_float(observation, "duration_s")
            if duration is not None:
                self._durations.setdefault(state, self._new_stats()).add(duration)
            if self._previous_state is not None:
                counts = self._counts.setdefault(self._previous_state, {})
                counts[state] = counts.get(state, 0) + 1
            self._previous_state = state
            return

        if model_class == ModelClass.RESPONSE_KERNEL.value:
            transition = _name(observation.transition) or "unknown"
            by_phase = self._kernel.setdefault(
                transition,
                {phase: {k: self._new_stats() for k in ("delay", "amplitude", "duration")} for phase in _PHASES},
            )
            delay = _attr_float(observation, "delay_s")
            amplitude = _attr_float(observation, "amplitude_w")
            if amplitude is None:
                amplitude = value
            duration = _attr_float(observation, "duration_s")
            for phase in _PHASES:
                if delay is not None:
                    by_phase[phase]["delay"].add(delay)
                if amplitude is not None:
                    by_phase[phase]["amplitude"].add(amplitude)
                if duration is not None:
                    by_phase[phase]["duration"].add(duration)
            return

        if model_class == ModelClass.COMPONENT_SUM.value:
            attrs: Mapping[str, Any] = observation.attributes or {}
            identifier = attrs.get("component_id")
            if identifier is not None and str(identifier) not in self._component_ids:
                self._component_ids.append(str(identifier))

    def merge(self, other: IncrementalAccumulator) -> IncrementalAccumulator:
        merged = IncrementalAccumulator(self.model_class, window=self.window, bins=self.bins)
        merged._levels = _merge_stats(self._levels, other._levels, self.window)
        merged._transitions = _merge_stats(self._transitions, other._transitions, self.window)
        merged._states = _merge_stats(self._states, other._states, self.window)
        merged._durations = _merge_stats(self._durations, other._durations, self.window)
        merged._counts = _merge_counts(self._counts, other._counts)
        merged._control_points = _merge_stats(self._control_points, other._control_points, self.window)
        merged._component_ids = list(dict.fromkeys([*self._component_ids, *other._component_ids]))
        for stats, other_stats in ((self._standby, other._standby),):
            merged._standby = _combine_stats(stats, other_stats, self.window)
        for transition, by_phase in self._kernel.items():
            target = merged._kernel.setdefault(
                transition,
                {phase: {k: self._new_stats() for k in ("delay", "amplitude", "duration")} for phase in _PHASES},
            )
            for phase in _PHASES:
                for key in ("delay", "amplitude", "duration"):
                    target[phase][key] = _combine_stats(
                        by_phase[phase][key],
                        other._kernel.get(transition, {}).get(phase, {}).get(key, RobustStats(window=self.window)),
                        self.window,
                    )
        return merged

    def spec(self):  # type: ignore[no-untyped-def]
        model_class = _name(self.model_class)
        if model_class == ModelClass.ADJUSTABLE.value:
            points = _pava_points(self._bin_values())
            return AdjustableSpec(
                control_variable="control_value",
                knots_x=[float(x) for x, _ in points],
                knots_w=[float(y) for _, y in points],
                monotone=True,
                x_coverage=self._coverage(),
                standby=self._standby.stat(),
            )
        if model_class == ModelClass.DYNAMIC.value:
            return DynamicSpec(
                states={key: value.stat() for key, value in self._states.items()},
                duration_s={key: value.stat() for key, value in self._durations.items()},
                transition_counts=dict(self._counts),
            )
        if model_class == ModelClass.RESPONSE_KERNEL.value:
            return ResponseKernelSpec(
                by_transition={
                    transition: [
                        KernelPhase(
                            phase=phase,  # type: ignore[arg-type]
                            delay_s=by_phase[phase]["delay"].stat(),
                            amplitude_w=by_phase[phase]["amplitude"].stat(),
                            duration_s=by_phase[phase]["duration"].stat(),
                        )
                        for phase in _PHASES
                    ]
                    for transition, by_phase in self._kernel.items()
                }
            )
        if model_class == ModelClass.COMPONENT_SUM.value:
            return ComponentSumSpec(component_ids=list(self._component_ids))
        return FixedSpec(
            levels={PowerLevelV3(key): value.stat() for key, value in self._levels.items()},
            transition_delta={key: value.stat() for key, value in self._transitions.items()},
        )

    def _bin_values(self) -> list[list[float]]:
        """Distribute the per-control-value robust medians into ``bins`` bins."""
        if not self._control_points:
            return []
        xs = sorted(self._control_points)
        low, high = xs[0], xs[-1]
        bins: list[list[float]] = [[] for _ in range(max(1, self.bins))]
        span = high - low
        for x in xs:
            med = self._control_points[x].median()
            if med is None:
                continue
            idx = 0 if span == 0 else min(len(bins) - 1, int((x - low) / span * len(bins)))
            bins[idx].append(med)
        return bins

    def _coverage(self) -> tuple[float, float] | None:
        if not self._control_points:
            return None
        xs = sorted(self._control_points)
        return (xs[0], xs[-1])


def _merge_stats(
    left: Mapping[Any, RobustStats], right: Mapping[Any, RobustStats], window: int
) -> dict[Any, RobustStats]:
    out: dict[Any, RobustStats] = {}
    for key in set(left) | set(right):
        out[key] = _combine_stats(
            left.get(key, RobustStats(window=window)),
            right.get(key, RobustStats(window=window)),
            window,
        )
    return out


def _combine_stats(left: RobustStats, right: RobustStats, window: int) -> RobustStats:
    merged = RobustStats(window=window)
    for value in [*left._values, *right._values]:
        merged.add(value)
    return merged


def _merge_counts(left: Mapping[str, Mapping[str, int]], right: Mapping[str, Mapping[str, int]]) -> dict:  # type: ignore[no-untyped-def]
    out: dict[str, dict[str, int]] = {}
    for src in (left, right):
        for state, counts in src.items():
            target = out.setdefault(state, {})
            for dest, count in counts.items():
                target[dest] = target.get(dest, 0) + count
    return out


def fit_incremental(
    observations: Iterable[LearningObservation],
    model_class: ModelClass | str,
    *,
    window: int = DEFAULT_WINDOW,
    bins: int = DEFAULT_BINS,
):  # type: ignore[no-untyped-def]
    """Fit a per-class spec by accumulating observations one at a time."""
    accumulator = IncrementalAccumulator(model_class, window=window, bins=bins)
    for observation in observations:
        accumulator.update(observation)
    return accumulator.spec()


def fit_batch(
    observations: Iterable[LearningObservation],
    model_class: ModelClass | str,
    *,
    bins: int = DEFAULT_BINS,
):  # type: ignore[no-untyped-def]
    """Fit a per-class spec from the complete observation set (reference path)."""
    obs = list(observations)
    model = _name(model_class)
    if model == ModelClass.ADJUSTABLE.value:
        return _batch_adjustable(obs, bins)
    if model == ModelClass.DYNAMIC.value:
        return _batch_dynamic(obs)
    if model == ModelClass.RESPONSE_KERNEL.value:
        return _batch_kernel(obs)
    if model == ModelClass.COMPONENT_SUM.value:
        return _batch_component_sum(obs)
    return _batch_fixed(obs)


# --------------------------------------------------------------------------- #
# Prediction from a spec (used by L5 validation/promotion on the frozen set).
# --------------------------------------------------------------------------- #


def predict_spec(spec: Any, observation: LearningObservation) -> float | None:
    """Point prediction of one observation from a persisted spec, or ``None``.

    ``None`` is returned when there is no honest value (HB §18); it is never a
    fabricated zero.
    """
    if spec is None:
        return None
    kind = _name(getattr(spec, "kind", None))
    if not kind:
        for candidate in ("fixed", "adjustable", "dynamic", "response_kernel", "component_sum"):
            if _name(getattr(spec, candidate, None)) or hasattr(spec, "levels") and candidate == "fixed":
                kind = candidate
                break
    if kind in ("fixed", ""):
        if hasattr(spec, "levels") and spec.levels:
            level = _level_for(observation)
            if level is not None and level in spec.levels:
                return _finite(spec.levels[level].median_w)
            usable = [stat.median_w for stat in spec.levels.values() if stat.median_w is not None]
            if len(usable) == 1:
                return float(usable[0])
            if level is not None:
                return None
        transitions = getattr(spec, "transition_delta", {}) or {}
        transition = _name(observation.transition)
        if transition in transitions:
            return _finite(transitions[transition].median_w)
        return None
    if kind == "adjustable":
        control = _control(observation)
        xs = list(getattr(spec, "knots_x", []) or [])
        ys = list(getattr(spec, "knots_w", []) or [])
        if control is not None and xs and ys:
            return _interp(control, xs, ys)
        return _finite(getattr(spec, "standby", None).median_w) if getattr(spec, "standby", None) else None
    if kind == "dynamic":
        states = getattr(spec, "states", {}) or {}
        state = _state(observation)
        if state in states:
            return _finite(states[state].median_w)
        return None
    if kind == "response_kernel":
        by_transition = getattr(spec, "by_transition", {}) or {}
        phases = by_transition.get(_name(observation.transition)) or []
        for preferred in ("steady", "active"):
            for phase in phases:
                if _name(phase.phase) == preferred:
                    return _finite(phase.amplitude_w.median_w)
        if phases:
            return _finite(phases[0].amplitude_w.median_w)
        return None
    return None


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float | None:
    from ..models.base import _linear_interp

    return _linear_interp(x, list(zip(xs, ys, strict=False)))
