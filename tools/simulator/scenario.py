"""Scenario orchestration: ground truth + impaired measurements + HA states.

The record stream is the contract between the simulator and every test harness:

``record = {
    "timestamp", "t_s", "sequence",
    "scenario": {...},                       # immutable run metadata
    "ground_truth": {
        "devices": {device_id: {...}},        # independent of measurements
        "ct_powers_w": {ct_id: watts},        # true CT aggregation
        "ct_energy_kwh": {ct_id: kwh},
        "main_total_w", "panel_totals_w", "unmetered_w",
        "concurrency": int,
        "devices_on": [...],
    },
    "measurements": {ct_id: {...}},           # samples *received* in this tick
    "latest_measurements": {ct_id: {...}},    # aligned snapshot as-of tick end
    "ha_states": {entity_id: {...}},
    "events": [...],                          # device events in this tick
}``
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .behavior import DeviceProfile, build_profiles, inject_simultaneous_events
from .factors import RefossFactorChannel
from .sensors import (
    ChannelImpairment,
    Observation,
    QualityLevel,
    SensorPipeline,
    sampled_times,
)
from .topology import REAL_HOME_TOPOLOGY, HouseholdTopology, SimDeviceType, build_real_home_topology


@dataclass(slots=True)
class SimulatorConfig:
    seed: int = 42
    minutes: float = 60.0
    interval: float = 10.0                # record cadence (seconds)
    start_time: datetime = field(default_factory=lambda: datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    quality: QualityLevel = QualityLevel.GOOD
    # Per-channel cadence overrides; default is the audited 15.7 s median.
    channel_intervals: dict[str, float] = field(default_factory=dict)
    # Channels whose physical CT is reversed at install time.
    inverted_channels: tuple[str, ...] = ()
    # Channels with slow calibration drift (ppm/hour).
    drifting_channels: dict[str, float] = field(default_factory=dict)
    # Per-channel quality overrides.
    channel_quality: dict[str, QualityLevel] = field(default_factory=dict)
    # Communication outage windows, sim seconds: ct_id -> [(start, end), ...].
    outage_windows: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    include_unknown_loads: bool = True
    simultaneous_event_batches: int = 6
    #: Per-device lag (seconds) applied to the Home Assistant *state* timestamp.
    #: The CT sample path and the device-state path are independent in HA: a
    #: `binary_sensor` can be reported later than the wattage it explains, so
    #: `last_updated` is not the instant the load physically changed.  A lag
    #: here reproduces that: the state stream still *transitions* at the physical
    #: instant, but the reported ``last_updated`` is that instant minus the lag.
    #: Used to prove transition attribution does not depend on the two clocks
    #: agreeing.
    state_lag_s: dict[str, float] = field(default_factory=dict)
    #: Extra delivery latency per channel, on top of the quality preset's mean.
    #: Reproduces a slower poller on one CT (``delayed_ct``); the value is still
    #: the value at ``state_timestamp``, only its arrival is late.
    channel_delay_s: dict[str, float] = field(default_factory=dict)
    #: Samples that are *not produced at all* inside the window, as opposed to
    #: :attr:`outage_windows` which produces a delivered stale value.  The
    #: difference matters: an outage says "no new information", a gap says "the
    #: channel was silent".
    missing_windows: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    #: Measurement-only impulses: ``ct_id -> [(start, end, extra_watts)]``.  The
    #: spike is added to the reported value only; ground truth is untouched, so a
    #: test can prove a single spike is neither learned nor charged as device
    #: power.
    spike_windows: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)
    #: Physical polarity changes at a simulated second (a CT re-clamped the wrong
    #: way round mid-run): ``ct_id -> t_s``.  The *declared* polarity stays
    #: normal, so the reading flips sign with no metadata explaining it.
    polarity_flips: dict[str, float] = field(default_factory=dict)
    #: Refoss factor channels: a 240 V appliance whose one leg is metered, with
    #: an explicit factor history.  Absent by default on purpose — a bare float
    #: default would make "no metadata" indistinguishable from a deliberate ×1.
    factor_channels: dict[str, RefossFactorChannel] = field(default_factory=dict)

    @property
    def horizon_s(self) -> float:
        return self.minutes * 60.0

    def as_metadata(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "minutes": self.minutes,
            "interval_s": self.interval,
            "start_time": self.start_time.isoformat(),
            "horizon_s": self.horizon_s,
            "quality": self.quality.value,
            "inverted_channels": list(self.inverted_channels),
            "drifting_channels": dict(self.drifting_channels),
            "channel_quality": {k: v.value for k, v in self.channel_quality.items()},
            "outage_windows": {k: list(v) for k, v in self.outage_windows.items()},
            "include_unknown_loads": self.include_unknown_loads,
            "state_lag_s": dict(self.state_lag_s),
            "channel_delay_s": dict(self.channel_delay_s),
            "missing_windows": {k: list(v) for k, v in self.missing_windows.items()},
            "spike_windows": {k: list(v) for k, v in self.spike_windows.items()},
            "polarity_flips": dict(self.polarity_flips),
            "factor_channels": {
                k: v.as_metadata() for k, v in self.factor_channels.items()
            },
            "simulator_version": "1.1.0",
            "topology_notes": [],
        }


def _iso(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).astimezone(UTC).isoformat()


class HouseholdScenario:
    """Deterministic synthetic household usable directly from tests."""

    def __init__(
        self,
        config: SimulatorConfig | None = None,
        *,
        seed: int | None = None,
        minutes: float | None = None,
        interval: float | None = None,
        topology: HouseholdTopology | None = None,
    ) -> None:
        cfg = config or SimulatorConfig()
        if seed is not None:
            cfg.seed = seed
        if minutes is not None:
            cfg.minutes = float(minutes)
        if interval is not None:
            cfg.interval = float(interval)
        self.config = cfg
        self.topology = topology or build_real_home_topology(
            inverted_channels=cfg.inverted_channels
        )
        self.devices = [
            d for d in self.topology.devices
            if cfg.include_unknown_loads or d.sim_type != SimDeviceType.UNKNOWN
        ]
        self.device_map = {d.device_id: d for d in self.devices}
        self.profiles: dict[str, DeviceProfile] = build_profiles(
            self.devices, cfg.seed, cfg.horizon_s
        )
        self.simultaneous_anchors = inject_simultaneous_events(
            self.profiles,
            seed=cfg.seed,
            horizon_s=cfg.horizon_s,
            batches=cfg.simultaneous_event_batches,
        )
        self._pipeline = SensorPipeline(cfg.seed)
        self._rng = random.Random(cfg.seed ^ 0x5EED)
        self._impairments = self._build_impairments()
        self._sample_times = self._build_sample_times()
        # Delivery bookkeeping.  A sample is *generated* when its physical
        # sample time falls inside the tick, but it is *delivered* only once
        # ``received_at`` has actually elapsed.  Samples that arrive late are
        # therefore held in ``_pending`` and surface in the next tick's
        # ``measurements`` instead of being silently dropped.
        self._next_sample: dict[str, int] = {ch.ct_id: 0 for ch in self.topology.channels}
        self._pending: dict[str, list[Observation]] = {ch.ct_id: [] for ch in self.topology.channels}
        self._energy_kwh = {d.device_id: 0.0 for d in self.devices}
        self._ct_energy_kwh = {c.ct_id: 0.0 for c in self.topology.channels}
        self._prev_tick: float | None = None
        self._previous_reported: dict[str, float] = {}
        self._last_obs: dict[str, Observation] = {}
        self._last_device_state: dict[str, str] = {}
        self._last_changed: dict[str, float] = {}
        self._cumulative_sequence = 0

    # -- setup ----------------------------------------------------------- #
    def _build_impairments(self) -> dict[str, ChannelImpairment]:
        out: dict[str, ChannelImpairment] = {}
        for ch in self.topology.channels:
            q = self.config.channel_quality.get(ch.ct_id, self.config.quality)
            imp = ChannelImpairment.from_quality(ch, q)
            imp.polarity = ch.polarity
            imp.drift_ppm_per_hour = self.config.drifting_channels.get(ch.ct_id, 0.0)
            imp.outage_windows = list(self.config.outage_windows.get(ch.ct_id, []))
            extra_delay = float(self.config.channel_delay_s.get(ch.ct_id, 0.0))
            if extra_delay:
                imp.delay_s = imp.delay_s + extra_delay
            out[ch.ct_id] = imp
        return out

    def _build_sample_times(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for ch in self.topology.channels:
            interval = self.config.channel_intervals.get(ch.ct_id, ch.expected_interval_s)
            jitter = self._impairments[ch.ct_id].jitter_s
            rng = random.Random(self.config.seed ^ (hash_stable(ch.seed_key or ch.ct_id) << 3))
            phase = rng.uniform(0.0, interval)
            out[ch.ct_id] = sampled_times(
                interval, self.config.horizon_s, rng=rng, jitter=jitter, phase=phase
            )
        return out

    # -- truth ----------------------------------------------------------- #
    def ground_truth_at(self, t: float) -> dict[str, Any]:
        device_power: dict[str, float] = {}
        device_states: dict[str, dict[str, Any]] = {}
        for dev in self.devices:
            prof = self.profiles[dev.device_id]
            p = prof.effective_power(t)
            device_power[dev.device_id] = p
            state = prof.effective_state(t)
            attrib = dev.metadata.get("attributable", True)
            entry: dict[str, Any] = {
                "name": dev.name,
                "device_type": dev.sim_type.value,
                "phase": dev.phase.value,
                "power_w": round(p, 4),
                "state": state,
                "dedicated_ct": dev.dedicated_ct,
                "double_pole": dev.double_pole,
                "attributable": attrib,
                "share_branch": not dev.dedicated_ct,
                "branch_cts": list(dev.circuits),
            }
            attrs = prof.extra_attributes(t)
            if attrs:
                entry["attributes"] = attrs
            device_states[dev.device_id] = entry
        ct_powers = self.topology.true_ct_powers(device_power)
        all_on = [
            d for d in self.devices
            if device_states[d.device_id]["power_w"] > 5.0
            and device_states[d.device_id]["attributable"]
        ]
        unmetered = sum(
            p for dev_id, p in device_power.items()
            if not self.device_map[dev_id].metadata.get("attributable", True)
        )
        panel_totals = self.topology.panel_totals(ct_powers)
        return {
            "devices": device_states,
            "ct_powers_w": {k: round(v, 4) for k, v in ct_powers.items()},
            "main_total_w": panel_totals["main"],
            "panel_totals_w": panel_totals,
            "unmetered_w": round(unmetered, 4),
            "concurrency": len(all_on),
            "devices_on": [d.device_id for d in all_on],
        }

    def device_power(self, device_id: str, t: float) -> float:
        return self.profiles[device_id].effective_power(t)

    def device_energy_kwh(self, device_id: str, t: float) -> float:
        # Deterministic trapezoidal integration from 0 to t via fine steps.
        steps = max(1, int(t / 5.0))
        acc = 0.0
        prev = self.profiles[device_id].effective_power(0.0)
        for i in range(1, steps + 1):
            cur_t = i * t / steps
            cur = self.profiles[device_id].effective_power(cur_t)
            acc += (prev + cur) * 0.5 * (t / steps)
            prev = cur
        return acc / 3_600_000.0

    # -- measurement simulation ----------------------------------------- #
    def _in_outage(self, ct_id: str, t: float) -> bool:
        imp = self._impairments[ct_id]
        return any(a <= t < b for a, b in imp.outage_windows)

    def _in_gap(self, ct_id: str, t: float) -> bool:
        """True when the channel produced *no sample at all* at ``t``."""
        return any(
            a <= t < b for a, b in self.config.missing_windows.get(ct_id, ())
        )

    def _spike_at(self, ct_id: str, t: float) -> float:
        extra = 0.0
        for start, end, watts in self.config.spike_windows.get(ct_id, ()):
            if start <= t < end:
                extra += float(watts)
        return extra

    def _reported_multiplier(self, ct_id: str, t: float) -> float:
        channel = self.config.factor_channels.get(ct_id)
        return 1.0 if channel is None else channel.reported_multiplier_at(t)

    def factor_frame(self, ct_id: str, t: float) -> dict[str, Any] | None:
        """Observable factor metadata for a factor channel at ``t``.

        Deliberately contains no truth: it is what the integration's
        configuration says about the CT, which a user could read back.
        """
        channel = self.config.factor_channels.get(ct_id)
        if channel is None:
            return None
        epoch = channel.epoch_at(t)
        return {
            "factor": channel.factor_at(t),
            "applied_by": channel.applied_by_at(t).value,
            "published": bool(epoch is not None and epoch.published),
            "declared": channel.is_declared_at(t),
            "double_applied": channel.double_applied_at(t),
            "metered_leg": channel.metered_leg.value,
            "interval_s": list(epoch.interval) if epoch is not None else None,
        }

    def _observations_for_tick(
        self, t_start: float, t_end: float
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Observation]]:
        """Generate samples whose physical time falls in the tick, then deliver
        everything whose ``received_at`` has elapsed by ``t_end``.

        Returns ``(measurements, latest)`` where ``measurements`` only contains
        samples *received* during this tick and ``latest`` is the as-of-``t_end``
        snapshot per channel.
        """
        delivered: dict[str, dict[str, Any]] = {}
        for ch in self.topology.channels:
            times = self._sample_times[ch.ct_id]
            idx = self._next_sample[ch.ct_id]
            # 1. Generate every physical sample in [t_start, t_end).
            while idx < len(times) and times[idx] < t_end:
                sample_t = times[idx]
                idx += 1
                self._next_sample[ch.ct_id] = idx
                if self._in_gap(ch.ct_id, sample_t):
                    # A gap is silence, not staleness: no sample is produced.
                    continue
                # Evaluate physical truth at the *sample* instant, not tick start.
                truth_devices = {
                    dev.device_id: self.profiles[dev.device_id].effective_power(sample_t)
                    for dev in self.devices
                }
                ct_truth = self.topology.true_ct_powers(truth_devices)
                outage = self._in_outage(ch.ct_id, sample_t)
                conductor = ct_truth.get(ch.ct_id, 0.0)
                # A Refoss factor channel publishes ``conductor * factor``.  The
                # conductor reading is ground truth and is never derived from
                # the factor; only the published value is scaled.
                truth = conductor * self._reported_multiplier(ch.ct_id, sample_t)
                obs = self._pipeline.observe(
                    ch,
                    self._impairments[ch.ct_id],
                    sample_t,
                    truth,
                    previous_reported=self._previous_reported.get(ch.ct_id),
                    outage=outage,
                )
                self._cumulative_sequence += 1
                if obs is None:
                    continue
                flip = self.config.polarity_flips.get(ch.ct_id)
                if flip is not None and sample_t >= float(flip) and not obs.dropped_by_outage:
                    # Re-clamped the wrong way round mid-run.  The declared
                    # polarity still says "normal", so nothing explains it.
                    obs.reported_w = -obs.reported_w
                spike = self._spike_at(ch.ct_id, sample_t)
                if spike and not obs.dropped_by_outage:
                    obs.reported_w = obs.reported_w + spike
                self._previous_reported[ch.ct_id] = obs.reported_w
                self._pending[ch.ct_id].append(obs)

        # 2. Deliver in received_at order, keeping late samples queued.
        for ch in self.topology.channels:
            pending = sorted(self._pending[ch.ct_id], key=lambda o: o.received_at)
            keep: list[Observation] = []
            for obs in pending:
                if obs.received_at > t_end:
                    keep.append(obs)
                    continue
                self._last_obs[ch.ct_id] = obs
                prior = delivered.get(ch.ct_id)
                count = (prior["samples_in_tick"] + 1) if prior else 1
                delivered[ch.ct_id] = {
                    "entity_id": obs.entity_id,
                    "ct_id": obs.ct_id,
                    "value": round(obs.reported_w, 6),
                    "value_w": round(obs.reported_w, 6),
                    "unit": "W",
                    "state_timestamp": _iso(self.config.start_time, obs.state_timestamp),
                    "received_at": _iso(self.config.start_time, obs.received_at),
                    "physical_timestamp": (
                        _iso(self.config.start_time, obs.physical_timestamp)
                        if obs.physical_timestamp is not None else None
                    ),
                    "quality": obs.quality.value,
                    "repeated": obs.repeated,
                    "polarization": self._impairments[ch.ct_id].polarity.value,
                    "sequence": self._cumulative_sequence,
                    "sample_time_s": round(obs.sample_time, 4),
                    "true_channel_power_w": round(obs.iot_truth_w, 4),
                    "injected_noise_w": round(obs.noise_w, 4),
                    "delivered": not obs.dropped_by_outage,
                    "samples_in_tick": count,
                }
                factor = self.factor_frame(ch.ct_id, obs.sample_time)
                if factor is not None:
                    # Metadata only: ``factor`` describes the *declaration* in
                    # force, never the physical truth behind the reading.
                    delivered[ch.ct_id]["factor"] = factor
            self._pending[ch.ct_id] = keep

        latest = {
            ct_id: obs
            for ct_id, obs in self._last_obs.items()
            if obs.received_at <= t_end
        }
        return delivered, latest

    def _ha_states_at(self, t: float) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for dev in self.devices:
            prof = self.profiles[dev.device_id]
            # A device-state path with a reported lag is a *delayed view* of the
            # same physical state machine: at record time ``t`` HA still shows
            # what was true at ``t - lag``.  Only the state stream is delayed;
            # the CT samples are not, which is exactly the mismatch this knob
            # exists to reproduce.
            lag = float(self.config.state_lag_s.get(dev.device_id, 0.0))
            observed_at = max(0.0, t - lag)
            state = prof.effective_state(observed_at)
            if state != self._last_device_state.get(dev.device_id):
                self._last_device_state[dev.device_id] = state
                self._last_changed[dev.device_id] = observed_at
            states[dev.ha_entity_id] = {
                "entity_id": dev.ha_entity_id,
                "state": state,
                "attributes": {
                    "friendly_name": dev.name,
                    "device_class": "power" if dev.sim_type != SimDeviceType.UNKNOWN else None,
                    "unit_of_measurement": "W",
                    **prof.extra_attributes(observed_at),
                },
                "last_changed": _iso(self.config.start_time, self._last_changed.get(dev.device_id, 0.0)),
                "last_updated": _iso(self.config.start_time, observed_at),
                "received_at": _iso(self.config.start_time, t + 0.05),
                "device_id": dev.device_id,
                "attributable": dev.metadata.get("attributable", True),
            }
        return states

    # -- record stream --------------------------------------------------- #
    def records(self) -> Iterator[dict[str, Any]]:
        cfg = self.config
        meta = cfg.as_metadata()
        meta["topology_notes"] = list(self.topology.notes)
        meta["device_count"] = len(self.devices)
        meta["channel_count"] = len(self.topology.channels)
        meta["simultaneous_event_anchors"] = self.simultaneous_anchors

        tick = 0
        t = 0.0
        while t < cfg.horizon_s:
            t_next = min(cfg.horizon_s, t + cfg.interval)
            # The record is an *as-of tick end* snapshot: measurements that
            # arrived in (t, t_next] are compared against physical truth at
            # t_next, so accuracy numbers are not polluted by a one-tick lag.
            gt = self.ground_truth_at(t_next)
            measurements, latest_obs = self._observations_for_tick(t, t_next)
            ha_states = self._ha_states_at(t_next)

            # Energy integration on actual elapsed time (trapezoid).
            dt_h = (t_next - t) / 3600.0
            if self._prev_tick is not None:
                prev_gt = self.ground_truth_at(self._prev_tick)
                for dev in self.devices:
                    p0 = prev_gt["devices"][dev.device_id]["power_w"]
                    p1 = gt["devices"][dev.device_id]["power_w"]
                    self._energy_kwh[dev.device_id] += (p0 + p1) * 0.5 * dt_h / 1000.0
                for ct_id in self._ct_energy_kwh:
                    c0 = prev_gt["ct_powers_w"].get(ct_id, 0.0)
                    c1 = gt["ct_powers_w"].get(ct_id, 0.0)
                    self._ct_energy_kwh[ct_id] += (c0 + c1) * 0.5 * dt_h / 1000.0

            latest = {
                ct_id: {
                    "entity_id": obs.entity_id,
                    "value_w": round(obs.reported_w, 6),
                    "state_timestamp": _iso(self.config.start_time, obs.state_timestamp),
                    "received_at": _iso(self.config.start_time, obs.received_at),
                    "physical_timestamp": (
                        _iso(self.config.start_time, obs.physical_timestamp)
                        if obs.physical_timestamp is not None else None
                    ),
                    "sample_time_s": round(obs.sample_time, 4),
                    "quality": obs.quality.value,
                    "repeated": obs.repeated,
                    "age_s": round(max(0.0, t_next - obs.received_at), 3),
                }
                for ct_id, obs in sorted(latest_obs.items())
            }

            events = self._events_for_tick(t, t_next)

            rec = {
                "timestamp": _iso(cfg.start_time, t_next),
                "t_s": round(t_next, 4),
                "sequence": tick,
                "scenario": meta,
                "ground_truth": {
                    **gt,
                    "ct_energy_kwh": {k: round(v, 8) for k, v in self._ct_energy_kwh.items()},
                    "device_energy_kwh": {k: round(v, 8) for k, v in self._energy_kwh.items()},
                },
                "measurements": measurements,
                "latest_measurements": latest,
                "ha_states": ha_states,
                "events": events,
            }
            self._prev_tick = t_next
            yield rec
            tick += 1
            t = t_next

    def _events_for_tick(self, t0: float, t1: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for dev in self.devices:
            prof = self.profiles[dev.device_id]
            for ev in prof.events:
                if t0 <= ev.t < t1:
                    out.append({
                        "device_id": ev.device_id,
                        "entity_id": dev.ha_entity_id,
                        "state": ev.state,
                        "power_w": round(ev.power_w, 4),
                        "note": ev.note,
                        "occurred_at": _iso(self.config.start_time, ev.t),
                    })
        out.sort(key=lambda e: e["occurred_at"])
        return out

    def to_list(self) -> list[dict[str, Any]]:
        return list(self.records())

    def summary(self) -> dict[str, Any]:
        """Aggregate ground truth over the horizon (no measurements involved)."""
        if self.config.horizon_s <= 0:
            return {}
        mid = self.config.horizon_s / 2.0
        gt = self.ground_truth_at(mid)
        return {
            "seed": self.config.seed,
            "minutes": self.config.minutes,
            "devices": len(self.devices),
            "channels": len(self.topology.channels),
            "midpoint_main_w": gt["main_total_w"],
            "midpoint_sub_w": gt["panel_totals_w"]["sub"],
            "midpoint_unmetered_w": gt["unmetered_w"],
            "concurrency_at_midpoint": gt["concurrency"],
            "simultaneous_event_anchors": len(self.simultaneous_anchors),
        }


def hash_stable(text: str) -> int:
    h = 0
    for ch in text:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h


__all__ = ["HouseholdScenario", "SimulatorConfig", "REAL_HOME_TOPOLOGY"]
