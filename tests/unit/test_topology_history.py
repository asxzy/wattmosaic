"""Topology learned from recorded history ("Discover" also discovers topology).

Live (2026-11-20): pressing Discover found devices but no topology -- a fresh
engine waited hours of live events for its first edge, though HA's recorder
already held the evidence.  The replay uses the live T5 rules and is merged into
the live ledger without double counting.
"""

from __future__ import annotations

import bisect
import random
import time
from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import EdgeEvidence, RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.electrical.topology_history import (
    clean_steps,
    learn_topology_from_history,
    meter_links,
    robust_sigma,
)
from wattmosaic_engine.electrical.topology_inference import TopologyInferenceView

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
HOURS = 6
CADENCE_S = 18.0


def _toggle(rng: random.Random, high: float, mean_s: float) -> list[tuple[float, float]]:
    """(time, level) switch points of an on/off load."""
    t, on, out = 0.0, False, [(0.0, 0.0)]
    while t < HOURS * 3600:
        t += rng.uniform(0.5, 1.5) * mean_s
        on = not on
        out.append((t, high if on else 0.0))
    return out


def _at(points: list[tuple[float, float]], t: float) -> float:
    level = 0.0
    for when, value in points:
        if when > t:
            break
        level = value
    return level


def _house(seed: int = 7) -> dict[str, list[tuple[datetime, float]]]:
    rng = random.Random(seed)
    hrv = _toggle(rng, 300.0, 600.0)      # on the feed
    heater = _toggle(rng, 900.0, 1500.0)  # on the feed
    pump = _toggle(rng, 450.0, 900.0)     # on another panel entirely
    history: dict[str, list[tuple[datetime, float]]] = {
        "feed": [], "branch_hrv": [], "branch_heater": [], "other_panel_pump": []
    }
    t = 0.0
    while t < HOURS * 3600:
        noise = lambda: rng.uniform(-1.0, 1.0)  # noqa: E731
        history["branch_hrv"].append((T0 + timedelta(seconds=t), 20.0 + _at(hrv, t) + noise()))
        history["branch_heater"].append((T0 + timedelta(seconds=t), _at(heater, t) + noise()))
        history["other_panel_pump"].append((T0 + timedelta(seconds=t), _at(pump, t) + noise()))
        # Another device: 7 s out of phase, carrying both branches and a base load.
        tf = t + 7.0
        history["feed"].append(
            (T0 + timedelta(seconds=tf), 400.0 + 20.0 + _at(hrv, tf) + _at(heater, tf) + noise())
        )
        t += CADENCE_S
    return history


def _lifecycle(view: TopologyInferenceView, parent: str, child: str) -> str | None:
    edge = view.edge(parent, child)
    return None if edge is None else str(getattr(edge.lifecycle, "value", edge.lifecycle))


def _empty() -> TopologyInferenceView:
    return TopologyInferenceView(clock=lambda: T0, at=T0, require_direction=True)


def test_clean_steps_finds_a_load_switching_and_ignores_noise() -> None:
    history = _house()["branch_hrv"]
    times = [t for t, _ in history]
    values = [v for _, v in history]
    sigma = robust_sigma(values)
    assert sigma < 3.0
    steps = clean_steps(times, values, sigma)
    assert steps and all(abs(abs(delta) - 300.0) < 10.0 for _lo, _hi, delta in steps)


def test_a_history_replay_confirms_the_wiring_and_nothing_else() -> None:
    learned, stats = learn_topology_from_history(_empty(), _house())
    assert stats.steps > 40
    levels = {"feed": 900.0, "branch_hrv": 170.0, "branch_heater": 450.0, "other_panel_pump": 220.0}
    view = _empty().merge_evidence(learned, levels=levels)
    assert _lifecycle(view, "feed", "branch_hrv") == "structural"
    assert _lifecycle(view, "feed", "branch_heater") == "structural"
    confirmed = {
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in view.edges
        if e.lifecycle == TopologyEdgeState.STRUCTURAL
    }
    assert confirmed == {("feed", "branch_hrv"), ("feed", "branch_heater")}


def test_merging_the_same_replay_twice_counts_nothing_twice() -> None:
    learned, _ = learn_topology_from_history(_empty(), _house())
    once = _empty().merge_evidence(learned)
    twice = once.merge_evidence(learned)
    assert once.edge("feed", "branch_hrv").evidence == twice.edge("feed", "branch_hrv").evidence


