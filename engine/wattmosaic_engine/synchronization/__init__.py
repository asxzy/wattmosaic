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

The resolved sampling cadence is published with its source
(:class:`ResolvedCadence`): ``observed_history`` is a measurement of this
installation, ``configured_default`` is the documented fallback for a reader
with no usable history.  A cadence number without its source cannot be audited,
so the two always travel together.
"""

from __future__ import annotations

from .aligner import (
    CADENCE_SOURCE_DEFAULT,
    CADENCE_SOURCE_OBSERVED,
    MIN_CADENCE_INTERVALS,
    AlignedChannel,
    AlignedWindow,
    ChannelHealth,
    EnergyChannel,
    EnergyHealth,
    EnergyWindow,
    ResolvedCadence,
    SampleAligner,
    median,
    p95,
    resolve_channel_cadence,
    resolve_topology_cadence,
    robust_noise_mad,
)

__all__ = [
    "CADENCE_SOURCE_DEFAULT",
    "CADENCE_SOURCE_OBSERVED",
    "MIN_CADENCE_INTERVALS",
    "AlignedChannel",
    "AlignedWindow",
    "ChannelHealth",
    "EnergyChannel",
    "EnergyHealth",
    "EnergyWindow",
    "ResolvedCadence",
    "SampleAligner",
    "median",
    "p95",
    "resolve_channel_cadence",
    "resolve_topology_cadence",
    "robust_noise_mad",
]
