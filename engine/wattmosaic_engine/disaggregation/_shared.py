"""Shared low-level helpers for the split disaggregation engine (X02).

This module is the bottom of the split's dependency graph: it holds the pure
helpers that more than one extracted module needs.  It deliberately imports
nothing from :mod:`~wattmosaic_engine.disaggregation.engine` so that the
extracted modules never depend on the thin orchestrator.  Moving these helpers
here is a pure code move -- no logic changes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import QualityStatus, StageStatus
from wattmosaic_engine.electrical.matrix import MeasurementMatrix
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.topology import TopologyGraph

#: A device explicitly reported OFF must not be credited with more than this
#: (or twice its modelled standby power) just because its circuit carries an
#: unexplained load.
OFF_ATTRIBUTION_TOLERANCE_W = 15.0

#: A device the state declares OFF, with no stated standby draw, is pinned at
#: zero.  The bounded solver requires ``lower < upper`` strictly, so the cap is
#: the smallest positive float that still behaves as zero at any realistic watt
#: magnitude rather than a literal ``0.0``.
_PINNED_ZERO_W = 1e-9

#: Relative tolerance when comparing a solved value with a modelled value.
MODEL_MATCH_REL_TOL = 0.40

ON_STATES = {
    "on",
    "running",
    "heating",
    "cooling",
    "active",
    "open",
    "playing",
    "washing",
    "drying",
    "cleaning",
}
#: States that positively indicate no *active* operation.  ``unavailable`` and
#: ``unknown`` are deliberately excluded: they mean "we do not know", not "off",
#: and treating them as OFF would silently pin a device to zero.
OFF_STATES = {"off", "idle", "standby", "closed", "paused", "none"}
UNKNOWN_STATES = {"unavailable", "unknown", ""}

#: The seven evidence sources REQ-DISAGG-001 names, in publication order.  Every
#: inference result publishes a ledger over exactly these ids, so a consumer can
#: tell "source was used" from "source was available but unused" without reading
#: the engine.  ``topology`` is the structural source a solve cannot run without,
#: so it is always present and always used.
SOURCE_IDS: tuple[str, ...] = (
    "ha_state",
    "ha_attributes",
    "smart_plug_measurement",
    "ct_measurement",
    "topology",
    "residual_events",
    "learned_models",
)


def _state_meaning(state: str | None, overrides: Mapping[str, str] | None = None) -> str | None:
    """``"on"``, ``"off"`` or ``None`` (unknown), honouring per-device overrides."""
    if state is None:
        return None
    key = state.strip().lower()
    if overrides:
        override = overrides.get(key)
        if override is not None:
            return override
    if key in ON_STATES:
        return "on"
    if key in OFF_STATES:
        return "off"
    return None


def _is_on(state: str | None, overrides: Mapping[str, str] | None = None) -> bool:
    return _state_meaning(state, overrides) == "on"


def _is_off(state: str | None, overrides: Mapping[str, str] | None = None) -> bool:
    return _state_meaning(state, overrides) == "off"


def _state_power(model: ModelView | None, state: str | None) -> float | None:
    if model is None:
        return None
    value = model.power_for_state(state)
    if value is not None:
        return value
    # The aggregate ``estimated_power_w`` is a fallback for a state the model does
    # not describe -- but never for a state that positively says the device is
    # off.  A model with no ``off_power_w``/``standby_power_w`` returns ``None``
    # for "off", and substituting the aggregate estimate there reported an OFF
    # device as drawing its full running power.  That is the same fabrication as
    # crediting a whole CT row to one lamp: a number that looks measured and is
    # not.  "No model value for this state" is the honest answer.
    normalized = (state or "").strip().lower()
    if normalized in {"off", "idle", "standby", "closed", "paused", "unavailable", "unknown"}:
        return None
    if model.estimated_power_w is not None:
        return float(model.estimated_power_w)
    return None


def _measured_level(
    metadata: Mapping[str, object] | None, device_id: str
) -> float | None:
    """Measured dimmer output for one device, or ``None`` when not reported.

    The facade collects each device's control value from its declared state
    entity and publishes it under ``metadata["device_levels"]``.  This reads it
    back per device and refuses anything that is not a finite number: a level is
    a fraction of full scale, and a malformed one must degrade to "not reported"
    rather than to some arbitrary wattage.

    Range handling is deliberately *not* repeated here.  ``interpolate_curve``
    already clamps to the measured points -- because extrapolating past what was
    observed would invent watts -- and that behaviour is tested there.  A second
    clamp here could drift from it, and neither copy would be provably
    load-bearing.
    """
    levels = (metadata or {}).get("device_levels")
    if not isinstance(levels, Mapping):
        return None
    raw = levels.get(device_id)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _basis_value(basis: Any) -> str:
    return str(getattr(basis, "value", basis))


def _stage_for(model: ModelView | None, power_w: float) -> tuple[str | None, StageStatus]:
    if model is None or not model.params.stages:
        return (model.stage if model else None), StageStatus.UNKNOWN
    best = None
    best_distance = float("inf")
    for stage in model.params.stages:
        distance = abs(power_w - stage.power_mean_w)
        if distance < best_distance:
            best_distance = distance
            best = stage
    if best is None:
        return None, StageStatus.UNKNOWN
    tolerance = max(2.0 * best.power_std_w, 0.10 * max(abs(best.power_mean_w), 1.0))
    if best_distance <= tolerance:
        return best.name, StageStatus.PROBABLE
    return best.name, StageStatus.UNKNOWN


def _quality_from_age(age_s: float, quality: QualityStatus) -> QualityStatus:
    if quality in (QualityStatus.MISSING, QualityStatus.BAD):
        return quality
    if age_s > 180.0:
        return QualityStatus.STALE
    if age_s > 30.0:
        return QualityStatus.DEGRADED
    return quality


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _worst(qualities: Sequence[QualityStatus]) -> QualityStatus:
    order = {
        QualityStatus.GOOD: 0,
        QualityStatus.DEGRADED: 1,
        QualityStatus.STALE: 2,
        QualityStatus.MISSING: 3,
        QualityStatus.BAD: 4,
    }
    return max(qualities, key=lambda q: order.get(q, 4))


def _used_support(
    graph: TopologyGraph, device_id: str, supporting: Sequence[str]
) -> list[str]:
    attribution = set(graph.attribution_ct_ids(device_id))
    used = [c for c in supporting if c in attribution]
    return used or list(supporting)


def _is_solo_row(matrix: MeasurementMatrix, col: int) -> bool:
    """True when exactly one device has a non-zero coefficient in each of the
    device's attribution rows (that row measures this device and nothing else)."""
    rows = np.flatnonzero(np.abs(matrix.A[:, col]) > 0)
    if rows.size == 0:
        return False
    for row in rows:
        others = np.count_nonzero(np.abs(np.delete(matrix.A[row], col)) > 0)
        if others > 0:
            return False
    return True
