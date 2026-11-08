"""Independent ground-truth device behaviour models.

Every power value produced here is computed from a device's own physical state
machine — it never reads a measurement, noise term, or simulated sensor value.
That independence is what makes the simulator usable as an accuracy oracle.

Determinism: each device has its own ``random.Random`` seeded from
``(seed, device_id)``, and scheduled cycles are anchored to absolute simulated
seconds.  The same seed therefore produces byte-identical output.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .topology import SimDevice, SimDeviceType

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rng(seed: int, seed_key: str) -> random.Random:
    # Stable string hash (Python's hash() is salted per process).
    h = 0
    for ch in seed_key:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return random.Random((seed * 1_000_003) ^ h)


def _smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _ramp(t: float, start: float, end: float) -> float:
    if end <= start:
        return 1.0 if t >= end else 0.0
    return _smoothstep((t - start) / (end - start))


@dataclass(slots=True)
class ScheduledEvent:
    t: float
    device_id: str
    state: str
    power_w: float
    note: str = ""


@dataclass
class DeviceProfile:
    """Base class.  ``power(t)`` is deterministic given the seed."""

    device: SimDevice
    seed: int
    horizon_s: float
    rng: random.Random = field(init=False, repr=False)
    events: list[ScheduledEvent] = field(default_factory=list)
    # Externally injected simultaneous-activation windows: (start, end, power_w).
    forced_windows: list[tuple[float, float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Seed from the structural key, never the display id: renaming a load
        # must not change its simulated waveform (see
        # ``HouseholdTopology.assign_seed_keys``).
        self.rng = _rng(self.seed, self.device.seed_key or self.device.device_id)

    def power(self, t: float) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def effective_power(self, t: float) -> float:
        """Physical power including externally injected simultaneous events."""
        p = max(0.0, self.power(t))
        for start, end, forced in self.forced_windows:
            if start <= t < end:
                p = max(p, forced)
        return p

    def state(self, t: float) -> str:
        return "on" if self.effective_power(t) > 1.0 else "off"

    def effective_state(self, t: float) -> str:
        for start, end, _ in self.forced_windows:
            if start <= t < end:
                return "on"
        return self.state(t)

    def extra_attributes(self, t: float) -> dict:
        return {}


# --------------------------------------------------------------------------- #
# Concrete profiles
# --------------------------------------------------------------------------- #


class BinaryProfile(DeviceProfile):
    """Duty-cycled binary load with standby draw and on/off transitions."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.on_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(0.4, 2.5)
        # Occupancy-ish windows spread over the horizon.
        self.windows: list[tuple[float, float]] = []
        t = self.rng.uniform(60, 600)
        while t < self.horizon_s:
            dur = self.rng.uniform(120, 1500)
            self.windows.append((t, min(self.horizon_s, t + dur)))
            t += dur + self.rng.uniform(900, 5400)
        for start, end in self.windows:
            self.events.append(ScheduledEvent(start, self.device.device_id, "on", self.on_power, "binary_on"))
            self.events.append(ScheduledEvent(end, self.device.device_id, "off", self.standby, "binary_off"))

    def power(self, t: float) -> float:
        on = any(a <= t < b for a, b in self.windows)
        # Small inrush for the first 2 s of an activation.
        if on:
            for a, _ in self.windows:
                if 0 <= t - a < 2.0:
                    return self.on_power * (1.25 - 0.125 * (t - a))
            return self.on_power
        return self.standby


class StandbyProfile(DeviceProfile):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.standby = self.rng.uniform(1.5, 6.0)
        self.active = self.device.nominal_power_w
        self.windows = []
        t = self.rng.uniform(300, 1800)
        while t < self.horizon_s:
            dur = self.rng.uniform(30, 300)
            self.windows.append((t, t + dur))
            t += dur + self.rng.uniform(3600, 14400)

    def power(self, t: float) -> float:
        return self.active if any(a <= t < b for a, b in self.windows) else self.standby