def test_a_user_decision_wins_over_the_replay() -> None:
    rejected = TopologyEdgeV3(
        edge_id="te-inf::feed::branch_hrv", relation=RelationType.CONTAINS,
        source_endpoint_id="feed", target_endpoint_id="branch_hrv",
        lifecycle=TopologyEdgeState.REJECTED, declared_by="user",
        evidence=EdgeEvidence(), valid_from=T0, pinned=False,
    )
    live = TopologyInferenceView(edges=(rejected,), clock=lambda: T0, at=T0)
    learned, _ = learn_topology_from_history(live, _house())
    merged = live.merge_evidence(learned)
    assert _lifecycle(merged, "feed", "branch_hrv") == "rejected"
    assert merged.edge("feed", "branch_hrv").evidence.support == 0


def test_the_replay_never_folds_steps_through_the_live_ledger(monkeypatch) -> None:
    # Live: folding each of a week's ~27k steps through observe_group copied the
    # whole ledger per step and held the GIL for minutes, so the engine's event
    # loop starved and the gateway timed requests out (upstream_timeout).
    def _refuse(*_args, **_kwargs):
        raise AssertionError("history replay must count, not fold through observe_group")

    monkeypatch.setattr(TopologyInferenceView, "observe_group", _refuse)
    learned, stats = learn_topology_from_history(_empty(), _house())
    assert stats.steps > 40 and learned.edge("feed", "branch_hrv") is not None


# --------------------------------------------------------------------------- #
# Linking v2 wave 2 (design §5 T5): groups, explain-away, 240 V pairs,
# duplicate meters, change-only recorder rows.  Synthetic households only.
# --------------------------------------------------------------------------- #
def _sampled(
    level, rng: random.Random, *, phase: float = 0.0, cadence: float = 16.0,
    hours: float = HOURS, noise: float = 1.0,
) -> list[tuple[datetime, float]]:
    """A polling reader: one noisy sample of ``level(t)`` every ``cadence`` s."""
    rows, t = [], phase
    while t < hours * 3600:
        rows.append((T0 + timedelta(seconds=t), level(t) + rng.uniform(-noise, noise)))
        t += cadence
    return rows


def _support(view: TopologyInferenceView, parent: str, child: str) -> int:
    edge = view.edge(parent, child)
    return 0 if edge is None else edge.evidence.support


def _switches(rng: random.Random, mean_s: float, hours: float = HOURS) -> list[float]:
    t, out = 0.0, []
    while t < hours * 3600:
        t += rng.uniform(0.5, 1.5) * mean_s
        out.append(t)
    return out


def _on(switches: list[float], t: float) -> bool:
    return bisect.bisect_right(switches, t) % 2 == 1


def test_example_1_one_child_explains_the_parent_step() -> None:
    # P +100 = Y +100: the lone-step rule, unchanged.
    rng = random.Random(11)
    y, w, q = _switches(rng, 900), _switches(rng, 1300), _switches(rng, 1100)
    history = {
        "p": _sampled(lambda t: 200 + 100 * _on(y, t) + 400 * _on(w, t), rng, phase=7),
        "y": _sampled(lambda t: 100 * _on(y, t), rng),
        "q": _sampled(lambda t: 250 * _on(q, t), rng, phase=3),
    }
    learned, _ = learn_topology_from_history(_empty(), history)
    assert _support(learned, "p", "y") >= 8
    assert _support(learned, "p", "q") == 0
    view = _empty().merge_evidence(learned, levels={"p": 400.0, "y": 50.0, "q": 120.0})
    assert _lifecycle(view, "p", "y") == "structural"


def test_example_2_two_children_stepping_together_explain_the_parent() -> None:
    # P +100 = Y +30 + Z +70: neither alone matches; the pair does, every time.
    rng = random.Random(12)
    s = _switches(rng, 900)
    history = {
        "p": _sampled(lambda t: 200 + 100 * _on(s, t), rng, phase=9),
        "y": _sampled(lambda t: 30 * _on(s, t), rng, phase=2),
        "z": _sampled(lambda t: 70 * _on(s, t), rng, phase=5),
    }
    learned, stats = learn_topology_from_history(_empty(), history)
    assert stats.groups < stats.steps  # the three steps of a switch are one group
    assert _support(learned, "p", "y") >= 8
    assert _support(learned, "p", "z") >= 8
    # The lone-step rule never credited these: neither child matches alone.
    assert _support(learned, "y", "p") == 0
    view = _empty().merge_evidence(learned, levels={"p": 250.0, "y": 15.0, "z": 35.0})
    assert _lifecycle(view, "p", "y") == "structural"
    # (A stray reverse support can hold P -> Z at PROVISIONAL: direction rule.)
    assert _lifecycle(view, "p", "z") in ("provisional", "structural")


