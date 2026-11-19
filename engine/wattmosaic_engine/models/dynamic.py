"""Dynamic (hidden-state) power model for contract 3.0 (L3).

A single HA state (e.g. ``cool``) can cover several hidden electrical states
(e.g. ``fan`` plus ``compressor``).  This module initialises those states from
a robust change-point segmentation of an observed power series and keeps
running transition counts and per-state duration statistics.

Design rules:

* **Deterministic and pure** — all inputs are explicit; no DB, no wall clock and
  no runtime fakes.  A clock is injected where a timestamp is required.
* **Null policy (HB §18)** — a state with too few samples keeps
  ``mad_w=None``; absent values are never fabricated as ``0.0``.
* **Honest uncertainty** — when the segmentation is ambiguous (levels are not
  distinguishable from measurement noise) the model reports
  ``StageStatus.UNKNOWN`` and refuses to claim a specific stage.
* The persisted payload is always a :class:`~wattmosaic_engine.contracts.DynamicSpec`
  so the L1 codec round-trips it losslessly (DEF-02).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    DynamicSpec,
    LevelStat,
    PowerLevelV3,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
)
from .prediction import _finite
from .segmentation import SegmentationResult, group_segments_into_states
from .spec import decode_spec, encode_spec

__all__ = [
    "DynamicModel",
    "StateStats",
    "build_dynamic_spec_from_series",
    "fit_dynamic_spec",
]


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _key(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _state_id(index: int) -> str:
    return f"s{index}"


def _level_stat(values: Sequence[float]) -> LevelStat:
    """Robust ``(median, 1.4826*MAD, n)`` from finite values.

    ``mad_w`` is ``None`` when fewer than two values are available: a single
    observation has no estimable spread, and 0.0 would be a fabricated claim.
    """
    clean = [v for v in (_finite(x) for x in values) if v is not None]
    if not clean:
        return LevelStat()
    median = float(statistics.median(clean))
    mad: float | None = None
    if len(clean) >= 2:
        mad = float(1.4826 * statistics.median([abs(v - median) for v in clean]))
    return LevelStat(median_w=median, mad_w=mad, n=len(clean))


def build_dynamic_spec_from_series(
    result: SegmentationResult,
    *,
    ha_state: str = "on",
    tolerance_w: float = 5.0,
) -> tuple[DynamicSpec, list[str]]:
    """Build a :class:`DynamicSpec` from a segmentation result.

    Hidden states are named ``s0``, ``s1``, … in time order, collapsed by power
    so one noisy level does not become two states.  ``ha_state_to_states`` lists
    them in descending power (priority) order for a given HA state.  An
    ambiguous result still yields the resolved levels, but callers must report
    ``StageStatus.UNKNOWN`` rather than claim a specific stage.
    """
    groups = group_segments_into_states(result, tolerance_w=tolerance_w)
    states: dict[str, LevelStat] = {}
    durations: dict[str, LevelStat] = {}
    ordered: list[str] = []
    for group in groups:
        sid = _state_id(len(ordered))
        samples = [group.power_w]
        if group.mad_w is not None:
            # Preserve an estimable spread as a symmetric two-point envelope.
            samples = [group.power_w - group.mad_w, group.power_w + group.mad_w]
        stat = _level_stat(samples)
        stat.n = group.sample_count
        states[sid] = stat
        # Repeat each observed segment duration by its sample count so one
        # long-lived state contributes many observations, matching the power
        # statistic.  Without this a state seen in 9 samples would score one
        # duration and later observations would dominate the running median.
        duration_values: list[float] = []
        for s in group.segments:
            if s.duration_s > 0:
                duration_values.extend([s.duration_s] * max(1, s.sample_count))
        durations[sid] = _level_stat(duration_values) if duration_values else LevelStat()
        ordered.append(sid)

    def _priority(sid: str) -> float:
        value = states[sid].median_w
        return value if value is not None else float("-inf")

    priority = sorted(ordered, key=_priority, reverse=True)
    spec = DynamicSpec(
        states=states,
        ha_state_to_states={_key(ha_state): priority},
        transition_counts={},
        duration_s=durations,
    )
    return spec, ordered


# --------------------------------------------------------------------------- #
# Runtime state tracking.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StateStats:
    """Accumulator for one hidden state's observed durations."""

    durations: list[float] = field(default_factory=list)

    def add(self, duration_s: float | None) -> None:
        value = _finite(duration_s)
        if value is not None and value >= 0.0:
            self.durations.append(value)

    def as_level_stat(self) -> LevelStat:
        return _level_stat(self.durations)