class DimmableProfile(DeviceProfile):
    """Lighting: occupancy window + brightness steps -> non-linear watts."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.max_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(0.0, 0.5)
        self.steps: list[tuple[float, float]] = []  # (start, level 0..1)
        # Evening and morning occupancy windows.
        for base in (self.rng.uniform(6.5, 8.0) * 3600, self.rng.uniform(17.5, 20.0) * 3600):
            if base > self.horizon_s:
                continue
            t = base
            level = self.rng.uniform(0.2, 0.5)
            self.steps.append((t, level))
            for _ in range(self.rng.randint(1, 3)):
                t += self.rng.uniform(600, 3600)
                level = self.rng.uniform(0.1, 1.0)
                self.steps.append((t, level))
            self.steps.append((t + self.rng.uniform(600, 2400), 0.0))
        self.steps.sort()
        for t, level in self.steps:
            self.events.append(
                ScheduledEvent(t, self.device.device_id, "on" if level > 0 else "off",
                               self.max_power * level**1.6, "brightness")
            )

    def power(self, t: float) -> float:
        level = 0.0
        for start, lvl in self.steps:
            if t >= start:
                level = lvl
        if level <= 0:
            return self.standby
        # Convex brightness->power curve (as required by the model contract).
        return self.max_power * (level**1.6) + self.standby


class VariableSpeedProfile(DeviceProfile):
    """Motor / charger: discrete speed steps with an affine power curve."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.max_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(2.0, 12.0)
        self.steps: list[tuple[float, float]] = []
        t = self.rng.uniform(120, 900)
        while t < self.horizon_s:
            speed = self.rng.choice([0.0, 0.25, 0.5, 0.75, 1.0])
            self.steps.append((t, speed))
            for _ in range(self.rng.randint(2, 8)):
                t += self.rng.uniform(300, 2400)
                speed = max(0.0, min(1.0, speed + self.rng.choice([-0.25, 0.0, 0.25])))
                self.steps.append((t, speed))
            t += self.rng.uniform(1200, 5400)
        self.steps.sort()
        for t, speed in self.steps:
            self.events.append(
                ScheduledEvent(t, self.device.device_id, "on" if speed > 0 else "off",
                               self.standby + self.max_power * speed**1.3, "speed_step")
            )

    def power(self, t: float) -> float:
        speed = 0.0
        for start, sp in self.steps:
            if t >= start:
                speed = sp
        return self.standby + self.max_power * speed**1.3

    def state(self, t: float) -> str:
        return "on" if self.power(t) > self.standby * 1.5 else "idle"

    def extra_attributes(self, t: float) -> dict:
        speed = 0.0
        for start, sp in self.steps:
            if t >= start:
                speed = sp
        return {"speed": round(speed, 4)}


class HVACProfile(DeviceProfile):
    """Thermostat-driven cycling; outdoor temperature is a daily sine."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.max_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(3.0, 9.0)
        self.period_s = self.rng.uniform(900, 2400)  # compressor cycle
        self.phase = self.rng.uniform(0, self.period_s)
        self.duty = self.rng.uniform(0.25, 0.7)
        self.defrost_every_s = self.rng.uniform(5400, 10800)
        self.defrost_phase = self.rng.uniform(0, self.defrost_every_s)

    def _duty(self, t: float) -> float:
        # Demand rises in the afternoon and overnight, mildly.
        return max(0.1, min(0.95, self.duty + 0.12 * math.sin(2 * math.pi * t / 86400.0)))

    def power(self, t: float) -> float:
        cycle = ((t + self.phase) % self.period_s) / self.period_s
        running = cycle < self._duty(t)
        p = self.max_power if running else self.standby
        # Periodic defrost / reverse-cycle spike.
        if (t + self.defrost_phase) % self.defrost_every_s < 120:
            p += self.max_power * 0.15
        return p

    def state(self, t: float) -> str:
        cycle = ((t + self.phase) % self.period_s) / self.period_s
        return "heating" if cycle < self._duty(t) else "idle"


class RefrigeratorProfile(DeviceProfile):
    """Compressor duty cycle with a defrost heater event."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.run_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(0.2, 1.2)
        self.period_s = self.rng.uniform(1200, 2400)
        self.duty = self.rng.uniform(0.3, 0.5)
        self.phase = self.rng.uniform(0, self.period_s)
        self.defrost_every_s = self.rng.uniform(14400, 28800)
        self.defrost_phase = self.rng.uniform(0, self.defrost_every_s)

    def power(self, t: float) -> float:
        if (t + self.defrost_phase) % self.defrost_every_s < 480:
            return self.run_power * 1.6  # defrost heater
        cycle = ((t + self.phase) % self.period_s) / self.period_s
        return self.run_power if cycle < self.duty else self.standby

    def state(self, t: float) -> str:
        return "cooling" if self.power(t) > self.run_power * 0.6 else "idle"


