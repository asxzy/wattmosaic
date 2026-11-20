"""Topology learned from recorded history ("Discover" also discovers topology).

Live (2026-11-20): pressing Discover found devices but no topology -- a fresh
engine waited hours of live events for its first edge, though HA's recorder
already held the evidence.  The replay uses the live T5 rules and is merged into
the live ledger without double counting.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import EdgeEvidence, EdgeLifecycleV3, RelationType, TopologyEdgeV3
from wattmosaic_engine.electrical.topology_history import (
    clean_steps,
    learn_topology_from_history,
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
    assert _lifecycle(view, "feed", "branch_hrv") == "confirmed"
    assert _lifecycle(view, "feed", "branch_heater") == "confirmed"
    confirmed = {
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in view.edges
        if e.lifecycle == EdgeLifecycleV3.CONFIRMED
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
        lifecycle=EdgeLifecycleV3.REJECTED, declared_by="user",
        evidence=EdgeEvidence(), valid_from=T0, pinned=False,
    )
    live = TopologyInferenceView(edges=(rejected,), clock=lambda: T0, at=T0)
    learned, _ = learn_topology_from_history(live, _house())
    merged = live.merge_evidence(learned)
    assert _lifecycle(merged, "feed", "branch_hrv") == "rejected"
    assert merged.edge("feed", "branch_hrv").evidence.support == 0
