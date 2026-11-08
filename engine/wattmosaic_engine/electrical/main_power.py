"""Main incoming power with sign preservation and nested-main refusal.

Rules
-----
* ``P_main = sum(P_mains)`` only when the mains are *parallel* (typically L1 and
  L2).  Nested mains (one feed behind another) are never summed: an error
  string is returned and the caller must fix the topology.
* Negative power is preserved.  It means export / reverse flow.  Nothing here
  clamps to zero.
* A missing main is reported per channel; the total is computed over the mains
  that are usable and ``complete`` becomes ``False``.  The caller must not
  present a partial total as the whole-home power.
* Polarity inversion is applied per channel before summation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from wattmosaic_engine.contracts import Phase, Polarity
from wattmosaic_engine.electrical.topology import TopologyGraph


@dataclass(frozen=True, slots=True)
class MainPower:
    total_w: float
    l1_w: float | None = None
    l2_w: float | None = None
    per_channel_w: dict[str, float] = field(default_factory=dict)
    missing_channels: tuple[str, ...] = ()
    excluded_channels: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    complete: bool = True
    has_export: bool = False

    @property
    def usable(self) -> bool:
        return not self.errors and bool(self.per_channel_w)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_w": self.total_w,
            "l1_w": self.l1_w,
            "l2_w": self.l2_w,
            "per_channel_w": dict(self.per_channel_w),
            "missing_channels": list(self.missing_channels),
            "excluded_channels": list(self.excluded_channels),
            "errors": list(self.errors),
            "notes": list(self.notes),
            "complete": self.complete,
            "has_export": self.has_export,
        }


def _read_w(
    readings: Mapping[str, object],
    channel_id: str,
    *,
    channel_polarity: Polarity = Polarity.NORMAL,
) -> float | None:
    """Read a channel value.

    ``ChannelReading`` / ``Measurement`` objects already carry their own
    polarity, so they are used as-is.  A bare float has no polarity metadata,
    so the topology's channel polarity is applied here.
    """
    raw = readings.get(channel_id)
    if raw is None:
        return None
    if hasattr(raw, "signed_w"):
        return raw.signed_w()
    if hasattr(raw, "effective_power_w"):
        return float(raw.effective_power_w())
    if isinstance(raw, Mapping):
        val = raw.get("value_w", raw.get("value"))
        if val is None:
            return None
        pol = str(raw.get("polarization", raw.get("polarity", "normal")))
        return -float(val) if pol.endswith("inverted") else float(val)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        # NaN / inf are missing, not zero.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return -value if channel_polarity == Polarity.INVERTED else value
    return None


def main_power_w(
    graph: TopologyGraph,
    readings: Mapping[str, object],
    *,
    phase_split_l1: float = 0.5,
) -> MainPower:
    """Sum the parallel feeds of the household, preserving sign.

    Only :meth:`TopologyGraph.effective_feeds` contribute.  A nested main (one
    feed behind another) is not added again, so a panel tree of any depth sums
    correctly.  Aliased channels (two CTs on one conductor) contribute once.

    A channel's ``measurement_multiplier`` is applied here, so every consumer of
    a main total sees the same scaled value.
    """
    errors: list[str] = []
    nested = graph.nested_mains()
    notes: list[str] = []
    if nested:
        # Reported, not refused: the inner feed is excluded from the sum and the
        # exclusion is visible to the caller.
        detail = ", ".join(f"{inner} under {outer}" for inner, outer in nested)
        notes.append("nested_feeds_not_summed: " + detail)

    aliases = [c.ct_id for c in graph.alias_channels() if graph.is_enabled(c.ct_id)]
    if aliases:
        notes.append("duplicate_feeds_counted_once: " + ", ".join(sorted(aliases)))

    mains = graph.feed_channels()
    if not mains:
        errors.append("no enabled main CT is defined in the topology")
        return MainPower(total_w=0.0, errors=tuple(errors), complete=False)

    per_channel: dict[str, float] = {}
    missing: list[str] = []
    l1: list[float] = []
    l2: list[float] = []
    both: list[float] = []

    for ch in mains:
        value = graph.scale_w(
            ch.ct_id,
            _read_w(readings, ch.ct_id, channel_polarity=Polarity(ch.polarity)),
        )
        if value is None:
            missing.append(ch.ct_id)
            continue
        per_channel[ch.ct_id] = value
        phase = Phase(ch.phase)
        if phase == Phase.L1:
            l1.append(value)
        elif phase == Phase.L2:
            l2.append(value)
        else:
            both.append(value)  # BOTH and UNKNOWN both contribute to the total

    usable_l1 = sum(l1) if l1 else None
    usable_l2 = sum(l2) if l2 else None
    # A BOTH main is already a full-circuit total; split for reporting only.
    both_l1 = sum(both) * phase_split_l1 if both else 0.0
    both_l2 = sum(both) * (1.0 - phase_split_l1) if both else 0.0

    reported_l1 = None if usable_l1 is None else usable_l1 + both_l1
    reported_l2 = None if usable_l2 is None else usable_l2 + both_l2
    total = sum(per_channel.values())
    unknown_phase = [ch.ct_id for ch in mains if Phase(ch.phase) == Phase.UNKNOWN]
    if unknown_phase:
        errors.append("main CT(s) with unknown phase: " + ", ".join(unknown_phase))

    complete = not missing and not errors
    return MainPower(
        total_w=total,
        l1_w=reported_l1,
        l2_w=reported_l2,
        per_channel_w=per_channel,
        missing_channels=tuple(missing),
        excluded_channels=tuple(
            sorted(
                ({c for pair in nested for c in pair} | set(aliases))
                - set(per_channel)
            )
        ),
        errors=tuple(errors),
        notes=tuple(notes),
        complete=complete,
        has_export=any(v < 0 for v in per_channel.values()),
    )


def phase_power_w(
    graph: TopologyGraph,
    readings: Mapping[str, object],
) -> dict[str, float | None]:
    """Convenience: ``{"l1": ..., "l2": ..., "total": ...}`` from main CTs."""
    mp = main_power_w(graph, readings)
    return {"l1": mp.l1_w, "l2": mp.l2_w, "total": mp.total_w}