@dataclass(slots=True)
class CycleStage:
    name: str
    duration_s: float
    power_w: float


class ApplianceCycleProfile(DeviceProfile):
    """Multi-stage appliance cycle (washer / dishwasher / dryer)."""

    stage_templates: list[CycleStage] = []

    def __post_init__(self) -> None:
        super().__post_init__()
        self.standby = self.rng.uniform(0.5, 3.0)
        scale = self.device.nominal_power_w / max(
            1.0, max((s.power_w for s in self.stage_templates), default=1.0)
        )
        self.stages = [
            CycleStage(s.name, s.duration_s * self.rng.uniform(0.85, 1.15), s.power_w * scale)
            for s in self.stage_templates
        ]
        self.cycles: list[tuple[float, list[CycleStage]]] = []
        t = self.rng.uniform(600, 3600)
        while t < self.horizon_s:
            # Jitter stage durations per cycle but keep them deterministic.
            cycle = [
                CycleStage(s.name, s.duration_s * self.rng.uniform(0.9, 1.1),
                           s.power_w * self.rng.uniform(0.95, 1.05))
                for s in self.stages
            ]
            self.cycles.append((t, cycle))
            for stage in cycle:
                self.events.append(
                    ScheduledEvent(t, self.device.device_id, stage.name, stage.power_w, "stage_start")
                )
                t += stage.duration_s
            t += self.rng.uniform(5400, 21600)

    def _stage_at(self, t: float) -> tuple[str, float]:
        for start, cycle in self.cycles:
            cur = start
            for stage in cycle:
                if cur <= t < cur + stage.duration_s:
                    return stage.name, stage.power_w
                cur += stage.duration_s
        return "idle", self.standby

    def power(self, t: float) -> float:
        return self._stage_at(t)[1]

    def state(self, t: float) -> str:
        return self._stage_at(t)[0]

    def extra_attributes(self, t: float) -> dict:
        return {"stage": self._stage_at(t)[0]}


class WasherProfile(ApplianceCycleProfile):
    stage_templates = [
        CycleStage("fill", 180, 80),
        CycleStage("wash_heat", 900, 2100),
        CycleStage("wash", 600, 420),
        CycleStage("drain", 120, 260),
        CycleStage("rinse", 300, 300),
        CycleStage("spin", 480, 720),
    ]


class DishwasherProfile(ApplianceCycleProfile):
    stage_templates = [
        CycleStage("prewash", 420, 180),
        CycleStage("main_wash_heat", 1500, 1900),
        CycleStage("main_wash", 900, 260),
        CycleStage("drain", 120, 240),
        CycleStage("rinse_heat", 900, 1750),
        CycleStage("dry", 1200, 950),
    ]


class DryerProfile(ApplianceCycleProfile):
    stage_templates = [
        CycleStage("heat_up", 600, 5000),
        CycleStage("tumbling_heat", 1800, 4600),
        CycleStage("cooldown", 600, 900),
    ]


class DoublePoleProfile(DeviceProfile):
    """240 V appliance: total power split evenly across the two leg CTs."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.max_power = self.device.nominal_power_w
        self.standby = self.rng.uniform(4.0, 20.0)
        self.period_s = self.rng.uniform(1800, 3600)
        self.duty = self.rng.uniform(0.15, 0.55)
        self.phase = self.rng.uniform(0, self.period_s)
        self.boost_windows: list[tuple[float, float]] = []
        t = self.rng.uniform(1200, 7200)
        while t < self.horizon_s:
            dur = self.rng.uniform(600, 2400)
            self.boost_windows.append((t, t + dur))
            t += dur + self.rng.uniform(7200, 21600)

    def power(self, t: float) -> float:
        if any(a <= t < b for a, b in self.boost_windows):
            return self.max_power
        cycle = ((t + self.phase) % self.period_s) / self.period_s
        return self.max_power if cycle < self.duty else self.standby

    def state(self, t: float) -> str:
        return "heating" if self.power(t) > self.standby * 3 else "idle"


class UnknownLoadProfile(DeviceProfile):
    """Unmetered load: bounded random walk, smoothed, never identifiable."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.base = self.device.nominal_power_w * 0.5
        self.knots: list[tuple[float, float]] = []
        t = 0.0
        level = self.base
        while t < self.horizon_s:
            t += self.rng.uniform(300, 1800)
            level = max(0.0, min(self.device.nominal_power_w, level + self.rng.uniform(-300, 300)))
            self.knots.append((t, level))

    def power(self, t: float) -> float:
        prev_t, prev_v = 0.0, self.base
        for kt, kv in self.knots:
            if t < kt:
                w = _smoothstep((t - prev_t) / max(1.0, kt - prev_t))
                return prev_v + (kv - prev_v) * w
            prev_t, prev_v = kt, kv
        return prev_v


