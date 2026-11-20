"""Topology linking v2 review fixes (design §5 T5, ADR-0033).

Regression tests for the W2 review and the live validation of c89dbb9:

* structural confirmation is sticky (restart, window churn, hysteresis);
* edge health is applied before the structure (a BROKEN edge never wins);
* duplicates vs 240 V pairs get ONE class per reader pair;
* no structural confirmation closes a cycle or skips PROVISIONAL;
* the capacity cache is shared across folds;
* a malformed health entry is skipped on its own;
* the history replay's pass 2 never reads a parent it could not measure;
* paired-leg completion and crossed sub-panel confirmation.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import EdgeEvidence, RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.electrical import topology_capacity as tc
from wattmosaic_engine.electrical import topology_resolve as tr
from wattmosaic_engine.electrical.topology_health import HealthBook
from wattmosaic_engine.electrical.topology_history import learn_topology_from_history
from wattmosaic_engine.electrical.topology_inference import (
    CONFLICT_RATIO_CONFLICTED,
    CONFLICT_RATIO_PROVISIONAL,
    MIN_SUPPORT_PROVISIONAL,
    DeltaObservation,
    TopologyInferenceView,
)
from wattmosaic_engine.electrical.topology_regression import RegressionFit

T0 = datetime(2026, 11, 20, tzinfo=UTC)
STRUCTURAL = TopologyEdgeState.STRUCTURAL
PROVISIONAL = TopologyEdgeState.PROVISIONAL
CANDIDATE = TopologyEdgeState.CANDIDATE


def _edge(parent: str, child: str, lifecycle: TopologyEdgeState, *, support: int = 20,
          conflict: int = 0, declared_by: str = "inference") -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by=declared_by,
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, conflict=conflict, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )


def _causes(edges: list[TopologyEdgeV3]) -> dict[tuple[str, str, str], frozenset[str]]:
    return {
        (e.source_endpoint_id, e.target_endpoint_id, "contains"): frozenset({"c1", "c2", "c3"})
        for e in edges
    }


def _view(edges: list[TopologyEdgeV3], **kw) -> TopologyInferenceView:
    return TopologyInferenceView(edges=tuple(edges), causes_by_edge=_causes(edges), at=T0, **kw)


def _life(view_or_edges, parent: str, child: str) -> str:
    edges = view_or_edges.edges if hasattr(view_or_edges, "edges") else view_or_edges
    for edge in edges:
        if edge.source_endpoint_id == parent and edge.target_endpoint_id == child:
            return str(getattr(edge.lifecycle, "value", edge.lifecycle))
    return "absent"


def _lifecycles(view: TopologyInferenceView) -> dict[str, str]:
    return {e.edge_id: str(getattr(e.lifecycle, "value", e.lifecycle)) for e in view.edges}


def _steps(rng: random.Random, n: int, levels: list[float], p_change: float) -> list[float]:
    value = rng.choice(levels)
    out = []
    for _ in range(n):
        if rng.random() < p_change:
            value = rng.choice(levels)
        out.append(value)
    return out


def _noise(rng: random.Random, series: list[float], sigma: float = 5.0) -> list[float]:
    return [v + rng.gauss(0.0, sigma) for v in series]


def _crossed(n: int = 1500, seed: int = 3) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    sub_1 = _steps(rng, n, [100.0, 600.0, 1200.0, 2000.0], 0.06)
    sub_2 = _steps(rng, n, [80.0, 400.0, 900.0, 1500.0], 0.06)
    base_1 = _steps(rng, n, [400.0, 700.0, 1100.0, 1700.0], 0.08)
    base_2 = _steps(rng, n, [500.0, 900.0, 1300.0, 2000.0], 0.08)
    return {
        "main_1": _noise(rng, [b + s for b, s in zip(base_1, sub_2, strict=True)], 20.0),
        "main_2": _noise(rng, [b + s for b, s in zip(base_2, sub_1, strict=True)], 20.0),
        "sub_1": _noise(rng, sub_1),
        "sub_2": _noise(rng, sub_2),
    }


class _Versions:
    """Counts proposals; ``current`` is unknown so every structural change proposes."""

    def __init__(self) -> None:
        self.proposals: list[str] = []

    def current(self):
        raise RuntimeError("unknown")

    def propose_topology(self, edges, change_event_id):
        self.proposals.append("")
        return f"topo-{len(self.proposals)}"

    def promote_topology(self, version_id, reason):
        self.proposals[-1] = reason


def _quiet(view: TopologyInferenceView, name: str, **kw) -> TopologyInferenceView:
    """Fold a group no edge cares about: only the settle runs."""
    return view.observe_group(
        DeltaObservation(group_id=name, deltas={"elsewhere": (50.0, 2.0)}, at=T0, cause_id=name),
        **kw,
    )


def _structurally_confirmed() -> tuple[TopologyInferenceView, _Versions, dict[str, list[float | None]]]:
    series = _crossed()
    versions = _Versions()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
        _edge("main_1", "sub_2", PROVISIONAL, support=5),
        _edge("main_2", "sub_2", PROVISIONAL, support=5),
    ]
    view = _quiet(_view(edges, versions=versions, require_direction=True), "g0", aligned=series)
    assert _life(view, "main_2", "sub_1") == "structural"
    assert _life(view, "main_1", "sub_2") == "structural"
    return view, versions, series


# --------------------------------------------------------------------------- #
# Finding 1: sticky structural confirmation.
# --------------------------------------------------------------------------- #


def test_structural_confirmation_survives_missing_aligned_series() -> None:
    """First ~500 s after a restart (or a short window): no flips, no proposals."""
    view, versions, _series = _structurally_confirmed()
    proposals = len(versions.proposals)
    before = _lifecycles(view)
    for i in range(3):
        view = _quiet(view, f"empty-{i}", aligned={})
        assert _lifecycles(view) == before
    short = {name: [100.0] * 20 for name in ("main_1", "main_2", "sub_1", "sub_2")}
    view = _quiet(view, "short", aligned=short)
    assert _lifecycles(view) == before
    assert len(versions.proposals) == proposals


def test_structural_confirmation_survives_a_restart() -> None:
    view, _versions, _series = _structurally_confirmed()
    versions = _Versions()
    # The runtime restores the edges, their causes and the persisted decisions.
    restarted = TopologyInferenceView(
        edges=view.edges, causes_by_edge=view.causes_by_edge, at=T0, versions=versions,
        require_direction=True, structure_decisions=dict(view.structure_decisions),
    )
    assert restarted.structure_decisions == {"sub_1": "main_2", "sub_2": "main_1"}
    before = _lifecycles(restarted)
    for i in range(3):
        restarted = _quiet(restarted, f"r{i}")
    assert _lifecycles(restarted) == before
    assert versions.proposals == []


def test_window_churn_without_adequate_data_keeps_the_decision() -> None:
    """A window where the children never step cannot fit them: no evidence either way."""
    view, versions, series = _structurally_confirmed()
    proposals = len(versions.proposals)
    before = _lifecycles(view)
    rng = random.Random(9)
    quiet_window = {
        "main_1": _noise(rng, _steps(rng, 300, [900.0, 1400.0, 2000.0], 0.1), 20.0),
        "main_2": _noise(rng, _steps(rng, 300, [1000.0, 1500.0, 2200.0], 0.1), 20.0),
        "sub_1": _noise(rng, [600.0] * 300),
        "sub_2": _noise(rng, [400.0] * 300),
    }
    for i, window in enumerate((quiet_window, series, quiet_window)):
        view = _quiet(view, f"w{i}", aligned=window)
        assert _lifecycles(view) == before
    assert len(versions.proposals) == proposals


def _fitted(series, fits: dict[tuple[str, str], RegressionFit]) -> tr.StructureAnalysis:
    analysis = tr.analyse(series)
    analysis.sfits.update(fits)
    return analysis


def test_unconfirm_hysteresis_band() -> None:
    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", STRUCTURAL, support=5),
    ]
    sticky = {("main_2", "sub_1")}
    decided = {"sub_1": "main_2"}

    def run(own: float, rival: float) -> tr.StructureResolution:
        analysis = _fitted(series, {
            ("main_2", "sub_1"): RegressionFit(slope=0.8, r=own, n=100),
            ("main_1", "sub_1"): RegressionFit(slope=0.8, r=rival, n=100),
        })
        return tr.resolve(edges, series, analysis=analysis, decided=decided, sticky=sticky)

    # r 0.25 (below the 0.3 confirm bar) with a 0.05 margin: kept.
    kept = run(0.25, 0.20)
    assert _life(kept.edges, "main_2", "sub_1") == "structural"
    assert kept.transitions == [] and kept.decided == decided
    # r below 0.2: un-confirmed.
    weak = run(0.15, 0.05)
    assert _life(weak.edges, "main_2", "sub_1") == "provisional"
    # a rival fits better (margin < 0): un-confirmed.
    beaten = run(0.30, 0.35)
    assert _life(beaten.edges, "main_2", "sub_1") == "provisional"


def test_conflict_above_the_provisional_bound_drops_a_sticky_edge() -> None:
    view, _versions, _series = _structurally_confirmed()
    edges = [
        e.model_copy(update={"evidence": EdgeEvidence(support=5, conflict=2, independent_causes=3)})
        if e.edge_id == "te-inf::main_2::sub_1" else e
        for e in view.edges
    ]
    view = _quiet(TopologyInferenceView(
        edges=tuple(edges), causes_by_edge=view.causes_by_edge, at=T0,
        structure_decisions=dict(view.structure_decisions),
    ), "conflicted", aligned={})
    assert _life(view, "main_2", "sub_1") != "structural"
    assert "sub_1" not in view.structure_decisions


# --------------------------------------------------------------------------- #
# Finding 4: health before structure.
# --------------------------------------------------------------------------- #


def test_a_broken_edge_cannot_win_and_demote_its_rival() -> None:
    series = _crossed()
    # Headroom on main_1 so capacity alone (KCL) does not already rule it out
    # as sub_1's parent: the rivalry is decided by health, not capacity.
    series["main_1"] = [v + 2500.0 for v in series["main_1"]]
    edges = [
        _edge("main_1", "sub_1", STRUCTURAL, support=30),
        _edge("main_2", "sub_1", STRUCTURAL, support=30),
    ]
    book = HealthBook()
    for i in range(6):
        book = book.record("main_2", "sub_1", "conflict", T0 + timedelta(seconds=i))
    view = _quiet(_view(edges, health=book), "g", aligned=series)
    assert _life(view, "main_2", "sub_1") == "provisional"
    assert _life(view, "main_1", "sub_1") == "structural"


# --------------------------------------------------------------------------- #
# Findings 3 + 5: duplicates vs 240 V pairs.
# --------------------------------------------------------------------------- #


def _hot_tub(n: int = 3000, seed: int = 7) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    tub = _steps(rng, n, [0.0, 0.0, 0.0, 0.0, 0.0, 1500.0], 0.04)
    base1 = _steps(rng, n, [150.0, 300.0, 500.0, 900.0], 0.05)
    base2 = _steps(rng, n, [700.0, 1100.0, 1600.0, 2400.0], 0.05)
    return {
        "leg_a": _noise(rng, tub, 3.0),
        "leg_b": _noise(rng, tub, 3.0),
        "main_1": _noise(rng, [b + t for b, t in zip(base1, tub, strict=True)]),
        "main_2": _noise(rng, [b + t for b, t in zip(base2, tub, strict=True)]),
    }


def test_tub_legs_under_both_mains_are_a_pair_not_a_duplicate() -> None:
    series = _hot_tub()
    analysis = tr.analyse(series)
    # Balanced legs carry the same current: the raw detectors see both.
    assert ("leg_a", "leg_b") in {(a, b) for a, b, _r, _o in analysis.duplicates}
    assert analysis.symmetric_twins("leg_a", "leg_b")
    edges = [
        _edge("main_1", "leg_a", STRUCTURAL),
        _edge("main_1", "leg_b", STRUCTURAL),
        _edge("main_2", "leg_a", PROVISIONAL, support=6),
        _edge("main_2", "leg_b", PROVISIONAL, support=6),
        # Equal deltas also give the legs (soft) edges both ways: no direction.
        _edge("leg_a", "leg_b", PROVISIONAL, support=6),
        _edge("leg_b", "leg_a", PROVISIONAL, support=6),
    ]
    links = analysis.classify(edges)
    assert links.pairs == (("leg_a", "leg_b"),)
    assert links.duplicates == ()
    resolved, _transitions = tr.resolve_structure(edges, series, analysis=analysis)
    assert sorted(_life(resolved, "main_1", leg) for leg in ("leg_a", "leg_b")) == [
        "provisional", "structural"
    ]


def _feeder(n: int = 1500, seed: int = 13) -> dict[str, list[float | None]]:
    """A sub-panel feed under main_2, metered twice; the feed has its own load."""
    rng = random.Random(seed)
    load = _steps(rng, n, [0.0, 300.0, 700.0], 0.05)
    other = _steps(rng, n, [300.0, 600.0, 1000.0], 0.05)
    feed = [o + v for o, v in zip(other, load, strict=True)]
    base_1 = _steps(rng, n, [2500.0, 3500.0, 4500.0], 0.06)
    base_2 = _steps(rng, n, [400.0, 800.0, 1200.0], 0.06)
    return {
        "feed": _noise(rng, feed),
        "feed_meter": _noise(rng, [v + 40.0 for v in feed]),
        "load": _noise(rng, load),
        "main_1": _noise(rng, base_1, 20.0),
        "main_2": _noise(rng, [b + f for b, f in zip(base_2, feed, strict=True)], 20.0),
    }


def test_a_second_meter_on_a_feed_is_a_duplicate_never_split_like_a_leg_pair() -> None:
    series = _feeder()
    analysis = tr.analyse(series)
    edges = [
        _edge("main_2", "feed", STRUCTURAL),
        _edge("main_2", "feed_meter", STRUCTURAL),
        _edge("main_1", "feed", PROVISIONAL, support=6),
        _edge("main_1", "feed_meter", PROVISIONAL, support=6),
        _edge("feed", "load", STRUCTURAL),
        _edge("feed", "feed_meter", STRUCTURAL),
        _edge("feed_meter", "feed", PROVISIONAL, support=6),
    ]
    links = analysis.classify(edges)
    assert links.duplicates == (("feed", "feed_meter"),)
    assert links.pairs == ()
    resolved, _transitions = tr.resolve_structure(edges, series, analysis=analysis)
    # Not split across the mains like a 240 V pair: both stay under main_2.
    assert _life(resolved, "main_2", "feed") == "structural"
    assert _life(resolved, "main_2", "feed_meter") == "structural"
    # One wire measured twice is never parent and child.
    assert _life(resolved, "feed", "feed_meter") != "structural"
    assert _life(resolved, "feed_meter", "feed") != "structural"


def test_a_circuit_with_one_dominant_child_stays_its_parent() -> None:
    rng = random.Random(21)
    n = 1500
    child = _steps(rng, n, [0.0, 800.0, 1600.0, 2400.0], 0.03)
    small = _steps(rng, n, [0.0, 60.0, 120.0], 0.12)
    series = {
        "circuit": _noise(rng, [c + s for c, s in zip(child, small, strict=True)]),
        "appliance": _noise(rng, child),
    }
    analysis = tr.analyse(series)
    # Raw values alone call them one wire ...
    assert ("appliance", "circuit") in {(a, b) for a, b, _r, _o in analysis.duplicates}
    # ... but the circuit steps on its own too: the reverse fit differs.
    assert not analysis.symmetric_twins("appliance", "circuit")
    edges = [_edge("circuit", "appliance", STRUCTURAL)]
    resolved, transitions = tr.resolve_structure(edges, series, analysis=analysis)
    assert _life(resolved, "circuit", "appliance") == "structural", transitions
    assert analysis.classify(edges).duplicates == ()
    assert analysis.classify(edges).pairs == ()


# --------------------------------------------------------------------------- #
# Findings 6 + 7: acyclicity and no CANDIDATE -> STRUCTURAL in one settle.
# --------------------------------------------------------------------------- #


def test_no_structural_confirmation_against_a_user_declared_reverse_edge() -> None:
    series = _crossed()
    edges = [
        _edge("sub_1", "main_2", STRUCTURAL, declared_by="user"),
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
    ]
    resolved, _transitions = tr.resolve_structure(edges, series)
    assert _life(resolved, "main_2", "sub_1") != "structural"
    assert _life(resolved, "sub_1", "main_2") == "structural"


def test_no_structural_confirmation_against_the_direction_cap() -> None:
    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
        # The reverse is just as supported and never contradicted.
        _edge("sub_1", "main_2", PROVISIONAL, support=5),
    ]
    view = _quiet(_view(edges, require_direction=True), "g", aligned=series)
    assert _life(view, "main_2", "sub_1") != "structural"


def test_a_candidate_is_never_confirmed_structurally_in_one_settle() -> None:
    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", CANDIDATE, support=3),
        _edge("main_2", "sub_1", CANDIDATE, support=3),
    ]
    view = _quiet(_view(edges), "g0", aligned=series)
    assert _life(view, "main_2", "sub_1") == "provisional"
    view = _quiet(view, "g1")
    assert _life(view, "main_2", "sub_1") == "structural"


# --------------------------------------------------------------------------- #
# Finding 8: the capacity cache outlives one assignment.
# --------------------------------------------------------------------------- #


def test_repeated_resolves_never_recompute_capacity(monkeypatch) -> None:
    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
        _edge("main_1", "sub_2", PROVISIONAL, support=5),
        _edge("main_2", "sub_2", PROVISIONAL, support=5),
    ]
    analysis = tr.analyse(series)
    tr.resolve_structure(edges, series, analysis=analysis)
    calls = []
    real = tc.capacity_violation

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(tc, "capacity_violation", counting)
    monkeypatch.setattr(tr, "capacity_violation", counting)
    tr.resolve_structure(edges, series, analysis=analysis)
    assert calls == []


# --------------------------------------------------------------------------- #
# Finding 9: a malformed health entry is skipped on its own.
# --------------------------------------------------------------------------- #


def test_a_malformed_health_entry_does_not_drop_the_book() -> None:
    good = HealthBook().record("feed", "load", "support", T0).record("feed", "load", "conflict", T0)
    payload = good.to_json()[:-1] + (
        ',"bad\\u0000tokens":{"recent":["s","x"]},"no-separator":{"recent":["s"]},'
        '"x\\u0000y":{"recent":"sc"}}'
    )
    book = HealthBook.from_json(payload)
    assert set(book.entries) == {("feed", "load")}
    assert book.get("feed", "load").recent == ("s", "c")


# --------------------------------------------------------------------------- #
# Finding A: replay pass 2 never reads a parent it could not measure.
# --------------------------------------------------------------------------- #


def test_replay_pass_two_skips_parents_absent_from_a_group() -> None:
    """``parent in changes`` must guard the ancestor test too (KeyError live)."""
    rng = random.Random(4)
    cadence = 15.0
    hour = 3600.0
    history: dict[str, list[tuple[datetime, float]]] = {"grand": [], "branch": [], "other": []}
    t = 0.0
    other, branch = 0.0, 0.0
    while t < 4 * hour:
        if rng.random() < 0.02:
            other = 400.0 - other
        if t >= hour and rng.random() < 0.05:
            branch = 250.0 - branch
        at = T0 + timedelta(seconds=t)
        history["grand"].append((at, 500.0 + other + branch + rng.uniform(-1, 1)))
        history["other"].append((at, other + rng.uniform(-1, 1)))
        if t >= hour:  # the branch reader only starts reporting after an hour
            history["branch"].append((at, branch + rng.uniform(-1, 1)))
        t += cadence
    seed = TopologyInferenceView(clock=lambda: T0, at=T0, require_direction=True)
    learned, stats = learn_topology_from_history(seed, history)
    assert stats.passes == 2
    view = seed.merge_evidence(learned, levels={"grand": 900.0, "other": 200.0, "branch": 120.0})
    assert _life(view, "grand", "branch") == "structural"


# --------------------------------------------------------------------------- #
# Finding B: 240 V pair completion.
# --------------------------------------------------------------------------- #


def test_the_second_leg_of_a_pair_is_confirmed_under_the_sibling_main() -> None:
    """Live: CT1 STRUCTURAL under L1, CT2 left PROVISIONAL under both mains."""
    assert tr.PAIR_MAX_CONFLICT_RATIO == CONFLICT_RATIO_PROVISIONAL
    series = _hot_tub()
    edges = [
        _edge("main_1", "leg_a", STRUCTURAL, support=300, conflict=4),
        _edge("main_1", "leg_b", PROVISIONAL, support=289, conflict=6),
        _edge("main_2", "leg_a", PROVISIONAL, support=300, conflict=25),
        _edge("main_2", "leg_b", PROVISIONAL, support=312, conflict=25),
    ]
    view = _quiet(_view(edges, require_direction=True), "g0", aligned=series)
    assert _life(view, "main_1", "leg_a") == "structural"
    assert _life(view, "main_2", "leg_b") == "structural"
    assert _life(view, "main_1", "leg_b") == "provisional"
    assert _life(view, "main_2", "leg_a") == "provisional"
    settled = _lifecycles(view)
    for i in range(3):  # sticky: no churn on later settles, with or without series
        view = _quiet(view, f"g{i + 1}", aligned=series if i % 2 else {})
        assert _lifecycles(view) == settled


# --------------------------------------------------------------------------- #
# Finding C: crossed sub-panels under unsynchronised sampling.
# --------------------------------------------------------------------------- #


def _switching(rng: random.Random, hours: float, watts: list[float], on_s: float,
               off_s: float) -> list[tuple[float, float]]:
    t, on, points = 0.0, False, [(0.0, 0.0)]
    while t < hours * 3600:
        t += rng.expovariate(1.0 / (on_s if on else off_s))
        on = not on
        points.append((t, rng.choice(watts) if on else 0.0))
    return points


def _level_at(loads: list[list[tuple[float, float]]], cursor: list[int], t: float) -> float:
    total = 0.0
    for k, points in enumerate(loads):
        while cursor[k] + 1 < len(points) and points[cursor[k] + 1][0] <= t:
            cursor[k] += 1
        total += points[cursor[k]][1]
    return total


def _crossed_unsynchronised(hours: float = 24.0, seed: int = 1) -> dict[str, list[float | None]]:
    """Live-like: busy noisy mains (sigma 12/27 W), ~100 W sub steps, 16 s sampling
    at a random phase per reader, held onto the 10 s grid (recorder semantics)."""
    rng = random.Random(seed)
    small = [60.0, 90.0, 105.0, 120.0, 150.0, 250.0]
    sub_1 = [_switching(rng, hours, small, 300, 900) for _ in range(4)]
    sub_2 = [_switching(rng, hours, small, 300, 900) for _ in range(4)]
    base_1 = [_switching(rng, hours, [40.0, 80.0, 150.0, 300.0], 240, 1500) for _ in range(4)]
    base_2 = [_switching(rng, hours, [40.0, 80.0, 150.0, 300.0], 240, 1500) for _ in range(4)]
    readers = {
        "main_l1": (300.0, base_1 + sub_2, 12.0),
        "main_l2": (350.0, base_2 + sub_1, 27.0),
        "sub_l1": (20.0, sub_1, 3.0),
        "sub_l2": (25.0, sub_2, 3.0),
    }
    n = int(hours * 360)
    out: dict[str, list[float | None]] = {}
    for name, (base, loads, sigma) in readers.items():
        cursor = [0] * len(loads)
        samples, t = [], rng.uniform(0.0, 16.0)
        while t < hours * 3600:
            samples.append((t, base + _level_at(loads, cursor, t) + rng.gauss(0.0, sigma)))
            t += 16.0
        values: list[float | None] = []
        j, last = 0, None
        for i in range(n):
            while j < len(samples) and samples[j][0] <= i * 10.0:
                last = samples[j][1]
                j += 1
            values.append(last)
        out[name] = values
    return out


def test_crossed_sub_panels_confirm_under_unsynchronised_sampling() -> None:
    series = _crossed_unsynchronised()
    analysis = tr.analyse(series)
    # The exact M4 lag-1 fit is deflated by the sampling skew; the
    # span-differenced structural fit is not.
    for parent, child in (("main_l2", "sub_l1"), ("main_l1", "sub_l2")):
        assert analysis.sfit(parent, child).slope > analysis.fit(parent, child).slope + 0.1
    edges = [
        _edge("main_l2", "sub_l1", PROVISIONAL, support=2089, conflict=124),
        _edge("main_l1", "sub_l1", PROVISIONAL, support=1500, conflict=110),
        _edge("main_l1", "sub_l2", PROVISIONAL, support=898, conflict=71),
        _edge("main_l2", "sub_l2", PROVISIONAL, support=800, conflict=60),
    ]
    resolved, transitions = tr.resolve_structure(edges, series, analysis=analysis)
    assert _life(resolved, "main_l2", "sub_l1") == "structural", transitions
    assert _life(resolved, "main_l1", "sub_l2") == "structural", transitions
    assert _life(resolved, "main_l1", "sub_l1") == "provisional"
    assert _life(resolved, "main_l2", "sub_l2") == "provisional"


# --------------------------------------------------------------------------- #
# Finding F: a replay does not manufacture CONFLICTED noise rows.
# --------------------------------------------------------------------------- #


def _branch_house(seed: int = 3, hours: float = 6.0, branches: int = 12):
    rng = random.Random(seed)

    def toggle(high: float, mean_s: float) -> list[tuple[float, float]]:
        t, on, out = 0.0, False, [(0.0, 0.0)]
        while t < hours * 3600:
            t += rng.uniform(0.5, 1.5) * mean_s
            on = not on
            out.append((t, high if on else 0.0))
        return out

    loads = {
        f"b{i}": toggle(rng.choice([150.0, 300.0, 600.0, 900.0]), rng.uniform(400, 1500))
        for i in range(branches)
    }
    cursors = dict.fromkeys(loads, 0)

    def at(name: str, t: float) -> float:
        points = loads[name]
        while cursors[name] + 1 < len(points) and points[cursors[name] + 1][0] <= t:
            cursors[name] += 1
        return points[cursors[name]][1]

    history: dict[str, list[tuple[datetime, float]]] = {name: [] for name in loads}
    history["main_a"], history["main_b"] = [], []
    t = 0.0
    while t < hours * 3600:
        stamp = T0 + timedelta(seconds=t)
        totals = [0.0, 0.0]
        for i, name in enumerate(loads):
            value = at(name, t)
            history[name].append((stamp, value + rng.uniform(-1, 1)))
            totals[i % 2] += value
        history["main_a"].append((stamp + timedelta(seconds=7), 300 + totals[1] + rng.uniform(-3, 3)))
        history["main_b"].append((stamp + timedelta(seconds=7), 400 + totals[0] + rng.uniform(-3, 3)))
        t += 18.0
    return history


def test_a_replay_merge_keeps_no_coincidence_level_noise_rows() -> None:
    seed = TopologyInferenceView(clock=lambda: T0, at=T0, require_direction=True)
    learned, _stats = learn_topology_from_history(seed, _branch_house())
    merged = seed.merge_evidence(learned)
    noise = [
        e.edge_id for e in merged.edges
        if e.evidence.support < MIN_SUPPORT_PROVISIONAL
        and e.evidence.conflict_ratio > CONFLICT_RATIO_CONFLICTED
    ]
    assert noise == []
    confirmed = {
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in merged.edges if _life(merged, e.source_endpoint_id, e.target_endpoint_id) == "structural"
    }
    # The real wiring is still learned: every branch under its main.
    expected = {("main_a" if i % 2 else "main_b", f"b{i}") for i in range(12)}
    assert confirmed <= expected and len(confirmed) >= 6


def test_an_existing_low_support_edge_still_takes_the_replay_record() -> None:
    live = _view([_edge("x", "y", CANDIDATE, support=1, conflict=0)])
    learned = _view([_edge("x", "y", CANDIDATE, support=1, conflict=9)])
    merged = live.merge_evidence(learned)
    assert merged.edge("x", "y").evidence.conflict == 9
