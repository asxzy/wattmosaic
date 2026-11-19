"""The HB §17 reference house: a deterministic, scripted end-to-end fixture.

This module turns the handbook's mandatory walkthrough (HB §17, steps 1–10)
into data.  It is used by later tasks (K07's acceptance harness, V1's e2e run,
the three-loop replay driver) so every builder tests against the *same* house.

Topology (HB §17)::

    Main 1500 W
      +-- Subpanel 800 W
      |     +-- Circuit A 300 W
      |     +-- Circuit B 200 W
      +-- Circuit C 400 W
      (plus an unmonitored load directly on Main)

Devices: two always-co-switched living-room lights and a TV on a smart plug
(3 W standby) on Circuit A; a fridge with no HA entity on Circuit B.  Step 9
moves the TV plug from Circuit A to Circuit C in *truth only* — the TV's power
model and its HA entity never change, which is AT-15.

The returned object keeps the two frames strictly separate (see
:mod:`tools.simulator.frames`):

* :attr:`HB17House.ha_messages` / :attr:`HB17House.reader_samples` are the
  observation stream — HA-shaped ``state_changed`` messages and reader samples
  with three timestamps, HA ``context`` and a declared measurement window.
* :attr:`HB17House.truth` is the oracle.  It carries the new truth keys
  ``true_onset_at`` and ``true_circuit`` which the leak detector
  (:func:`tools.simulator.frames.find_ground_truth_keys`) refuses to let cross
  the seam.

Everything is a pure function of ``seed``: two builds with the same seed are
byte-identical.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: Scripted minutes for the physical HB §17 steps.  Steps 1/2/4/5/6/10 are
#: interpretation/state steps (no distinct physical instant of their own) and
#: are anchored to the event that triggers them; steps 3/7/8/9 are real
#: transitions with their own onset.
DEFAULT_STEP_MINUTES: dict[int, float] = {
    3: 5.0,    # living-room lights turn on (+45 W on A/Sub/Main)
    4: 5.0,    # dynamic matching (same instant as step 3)
    5: 5.0,    # joint inference (same instant as step 3)
    6: 5.0,    # matrix update (same instant as step 3)
    7: 10.0,   # fridge compressor starts — no HA event
    8: 12.0,   # TV plug reports its 3 W standby explicitly
    9: 20.0,   # user moves the TV plug to Circuit C (configurable)
    10: 22.0,  # reconciliation / new topology version
}

#: Post-move TV on/off cycles for the ``"relocated_tv"`` variant.  The plug move
#: alone is a single 3 W step; the T7 relocation rule needs a subject's matches
#: to *vanish* on the old reader and *appear* on the new one over >= 5 events
#: (design §5).  Six cycles keep the detector honestly fed with independent
#: observations while staying deterministic.
RELOCATED_TV_TOGGLES = 6

#: Baseline reader powers at the "current measurement state" (HB §17 step 1).
BASELINE_W: dict[str, float] = {
    "ct_main": 1500.0,
    "ct_sub": 800.0,
    "ct_a": 300.0,
    "ct_b": 200.0,
    "ct_c": 400.0,
}

LIGHT_ON_W = 45.0        # two co-switched lights together (HB §17 step 3)
LIGHT_EACH_W = 22.5
FRIDGE_ON_W = 150.0      # compressor draw, no HA (HB §17 step 7)
TV_STANDBY_W = 3.0       # plug-reported standby (HB §17 step 8)
TV_ACTIVE_W = 120.0      # TV on-state, measured by the plug

_STEP_DETAILS = {
    3: "Living-room lights turn on: +45 W on Circuit A, Subpanel and Main.",
    4: "Both lights become candidates; one physical load, three reader columns.",
    5: "Joint inference: a group estimate if the lights cannot be separated.",
    6: "Matrix update: group-level cell for the co-switched lights.",
    7: "Fridge compressor starts with no HA event — unknown-load activity.",
    8: "Smart plug reports the TV's 3 W standby explicitly.",
    9: "User moves the TV plug to Circuit C; mapping changes, model does not.",
    10: "Reconciliation: new topology version, affected cells updated.",
}

LIGHT_ENTITIES = ("light.living_room_1", "light.living_room_2")
TV_ENTITY = "switch.tv_plug"
TV_DEVICE_ENTITY = "media_player.tv"

# Response-timing devices (HB §17 via AT-05 / analysis B WP-B8): a compressor
# that draws power 90 s *after* its thermostat command, and a blind whose power
# is a short transient that settles to a small standby draw.
THERMOSTAT_ENTITY = "climate.living_room_thermostat"
BLIND_ENTITY = "cover.living_room_blind"
HVAC_DELAY_S = 90.0
BLIND_TRANSIENT_S = 8.0
BLIND_ACTIVE_W = 60.0
BLIND_STANDBY_W = 1.5


@dataclass(frozen=True, slots=True)
class HB17Step:
    """One scripted HB §17 step, with its physical onset in simulated seconds."""

    step: int
    name: str
    minute: float
    at_s: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "name": self.name,
            "minute": self.minute,
            "at_s": self.at_s,
            "detail": self.detail,
        }


@dataclass
class HB17House:
    """A built HB §17 house: observation streams plus a separate truth frame."""

    seed: int
    minutes: float
    variant: str
    interval_s: float
    start_time: datetime
    steps: list[HB17Step]
    plug_move_minute: float
    records: list[dict[str, Any]] = field(default_factory=list)

    # -- observation frame ------------------------------------------------- #
    @property
    def ha_messages(self) -> list[dict[str, Any]]:
        """Every HA ``state_changed`` message, in delivery order."""
        out: list[dict[str, Any]] = []
        for record in self.records:
            out.extend(record.get("ha_messages") or ())
        return out

    @property
    def reader_samples(self) -> list[dict[str, Any]]:
        """Every delivered reader sample, flattened across records."""
        out: list[dict[str, Any]] = []
        for record in self.records:
            for reader_id, sample in (record.get("reader_samples") or {}).items():
                out.append({"reader_id": reader_id, **sample})
        return out

    def observation_view(self) -> list[dict[str, Any]]:
        """The observation frame only — the ground-truth seam, enforced."""
        from .frames import observation_view

        return [observation_view(record) for record in self.records]

    # -- truth frame ------------------------------------------------------- #
    @property
    def truth(self) -> dict[str, Any]:
        """The full oracle: per-step physical onsets and true circuit mapping."""
        onsets = {s.name: s.at_s for s in self.steps if s.step in (3, 7, 8, 9)}
        onsets.update({f"step_{s.step}": s.at_s for s in self.steps})
        # The final per-record truth also carries the response-timing onsets.
        if self.records:
            onsets.update(self.records[-1]["truth"]["true_onset_at"])
        return {
            "true_onset_at": onsets,
            "true_circuit": {
                "tv": "ct_c",
                "lights": "ct_a",
                "fridge": "ct_b",
            },
            "steps": [s.as_dict() for s in self.steps],
            "topology": {
                "ct_main": {"parent": None, "baseline_w": BASELINE_W["ct_main"]},
                "ct_sub": {"parent": "ct_main", "baseline_w": BASELINE_W["ct_sub"]},
                "ct_a": {"parent": "ct_sub", "baseline_w": BASELINE_W["ct_a"]},
                "ct_b": {"parent": "ct_sub", "baseline_w": BASELINE_W["ct_b"]},
                "ct_c": {"parent": "ct_main", "baseline_w": BASELINE_W["ct_c"]},
                "unmonitored": {"parent": "ct_main", "baseline_w": 300.0},
            },
            "plug_move_minute": self.plug_move_minute,
            "variant": self.variant,
        }

    def messages_for(self, entity_id: str) -> list[dict[str, Any]]:
        return [m for m in self.ha_messages if m["entity_id"] == entity_id]

    # -- convenience ------------------------------------------------------- #
    def step_at(self, step: int) -> HB17Step:
        for s in self.steps:
            if s.step == step:
                return s
        raise KeyError(f"no HB §17 step {step}")


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def _snap(t: float) -> float:
    """A deterministic sub-watt measurement perturbation (zero mean)."""
    return math.sin(t * 0.017) * 0.4 + math.cos(t * 0.031) * 0.3


def _ctx(device: str, t: float, *, parent: str | None = None, user: str | None = None) -> dict:
    """An HA-shaped context: id / parent_id / user_id at the top level."""
    return {
        "id": f"ctx-{device}-{int(round(t * 1000)):06d}",
        "parent_id": parent,
        "user_id": user,
    }


def build(
    seed: int,
    minutes: float = 30.0,
    variant: str = "co_switched",
    *,
    plug_move_minute: float | None = None,
    interval_s: float = 10.0,
    start_time: datetime | None = None,
    lead_in_minutes: float = 0.0,
) -> HB17House:
    """Build the HB §17 reference house.

    ``variant`` is ``"co_switched"`` (the default: the two lights always switch
    together and are non-identifiable), ``"solo_switches"`` (one light gets
    three extra solo on/off cycles, giving I4's identifiability positive
    control) or ``"relocated_tv"`` (after step 9 the TV toggles on/off
    :data:`RELOCATED_TV_TOGGLES` times so the T7 relocation detector sees >= 5
    post-move events).  ``plug_move_minute`` overrides when step 9 happens.

    ``lead_in_minutes`` prepends a quiet, steady prelude (HB §17 step 1,
    "establish the current measurement state") before the scripted steps: every
    step instant -- and the horizon -- is shifted later by that amount, and the
    prelude carries only baseline physics (no HA message, no transition).  A
    real installation has been observing its readers long before a load event;
    a streaming detector needs that history to trust its noise estimate (P3
    ``sigma_warmup_samples``).  The default ``0.0`` keeps the original script.
    """
    if lead_in_minutes < 0:
        raise ValueError("lead_in_minutes must be >= 0")
    lead = float(lead_in_minutes)
    if variant not in ("co_switched", "solo_switches", "relocated_tv"):
        raise ValueError(
            f"unknown hb17 variant {variant!r}: expected "
            "'co_switched', 'solo_switches' or 'relocated_tv'"
        )
    rng = random.Random(seed ^ 0x17)
    base = start_time or datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    move_min = (
        plug_move_minute if plug_move_minute is not None else DEFAULT_STEP_MINUTES[9]
    ) + lead
    recon_min = move_min + 2.0

    # The relocated variant needs a stream long enough for every post-move TV
    # toggle (the T7 rule wants >= 5).  Extend the horizon rather than silently
    # truncating the script: a shorter caller-supplied duration is honoured for
    # the other variants, but the relocated variant is self-contained.
    if variant == "relocated_tv":
        spacing_minutes = 5.0
        needed_min = (
            move_min + spacing_minutes + (RELOCATED_TV_TOGGLES - 1) * spacing_minutes + 1.0
        )
        minutes = max(float(minutes), needed_min - lead)
    # The caller's ``minutes`` is the scripted duration; the prelude is added.
    minutes = float(minutes) + lead

    step_minutes = {n: m + lead for n, m in DEFAULT_STEP_MINUTES.items()}
    step_minutes[9] = move_min
    step_minutes[10] = recon_min
    step_names = {
        3: "lights_on",
        4: "dynamic_matching",
        5: "joint_inference",
        6: "matrix_update",
        7: "fridge_compressor_on",
        8: "tv_standby_reported",
        9: "plug_moved",
        10: "reconciliation",
    }
    steps = [
        HB17Step(
            step=n,
            name=step_names[n],
            minute=step_minutes[n],
            at_s=round(step_minutes[n] * 60.0, 4),
            detail=_STEP_DETAILS[n],
        )
        for n in sorted(step_minutes)
    ]
    at = {s.step: s.at_s for s in steps}

    house = HB17House(
        seed=seed,
        minutes=float(minutes),
        variant=variant,
        interval_s=float(interval_s),
        start_time=base,
        steps=steps,
        plug_move_minute=float(move_min),
    )

    horizon = float(minutes) * 60.0
    n_ticks = max(1, int(math.ceil(horizon / interval_s)))

    # Track the current on/off state of each physical device.
    lights_on = False
    fridge_on = False
    tv_on = False
    tv_circuit = "ct_a"
    tv_circuit_changed = False

    # Delayed/transient response onsets (AT-05): the thermostat emits an HA
    # command, the compressor follows 90 s later; the blind emits a command and
    # its power is a short transient settling to standby.
    thermostat_at = at[3] + 9.0 * 60.0
    compressor_at = thermostat_at + HVAC_DELAY_S
    blind_cmd_at = at[3] + 11.0 * 60.0
    blind_end_at = blind_cmd_at + BLIND_TRANSIENT_S

    # Solo switches for the identifiable variant: three isolated on/off cycles
    # of light 1 only, after the co-switched event and before the plug move.
    solo_windows: list[tuple[float, float]] = []
    if variant == "solo_switches":
        gap = (move_min - 6.0 - step_minutes[3]) * 60.0 / 3.0
        start = at[3] + 60.0
        for i in range(3):
            s = start + i * max(60.0, gap)
            solo_windows.append((s, s + 45.0))

    # Post-move TV on/off cycles for the relocated variant.  Each cycle is an
    # independent HA-commanded event whose physics appears on the plug endpoint
    # (still measuring the TV) and on whichever circuit the TV is on.  After
    # step 9 that circuit is ct_c, so the old mapping's reader (ct_a) loses the
    # TV's match while ct_c gains it — exactly the T7 relocation signature.
    # A couple of pre-move cycles on ct_a establish the "vanished on A" half of
    # the history; spacing the starts keeps every cycle an independent event.
    tv_toggle_starts: list[float] = []
    if variant == "relocated_tv":
        spacing = 300.0  # one cycle every 5 simulated minutes (independent groups)
        pre = [at[8] + 60.0]  # one pre-move cycle establishes the old reader
        post = [at[9] + spacing + i * spacing for i in range(RELOCATED_TV_TOGGLES)]
        tv_toggle_starts = pre + post

    def _iso(t: float) -> str:
        return (base + timedelta(seconds=float(t))).astimezone(UTC).isoformat()

    def _lights_power(t: float) -> tuple[float, float]:
        """Return (light1, light2) watts at time t."""
        solo = any(a <= t < b for a, b in solo_windows)
        if lights_on:
            return (LIGHT_EACH_W, LIGHT_EACH_W)
        if solo:
            return (LIGHT_EACH_W, 0.0)
        return (0.0, 0.0)

    def _tv_power(t: float) -> float:
        """TV power at ``t``: active during any post-move on-window."""
        if tv_on:
            return TV_ACTIVE_W
        if any(a <= t < a + 45.0 for a in tv_toggle_starts):
            return TV_ACTIVE_W
        return TV_STANDBY_W

    def _fridge_power() -> float:
        return FRIDGE_ON_W if fridge_on else 0.0

    def _compressor_power(t: float) -> float:
        return 900.0 if t >= compressor_at else 0.0

    def _blind_power(t: float) -> float:
        if blind_cmd_at <= t < blind_end_at:
            return BLIND_ACTIVE_W
        return BLIND_STANDBY_W if t >= blind_end_at else 0.0

    def _reader_values(t: float) -> dict[str, float]:
        l1, l2 = _lights_power(t)
        lights = l1 + l2
        tv = _tv_power(t)
        fridge = _fridge_power()
        tv_on_a = tv if tv_circuit == "ct_a" else 0.0
        tv_on_c = tv if tv_circuit == "ct_c" else 0.0
        a = BASELINE_W["ct_a"] + lights + tv_on_a
        b = BASELINE_W["ct_b"] + fridge
        c = BASELINE_W["ct_c"] + tv_on_c
        sub = BASELINE_W["ct_sub"] + lights + fridge + tv_on_a
        main = BASELINE_W["ct_main"] + lights + fridge + tv_on_a + tv_on_c
        return {
            "ct_a": a,
            "ct_b": b,
            "ct_c": c,
            "ct_sub": sub,
            "ct_main": main,
            "ct_plug": tv,
            "ct_hvac": _compressor_power(t),
            "ct_blind": _blind_power(t),
        }

    READER_META: dict[str, dict[str, Any]] = {
        "ct_main": {"parent": None, "entity": "sensor.panel_main_power"},
        "ct_sub": {"parent": "ct_main", "entity": "sensor.subpanel_power"},
        "ct_a": {"parent": "ct_sub", "entity": "sensor.circuit_a_power"},
        "ct_b": {"parent": "ct_sub", "entity": "sensor.circuit_b_power"},
        "ct_c": {"parent": "ct_main", "entity": "sensor.circuit_c_power"},
        # The plug is a single-subject endpoint measuring only the TV.
        "ct_plug": {"parent": None, "entity": "sensor.tv_plug_power", "window_ms": 0.0},
        # Response-timing readers for AT-05 (delayed compressor, blind transient).
        "ct_hvac": {"parent": None, "entity": "sensor.hvac_compressor_power"},
        "ct_blind": {"parent": None, "entity": "sensor.living_room_blind_power"},
    }

    for tick in range(n_ticks):
        t0 = tick * interval_s
        t1 = min(horizon, (tick + 1) * interval_s)
        t_mid = (t0 + t1) / 2.0
        tick_messages: list[dict[str, Any]] = []

        def _ha(entity: str, state: str, *, t: float, parent: str | None = None,
                user: str | None = None, power: float | None = None,
                _messages: list[dict[str, Any]] = tick_messages) -> None:
            attrs: dict[str, Any] = {"friendly_name": entity.split(".")[-1].replace("_", " ")}
            if entity in LIGHT_ENTITIES:
                attrs["device_class"] = "light"
            if entity == TV_ENTITY:
                attrs["device_class"] = "power"
                attrs["unit_of_measurement"] = "W"
            if power is not None:
                attrs["power_w_observation"] = round(power, 4)
            _messages.append({
                "entity_id": entity,
                "state": state,
                "attributes": attrs,
                "context": _ctx(entity, t, parent=parent, user=user),
                "last_changed": _iso(t),
                "last_updated": _iso(t),
                "observed_at": _iso(t),
                "received_at": _iso(t + 0.05),
            })

        # -- advance physical state at scripted instants ------------------- #
        # We process each step whose onset falls in (t0, t1].
        for s in steps:
            if not (t0 < s.at_s <= t1):
                continue
            if s.step == 3:
                lights_on = True
                parent = f"automation-hb17-lights-{seed}"
                for entity in LIGHT_ENTITIES:
                    _ha(entity, "on", t=s.at_s, parent=parent)
                # A decoy context: two switches share a parent id, but their
                # physical changes are 60 s apart (so context alone is not
                # proof of simultaneity — HB §6.6 / analysis B WP-B8).
                _ha("switch.decoy_a", "on", t=s.at_s, parent="decoy-parent")
                _ha("switch.decoy_b", "on", t=s.at_s + 60.0, parent="decoy-parent")
            elif s.step == 7:
                # Fridge compressor: a physical change with NO HA message.
                fridge_on = True
            elif s.step == 8:
                tv_on = False
                _ha(TV_ENTITY, "standby", t=s.at_s, power=TV_STANDBY_W)
            elif s.step == 9:
                tv_circuit = "ct_c"
                tv_circuit_changed = True
            elif s.step == 10:
                pass  # reconciliation is an engine-side interpretation step

        if variant == "solo_switches":
            for i, (a, b) in enumerate(solo_windows):
                if t0 < a <= t1:
                    _ha(LIGHT_ENTITIES[0], "on", t=a, user=f"user-solo-{i}")
                if t0 < b <= t1:
                    _ha(LIGHT_ENTITIES[0], "off", t=b, user=f"user-solo-{i}")

        if variant == "relocated_tv":
            for i, a in enumerate(tv_toggle_starts):
                if t0 < a <= t1:
                    _ha(TV_ENTITY, "on", t=a, user=f"user-tv-toggle-{i}")
                if t0 < a + 45.0 <= t1:
                    _ha(TV_ENTITY, "off", t=a + 45.0, user=f"user-tv-toggle-{i}")

        # Delayed/transient response commands.  Only the HA *command* is at the
        # scripted instant; the physical response is in the reader values above.
        if t0 < thermostat_at <= t1:
            _ha(THERMOSTAT_ENTITY, "heat", t=thermostat_at)
        if t0 < blind_cmd_at <= t1:
            _ha(BLIND_ENTITY, "opening", t=blind_cmd_at)

        # -- reader samples ------------------------------------------------ #
        values = _reader_values(t_mid)
        reader_samples: dict[str, Any] = {}
        for reader_id, meta in READER_META.items():
            value = values[reader_id] + _snap(t_mid + rng.uniform(-1e-6, 1e-6))
            window_ms = float(meta.get("window_ms", 1000.0))
            state_t = t_mid
            received_t = t_mid + 0.05
            # WP-B8 delivery impairments, pinned by the named tests:
            #  * ct_b's fridge-step sample is a late backfill (receipt 25 s late);
            #  * ct_c samples are delivered out of order (negative receipt shift);
            #  * ct_a carries a +30 s reader clock skew on its state timestamp.
            if reader_id == "ct_b" and fridge_on and t0 < at[7] + 10 <= t1:
                received_t += 130.0
            if reader_id == "ct_c":
                received_t -= 0.03
            if reader_id == "ct_a":
                state_t += 30.0
            reader_samples[reader_id] = {
                "reader_id": reader_id,
                "entity_id": meta["entity"],
                "parent": meta["parent"],
                "value_w": round(value, 6),
                "value": round(value, 6),
                "unit": "W",
                "state_timestamp": _iso(state_t),
                "received_at": _iso(received_t),
                "last_reported": _iso(state_t),
                "measurement_window_ms": window_ms,
                "quality": "good",
                "sequence": tick - 1 if reader_id == "ct_c" else tick,
            }

        # -- truth frame (never crosses the observation seam) ------------- #
        truth: dict[str, Any] = {
            "t_s": round(t1, 4),
            "true_onset_at": {
                **{s.name: s.at_s for s in steps if s.step in (3, 7, 8, 9)},
                "thermostat_command": thermostat_at,
                "compressor_response": compressor_at,
                "blind_command": blind_cmd_at,
            },
            "true_circuit": {
                "tv": tv_circuit,
                "lights": "ct_a",
                "fridge": "ct_b",
            },
            "true_lights_on": lights_on,
            "true_fridge_on": fridge_on,
            "true_tv_power_w": round(_tv_power(t_mid), 4),
            "plug_moved": tv_circuit_changed,
            # Grader-only per-device and per-conductor watts at the sample
            # instant (the accuracy harness scores published estimates against
            # these).  Keyed by the subject ids a user declares: one per HA light.
            "device_power_w": {
                LIGHT_ENTITIES[0]: _lights_power(t_mid)[0],
                LIGHT_ENTITIES[1]: _lights_power(t_mid)[1],
                "tv": round(_tv_power(t_mid), 4),
                "fridge": _fridge_power(),
                "hvac_compressor": _compressor_power(t_mid),
                "blind": _blind_power(t_mid),
            },
            "ct_powers_w": {k: round(v, 6) for k, v in _reader_values(t_mid).items()},
            #: The unmonitored load directly on Main (HB §17 preamble).
            "unmetered_w": BASELINE_W["ct_main"] - BASELINE_W["ct_sub"] - BASELINE_W["ct_c"],
            #: Subjects whose individual power the scripted evidence cannot
            #: separate: the co-switched lights never switch alone.
            "non_identifiable_groups": (
                [] if variant == "solo_switches" else [list(LIGHT_ENTITIES)]
            ),
        }

        house.records.append({
            "t_s": round(t1, 4),
            "timestamp": _iso(t1),
            "sequence": tick,
            "reader_samples": reader_samples,
            "ha_messages": tick_messages,
            "truth": truth,
        })

    return house


__all__ = [
    "BASELINE_W",
    "DEFAULT_STEP_MINUTES",
    "HB17House",
    "HB17Step",
    "build",
]