def _explain_away_house() -> dict[str, list[tuple[datetime, float]]]:
    # P +100 = K +60 + U +40, while V (another panel) steps +100 at the same
    # instants and also carries its own, independent load.  P also carries an
    # unmetered load, so "V contains P" is contradicted.
    rng = random.Random(13)
    s, own, unmetered = _switches(rng, 900), _switches(rng, 700), _switches(rng, 1100)
    return {
        "p": _sampled(lambda t: 50 + 100 * _on(s, t) + 200 * _on(unmetered, t), rng, phase=9),
        "k": _sampled(lambda t: 60 * _on(s, t), rng, phase=1),
        "u": _sampled(lambda t: 40 * _on(s, t), rng, phase=4),
        "v": _sampled(lambda t: 100 * _on(s, t) + 300 * _on(own, t), rng, phase=6),
    }


def _confirmed_edge(parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}", relation=RelationType.CONTAINS,
        source_endpoint_id=parent, target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL, declared_by="user",
        evidence=EdgeEvidence(), valid_from=T0, pinned=False,
    )


def test_a_confirmed_child_is_explained_away_before_matching_the_rest() -> None:
    history = _explain_away_house()
    # Without the known child, V's equal step explains P alone: U gets
    # (almost) nothing.
    blind, _ = learn_topology_from_history(_empty(), history)
    assert _support(blind, "p", "u") <= 2
    assert _support(blind, "p", "v") >= 8
    # Known P -> K (live CONFIRMED): the remainder is U's 40 W, not V's 100 W.
    learned, _ = learn_topology_from_history(_empty(), history, known=[("p", "k")])
    assert _support(learned, "p", "u") >= 8
    assert _support(learned, "p", "v") == 0
    # The seed's own CONFIRMED edges are known children too.
    seeded = TopologyInferenceView(
        edges=(_confirmed_edge("p", "k"),), clock=lambda: T0, at=T0, require_direction=True
    )
    from_seed, _ = learn_topology_from_history(seeded, history)
    assert _support(from_seed, "p", "u") == _support(learned, "p", "u")


def _tub_house(seed: int = 14) -> dict[str, list[tuple[datetime, float]]]:
    """A 240 V tub: one leg on each main; every main also carries its own loads."""
    rng = random.Random(seed)
    tub = _switches(rng, 1000)
    a1, a2, b1, b2 = (_switches(rng, m) for m in (700, 1300, 800, 1500))

    def leg(t: float) -> float:
        return 2000.0 * _on(tub, t)

    return {
        "tub_l1": _sampled(leg, rng, phase=0),
        "tub_l2": _sampled(leg, rng, phase=5),
        "main_l1": _sampled(
            lambda t: 600 + 1500 * _on(a1, t) + 900 * _on(a2, t) + leg(t), rng, phase=9
        ),
        "main_l2": _sampled(
            lambda t: 500 + 1400 * _on(b1, t) + 1100 * _on(b2, t) + leg(t), rng, phase=13
        ),
    }


def test_both_legs_of_a_240v_load_get_support_from_both_mains() -> None:
    history = _tub_house()
    links = meter_links(history)
    assert ("tub_l1", "tub_l2") in links.couples
    learned, _ = learn_topology_from_history(_empty(), history)
    # Each main moves by ONE leg's watts: both legs are supported by both mains
    # (which leg sits on which main is the structural pass's job, M2).
    for main in ("main_l1", "main_l2"):
        for leg in ("tub_l1", "tub_l2"):
            assert _support(learned, main, leg) >= 8, (main, leg)


def test_a_main_is_never_credited_with_the_sum_of_both_legs() -> None:
    # A reader that carries both legs moves by twice one leg: the pair is ONE
    # candidate (the mean), never summed into it (live: -402 W on main L2 =
    # tub -209 -211 was spurious).
    rng = random.Random(15)
    tub, own = _switches(rng, 1000), _switches(rng, 600)
    # The other load never switches within 2 min of the tub: a coincidence
    # (4000 - 1800 = 2200 W ~ one leg) is not what this test is about.
    own = [t for t in own if min(abs(t - s) for s in tub) > 120.0]

    def leg(t: float) -> float:
        return 2000.0 * _on(tub, t)

    history = {
        "tub_l1": _sampled(leg, rng, phase=0),
        "tub_l2": _sampled(leg, rng, phase=5),
        "both": _sampled(lambda t: 300 + 1800 * _on(own, t) + 2 * leg(t), rng, phase=9),
    }
    assert ("tub_l1", "tub_l2") in meter_links(history).couples
    learned, _ = learn_topology_from_history(_empty(), history)
    assert _support(learned, "both", "tub_l1") == 0
    assert _support(learned, "both", "tub_l2") == 0