@dataclass(slots=True)
class DynamicModel:
    """A dynamic model fitted from :class:`DynamicSpec` data.

    ``clock`` is injected and used only for stamping observations; it is never
    read implicitly, so replay with simulated time is deterministic.  Transition
    counts and duration statistics accumulate via :meth:`observe` on a copy of
    the spec, leaving the source spec immutable.
    """

    spec: DynamicSpec
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = lambda: datetime.now(UTC)

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_series(
        cls,
        series: Any,
        *,
        ha_state: str = "on",
        clock: Any = None,
        **segment_kwargs: Any,
    ) -> DynamicModel:
        """Initialise from a ``[(datetime, watts)]`` power series via the segmenter."""
        from .segmentation import segment_power_series

        result = segment_power_series(series, **segment_kwargs)
        spec, _ = build_dynamic_spec_from_series(result, ha_state=ha_state)
        return cls(spec=spec, clock=clock)

    @classmethod
    def from_version(cls, version: PowerModelVersion) -> DynamicModel:
        """Load a model from a version whose spec is a :class:`DynamicSpec`."""
        if not isinstance(version.spec, DynamicSpec):
            raise ValueError("a DynamicModel requires a DynamicSpec")
        return cls(spec=version.spec)

    # -- serialisation ------------------------------------------------------- #
    def to_spec(self) -> DynamicSpec:
        """Return the current (possibly updated) spec."""
        return self.spec

    def encoded(self) -> dict[str, Any]:
        """Serialise the current spec with the L1 codec."""
        return encode_spec(self.spec)

    @classmethod
    def from_encoded(cls, raw: Any) -> DynamicModel:
        """Decode a spec dict/JSON produced by :func:`encoded`."""
        spec = decode_spec(raw)
        if not isinstance(spec, DynamicSpec):
            raise ValueError("a DynamicModel requires a DynamicSpec")
        return cls(spec=spec)

    # -- prediction --------------------------------------------------------- #
    def predict(self, attrs: Mapping[str, Any], level: PowerLevelV3 | None = None) -> PowerPrediction:
        """Predict power for a HA state using the priority-state rule.

        When multiple hidden states share one HA state, the first state with a
        usable statistic wins (the mapping lists them in priority order).  An
        absent state or unusable statistic yields ``UNKNOWN`` with ``mean_w=None``.
        """
        raw_state = attrs.get("state") or attrs.get("ha_state") if isinstance(attrs, Mapping) else None
        state_key = _key(raw_state) if raw_state is not None else None
        candidates = self.spec.ha_state_to_states.get(state_key or "", []) if state_key else []
        stat: LevelStat | None = None
        for candidate in candidates:
            found = self.spec.states.get(candidate) or self.spec.states.get(_key(candidate))
            if found is not None:
                stat = found
                break
        if stat is None or stat.median_w is None:
            return PowerPrediction(
                level=level or PowerLevelV3.UNKNOWN,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        return PowerPrediction(
            level=level or PowerLevelV3.UNKNOWN,
            mean_w=stat.median_w,
            sigma_w=stat.mad_w,
            status=PredictionStatus.KNOWN,
            model_version_id=None,
        )

    # -- incremental learning ------------------------------------------------ #
    def observe(
        self,
        *,
        from_state: str | None,
        to_state: str | None,
        duration_s: float | None,
    ) -> DynamicSpec:
        """Fold one transition + dwell duration into a copy of the spec.

        Transition counts are keyed ``from->to``; a missing endpoint is recorded
        as the empty string (``""``), never as a fabricated state.  Duration
        statistics are appended to the destination state's accumulator.  Returns
        a new :class:`DynamicSpec`; ``self.spec`` is not mutated in place, which
        keeps observations append-only and replay deterministic.
        """
        counts = {src: dict(mapping) for src, mapping in self.spec.transition_counts.items()}
        durations: dict[str, LevelStat] = dict(self.spec.duration_s)
        src_key = _key(from_state) if from_state is not None else ""
        dst_key = _key(to_state) if to_state is not None else ""
        if dst_key:
            counts.setdefault(src_key, {})
            counts[src_key][dst_key] = counts[src_key].get(dst_key, 0) + 1
            existing = durations.get(dst_key, LevelStat())
            merged = StateStats(list(_durations_as_values(existing)))
            merged.add(duration_s)
            durations[dst_key] = merged.as_level_stat()

        self.spec = DynamicSpec(
            states=dict(self.spec.states),
            ha_state_to_states={k: list(v) for k, v in self.spec.ha_state_to_states.items()},
            transition_counts=counts,
            duration_s=durations,
        )
        return self.spec


def _durations_as_values(stat: LevelStat) -> list[float]:
    """Rehydrate a duration stat into representative samples for merging.

    A persisted ``LevelStat`` keeps only median/MAD/n, so we reconstruct ``n``
    representative values around the median.  With ``n=1`` there is no spread to
    invent; with ``n>=2`` the MAD is preserved symmetrically.  A stat with no
    median contributes nothing (absent stays absent).
    """
    if stat.median_w is None or stat.n <= 0:
        return []
    if stat.n == 1 or stat.mad_w is None:
        return [stat.median_w]
    half = stat.n // 2
    out = [stat.median_w - stat.mad_w] * half + [stat.median_w + stat.mad_w] * (stat.n - half)
    return out


def fit_dynamic_spec(
    series: Any,
    *,
    ha_state: str = "on",
    **segment_kwargs: Any,
) -> DynamicSpec:
    """Pure helper that fits a :class:`DynamicSpec` from a power series."""
    from .segmentation import segment_power_series

    result = segment_power_series(series, **segment_kwargs)
    spec, _ = build_dynamic_spec_from_series(result, ha_state=ha_state)
    return spec
