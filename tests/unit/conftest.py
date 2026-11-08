"""Shared helpers for the learning/model/aggregation unit tests.

Deterministic: no network, no clock dependence (all times are explicit), fixed
random seeds.  These helpers only build contract objects; they never touch
production code paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import LearningSample

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_sample(
    device_id: str = "device",
    index: int = 0,
    *,
    baseline: float = 0.0,
    event: float = 0.0,
    delta: float | None = None,
    control_value: float | None = None,
    control_variable: str | None = None,
    state: str | None = "on",
    notes: list[str] | None = None,
    accepted: bool = True,
    supporting_cts: list[str] | None = None,
    captured_at: datetime | None = None,
    minutes: float = 5.0,
) -> LearningSample:
    """Build a well-formed sample with collector-style provenance notes."""
    if delta is None:
        delta = event - baseline
    note_list = [
        *(notes or []),
        "baseline_samples:4",
        "event_samples:4",
    ]
    if state is not None:
        note_list.insert(0, f"state:{state}")
    return LearningSample(
        sample_id=f"{device_id}-{index:04d}",
        device_id=device_id,
        captured_at=captured_at or (BASE_TIME + timedelta(minutes=index * minutes)),
        duration_s=60.0,
        baseline_power_w=float(baseline),
        event_power_w=float(event),
        delta_power_w=float(delta),
        control_value=control_value,
        control_variable=control_variable,
        supporting_ct_ids=supporting_cts if supporting_cts is not None else ["ct_branch"],
        accepted=accepted,
        notes=note_list,
    )


def binary_samples(
    device_id: str = "lamp",
    count: int = 40,
    on_power: float = 60.0,
    off_power: float = 0.0,
) -> list[LearningSample]:
    samples: list[LearningSample] = []
    for i in range(count):
        if i % 2 == 0:
            samples.append(
                make_sample(
                    device_id,
                    i,
                    baseline=off_power,
                    event=on_power,
                    delta=on_power - off_power,
                    state="on",
                )
            )
        else:
            samples.append(
                make_sample(
                    device_id,
                    i,
                    baseline=on_power,
                    event=off_power,
                    delta=off_power - on_power,
                    state="off",
                )
            )
    return samples


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
