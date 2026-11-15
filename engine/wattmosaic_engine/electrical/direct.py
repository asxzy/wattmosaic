"""Level-1 direct measurement path (spec §9.1).

**Retired.**  Direct measurement was gated on a dedicated CT role that the
contract no longer defines, so no device can be resolved to a direct reading.
The module is kept until the Level-1 branch in ``disaggregation.engine`` is
deleted; :func:`resolve_direct` therefore returns an empty mapping rather than
resolving anything.

The remaining helpers describe the electrical rules a direct reading used to
apply (leg skew, quality, non-negativity).  They are retained unchanged so the
deletion is a pure removal rather than a rewrite.

Two legs of one double-pole circuit are usable only *as a pair*.  A leg is not a
measurement of the circuit at half its value: with the partner missing the sum
is one leg's power, and with the legs polled a cycle apart the sum mixes two
different instants.  Both cases are reported as reasons and never repaired by
averaging, by taking the newer leg, or by falling back to one leg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from wattmosaic_engine.contracts import QualityStatus
from wattmosaic_engine.electrical.topology import TopologyGraph

#: How far apart in time the two legs of a declared double-pole pair may be
#: before the pair stops describing one instant.  Two legs of one circuit are
#: sampled within a cycle of each other (the reference install's Refoss panel
#: polls at 15.7 s and its legs arrive ~2 s apart); a larger skew means one leg
#: is a leftover from an earlier poll, so the pair is a mix of two instants and
#: neither leg may be used.
#:
#: This is the *single* definition of the limit.  The solver path
#: (``DisaggregationEngine._exclude_partial_legs``) and the direct path read the
#: same constant, so they can never disagree about which pairs are usable.
DEFAULT_MAX_LEG_SKEW_S = 20.0


@dataclass(frozen=True, slots=True)
class DirectReading:
    device_id: str
    power_w: float | None
    ct_ids: tuple[str, ...]
    age_s: float
    quality: QualityStatus
    usable: bool
    repeated: bool = False
    reasons: tuple[str, ...] = ()
    per_leg_w: dict[str, float | None] = field(default_factory=dict)
    #: Signed sum of the legs *before* the device-level non-negativity clamp.
    #: ``None`` when the reading was already non-negative (so the common case
    #: carries no extra field).
    raw_power_w: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "power_w": self.power_w,
            "ct_ids": list(self.ct_ids),
            "age_s": self.age_s,
            "quality": self.quality.value,
            "usable": self.usable,
            "repeated": self.repeated,
            "reasons": list(self.reasons),
            "per_leg_w": dict(self.per_leg_w),
            "raw_power_w": self.raw_power_w,
        }


def resolve_direct(
    window: object,
    topology: TopologyGraph,
    *,
    max_age_s: float = 120.0,
    max_leg_skew_s: float = DEFAULT_MAX_LEG_SKEW_S,
    at: datetime | None = None,
) -> dict[str, DirectReading]:
    """Return a :class:`DirectReading` for every device with a private channel.

    Retired: direct measurement was gated on the dedicated-device channel role,
    which the contract no longer defines, so no device can qualify.  Returning
    an empty mapping keeps the Level-1 call site inert until it is deleted
    rather than resolving a reading from a branch row, which would claim a
    direct measurement the topology does not support.
    """
    del window, max_age_s, max_leg_skew_s, at
    return {}


_QUALITY_ORDER = {
    QualityStatus.GOOD: 0,
    QualityStatus.DEGRADED: 1,
    QualityStatus.STALE: 2,
    QualityStatus.MISSING: 3,
    QualityStatus.BAD: 4,
}


def _worst_quality(qualities: list[QualityStatus]) -> QualityStatus:
    if not qualities:
        return QualityStatus.MISSING
    return max(qualities, key=lambda q: _QUALITY_ORDER.get(q, 4))


def direct_power_w(
    window: object,
    topology: TopologyGraph,
    *,
    max_age_s: float = 120.0,
) -> dict[str, float]:
    """Convenience: usable direct measurements as ``{device_id: watts}``."""
    return {
        did: r.power_w
        for did, r in resolve_direct(window, topology, max_age_s=max_age_s).items()
        if r.usable and r.power_w is not None
    }
