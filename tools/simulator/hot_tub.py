"""The mandatory Hot Tub factor fixture and its variants.

The regression the brief is about
--------------------------------
A Refoss monitor clamped on **one leg** of a 240 V appliance publishes that
leg's current.  The appliance's real power is the *two-leg total*.  When the
monitor (or the user) applies a ×2 factor the published number becomes an
*estimate of the whole load* — and that estimate can be right, wrong, or
unverifiable.  The mandatory numbers are:

==================  ========  ========  =======
Observation         Before    After     Delta
==================  ========  ========  =======
Hot Tub HA power    0 W       500 W     +500 W
CT1                 1000 W    1250 W    +250 W
CT2                 900 W     1150 W    +250 W
==================  ========  ========  =======

So both legs rise 250 W, the appliance rises 500 W, and `2 × 250 = 500` is the
factor relationship.  The point of the fixture is that the *same* observation
triple supports several mutually incompatible conclusions depending on the
factor metadata, and that one of them (summing every CT) is a 250 W
double-count.

Every variant therefore states three separate things:

``observations``
    the raw readings and HA states, exactly as an integration would receive
    them (with the declared factor metadata, which is observable configuration,
    not truth);
``ground_truth``
    the physical leg/total power, which no variant derives from the readings;
``expected``
    the interpretation a correct consumer must reach, *including the variants
    that must be refused*.

Determinism: every value is a pure function of the variant spec plus ``seed``;
the same seed produces byte-identical samples.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from wattmosaic_engine.contracts import Phase

from .expectations import ExpectedInterpretation
from .factors import FactorEpoch, FactorOwner, FactorTimeline, RefossFactorChannel
from .frames import assert_no_ground_truth_keys

# --------------------------------------------------------------------------- #
# Canonical numbers (the brief's table, in one place)
# --------------------------------------------------------------------------- #

DEVICE_ID = "hot_tub"
HA_ENTITY = "sensor.hot_tub_power"
STATE_ENTITY = "binary_sensor.hot_tub"
CT_L1 = "ct_hot_tub_l1"
CT_L2 = "ct_hot_tub_l2"

CT1_BEFORE_W = 1000.0
CT1_AFTER_W = 1250.0
CT2_BEFORE_W = 900.0
CT2_AFTER_W = 1150.0
HA_BEFORE_W = 0.0
HA_AFTER_W = 500.0

#: The appliance is balanced: each leg carries half of the 500 W step.
LEG_DELTA_W = 250.0
APPLIANCE_DELTA_W = 500.0

#: A `before` sample is taken here; the step happens at ``SWITCH_S``; the
#: `after` sample is taken at ``SWITCH_S + SETTLE_S``.
BEFORE_S = 300.0
SWITCH_S = 600.0
SETTLE_S = 120.0


class CtRole(str, Enum):
    """What the CT is physically clamped on."""

    LEG_CONDUCTOR = "leg_conductor"
    """One leg of the appliance: a single-leg reading, needs a factor to become
    an appliance total."""

    WHOLE_LOAD = "whole_load"
    """The monitor already publishes the whole appliance (two-leg) load."""

    INDEPENDENT_LOAD = "independent_load"
    """A genuinely separate load that happens to step by the same amount.  It
    must never be folded into the appliance."""


@dataclass(frozen=True, slots=True)
class HotTubCt:
    """One CT of the fixture, with its role, truth and factor history."""

    ct_id: str
    entity_id: str
    role: CtRole
    leg: Phase
    conductor_before_w: float
    conductor_after_w: float
    timeline: FactorTimeline = field(default_factory=FactorTimeline)
    note: str = ""

    def __post_init__(self) -> None:
        if self.role is not CtRole.WHOLE_LOAD:
            return
        # A whole-load CT publishes the appliance total; its conductor reading
        # already spans both legs.
        if self.leg is not Phase.BOTH:
            raise ValueError("a whole_load CT must declare phase=both")

    @property
    def channel(self) -> RefossFactorChannel:
        return RefossFactorChannel(
            ct_id=self.ct_id,
            device_id=DEVICE_ID,
            metered_leg=self.leg,
            timeline=self.timeline,
            leg_share=0.5 if self.role is CtRole.LEG_CONDUCTOR else 1.0,
            note=self.note,
        )

    def published_w(self, t: float, *, which: str) -> float:
        base = self.conductor_before_w if which == "before" else self.conductor_after_w
        return self.channel.reported_w(base, t)

    def delta_w(self, before_t: float, after_t: float) -> float:
        return self.published_w(after_t, which="after") - self.published_w(
            before_t, which="before"
        )

    @property
    def conductor_delta_w(self) -> float:
        return self.conductor_after_w - self.conductor_before_w


@dataclass(frozen=True, slots=True)
class HotTubFixture:
    """A named, buildable Hot Tub observation dataset.

    Attributes are immutable; :meth:`observations` and :meth:`ground_truth_view`
    return fresh dicts so a caller cannot mutate the fixture through a view.
    """

    name: str
    doc: str
    seed: int
    cts: tuple[HotTubCt, ...]
    expected: ExpectedInterpretation
    ha_before_w: float = HA_BEFORE_W
    ha_after_w: float = HA_AFTER_W
    #: Extra HA evidence that may contradict the power entity (inconsistent
    #: variant).  ``None`` means no state entity is simulated.
    state_before: str | None = None
    state_after: str | None = None
    #: Measurement noise sigma applied to the *samples* (never to the table).
    noise_sigma_w: float = 0.0
    #: Extra pre-existing load on CT1/CT2 so observed legs look unbalanced.
    observed_leg_bias_w: tuple[float, float] = (0.0, 0.0)
    horizon_s: float = 900.0
    interval_s: float = 15.0
    start_time: datetime = field(
        default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    )
    #: Memoised per-channel noise streams.  A mutable dict on a frozen dataclass
    #: is the smallest thing that keeps `samples()` O(n) without weakening the
    #: immutability of the declared fixture.
    _noise_cache: dict[str, list[float]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    # -- identity ---------------------------------------------------------- #
    @property
    def appliance_total_before_w(self) -> float:
        return self.ha_before_w

    @property
    def appliance_total_after_w(self) -> float:
        return self.ha_after_w

    def ct(self, ct_id: str) -> HotTubCt:
        for item in self.cts:
            if item.ct_id == ct_id:
                return item
        raise KeyError(ct_id)

    # -- the brief's table, as asserted numbers ---------------------------- #
    def conductor_table(self) -> dict[str, dict[str, float]]:
        """The *physical conductor* readings: the brief's table, every variant.

        These are the single-leg readings a clamp physically sees.  They are
        identical in every variant because the appliance is; only the factor
        metadata (and therefore the interpretation) differs.
        """
        rows: dict[str, dict[str, float]] = {}
        for item in self.cts:
            rows[item.ct_id] = {
                "before_w": round(item.conductor_before_w, 6),
                "after_w": round(item.conductor_after_w, 6),
                "delta_w": round(item.conductor_delta_w, 6),
            }
        return rows

    def published_table(self) -> dict[str, dict[str, float]]:
        """What each CT *publishes*, i.e. the conductor reading times the
        multiplier the monitor already applied.

        Differs from :meth:`conductor_table` only for a monitor-published total,
        which is exactly the case where multiplying again would double count.
        """
        rows: dict[str, dict[str, float]] = {}
        for item in self.cts:
            rows[item.ct_id] = {
                "before_w": round(item.published_w(BEFORE_S, which="before"), 6),
                "after_w": round(
                    item.published_w(SWITCH_S + SETTLE_S, which="after"), 6
                ),
                "delta_w": round(item.delta_w(BEFORE_S, SWITCH_S + SETTLE_S), 6),
            }
        return rows

    def table(self) -> dict[str, Any]:
        """The mandatory table: physical CT deltas plus the HA power row."""
        rows: dict[str, dict[str, float]] = {
            ct_id: dict(values) for ct_id, values in self.conductor_table().items()
        }
        rows["ha_power"] = {
            "before_w": self.ha_before_w,
            "after_w": self.ha_after_w,
            "delta_w": round(self.ha_after_w - self.ha_before_w, 6),
        }
        return rows

    # -- truth ------------------------------------------------------------- #
    def leg_deltas_w(self) -> dict[str, float]:
        """Physical leg deltas.  Balanced by construction for every variant."""
        return {"l1": LEG_DELTA_W, "l2": LEG_DELTA_W}

    def ground_truth_view(self) -> dict[str, Any]:
        """Physical truth.  Never merged into :meth:`observations`."""
        legs_before = {leg: 0.0 for leg in ("l1", "l2")}
        legs_after = dict(self.leg_deltas_w())
        return {
            "device_id": DEVICE_ID,
            "appliance_total_before_w": round(self.appliance_total_before_w, 6),
            "appliance_total_after_w": round(self.appliance_total_after_w, 6),
            "appliance_total_delta_w": round(
                self.appliance_total_after_w - self.appliance_total_before_w, 6
            ),
            "legs_before_w": legs_before,
            "legs_after_w": legs_after,
            "leg_deltas_w": dict(self.leg_deltas_w()),
            "ct_conductor_before_w": {
                item.ct_id: round(item.conductor_before_w, 6) for item in self.cts
            },
            "ct_conductor_after_w": {
                item.ct_id: round(item.conductor_after_w, 6) for item in self.cts
            },
            "truth_source": "fixture_spec",
        }

    # -- observations ------------------------------------------------------ #
    def _rng(self, ct_id: str) -> random.Random:
        h = 0
        for ch in f"{self.name}/{ct_id}":
            h = (h * 131 + ord(ch)) & 0xFFFFFFFF
        return random.Random((self.seed * 7919) ^ h)

    def noise_series(self, ct_id: str) -> list[float]:
        """One deterministic noise draw per sample of one CT.

        Drawn from a *single* RNG stream per channel and consumed in order, so
        the noise realises differently on each sample (a per-call RNG would
        return the same first gaussian every time and cancel out of any delta).
        """
        if self.noise_sigma_w <= 0.0:
            return []
        rng = self._rng(ct_id)
        count = max(1, math.ceil(self.horizon_s / self.interval_s))
        return [rng.gauss(0.0, self.noise_sigma_w) for _ in range(count)]

    def _sample(
        self,
        item: HotTubCt,
        t: float,
        *,
        index: int,
        tick: int,
        biased: float,
    ) -> dict[str, Any]:
        active = t >= SWITCH_S
        conductor = (item.conductor_after_w if active else item.conductor_before_w) + biased
        channel = item.channel
        noise = self._noise_cache.setdefault(item.ct_id, self.noise_series(item.ct_id))
        reported = channel.reported_w(conductor, t) + (
            noise[tick] if tick < len(noise) else 0.0
        )
        sample: dict[str, Any] = {
            "ct_id": item.ct_id,
            "entity_id": item.entity_id,
            "value_w": round(reported, 6),
            "unit": "W",
            "state_timestamp": (
                self.start_time + timedelta(seconds=t)
            ).isoformat(),
            "received_at": (
                self.start_time + timedelta(seconds=t + 0.3)
            ).isoformat(),
            "physical_timestamp": None,
            "quality": "good",
            "repeated": False,
            "polarization": "normal",
            "sequence": index,
            "sample_time_s": round(t, 4),
            "delivered": True,
            "samples_in_tick": 1,
            "factor": channel.observation(conductor, t) | {
                "ct_id": item.ct_id,
                "device_id": DEVICE_ID,
                "metered_leg": item.leg.value,
            },
        }
        # The factor frame from `observation()` carries no truth; assert it here
        # so a future edit to that method cannot quietly leak through this path.
        assert_no_ground_truth_keys(sample, where=f"{self.name}/{item.ct_id}")
        return sample

    def samples(self) -> list[dict[str, Any]]:
        """A deterministic sample timeline: before, step, after."""
        out: list[dict[str, Any]] = []
        index = 0
        tick = 0
        t = 0.0
        bias = {
            CT_L1: self.observed_leg_bias_w[0],
            CT_L2: self.observed_leg_bias_w[1],
        }
        while t < self.horizon_s:
            for item in self.cts:
                out.append(
                    self._sample(
                        item,
                        t,
                        index=index,
                        tick=tick,
                        biased=bias.get(item.ct_id, 0.0),
                    )
                )
                index += 1
            tick += 1
            t += self.interval_s
        return out

    def observations(self) -> dict[str, Any]:
        """The observable frame: the table, the samples, the declared factors."""
        return {
            "fixture": self.name,
            "device_id": DEVICE_ID,
            "ha_entity": HA_ENTITY,
            "ha_power": {
                "before_w": self.ha_before_w,
                "after_w": self.ha_after_w,
                "delta_w": round(self.ha_after_w - self.ha_before_w, 6),
            },
            "ct_table": self.table(),
            "factor_declarations": {
                item.ct_id: {
                    "role": item.role.value,
                    "metered_leg": item.leg.value,
                    "intervals": item.timeline.as_metadata(),
                }
                for item in self.cts
            },
            "samples": self.samples(),
            "switch_s": SWITCH_S,
            "interval_s": self.interval_s,
        }

    # -- interpretation ---------------------------------------------------- #
    def explained_by(self) -> dict[str, Any]:
        """Which mechanisms reproduce the 500 W HA step, computed from
        observations only.

        Every number here is either a published delta or a declared factor, so a
        production consumer could compute the same table.  ``ha_delta_w`` is the
        HA power entity's own step, which is observation, not truth.
        """
        after_t = SWITCH_S + SETTLE_S
        ha_delta = round(self.ha_after_w - self.ha_before_w, 6)

        conductor_sum = sum(item.conductor_delta_w for item in self.cts)
        published_sum = sum(item.delta_w(BEFORE_S, after_t) for item in self.cts)

        factor_estimates: dict[str, float | None] = {}
        for item in self.cts:
            epoch = item.channel.epoch_at(BEFORE_S)
            base = item.delta_w(BEFORE_S, after_t)
            if epoch is None or not epoch.declared:
                factor_estimates[item.ct_id] = None
            elif epoch.published:
                factor_estimates[item.ct_id] = base
            else:
                factor_estimates[item.ct_id] = base * float(epoch.multiplier)

        # What a naive "add every CT's best total" consumer would publish.  For
        # a variant where a factor already claims the whole appliance this is
        # the double count, which is why it is reported explicitly.
        naive_sum = sum(
            factor_estimates[item.ct_id]
            if factor_estimates[item.ct_id] is not None
            else item.delta_w(BEFORE_S, after_t)
            for item in self.cts
        )

        def _matches(value: float | None) -> bool:
            return value is not None and math.isclose(value, ha_delta, abs_tol=1e-6)

        return {
            "ha_delta_w": ha_delta,
            "conductor_sum_w": round(conductor_sum, 6),
            "conductor_sum_matches": _matches(conductor_sum),
            "published_sum_w": round(published_sum, 6),
            "published_sum_matches": _matches(published_sum),
            "factor_estimates_w": {
                ct_id: (None if value is None else round(value, 6))
                for ct_id, value in factor_estimates.items()
            },
            "factor_matches": {
                ct_id: _matches(value) for ct_id, value in factor_estimates.items()
            },
            "naive_sum_w": round(naive_sum, 6),
            "naive_sum_matches": _matches(naive_sum),
            "summing_allowed": self.expected.summing_allowed,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seed": self.seed,
            "table": self.table(),
            "truth": self.ground_truth_view(),
            "expected": self.expected.as_dict(),
            "explained_by": self.explained_by(),
        }

    def as_measurement_factors(self) -> dict[str, list[Any]]:
        """Every declared factor as contract ``MeasurementFactor`` rows."""
        return {
            item.ct_id: item.channel.as_measurement_factors(self.start_time)
            for item in self.cts
            if item.timeline.epochs
        }


# --------------------------------------------------------------------------- #
# Variant construction
# --------------------------------------------------------------------------- #


def _leg(
    ct_id: str,
    entity_id: str,
    leg: Phase,
    before: float,
    after: float,
    timeline: FactorTimeline,
    *,
    role: CtRole = CtRole.LEG_CONDUCTOR,
    note: str = "",
) -> HotTubCt:
    return HotTubCt(
        ct_id=ct_id,
        entity_id=entity_id,
        role=role,
        leg=leg,
        conductor_before_w=before,
        conductor_after_w=after,
        timeline=timeline,
        note=note,
    )


def _declared(
    multiplier: float,
    *,
    applied_by: FactorOwner = FactorOwner.USER,
    published: bool = False,
    note: str = "",
) -> FactorTimeline:
    return FactorTimeline.constant(
        multiplier, applied_by=applied_by, published=published, declared=True, note=note
    )


def _undeclared() -> FactorTimeline:
    return FactorTimeline(epochs=())


def hot_tub_factor_one(seed: int = 42) -> HotTubFixture:
    """Both legs physically metered, factor declared as ×1.

    Valid because it is the uncontroversial baseline: the two conductor
    readings are 250 W each and *summing them* is the correct way to obtain the
    500 W appliance total.  Any implementation that invents a factor here — or
    that refuses to sum two genuine legs — is wrong.
    """
    return HotTubFixture(
        name="hot_tub_factor_one",
        doc=(
            "Both hot-tub legs are metered by their own CT and the declared "
            "factor is 1 on each. The appliance total is the leg sum (500 W); "
            "no factor is needed and none may be invented."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(1.0, note="leg conductor, raw reading")),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0, note="leg conductor, raw reading")),
        ),
        expected=ExpectedInterpretation(
            basis="ct_measured",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=None,
            summing_allowed=True,
            explainable=True,
            reason=(
                "two genuine leg conductors, factor 1: 250 W + 250 W = 500 W and "
                "the HA total agrees"
            ),
            details={"explained_by": "leg_sum", "legs_are_independent_conductors": True},
        ),
    )


def hot_tub_factor_two(seed: int = 42) -> HotTubFixture:
    """**The mandatory regression fixture.**  User-declared ×2 on the metered leg.

    Valid because the published CT deltas stay 250 W each (raw conductor
    readings) while the appliance total is 500 W.  A correct consumer must use
    the declared ×2 to estimate the two-leg total from *one* CT and must not
    then add the second CT's 250 W as well: that is a 250 W double count of one
    appliance.  The HA entity independently reports 500 W.
    """
    return HotTubFixture(
        name="hot_tub_factor_two",
        doc=(
            "The brief's mandatory dataset. One leg is metered (CT1, 1000->1250 "
            "W) and the user declares x2 on it, so CT1's estimate of the "
            "two-leg appliance is 2*250 = 500 W. CT2 is the other leg "
            "(900->1150 W). HA power 0->500 W. Summing CT1+CT2 would report "
            "750 W: the factor already claims the whole appliance."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(2.0, applied_by=FactorOwner.USER, published=False,
                           note="Refoss single-leg reading, user declares x2")),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0, note="second leg, raw reading")),
        ),
        expected=ExpectedInterpretation(
            basis="factor_adjusted",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=APPLIANCE_DELTA_W,
            summing_allowed=False,
            explainable=True,
            reason=(
                "CT1 is a single-leg conductor with a declared x2 factor: "
                "2 x 250 W = 500 W matches HA, and CT2 must not be added on top"
            ),
            details={
                "explained_by": "factor:x2",
                "factor_ct": CT_L1,
                "factor": 2.0,
                "leg_delta_w": LEG_DELTA_W,
                "double_count_if_summed_w": 750.0,
            },
        ),
    )


def hot_tub_factor_monitor_published(seed: int = 42) -> HotTubFixture:
    """The monitor already published the whole load; no second application.

    The same physical appliance as ``hot_tub_factor_two``, but the ×2 is owned
    by the *monitor* and already baked into the published value.  A consumer
    that multiplies again reports 1000 W for a 500 W appliance.  Included
    because it is the only way to prove the double-application guard.
    """
    return HotTubFixture(
        name="hot_tub_factor_monitor_published",
        doc=(
            "The monitor publishes the two-leg total itself (applied_by=monitor). "
            "CT1's 1000->1250 W *is* the whole-load estimate; applying the "
            "declared factor a second time would report 1000 W for a 500 W step."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(2.0, applied_by=FactorOwner.MONITOR, published=True,
                           note="monitor publishes the whole load")),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0, note="second leg, raw reading")),
        ),
        expected=ExpectedInterpretation(
            basis="monitor_reported",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=APPLIANCE_DELTA_W,
            summing_allowed=False,
            explainable=True,
            reason=(
                "the monitor already applied the factor: CT1's 250 W delta is an "
                "estimated whole-load delta and must not be multiplied again"
            ),
            details={"explained_by": "monitor_factor", "factor_ct": CT_L1, "factor": 2.0},
        ),
    )


def hot_tub_factor_changes_midhistory(seed: int = 42) -> HotTubFixture:
    """×1 before ``SWITCH_S``, ×2 after it.

    Valid because it is the only shape that proves the factor is resolved *per
    sample*.  A consumer that applies today's ×2 to the earlier reading reports
    500 W before the switch, when the appliance was drawing nothing extra; a
    consumer that applies ×1 after the switch reports 250 W for a 500 W step.
    """
    before = FactorEpoch(
        multiplier=1.0, start_s=0.0, end_s=SWITCH_S,
        applied_by=FactorOwner.USER, published=False, note="factor 1 before the change",
    )
    after = FactorEpoch(
        multiplier=2.0, start_s=SWITCH_S, end_s=None,
        applied_by=FactorOwner.USER, published=False, note="factor changed to 2",
    )
    timeline = FactorTimeline(epochs=(before, after))
    return HotTubFixture(
        name="hot_tub_factor_changes_midhistory",
        doc=(
            "The declared factor changes from 1 to 2 at the switch instant. "
            "Readings before the change must not be doubled; readings after it "
            "must use x2. The 250 W leg deltas and 500 W HA delta are unchanged."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 timeline),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0)),
        ),
        expected=ExpectedInterpretation(
            basis="factor_adjusted",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=APPLIANCE_DELTA_W,
            summing_allowed=False,
            explainable=True,
            reason=(
                "the factor in force after the change is x2, so CT1 estimates "
                "500 W; the x1 interval before it must stay unscaled"
            ),
            details={
                "explained_by": "factor:x2",
                "factor_ct": CT_L1,
                "factor_intervals": [list(i) for i in timeline.intervals()],
                "basis_by_interval": [
                    {"start_s": 0.0, "end_s": SWITCH_S, "basis": "ct_measured", "factor": 1.0},
                    {"start_s": SWITCH_S, "end_s": None, "basis": "factor_adjusted", "factor": 2.0},
                ],
            },
        ),
    )


def hot_tub_factor_missing(seed: int = 42) -> HotTubFixture:
    """No factor metadata at all — which is *not* the same as a declared ×1.

    Valid because the numbers look right (250 + 250 = 500) while the basis is
    unknown: with no declaration there is no way to know whether a 250 W CT is
    a leg or a whole load.  A correct consumer must report ``UNKNOWN`` and
    refuse to publish a total rather than accept the arithmetic coincidence.
    """
    return HotTubFixture(
        name="hot_tub_factor_missing",
        doc=(
            "Neither CT carries any factor metadata. The readings still happen "
            "to sum to the HA total, but the basis is unestablished: a single "
            "leg reading with no declared multiplier cannot be promoted to an "
            "appliance total. Expected outcome is UNKNOWN, not 500 W."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _undeclared()),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _undeclared()),
        ),
        expected=ExpectedInterpretation(
            basis="unknown",
            appliance_total_w=None,
            single_ct_estimate_w=None,
            summing_allowed=False,
            explainable=False,
            reason=(
                "no factor metadata exists: 250 W could be a leg or a whole "
                "load, so the basis is unknown and no total may be published"
            ),
            details={"explained_by": "none", "arithmetic_coincidence_w": 500.0},
        ),
    )


def hot_tub_factor_incorrect_metadata(seed: int = 42) -> HotTubFixture:
    """Ownership is self-contradictory: the user factor sits on a monitor total.

    Valid because the declaration claims both "the monitor already applied ×2"
    and "a user still has to apply ×2".  Exactly one can be true.  The correct
    outcome is a refusal with a named conflict, not a 500 W or a 1000 W guess.
    """
    return HotTubFixture(
        name="hot_tub_factor_incorrect_metadata",
        doc=(
            "CT1 declares x2 with applied_by=user *and* published=True. Both "
            "cannot hold, so the double-application guard must fire and the "
            "basis is unverifiable. A consumer that picks either reading of the "
            "metadata reports a wrong appliance total."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(2.0, applied_by=FactorOwner.USER, published=True,
                           note="contradictory ownership")),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0)),
        ),
        expected=ExpectedInterpretation(
            basis="unknown",
            appliance_total_w=None,
            single_ct_estimate_w=None,
            summing_allowed=False,
            explainable=False,
            conflict=True,
            reason=(
                "the factor is declared as both user-applied and monitor-applied; "
                "applying it yields 1000 W, skipping it yields 250 W, truth is 500 W"
            ),
            details={"explained_by": "none", "double_applied": True, "factor_ct": CT_L1},
        ),
    )


def hot_tub_multiple_factor_cts(seed: int = 42) -> HotTubFixture:
    """Two CTs on one appliance both declare ×2.

    Valid because it is the "two factor CTs" trap: each independently estimates
    the same 500 W appliance, so they must be recognised as two measurements of
    one appliance, never summed to 1000 W.
    """
    return HotTubFixture(
        name="hot_tub_multiple_factor_cts",
        doc=(
            "Both CTs are leg conductors and both declare x2. Each estimates "
            "500 W, so CT1+CT2 = 750 W (published deltas) or 1000 W (factor "
            "estimates) double counts. The appliance is 500 W."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(2.0, note="first monitor, x2")),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(2.0, note="second monitor, x2")),
        ),
        expected=ExpectedInterpretation(
            basis="factor_adjusted",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=APPLIANCE_DELTA_W,
            summing_allowed=False,
            explainable=True,
            reason=(
                "two CTs on one appliance both claim x2; each estimate is 500 W "
                "and the pair is an overlap, not two loads"
            ),
            details={
                "explained_by": "factor:x2",
                "factor_ct": CT_L1,
                "double_count_if_summed_w": 1000.0,
                "overlapping_cts": [CT_L1, CT_L2],
            },
        ),
    )


def hot_tub_independent_physical_cts(seed: int = 42) -> HotTubFixture:
    """Two genuinely separate 120 V circuits, each rising 250 W.

    Valid because it is the negative control for the factor story: the numbers
    are identical to ``factor_two``, but here *summing is correct*.  A consumer
    that treats every 250 W step as "half a 240 V load needing ×2" fails this
    fixture, and so does one that refuses to sum.
    """
    return HotTubFixture(
        name="hot_tub_independent_physical_cts",
        doc=(
            "Two independent 120 V circuits (pump and control) on separate "
            "breakers each rise 250 W. The hot-tub HA total rises 500 W. No "
            "factor applies: 250 + 250 is the appliance, and summing is right."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_pump_ct_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(1.0), role=CtRole.INDEPENDENT_LOAD,
                 note="pump circuit, independent 120 V load"),
            _leg(CT_L2, "sensor.hot_tub_control_ct_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0), role=CtRole.INDEPENDENT_LOAD,
                 note="control circuit, independent 120 V load"),
        ),
        expected=ExpectedInterpretation(
            basis="ct_measured",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=None,
            summing_allowed=True,
            explainable=True,
            reason=(
                "two independent 120 V loads: 250 + 250 = 500 W, no factor "
                "involves itself and refusing to sum would lose 250 W"
            ),
            details={"explained_by": "leg_sum", "legs_are_independent_conductors": True},
        ),
    )


def hot_tub_noisy_unbalanced_legs(seed: int = 42) -> HotTubFixture:
    """Noisy readings whose observed legs look unbalanced; truth stays balanced.

    Valid because it separates measurement asymmetry from physical asymmetry:
    the ground-truth legs are 250/250 W, but seeded noise makes the observed
    deltas differ by tens of watts.  A consumer must not "correct" the split
    from that noise, and must not invent a missing leg.
    """
    return HotTubFixture(
        name="hot_tub_noisy_unbalanced_legs",
        doc=(
            "Balanced 250/250 W legs with per-channel noise and a fixed bias so "
            "the observed deltas differ. Truth is balanced: noise must not be "
            "read as an unbalanced load, and no leg may be fabricated."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(1.0)),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0)),
        ),
        expected=ExpectedInterpretation(
            basis="ct_measured",
            appliance_total_w=APPLIANCE_DELTA_W,
            single_ct_estimate_w=None,
            summing_allowed=True,
            explainable=True,
            reason=(
                "observed asymmetry is noise: the physical legs are 250/250 W, "
                "so the total remains the leg sum within noise"
            ),
            details={"explained_by": "leg_sum", "truth_is_balanced": True},
        ),
        noise_sigma_w=18.0,
        observed_leg_bias_w=(35.0, -25.0),
    )


def hot_tub_inconsistent_ha_vs_ct(seed: int = 42) -> HotTubFixture:
    """The HA power entity and the HA *state* entity contradict each other.

    Valid because two independent sources disagree — the power entity reports
    the 500 W step while the binary sensor still says ``off`` — and neither may
    silently win.  The correct outcome is an explicit conflict, not a total.
    """
    return HotTubFixture(
        name="hot_tub_inconsistent_ha_vs_ct",
        doc=(
            "CT1/CT2 rise 250 W each and the HA power entity rises 500 W, but "
            "binary_sensor.hot_tub reports off after the step. The evidence is "
            "self-contradictory: expected outcome is CONFLICT, no total."
        ),
        seed=seed,
        cts=(
            _leg(CT_L1, "sensor.hot_tub_ct1_power", Phase.L1, CT1_BEFORE_W, CT1_AFTER_W,
                 _declared(2.0)),
            _leg(CT_L2, "sensor.hot_tub_ct2_power", Phase.L2, CT2_BEFORE_W, CT2_AFTER_W,
                 _declared(1.0)),
        ),
        expected=ExpectedInterpretation(
            basis="unknown",
            appliance_total_w=None,
            single_ct_estimate_w=None,
            summing_allowed=False,
            explainable=False,
            conflict=True,
            reason=(
                "the state entity says off while both CT legs and the power "
                "entity show a 500 W step; one source must be wrong"
            ),
            details={"explained_by": "none", "conflict_sources": ["ha_state", "ct_evidence"]},
        ),
        state_before="off",
        state_after="off",
    )


#: Registry of every buildable Hot Tub variant, keyed by fixture name.
HOT_TUB_BUILDERS = {
    "hot_tub_factor_one": hot_tub_factor_one,
    "hot_tub_factor_two": hot_tub_factor_two,
    "hot_tub_factor_monitor_published": hot_tub_factor_monitor_published,
    "hot_tub_factor_changes_midhistory": hot_tub_factor_changes_midhistory,
    "hot_tub_factor_missing": hot_tub_factor_missing,
    "hot_tub_factor_incorrect_metadata": hot_tub_factor_incorrect_metadata,
    "hot_tub_multiple_factor_cts": hot_tub_multiple_factor_cts,
    "hot_tub_independent_physical_cts": hot_tub_independent_physical_cts,
    "hot_tub_noisy_unbalanced_legs": hot_tub_noisy_unbalanced_legs,
    "hot_tub_inconsistent_ha_vs_ct": hot_tub_inconsistent_ha_vs_ct,
}

#: The shorthand names required by the brief, mapped to their fixture.
HOT_TUB_VARIANTS = {
    "factor_one": "hot_tub_factor_one",
    "factor_two": "hot_tub_factor_two",
    "factor_changes_midhistory": "hot_tub_factor_changes_midhistory",
    "factor_missing": "hot_tub_factor_missing",
    "factor_incorrect_metadata": "hot_tub_factor_incorrect_metadata",
    "multiple_factor_cts": "hot_tub_multiple_factor_cts",
    "independent_physical_cts": "hot_tub_independent_physical_cts",
    "noisy_unbalanced_legs": "hot_tub_noisy_unbalanced_legs",
    "inconsistent_ha_vs_ct": "hot_tub_inconsistent_ha_vs_ct",
    "monitor_published": "hot_tub_factor_monitor_published",
}


def hot_tub(name: str, seed: int = 42) -> HotTubFixture:
    """Build a Hot Tub fixture by its full or shorthand name.

    Raises ``KeyError`` naming the valid choices rather than silently falling
    back to a default: a typo in a fixture name must fail loudly, because a
    silently substituted variant would make a regression test assert nothing.
    """
    resolved = HOT_TUB_VARIANTS.get(name, name)
    try:
        builder = HOT_TUB_BUILDERS[resolved]
    except KeyError:  # pragma: no cover - defensive, exercised by tests
        raise KeyError(
            f"unknown hot tub fixture {name!r}; valid names: "
            f"{sorted(set(HOT_TUB_BUILDERS) | set(HOT_TUB_VARIANTS))}"
        ) from None
    return builder(seed)


def hot_tub_list(seed: int = 42) -> dict[str, HotTubFixture]:
    """Every variant, built once, keyed by full fixture name."""
    return {name: builder(seed) for name, builder in HOT_TUB_BUILDERS.items()}


__all__ = [
    "APPLIANCE_DELTA_W",
    "BEFORE_S",
    "CT1_AFTER_W",
    "CT1_BEFORE_W",
    "CT2_AFTER_W",
    "CT2_BEFORE_W",
    "CT_L1",
    "CT_L2",
    "CtRole",
    "DEVICE_ID",
    "HA_AFTER_W",
    "HA_BEFORE_W",
    "HA_ENTITY",
    "HOT_TUB_BUILDERS",
    "HOT_TUB_VARIANTS",
    "HotTubCt",
    "HotTubFixture",
    "LEG_DELTA_W",
    "SETTLE_S",
    "SWITCH_S",
    "hot_tub",
    "hot_tub_factor_changes_midhistory",
    "hot_tub_factor_incorrect_metadata",
    "hot_tub_factor_missing",
    "hot_tub_factor_monitor_published",
    "hot_tub_factor_one",
    "hot_tub_factor_two",
    "hot_tub_independent_physical_cts",
    "hot_tub_inconsistent_ha_vs_ct",
    "hot_tub_list",
    "hot_tub_multiple_factor_cts",
    "hot_tub_noisy_unbalanced_legs",
]