def _change_only(level, switches: list[float], *, latency: float) -> list[tuple[datetime, float]]:
    """A recorder row only when the value changes (``latency`` s after it)."""
    rows = [(T0, level(0.0))]
    for t in switches:
        rows.append((T0 + timedelta(seconds=t + latency), level(t + 0.001)))
    return rows


def test_a_sparse_change_only_reader_is_measured_with_an_unlimited_hold(monkeypatch) -> None:
    # The recorder writes a row only on change: the parent's level before a
    # child step is often older than 600 s, and capping the hold dropped it.
    import wattmosaic_engine.electrical.topology_history as history_module

    rng = random.Random(16)
    c, d = _switches(rng, 1800, hours=12), _switches(rng, 3100, hours=12)

    def parent(t: float) -> float:
        return 300 + 500 * _on(c, t) + 250 * _on(d, t)

    history = {
        "plug": _sampled(lambda t: 500 * _on(c, t), rng, hours=12),
        "sparse": _change_only(parent, sorted(c + d), latency=17.0),
    }
    assert history_module.HOLD_LOOKBACK_S is None
    learned, _ = learn_topology_from_history(_empty(), history)
    unlimited = _support(learned, "sparse", "plug")
    assert unlimited >= 8
    monkeypatch.setattr(history_module, "HOLD_LOOKBACK_S", 600.0)
    capped, _ = learn_topology_from_history(_empty(), history)
    assert _support(capped, "sparse", "plug") < unlimited


def test_a_duplicate_meter_is_never_a_child_of_its_twin() -> None:
    # A second meter on the mains (+60 W offset) moves with every step of it.
    rng = random.Random(17)
    c1, own = _switches(rng, 2400, hours=12), _switches(rng, 1800, hours=12)

    def mains(t: float) -> float:
        return 400 + 400 * _on(c1, t) + 700 * _on(own, t)

    history = {
        "mains": _sampled(mains, rng, phase=3, hours=12),
        "mains_meter": _sampled(lambda t: mains(t) + 60.0, rng, phase=11, hours=12),
        "circuit": _sampled(lambda t: 400 * _on(c1, t), rng, phase=7, hours=12),
    }
    assert ("mains", "mains_meter") in meter_links(history).duplicates
    learned, stats = learn_topology_from_history(_empty(), history)
    assert stats.duplicates == 1
    assert learned.edge("mains", "mains_meter") is None
    assert learned.edge("mains_meter", "mains") is None
    assert _support(learned, "mains", "circuit") >= 8


def _panel_house(days: float, readers: int, seed: int = 3) -> dict[str, list[tuple[datetime, float]]]:
    """Two mains over four sub-panels over circuits, sampled every 16 s."""
    rng = random.Random(seed)
    cadence, n = 16.0, int(days * 86400 / 16.0)
    circuits = [f"c{i:02d}" for i in range(readers - 6)]
    sub_of = {c: f"sub{i % 4}" for i, c in enumerate(circuits)}
    high = {c: rng.uniform(60, 1500) for c in circuits}
    rate = {c: 1 / rng.uniform(60, 400) for c in circuits}
    level = dict.fromkeys(circuits, 0.0)
    names = [*circuits, "sub0", "sub1", "sub2", "sub3", "main0", "main1"]
    phase = {r: rng.uniform(0, cadence) for r in names}
    out: dict[str, list[tuple[datetime, float]]] = {r: [] for r in names}
    for i in range(n):
        for c in circuits:
            if rng.random() < rate[c]:
                level[c] = high[c] if level[c] == 0 else 0.0
        subs = {f"sub{k}": 30.0 for k in range(4)}
        for c in circuits:
            subs[sub_of[c]] += level[c]
        values = {
            **level, **subs,
            "main0": 200.0 + subs["sub0"] + subs["sub1"],
            "main1": 150.0 + subs["sub2"] + subs["sub3"],
        }
        for r, v in values.items():
            out[r].append((T0 + timedelta(seconds=i * cadence + phase[r]), v + rng.uniform(-1, 1)))
    return out


def test_a_day_of_twenty_readers_replays_in_well_under_three_seconds() -> None:
    # ADR-0033: the replay runs in a worker thread but holds the GIL; a week x
    # 46 readers must stay around 5 s of CPU (live incident: upstream_timeout).
    history = _panel_house(1.0, 20)
    started = time.process_time()
    learned, stats = learn_topology_from_history(_empty(), history)
    elapsed = time.process_time() - started
    assert stats.steps > 500 and stats.passes == 2
    assert _support(learned, "sub0", "c00") >= 8
    assert elapsed < 3.0, f"replay took {elapsed:.2f} s CPU"