_PROFILE_BY_TYPE = {
    SimDeviceType.BINARY: BinaryProfile,
    SimDeviceType.STANDBY: StandbyProfile,
    SimDeviceType.DIMMABLE: DimmableProfile,
    SimDeviceType.VARIABLE_SPEED: VariableSpeedProfile,
    SimDeviceType.HVAC: HVACProfile,
    SimDeviceType.REFRIGERATOR: RefrigeratorProfile,
    SimDeviceType.WASHING_MACHINE: WasherProfile,
    SimDeviceType.DISHWASHER: DishwasherProfile,
    SimDeviceType.DRYER: DryerProfile,
    SimDeviceType.DOUBLE_POLE: DoublePoleProfile,
    SimDeviceType.UNKNOWN: UnknownLoadProfile,
}


def build_profiles(
    devices: list[SimDevice], seed: int, horizon_s: float
) -> dict[str, DeviceProfile]:
    profiles: dict[str, DeviceProfile] = {}
    for dev in devices:
        cls = _PROFILE_BY_TYPE.get(dev.sim_type, BinaryProfile)
        profiles[dev.device_id] = cls(device=dev, seed=seed, horizon_s=horizon_s)
    return profiles


def inject_simultaneous_events(
    profiles: dict[str, DeviceProfile], *, seed: int, horizon_s: float, batches: int = 6
) -> list[dict]:
    """Force several devices to switch at *exactly* the same simulated second.

    These are the hard cases for disaggregation: one aggregate step that must
    not be attributed to a single device.  The mutation is applied to the
    physical ground-truth profiles (never to measurements), so the records stay
    self-consistent and the step remains visible in the CT aggregation.
    """
    rng = random.Random(seed ^ 0xA5A5A5)
    # Order and sample by structural key, not display id: which loads are chosen
    # for a forced simultaneous step must not depend on what they are called.
    ids = sorted(profiles, key=lambda d: profiles[d].device.seed_key or d)
    anchors: list[dict] = []

    for k in range(batches):
        # Spread anchors across the horizon; leave room for the on-window.
        frac = (k + 1) / (batches + 1)
        t = max(30.0, horizon_s * frac + rng.uniform(-0.05, 0.05) * horizon_s)
        if t >= horizon_s - 1:
            continue
        dur = min(rng.uniform(60, 240), max(30.0, horizon_s - t - 1))
        # Only switch devices that are actually off, so the step is real.
        candidates = [
            dev_id for dev_id in ids
            if profiles[dev_id].device.nominal_power_w >= 100.0
            and profiles[dev_id].effective_power(t - 1.0) < 25.0
            and profiles[dev_id].effective_power(t + 1.0) < 25.0
        ]
        if len(candidates) < 2:
            continue
        group = rng.sample(candidates, k=min(3, len(candidates)))
        anchor: dict = {"t": round(t, 3), "device_ids": group, "duration_s": round(dur, 2)}
        for dev_id in group:
            prof = profiles[dev_id]
            before = prof.effective_power(t - 1.0)
            target = max(prof.effective_power(t - 1.0), prof.device.nominal_power_w)
            prof.forced_windows.append((t, min(horizon_s, t + dur), target))
            after = prof.effective_power(t + 1.0)
            prof.events.append(ScheduledEvent(t, dev_id, "on", after, "simultaneous_on"))
            prof.events.append(
                ScheduledEvent(min(horizon_s, t + dur), dev_id, "off",
                               prof.effective_power(min(horizon_s, t + dur) + 1.0), "simultaneous_off")
            )
            anchor[dev_id] = {"before_w": round(before, 3), "after_w": round(after, 3)}
        anchors.append(anchor)
    return anchors
