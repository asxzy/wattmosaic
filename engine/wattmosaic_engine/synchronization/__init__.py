"""Measurement synchronization and channel health.

``SampleAligner`` keeps bounded ring buffers per channel and produces aligned
windows for inference.  Two rules are non-negotiable:

1. A repeated value is **not** a fresh measurement.  It is still exposed as the
   current value, but it is flagged ``repeated`` and :meth:`AlignedWindow.fresh`
   excludes it and stale channels.
2. The buffer is bounded by both count and age so a stalled consumer cannot
   grow memory without limit.

Power and energy live in **separate domains**: ``window()`` only contains
watts; ``energy_window()`` only contains kWh.  Energy never enters the power
matrix or the power quality checks.
"""

from __future__ import annotations

from .aligner import (
    AlignedChannel,
    AlignedWindow,
    ChannelHealth,
    EnergyChannel,
    EnergyHealth,
    EnergyWindow,
    SampleAligner,
    median,
    p95,
    robust_noise_mad,
)

__all__ = [
    "AlignedChannel",
    "AlignedWindow",
    "ChannelHealth",
    "EnergyChannel",
    "EnergyHealth",
    "EnergyWindow",
    "SampleAligner",
    "median",
    "p95",
    "robust_noise_mad",
]
