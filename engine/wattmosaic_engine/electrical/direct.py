"""Level-1 direct measurement path (spec §9.1).

A device is ``direct_measured`` only when it has a **verified dedicated CT**
(``CTType.DEDICATED_DEVICE`` pointing at that device, or declared in
``DeviceTopology.dedicated_ct_ids``).  A branch CT is *not* direct measurement
for a device even if only one device currently draws power on it, because the
branch may carry unmodelled loads.

Double-pole devices measured by a symmetric paired CT sum both legs.  The sum
is the total device power; the individual legs must never be reported as two
separate devices.

Staleness and repeats are honoured: a stale value is returned with
``usable=False`` and a ``stale_measurement`` reason.  The inference layer then
falls back to the model/unknown rather than presenting an old value as current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from wattmosaic_engine.contracts import QualityStatus
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import ChannelReading, coerce_window


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
    at: datetime | None = None,
) -> dict[str, DirectReading]:
    """Return a :class:`DirectReading` for every device with a dedicated CT."""
    coerced = coerce_window(window, at=at)
    out: dict[str, DirectReading] = {}
    for device_id in topology.device_ids:
        ch = topology.dedicated_channel_for(device_id)
        if ch is None:
            continue
        ct_ids = [ch.ct_id]
        # Only a partner that belongs to *this* device is the other leg of a
        # double-pole load.  A pair spanning two devices would otherwise make
        # each device report the sum of both circuits.
        partner = topology.double_pole_leg(ch.ct_id)
        if partner is not None:
            ct_ids.append(partner)

        legs: dict[str, float | None] = {}
        ages: list[float] = []
        qualities: list[QualityStatus] = []
        reasons: list[str] = []
        repeated = False
        for cid in ct_ids:
            reading: ChannelReading | None = coerced.readings.get(cid)
            if reading is None:
                legs[cid] = None
                reasons.append(f"missing_channel:{cid}")
                qualities.append(QualityStatus.MISSING)
                continue
            # A declared circuit multiplier is applied once, here, on the way
            # out of the raw reading.  Two CTs on one leg (``paired_ct_id``)
            # each carry their own multiplier, so a 240 V load monitored on one
            # leg with ``measurement_multiplier=2.0`` sums to the full load.
            legs[cid] = topology.scale_w(cid, reading.signed_w())
            ages.append(reading.age_s)
            qualities.append(reading.quality)
            repeated = repeated or reading.repeated
            if reading.quality in (QualityStatus.MISSING, QualityStatus.BAD):
                reasons.append(f"bad_quality:{cid}:{reading.quality.value}")
            if reading.age_s > max_age_s:
                reasons.append(f"stale_measurement:{cid}")

        values = [v for v in legs.values() if v is not None]
        quality = _worst_quality(qualities)
        raw_power: float | None = None
        if not values:
            power: float | None = None
            reasons.append("missing_measurement")
        else:
            raw_power = float(sum(values))
            power = raw_power
            if len(values) != len(legs):
                reasons.append("partial_leg_measurement")
            if raw_power < 0.0:
                # A *device* cannot consume negative average power.  Negative
                # readings are legitimate on a panel main (export / reverse
                # flow) but on a dedicated device circuit a negative value is
                # sensor noise around zero, a reversed CT, or unmetered local
                # generation — none of which may be reported as a negatively
                # consuming appliance.  ``docs/electrical_model.md`` §9.5 states
                # this rule; the constrained solver enforces it via ``x >= 0``,
                # so the Level-1 direct path must enforce it too.
                power = 0.0
                reasons.append("negative_device_power_clipped")
                if len(values) > 1:
                    reasons.append("negative_device_power_signed_leg_sum")

        usable = (
            power is not None
            and not any(r.startswith(("stale_", "bad_quality", "missing_")) for r in reasons)
            # A double-pole device measured by a declared pair needs *both*
            # legs.  With one leg missing the sum is not the device's power but
            # roughly half of it, which is worse than reporting nothing: the
            # inference layer may still recover the device from its branch row,
            # but it must not trust the half reading.  The reason is flagged so
            # callers can tell a partial reading from an absent one.
            and len(values) == len(legs)
            and quality in (QualityStatus.GOOD, QualityStatus.DEGRADED)
        )
        if repeated:
            # A repeat is not a new physical sample; it is still the best known
            # value, so it remains usable but is flagged.
            reasons.append("repeated_state_update")
        out[device_id] = DirectReading(
            device_id=device_id,
            power_w=power,
            ct_ids=tuple(ct_ids),
            age_s=max(ages) if ages else float("inf"),
            quality=quality,
            usable=usable,
            repeated=repeated,
            reasons=tuple(dict.fromkeys(reasons)),
            per_leg_w=legs,
            raw_power_w=raw_power if raw_power is not None and raw_power < 0.0 else None,
        )
    return out


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
