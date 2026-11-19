"""Dependency-free numeric helpers for the v3 power models.

Used by the v3 learners (:mod:`wattmosaic_engine.learning.incremental`), the
concrete spec fitters (:mod:`.fixed`, :mod:`.adjustable`) and the declared
device profile (:mod:`.device_profile`).  Nothing here builds a power value from
a raw parameter bag, and ``ON``/``OFF`` never means ``0 W`` (HB §11.5).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from typing import Any

from .protocols import normalise_series

# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #

ON_STATES_DEFAULT = frozenset({"on", "open", "running", "active", "heating", "cooling", "true", "1"})
# HB §11.5: OFF, idle and standby are *distinct* levels and none of them means
# 0 W.  Idle/standby are removed from the legacy "off" classification so a
# device that is merely idle is never routed to an OFF/0 W fallback.
OFF_STATES_DEFAULT = frozenset({"off", "closed", "false", "0"})


def finite(value: Any) -> float | None:
    """Return ``float(value)`` only when it is a finite real number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def median(values: Iterable[float], default: float | None = None) -> float | None:
    """The sample median, or ``default`` for an empty iterable."""
    data = [float(v) for v in values]
    return statistics.median(data) if data else default


def quantile(
    values: Iterable[float], q: float, default: float | None = None
) -> float | None:
    """A linearly-interpolated quantile of ``values`` (``q`` in ``[0, 1]``)."""
    data = sorted(float(v) for v in values)
    if not data:
        return default
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * min(max(float(q), 0.0), 1.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def mean(values: Iterable[float], default: float | None = None) -> float | None:
    data = [float(v) for v in values]
    return statistics.fmean(data) if data else default


def stdev(values: Iterable[float], default: float = 0.0) -> float:
    data = [float(v) for v in values]
    return statistics.stdev(data) if len(data) > 1 else default


def robust_sigma(values: Iterable[float], default: float | None = None) -> float | None:
    """1.4826 × MAD — a robust standard-deviation estimate."""
    data = [float(v) for v in values]
    if not data:
        return default
    med = statistics.median(data)
    mad = statistics.median([abs(v - med) for v in data])
    return 1.4826 * mad


def _linear_interp(x: float, curve: Sequence[tuple[float, float]]) -> float | None:
    """Interpolate a monotone ``(x, y)`` curve, clamping outside its range."""
    if not curve:
        return None
    points = sorted((float(a), float(b)) for a, b in curve)
    if len(points) == 1:
        return points[0][1]
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


def enforce_monotonic(curve: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators isotonic regression on ``(x, y)`` pairs."""
    blocks: list[tuple[float, float, int]] = [
        (float(x), float(y), 1) for x, y in sorted((float(a), float(b)) for a, b in curve)
    ]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][1] > blocks[i + 1][1]:
            x0, y0, n0 = blocks[i]
            x1, y1, n1 = blocks[i + 1]
            merged = (x0, (y0 * n0 + y1 * n1) / (n0 + n1), n0 + n1)
            blocks[i : i + 2] = [merged]
            i = max(0, i - 1)
        else:
            i += 1
    return [(x, y) for x, y, _ in blocks]


def error_metrics(actual: Sequence[float], predicted: Sequence[float]) -> dict[str, float]:
    """MAE / RMSE / R² / MAPE / max-error with honest empty-input behaviour."""
    n = min(len(actual), len(predicted))
    if n == 0:
        return {"n": 0.0, "mae": 0.0, "rmse": 0.0, "r2": 0.0, "mape": 0.0, "max_error": 0.0}
    errors = [float(a) - float(p) for a, p in zip(actual[:n], predicted[:n], strict=False)]
    abs_errors = [abs(e) for e in errors]
    mae = statistics.fmean(abs_errors)
    rmse = math.sqrt(statistics.fmean([e * e for e in errors]))
    mean_actual = statistics.fmean(float(a) for a in actual[:n])
    ss_tot = sum((float(a) - mean_actual) ** 2 for a in actual[:n])
    ss_res = sum(e * e for e in errors)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    nz = [abs(float(a)) for a in actual[:n] if float(a) != 0]
    mape = (
        statistics.fmean([abs(e) / a for e, a in zip(errors, nz, strict=False)]) if len(nz) == n and nz else 0.0
    )
    return {
        "n": float(n),
        "mae": float(mae),
        "rmse": float(rmse),
        "r2": float(r2),
        "mape": float(mape),
        "max_error": float(max(abs_errors)),
    }


__all__ = [
    "ON_STATES_DEFAULT",
    "OFF_STATES_DEFAULT",
    "enforce_monotonic",
    "error_metrics",
    "finite",
    "mean",
    "median",
    "normalise_series",
    "quantile",
    "robust_sigma",
    "stdev",
]
