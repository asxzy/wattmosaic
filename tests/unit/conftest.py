"""Shared helpers for the learning/model/aggregation unit tests.

Deterministic: no network, no clock dependence (all times are explicit), fixed
random seeds.  These helpers only build contract objects; they never touch
production code paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def series(stages: list[tuple[int, float]], *, start: datetime = BASE_TIME, step_s: float = 15.7):
    """Build a ``[(datetime, watts)]`` series from ``[(count, watts)]``."""
    out: list[tuple[datetime, float]] = []
    for count, power in stages:
        for _ in range(count):
            out.append((start + timedelta(seconds=len(out) * step_s), power))
    return out


class FakeWindow:
    """Minimal duck-typed aligned window (not the production SimpleWindow)."""

    def __init__(self, at: datetime, values: dict[str, float], ages: dict[str, float] | None = None) -> None:
        self.at = at
        self.values = values
        self.ages = ages or {}
        self.qualities = {ct: "good" for ct in values}
        self.repeated = {ct: False for ct in values}


def windows_over(
    start: datetime,
    end: datetime,
    cadence_s: float,
    values: dict[str, Any],
) -> list[FakeWindow]:
    """Generate snapshots at ``cadence_s`` where ``values`` may be callables."""
    out: list[FakeWindow] = []
    t = start
    step = timedelta(seconds=cadence_s)
    while t <= end:
        resolved = {ct: (fn(t) if callable(fn) else fn) for ct, fn in values.items()}
        out.append(FakeWindow(t, resolved))
        t += step
    return out
