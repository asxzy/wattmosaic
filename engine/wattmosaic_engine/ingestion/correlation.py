"""Which switch owns which CT row?  (ADR-0024)

The problem this solves
-----------------------
A fixture behind a lighting circuit has no CT of its own.  It has a ``light``
entity whose on/off state and brightness are known, and the circuit has a CT
whose watts move when that light changes.  Attributing the watts to the fixture
requires knowing *which* row the fixture is on -- and nothing in the Home
Assistant registry records circuit membership.

Asking the user is always available and always preferred.  This module is the
alternative for the case where the user would rather not enumerate a hundred
dimmers: watch the history and let repeated, unambiguous co-movement decide.

What "clear evidence" means here -- and why every clause is strict
------------------------------------------------------------------
The standing rule is that a guess must never become an attribution.  So a
candidate is only produced when *all* of the following hold:

1. **The switch actually moved.**  A row whose watts wander while the entity sat
   still proves nothing about that entity.  Only transitions count as samples.
2. **The row actually moved.**  A flicker inside measurement noise is not a
   response; the step must exceed the row's own noise floor by a wide margin.
3. **The direction agrees.**  Turning on must raise the row and turning off must
   lower it.  A row that *falls* when the light comes on is a different load
   switching off nearby, not this fixture.
4. **It repeats.**  One coincidence is a coincidence.  At least
   :data:`MIN_CORROBORATIONS` independent on/off pairs must agree, so a single
   coincidence -- the postman opening the gate while the fridge cycles -- never
   creates a device.
5. **It is discriminating.**  A switch that always coincides with *every* row
   (a scene, a whole-house toggle, a row that follows occupancy) cannot single
   out a circuit.  When two or more rows respond equally, the evidence does not
   pick one and no candidate is emitted.
6. **Both directions agree.**  A load that draws 74 W when it turns on and 22 W
   when it turns off was measured against two different backgrounds; the OFF
   edge simply happened to coincide with other loads dropping.  Requiring
   ``min(on, off) / max(on, off)`` to clear
   :data:`MIN_ON_OFF_SYMMETRY` rejects that mixture.
7. **This row is the one that moved.**  Ten lights fired by one scene move
   several circuits at the same instant, and each of them then "correlates" with
   each light.  Dividing the winning row's step by the typical step on the
   *other* rows at the same instant separates that case -- where every circuit
   moves comparably and the ratio collapses to ≈1 -- from a fixture alone on its
   circuit, which scores in the hundreds.  :data:`MIN_SPECIFICITY_Z` is the
   measured midpoint of that gap.

When any clause fails the answer is "not established", which is reported as
``unknown`` rather than as the nearest guess.

This module is deliberately pure: it takes observations and returns candidates.
It never talks to Home Assistant, never reads a name, and never writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: A row must move by at least this multiple of its own quiet-period noise to
#: count as having responded to a switch.
MIN_RESPONSE_SIGMAS = 6.0
#: ...and by at least this absolute amount, so a rock-steady row cannot turn a
#: 0.1 W flicker into a "response" just because its noise floor is tiny.
MIN_RESPONSE_W = 1.0
#: Independent on/off **pairs** that must agree before a candidate is emitted.
#: A pair is one physical toggle: its rising and falling edge are the same event,
#: so counting them separately would let a single cycle look like two
#: corroborations and defeat the whole guard.
MIN_CORROBORATIONS = 3
#: Fraction of *all* observed transitions of this entity that must agree with the
#: winning row.  Below this the switch is inconsistent and no candidate is made.
MIN_AGREEMENT = 0.8
#: How closely the ON step and the OFF step of one fixture must agree, as
#: ``min(on, off) / max(on, off)``.
#:
#: Measured by replaying **11 days of production recorder data** (409 candidate
#: entities, 32 branch rows, 19 191 transitions) through this function with the
#: gate disabled, so the population is every would-be candidate rather than the
#: survivors: the 9 candidates the other gates accept and this one also accepts
#: score **0.875 to 0.998** (vanity 0.948, upstairs pot 0.990, media pot 0.910),
#: while the 6 this gate withholds score **0.174 to 0.670** (stairs 0.174,
#: living room 0.324, HRV sockets 0.517/0.523, shower 0.588, office 0.670).
#: A fixture whose ON and OFF steps disagree by more than 30 % is measuring two
#: different events, not one load: typically the OFF edge coincides with other
#: loads dropping.  0.70 sits inside that measured gap (0.670 < 0.70 < 0.875)
#: and, being a *withholding* gate, errs toward silence.
#:
#: The cost of that conservatism is visible in the same replay: `office_main`
#: is a genuine fixture whose OFF edges are contaminated, and at 0.670 it is
#: withheld by a margin of 0.03.  That is the intended trade -- a wrong wattage
#: is worse than no wattage -- but it is a real recall loss, not a clean cut,
#: and the gate should not be described as if no true fixture ever falls below
#: it.
#:
#: The feasibility study's plateau estimator (median of a 20-120 s window either
#: side of the edge) reports higher values for the same pairs -- office 0.845,
#: vanity 0.989 -- because it averages out the very drop-off this gate is meant
#: to detect.  This module deliberately uses the immediate step, so its numbers
#: run lower and its refusals are more conservative than the study's.
MIN_ON_OFF_SYMMETRY = 0.70
#: How many times larger the winning row's step must be than the typical step of
#: every *other* row at the same transition, i.e. how specific the response is.
#:
#: Measured on the same 11-day replay: the candidates that pass every other gate
#: score **70 to 987** (upstairs pot 70, toilet pot 81, upper-office pot 175,
#: vanity 559, hallway pot 987) while a scene confound -- several rows moving
#: together -- collapses toward 1, because the target is no larger than its
#: neighbours.  20 is below the weakest measured survivor and roughly two orders
#: of magnitude above 1, so it separates "this row was the one that moved" from
#: "the whole scene moved" with headroom for a noisier house.
#:
#: This gate is *not* what withholds the scene lights: on real data they still
#: score in the hundreds, because the scene's rows are summed by the same CT and
#: the target step remains large.  They are caught by ambiguity and symmetry
#: instead.  The gate earns its place on cases where a *different* circuit moves
#: comparably at the same instant -- a shared neutral, or a second fixture on
#: another row switched by the same scene button.
MIN_SPECIFICITY_Z = 20.0
#: Floor for the specificity denominator, in watts.  The recorder prints to
#: 0.001 W, so anything below a microwatt is indistinguishable from "nothing
#: else moved".  Using a floor keeps ``z`` finite instead of dividing by zero
#: when a house has no other circuit with a reading at that instant.
_MIN_SPECIFICITY_DENOM_W = 1e-6
#: A row that responds to at least this share of an entity's transitions, when
#: another row responds too, is not discriminating.  Both are withheld.
AMBIGUITY_SHARE = 0.5
#: Two fixtures on one row that toggle within this many seconds of each other
#: are one physical event (a scene, a Zigbee group, a three-way switch), not two
#: independent loads.  Both are withheld: each would otherwise be credited the
#: *whole* step the other caused, which is the double-counting failure this
#: module exists to prevent.
#:
#: 5 s is chosen from the installation: fixtures driven by one scene switch
#: report within 2 s of each other (median 0.55 s), while fixtures that merely
#: share a circuit are separated by minutes.  A gap that wide cannot be bridged
#: by scheduling jitter, so the bound is not tuned to one household's latency.
CO_TOGGLE_S = 5.0
#: Share of two entities' transitions that must coincide before they are treated
#: as one event.  Below this they are independent fixtures that happen to share a
#: row, which is exactly the case this must keep working.
CO_TOGGLE_SHARE = 0.8
#: Samples older than this are ignored: circuit wiring changes and bulbs get
#: replaced, so a very old correlation is not evidence about today.
MAX_AGE_S = 60 * 60 * 24 * 14
#: Minimum separation between two transitions of one entity to be independent.
MIN_PAIR_GAP_S = 2.0

#: Domains whose *state* proves runtime for a fixture.  A mode entity is never
#: evidence (ADR-0012).
CANDIDATE_DOMAINS = frozenset({"light", "switch", "fan", "input_boolean"})
#: States that mean "off" and "on".  Anything else (``unavailable``,
#: ``unknown``, ``restored``) is not a transition and is skipped, because an
#: offline sensor says nothing about the load (ADR-0015).
OFF_STATES = frozenset({"off", "0", "closed"})
ON_STATES = frozenset({"on", "open"})


@dataclass(slots=True, frozen=True)
class EntityObservation:
    """One state change of a candidate entity, with the row values around it."""

    entity_id: str
    at: datetime
    was_on: bool
    #: Row watts immediately before the change, by ct_id.
    before_w: Mapping[str, float]
    #: Row watts immediately after the change, by ct_id.
    after_w: Mapping[str, float]
    #: Dimmer output as a fraction of full scale *after* the change, when the
    #: entity reports one.  ``None`` means "not reported" -- never a guess, and
    #: never treated as full brightness.  A light that is merely ``on`` with no
    #: brightness attribute is not the same evidence as one at 40 %.
    level: float | None = None
    #: Output level *before* the change.  Needed to read a turn-off as "the
    #: fixture was drawing its watts at this level", which is the only way an
    #: off transition contributes a dimming point.
    previous_level: float | None = None


@dataclass(slots=True, frozen=True)
class CtCandidate:
    """A switch the history says sits on one CT row."""

    entity_id: str
    ct_id: str
    #: Independent on/off pairs that agreed.
    corroborations: int
    #: Share of this entity's usable transitions that the row explained.
    agreement: float
    #: Median absolute step seen on the row when this entity turned on.
    typical_on_w: float
    #: Median absolute step seen when it turned off.
    typical_off_w: float
    #: ``min(on, off) / max(on, off)``: how closely the two directions agree.
    #: ``1.0`` is a load that draws the same either way; near ``0`` one of the
    #: two edges measured something else.  Defaults to ``0.0`` -- "not measured"
    #: -- so a hand-built candidate never claims a symmetry it never measured.
    symmetry: float = 0.0
    #: How many times larger the winning row's step was than the typical step of
    #: every other row at the same instant.  ``~1`` means several circuits moved
    #: together (a scene); a large value means this row was the one that moved.
    specificity_z: float = 0.0
    #: Why the candidate was accepted -- shown to the user, never inferred from.
    evidence: tuple[str, ...] = field(default_factory=tuple)
    #: Measured ``(level_fraction, watts)`` points, ascending, when the entity is
    #: a dimmer and the history contained brightness.  Empty means "no dimming
    #: evidence", which is not the same as "not a dimmer".
    dimming_curve: tuple[tuple[float, float], ...] = field(default_factory=tuple)

    @property
    def typical_w(self) -> float:
        """Best single estimate of what this fixture draws.

        For a dimmer, the draw at *full* output: the curve already describes
        every other level, so collapsing it to a median would report a value the
        fixture never actually held and make ``on_w`` wrong at 100 %.

        For a switch, the *smaller* of the two directions: turning off is the
        cleaner measurement (no inrush, no warm-up), so when they disagree the
        lower value is the more conservative claim about the load.
        """
        if self.is_dimmable:
            return max(watts for _, watts in self.dimming_curve)
        values = [v for v in (self.typical_on_w, self.typical_off_w) if v > 0.0]
        return min(values) if values else 0.0

    @property
    def is_dimmable(self) -> bool:
        """True only when measured output varied with a measured level."""
        return len(self.dimming_curve) >= 2


@dataclass(slots=True, frozen=True)
class CorrelationReport:
    """What the history established, and what it deliberately did not."""

    candidates: tuple[CtCandidate, ...] = field(default_factory=tuple)
    #: Entity ids that moved but could not be placed, with the reason.
    unresolved: Mapping[str, str] = field(default_factory=dict)
    #: Observations discarded before analysis, by reason.
    skipped: Mapping[str, int] = field(default_factory=dict)

    def by_ct(self) -> dict[str, list[CtCandidate]]:
        out: dict[str, list[CtCandidate]] = {}
        for candidate in self.candidates:
            out.setdefault(candidate.ct_id, []).append(candidate)
        return out


def _state_is_on(state: object) -> bool | None:
    text = str(state or "").strip().lower()
    if text in ON_STATES:
        return True
    if text in OFF_STATES:
        return False
    return None


def _dimming_curve(
    observations: Sequence[EntityObservation], ct_id: str
) -> tuple[tuple[float, float], ...]:
    """Measured ``(level, watts)`` points for one fixture on one row.

    Only the **off** transition contributes.  One cycle therefore yields exactly
    one point, which avoids counting the rising and falling edge of the same
    physical measurement as two corroborating samples.  Turning off is also the
    cleaner edge: no inrush and no warm-up, so the row delta is the fixture's
    steady draw at the level it had reached.

    A point is only produced when the level was genuinely reported.  A light
    that says ``on`` with no brightness attribute contributes nothing: assuming
    full output would invent a point from an absence of evidence and then
    misreport every partially dimmed hour.

    Points are merged by level (median of repeats), ascending.
    """
    by_level: dict[float, list[float]] = {}
    for observation in observations:
        if observation.was_on:
            # Rising edge: same cycle as the falling edge below.
            continue
        level = observation.previous_level
        if level is None or level <= 0.0:
            continue
        step = _step(observation.before_w, observation.after_w, ct_id)
        if step is None:
            continue
        by_level.setdefault(round(float(level), 4), []).append(abs(float(step)))
    return tuple(
        (level, _median(values)) for level, values in sorted(by_level.items())
    )


def _count_pairs(
    agreed_rows: Sequence[tuple[bool, set[str]]], ct_id: str
) -> int:
    """Independent on/off **pairs** in which this row agreed.

    One toggle is one corroboration, not two.  Counting the rising and falling
    edge separately would let a single switch cycle satisfy the corroboration
    minimum on its own, which is exactly the single-coincidence failure the
    threshold exists to prevent.  A pair counts when at least one of its two
    transitions was explained by this row.

    The pairing is positional within an entity (transitions alternate on, off,
    on, off), so a cycle whose second half was lost still contributes its first.
    """
    pairs = 0
    index = 0
    while index < len(agreed_rows):
        was_on, agreed = agreed_rows[index]
        partner: set[str] = set()
        if index + 1 < len(agreed_rows) and agreed_rows[index + 1][0] != was_on:
            partner = agreed_rows[index + 1][1]
            index += 2
        else:
            index += 1
        if ct_id in agreed or ct_id in partner:
            pairs += 1
    return pairs


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _on_off_symmetry(typical_on_w: float, typical_off_w: float) -> float:
    """How closely one fixture's ON and OFF steps agree, in ``[0, 1]``.

    ``1.0`` means the row rose and fell by the same amount, which is what a real
    load does.  A value near zero means the two edges measured different things
    -- almost always that the OFF edge coincided with other loads dropping, so
    the number attributed to this fixture would be a mixture.

    Zero or negative input yields ``0.0`` rather than a divide-by-zero or a
    negative ratio: neither direction was measured, so nothing is claimed.  The
    caller treats that as a failed gate, which is the conservative reading.
    """
    if typical_on_w <= 0.0 or typical_off_w <= 0.0:
        return 0.0
    smaller = min(typical_on_w, typical_off_w)
    larger = max(typical_on_w, typical_off_w)
    if larger <= 0.0:
        return 0.0
    return smaller / larger


def _specificity_z(
    target_steps: Sequence[float],
    other_steps: Sequence[float],
    *,
    row_noise_w: float = 0.0,
) -> float:
    """How many times larger the winning row's step is than any other row's.

    This is the guard against a response that is not *this* circuit: when several
    rows move together, every one of them looks correlated with every fixture.
    Dividing the target step by the typical step of the other rows collapses that
    case toward 1 -- the target is no bigger than its neighbours -- while a
    fixture genuinely alone on its circuit scores in the hundreds.

    On production data a *scene* often still scores high, because the scene's
    loads are summed by one CT and the target step stays large; those are
    withheld by the ambiguity and symmetry gates instead.  What this gate catches
    is the case those gates cannot see: a *different* circuit moving comparably
    at the same instant (a shared neutral, or a second fixture on another row
    switched by the same button), where the ratio really does fall to ≈1.

    ``row_noise_w`` is the winning row's own quiet jitter and is used as the
    denominator floor.  It matters when no other row has a reading at the
    transition (a house with one CT, or channels that publish only on change):
    dividing by zero would either raise or report infinity, and neither is a
    measurement.  The tiny absolute floor after it does the same for a row whose
    noise was never estimated.
    """
    if not target_steps:
        return 0.0
    target = _median(target_steps)
    denominator = max(
        _median(other_steps),
        float(max(row_noise_w, 0.0)),
        _MIN_SPECIFICITY_DENOM_W,
    )
    return target / denominator


def _step(before: Mapping[str, float], after: Mapping[str, float], ct_id: str) -> float | None:
    """The change on one row, or ``None`` when either side is missing.

    A missing reading is not a zero step: an unavailable row says nothing, and
    treating it as "no change" would let a candidate be rejected for a gap it
    did not cause.
    """
    if ct_id not in before or ct_id not in after:
        return None
    try:
        return float(after[ct_id]) - float(before[ct_id])
    except (TypeError, ValueError):
        return None


def estimate_row_noise(
    row_readings: Mapping[str, Sequence[tuple[datetime, float]]],
) -> dict[str, float]:
    """Robust per-row jitter, in watts.

    Measured from the median absolute *successive difference* of the raw series.
    A load step is a single large difference among many small ones, so the median
    ignores it; a median absolute deviation of the values themselves would not,
    because a lamp that is on half the time makes the "typical" value sit between
    the on and off levels and inflates the estimate to the size of the load.
    """
    out: dict[str, float] = {}
    for ct_id, readings in row_readings.items():
        ordered = sorted(readings, key=lambda item: item[0])
        values = [float(value) for _, value in ordered]
        diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]
        if len(diffs) < 3:
            out[ct_id] = 0.0
            continue
        # 1.4826 * MAD is the normal-consistent scale of the median absolute
        # deviation; the constant only sets how a "sigma" reads, and every
        # threshold here is expressed in the same units.
        out[ct_id] = 1.4826 * _median(diffs)
    return out


def correlate(
    observations: Iterable[EntityObservation],
    *,
    now: datetime | None = None,
    row_noise_w: Mapping[str, float] | None = None,
    candidate_domains: Iterable[str] = CANDIDATE_DOMAINS,
    max_age_s: float = MAX_AGE_S,
    min_corroborations: int = MIN_CORROBORATIONS,
    min_agreement: float = MIN_AGREEMENT,
    min_response_w: float = MIN_RESPONSE_W,
    min_response_sigmas: float = MIN_RESPONSE_SIGMAS,
    min_on_off_symmetry: float = MIN_ON_OFF_SYMMETRY,
    min_specificity_z: float = MIN_SPECIFICITY_Z,
) -> CorrelationReport:
    """Return the CT rows the history places each switch on.

    Pure: no I/O, no registry, no names.  Everything is decided from co-movement.

    ``row_noise_w`` is the per-row jitter (see :func:`estimate_row_noise`).  It is
    supplied rather than derived from the observations because the observations
    are *selected* around transitions and therefore not a fair sample of the
    row's quiet behaviour.

    ``min_on_off_symmetry`` and ``min_specificity_z`` are the two gates measured
    from production history (see their module constants): the first requires the
    ON and OFF steps of a fixture to agree, the second requires its row to have
    moved far more than any other row at the same instant.  Defaults are the
    measured values; passing ``0.0`` disables a gate, which is useful for
    diagnosing *why* a candidate was withheld but is not a production setting.
    """
    domains = {str(d).lower() for d in candidate_domains}
    kept: list[EntityObservation] = []
    skipped: dict[str, int] = {"too_old": 0, "external_state": 0, "missing_rows": 0}

    horizon = max_age_s
    for observation in observations:
        if now is not None and (now - observation.at).total_seconds() > horizon:
            skipped["too_old"] += 1
            continue
        entity_domain = observation.entity_id.split(".", 1)[0].lower()
        if entity_domain not in domains:
            skipped["external_state"] += 1
            continue
        if not observation.before_w or not observation.after_w:
            skipped["missing_rows"] += 1
            continue
        kept.append(observation)

    if not kept:
        return CorrelationReport(skipped=skipped)

    kept.sort(key=lambda obs: (obs.entity_id, obs.at))
    noise = dict(row_noise_w or {})

    by_entity: dict[str, list[EntityObservation]] = {}
    for observation in kept:
        by_entity.setdefault(observation.entity_id, []).append(observation)

    candidates: list[CtCandidate] = []
    unresolved: dict[str, str] = {}

    for entity_id, entity_observations in sorted(by_entity.items()):
        # Independent pairs only: two transitions within MIN_PAIR_GAP_S are one
        # event split in two by a burst of state reports.
        independent: list[EntityObservation] = []
        last_at: datetime | None = None
        for observation in entity_observations:
            if last_at is not None:
                gap = (observation.at - last_at).total_seconds()
                if gap < MIN_PAIR_GAP_S:
                    continue
            independent.append(observation)
            last_at = observation.at

        if not independent:
            unresolved[entity_id] = "no_independent_transitions"
            continue

        # Per-row: which transitions it explained, split by direction.  Also
        # remembers, per transition, whether that transition agreed, so the
        # *pairs* below can be counted without re-deriving the decision.
        on_steps: dict[str, list[float]] = {}
        off_steps: dict[str, list[float]] = {}
        explained: dict[str, int] = {}
        agreed_rows: list[tuple[bool, set[str]]] = []
        #: Absolute step of every measured row at each transition, agreeing or
        #: not.  Kept so the specificity gate can compare the winning row against
        #: what the *other* circuits did at the very same instants.  A row that
        #: moved but did not pass the response threshold still counts as
        #: movement: it is the confound the gate is looking for.
        steps_at: list[dict[str, float]] = []
        for observation in independent:
            row_ids = set(observation.before_w) & set(observation.after_w)
            agreed: set[str] = set()
            measured: dict[str, float] = {}
            for ct_id in row_ids:
                delta = _step(observation.before_w, observation.after_w, ct_id)
                if delta is None:
                    continue
                measured[ct_id] = abs(delta)
                threshold = max(
                    min_response_w, min_response_sigmas * noise.get(ct_id, 0.0)
                )
                # Turning on must raise the row; turning off must lower it.
                expected_sign = 1.0 if observation.was_on else -1.0
                if delta * expected_sign < threshold:
                    continue
                explained[ct_id] = explained.get(ct_id, 0) + 1
                agreed.add(ct_id)
                target = on_steps if observation.was_on else off_steps
                target.setdefault(ct_id, []).append(abs(delta))
            agreed_rows.append((observation.was_on, agreed))
            steps_at.append(measured)

        if not explained:
            unresolved[entity_id] = "no_row_responded"
            continue

        best = max(explained.values())
        winners = [ct for ct, count in explained.items() if count == best]

        if len(winners) > 1:
            # Two rows moved by the same amount, the same number of times.  There
            # is no basis to prefer one, so state that rather than picking.
            unresolved[entity_id] = "ambiguous_rows"
            continue

        ct_id = winners[0]
        share = best / len(independent)
        corroborations = _count_pairs(agreed_rows, ct_id)
        if corroborations < min_corroborations:
            unresolved[entity_id] = "too_few_corroborations"
            continue
        if share < min_agreement:
            unresolved[entity_id] = "inconsistent_with_row"
            continue
        # Discriminating: if another row also responded on most transitions the
        # switch cannot single this circuit out.
        competing = [
            ct for ct, count in explained.items() if ct != ct_id and count / len(independent) >= AMBIGUITY_SHARE
        ]
        if competing:
            unresolved[entity_id] = "not_discriminating"
            continue

        typical_on_w = _median(on_steps.get(ct_id, []))
        typical_off_w = _median(off_steps.get(ct_id, []))
        # A fixture whose two directions disagree is measuring a confound, not a
        # load: its OFF edge coincided with other loads dropping.  The gate needs
        # *both* directions to have been observed, because an unmeasured
        # direction is not a disagreement -- a 48 h window can legitimately hold
        # only rising edges -- and this gate withholds on evidence, never on its
        # absence.  ``symmetry`` stays 0.0 in that case, which the candidate
        # reports as "not measured".
        symmetric_directions = typical_on_w > 0.0 and typical_off_w > 0.0
        symmetry = _on_off_symmetry(typical_on_w, typical_off_w)
        if symmetric_directions and symmetry < min_on_off_symmetry:
            unresolved[entity_id] = "asymmetric_response"
            continue

        # Specificity: at the transitions this row explained, how big was this
        # row's step compared with what every *other* row did at the same
        # instant?  A scene moves several rows by comparable amounts and scores
        # near 1; a fixture alone on its circuit scores in the hundreds.
        target_steps: list[float] = []
        other_steps: list[float] = []
        for index in range(len(independent)):
            if ct_id not in agreed_rows[index][1]:
                continue
            measured = steps_at[index]
            if ct_id in measured:
                target_steps.append(measured[ct_id])
            other_steps.extend(value for other, value in measured.items() if other != ct_id)
        specificity_z = _specificity_z(
            target_steps, other_steps, row_noise_w=noise.get(ct_id, 0.0)
        )
        if specificity_z < min_specificity_z:
            unresolved[entity_id] = "not_specific"
            continue

        candidates.append(
            CtCandidate(
                entity_id=entity_id,
                ct_id=ct_id,
                corroborations=corroborations,
                agreement=share,
                typical_on_w=typical_on_w,
                typical_off_w=typical_off_w,
                symmetry=symmetry,
                specificity_z=specificity_z,
                evidence=(
                    f"{best} of {len(independent)} independent transitions agreed",
                    f"row moved in the expected direction {share:.0%} of the time",
                    f"no other row responded on {AMBIGUITY_SHARE:.0%} or more",
                    f"on/off symmetry {symmetry:.2f}",
                    f"row moved {specificity_z:.0f}x more than any other circuit",
                ),
                dimming_curve=_dimming_curve(independent, ct_id),
            )
        )

    return CorrelationReport(
        candidates=_withhold_co_toggling(candidates, by_entity, unresolved),
        unresolved=unresolved,
        skipped=skipped,
    )


def _withhold_co_toggling(
    candidates: list[CtCandidate],
    by_entity: Mapping[str, list[EntityObservation]],
    unresolved: dict[str, str],
) -> tuple[CtCandidate, ...]:
    """Drop fixtures on one row that toggle together, because one step paid twice.

    The per-entity guards above ask whether *this entity* singles out a row.  They
    never ask the converse: whether *another entity* claims the same row from the
    same physical event.  Two fixtures on one circuit driven by one scene (a
    double-gang switch, a Zigbee group, two lights on one motion rule) each see
    the row rise by the sum of both, so each independently "explains" the whole
    step and each is credited the whole step.  Summed, the row is counted twice
    and the error is not visible in any single candidate.

    The discriminator is timing, not wattage: two loads that genuinely share a
    circuit switch at unrelated instants, while one scene fires every member
    within a couple of seconds.  So two candidates on the same row are withheld
    only when at least :data:`CO_TOGGLE_SHARE` of one's transitions coincide with
    one of the other's within :data:`CO_TOGGLE_S`.

    Withholding both is deliberate.  Picking one would be a guess about which
    fixture the step belongs to, and this module does not guess.  The pair stays
    reportable as ``unresolved`` so the user can declare the wiring by hand.
    """
    if len(candidates) < 2:
        return tuple(candidates)

    def instants(entity_id: str) -> list[datetime]:
        out: list[datetime] = []
        previous: bool | None = None
        for observation in sorted(by_entity.get(entity_id) or [], key=lambda o: o.at):
            is_on = observation.was_on
            if previous is not None and is_on != previous:
                out.append(observation.at)
            previous = is_on
        return out

    by_row: dict[str, list[CtCandidate]] = {}
    for candidate in candidates:
        by_row.setdefault(candidate.ct_id, []).append(candidate)

    withheld: set[str] = set()
    for ct_id, group in by_row.items():
        if len(group) < 2:
            continue
        times = {c.entity_id: instants(c.entity_id) for c in group}
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                a = times.get(first.entity_id) or []
                b = times.get(second.entity_id) or []
                if not a or not b:
                    continue
                # Each entity's own share of transitions that have a partner in
                # the other list, then the larger of the two.  Dividing a count
                # taken from one list by the length of the other would be
                # meaningless, and a scene that fires only a subset must still
                # trip the guard when that subset is all of the quieter side.
                def matched(source: list[datetime], target: list[datetime]) -> float:
                    if not source:
                        return 0.0
                    hits = sum(
                        1
                        for at in source
                        if any(
                            abs((at - other).total_seconds()) <= CO_TOGGLE_S
                            for other in target
                        )
                    )
                    return hits / len(source)

                share = max(matched(a, b), matched(b, a))
                if share < CO_TOGGLE_SHARE:
                    continue
                for entity_id in (first.entity_id, second.entity_id):
                    withheld.add(entity_id)
                    unresolved[entity_id] = (
                        f"co_toggles_with_another_fixture_on_{ct_id}"
                    )

    if not withheld:
        return tuple(candidates)
    return tuple(c for c in candidates if c.entity_id not in withheld)


def observations_from_history(
    *,
    entity_states: Mapping[str, Sequence[tuple[datetime, str]]],
    row_readings: Mapping[str, Sequence[tuple[datetime, float]]],
    entity_levels: Mapping[str, Sequence[tuple[datetime, float]]] | None = None,
    window_s: float = 30.0,
) -> list[EntityObservation]:
    """Pair entity transitions with the row values around them.

    ``row_readings`` is per ct_id, time-ordered.  For each transition the value
    just before and just after are taken from the nearest reading at or outside
    the transition.  A transition with no reading on one side is dropped: a
    correlation built on an interpolated value would be an inference about a
    measurement that was never made.

    ``entity_levels`` is the optional dimmer output history from
    :func:`parse_history_payload`.  When present, each observation carries the
    level it was at, which is what lets :func:`correlate` learn watts *per
    level* instead of one full-on number.

    ``window_s`` bounds how far away that reading may sit.  A CT that did not
    report for an hour has not measured this transition, and pairing a stale
    value with a fresh one would manufacture a step out of the gap.
    """
    ordered_rows = {
        ct_id: sorted(readings, key=lambda item: item[0])
        for ct_id, readings in row_readings.items()
    }
    ordered_levels = {
        entity_id: sorted(levels, key=lambda item: item[0])
        for entity_id, levels in (entity_levels or {}).items()
    }

    def _level_at_or_before(entity_id: str, at: datetime) -> float | None:
        values = ordered_levels.get(entity_id)
        if not values:
            return None
        found = None
        for stamp, fraction in values:
            if stamp <= at:
                found = fraction
            else:
                break
        return found

    out: list[EntityObservation] = []
    for entity_id, states in entity_states.items():
        ordered = sorted(states, key=lambda item: item[0])
        previous_on: bool | None = None
        previous_level: float | None = None
        for at, raw_state in ordered:
            is_on = _state_is_on(raw_state)
            if is_on is None:
                continue
            level = _level_at_or_before(entity_id, at)
            if previous_on is None:
                # The first reading is a starting point, not a transition.
                previous_on = is_on
                previous_level = level
                continue
            if is_on == previous_on:
                # A brightness change at the same on/off state is not a
                # transition, but it *is* a new level for the next one.
                if level is not None:
                    previous_level = level
                continue
            before_level = previous_level
            previous_on = is_on
            previous_level = level

            before: dict[str, float] = {}
            after: dict[str, float] = {}
            for ct_id, readings in ordered_rows.items():
                prior_at = None
                prior_value = None
                next_at = None
                next_value = None
                for stamp, value in readings:
                    # Strictly before / at-or-after.  A reading stamped at the
                    # transition instant is the *new* value, so treating it as
                    # the baseline would invert the step: the row would appear to
                    # have fallen when the light came on.
                    if stamp < at:
                        prior_at, prior_value = stamp, value
                    else:
                        next_at, next_value = stamp, value
                        break
                if prior_at is None or next_at is None:
                    continue
                if (at - prior_at).total_seconds() > window_s:
                    continue
                if (next_at - at).total_seconds() > window_s:
                    continue
                before[ct_id] = float(prior_value)
                after[ct_id] = float(next_value)
            # A row that did not report around this transition is *absent* from
            # the observation, not a row that failed to move.  Discarding the
            # whole observation because one row was quiet would throw away every
            # other row's valid measurement, and on a real installation many
            # channels publish only on change: one such row would make history
            # correlation impossible.  Only an observation with no measured row
            # at all carries no information.
            if not before or not after:
                continue
            out.append(
                EntityObservation(
                    entity_id=entity_id,
                    at=at,
                    was_on=is_on,
                    before_w=before,
                    after_w=after,
                    level=level,
                    previous_level=before_level,
                )
            )
    return out


def parse_history_payload(
    payload: Mapping[str, Any],
) -> tuple[
    dict[str, list[tuple[datetime, str]]],
    dict[str, list[tuple[datetime, float]]],
    dict[str, list[tuple[datetime, float]]],
]:
    """Split a ``history/history_during_period`` result into states, watts, levels.

    Returns ``(states, readings, levels)``:

    * ``states`` -- ``entity_id -> [(time, state_text)]`` for every entity;
    * ``readings`` -- ``entity_id -> [(time, watts)]`` for entities whose state
      parses as a number (the caller decides which of those is a CT row);
    * ``levels`` -- ``entity_id -> [(time, fraction)]`` for entities that
      reported a dimmer level in their attributes.

    The recorder returns ``{entity_id: [{"s": state, "a": {...}, "lu": epoch}]}``.
    Anything unparseable is dropped rather than coerced: a fabricated timestamp
    would place a transition next to the wrong readings, which is worse than
    having one fewer sample.

    Level history only exists when the query asked for attributes *and* the
    integration records brightness changes.  An empty list is therefore normal
    and means "no dimming evidence", never "level zero".
    """
    states: dict[str, list[tuple[datetime, str]]] = {}
    readings: dict[str, list[tuple[datetime, float]]] = {}
    levels: dict[str, list[tuple[datetime, float]]] = {}
    for entity_id, rows in (payload or {}).items():
        if not isinstance(entity_id, str) or not isinstance(rows, Iterable):
            continue
        state_rows: list[tuple[datetime, str]] = []
        reading_rows: list[tuple[datetime, float]] = []
        level_rows: list[tuple[datetime, float]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw_stamp = row.get("lu", row.get("last_updated"))
            stamp = _parse_stamp(raw_stamp)
            if stamp is None:
                continue
            raw_state = row.get("s", row.get("state"))
            if raw_state is None:
                continue
            text = str(raw_state).strip()
            state_rows.append((stamp, text))
            level = _level_from_attributes(row.get("a", row.get("attributes")))
            if level is not None:
                level_rows.append((stamp, level))
            try:
                value = float(text)
            except ValueError:
                continue
            reading_rows.append((stamp, value))
        if state_rows:
            states[entity_id] = state_rows
        if reading_rows:
            readings[entity_id] = reading_rows
        if level_rows:
            levels[entity_id] = level_rows
    return states, readings, levels


#: Attribute keys that carry dimmer output, with their full-scale value.  A
#: brightness reported as ``0`` while the light is on is *not* full brightness
#: and not a usable level, so zero is dropped (see :func:`_level_from_attributes`).
_LEVEL_KEYS: tuple[tuple[str, float], ...] = (
    ("brightness", 255.0),
    ("brightness_pct", 100.0),
    ("level", 100.0),
    ("percent", 100.0),
    ("dimmer", 100.0),
)


def _level_from_attributes(attributes: Any) -> float | None:
    """Dimmer output as a fraction of full scale, or ``None`` when not stated."""
    if not isinstance(attributes, Mapping):
        return None
    for key, full_scale in _LEVEL_KEYS:
        raw = attributes.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        if value <= 0.0:
            # ``brightness: 0`` is what many integrations publish while off.
            # Treating it as "measured 0 % output" would add a false curve point.
            continue
        return min(value / full_scale, 1.0)
    return None


#: Public alias: live ingestion reads the *same* level definition as history
#: correlation.  Two parsers would drift and the learned curve would not match
#: the level attribution later applies.
level_from_attributes = _level_from_attributes


def _parse_stamp(value: Any) -> datetime | None:
    """Accept the recorder's epoch seconds or an ISO timestamp."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


__all__ = [
    "AMBIGUITY_SHARE",
    "CANDIDATE_DOMAINS",
    "CO_TOGGLE_S",
    "CO_TOGGLE_SHARE",
    "CorrelationReport",
    "CtCandidate",
    "EntityObservation",
    "MAX_AGE_S",
    "MIN_AGREEMENT",
    "MIN_CORROBORATIONS",
    "MIN_ON_OFF_SYMMETRY",
    "MIN_RESPONSE_SIGMAS",
    "MIN_RESPONSE_W",
    "MIN_SPECIFICITY_Z",
    "correlate",
    "estimate_row_noise",
    "level_from_attributes",
    "observations_from_history",
    "parse_history_payload",
]
