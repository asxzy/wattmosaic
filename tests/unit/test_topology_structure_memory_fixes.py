"""Structural pass on a week of evidence: live-replay root causes (design §5 T5).

An offline replay of a week of recorder history showed why the structural pass
(:mod:`~wattmosaic_engine.electrical.topology_resolve`) never fixed known
errors.  Each test reproduces one root cause with synthetic data shaped like
the real facts:

* a 240 V pair whose legs were both STRUCTURAL (and sticky) under one main was
  never split, although the main cannot carry both legs while they run;
* a confirmed grandparent kept a sub-panel's circuits, so the grandparent's
  capacity counted them twice and refused the sub-panel's own confirmation;
* a constant load below the 300 W capacity bar sat under a parent it outdraws
  (KCL), poisoning that parent's capacity for its real circuits;
* a sparsely sampled second meter on a main was not recognised as a duplicate
  and became a parent;
* structural confirmations did not build on each other within one settle;
* the live runtime saw 6 h and Discover 24 h: the structural memory keeps a
  week, compactly, across restarts (``StructureMemory``).
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.electrical import topology_resolve as tr
from wattmosaic_engine.electrical.topology_memory import StructureMemory
from wattmosaic_engine.electrical.topology_resolve import AlignedSeries
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

from tests.unit.test_topology_w2_fixes import (
    PROVISIONAL,
    STRUCTURAL,
    _edge,
    _hot_tub,
    _life,
    _noise,
    _quiet,
    _steps,
    _view,
)

T0 = datetime(2026, 11, 20, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 240 V pair held under one main.
# --------------------------------------------------------------------------- #


def test_a_pair_held_under_one_main_is_split_when_the_main_cannot_carry_both() -> None:
    series = _hot_tub()
    # Both legs STRUCTURAL under main_1 and decided there by earlier settles
    # (live: support 288/289, both sticky).  main_1 cannot carry 2 x 1500 W.
    assert not tr.analyse(series).fits_capacity("main_1", ["leg_a", "leg_b"])
    edges = [
        _edge("main_1", "leg_a", STRUCTURAL, support=40),
        _edge("main_1", "leg_b", STRUCTURAL, support=40),
        _edge("main_2", "leg_a", PROVISIONAL, support=6),
        _edge("main_2", "leg_b", PROVISIONAL, support=6),
        # The legs step together: an edge each way, a few unpaired steps apart
        # (live 391/1 vs 384/0) -- no direction, so neither leg has a child.
        _edge("leg_a", "leg_b", PROVISIONAL, support=391, conflict=1),
        _edge("leg_b", "leg_a", PROVISIONAL, support=384),
    ]
    decided = {"leg_a": "main_1", "leg_b": "main_1"}
    view = _quiet(
        _view(edges, require_direction=True, structure_decisions=decided), "g", aligned=series
    )
    parents = {
        leg: [p for p in ("main_1", "main_2") if _life(view, p, leg) == "structural"]
        for leg in ("leg_a", "leg_b")
    }
    assert sorted(sum(parents.values(), [])) == ["main_1", "main_2"], parents
    assert sorted(view.structure_decisions[leg] for leg in ("leg_a", "leg_b")) == [
        "main_1", "main_2"
    ]


# --------------------------------------------------------------------------- #
# Confirmed grandparent vs the provisional sub-panel.
# --------------------------------------------------------------------------- #


def _sub_panel(n: int = 3000, seed: int = 31) -> dict[str, list[float | None]]:
    """main_2 feeds sub_1; sub_1 feeds ``circuit`` (big) and its own busy load."""
    rng = random.Random(seed)
    circuit = _steps(rng, n, [0.0, 800.0, 1600.0], 0.03)
    own = _steps(rng, n, [100.0, 300.0, 600.0], 0.08)
    # The main is busy with loads of its own (live: sigma 12-27 W, many steps).
    base_2 = _steps(rng, n, [50.0, 250.0, 500.0, 800.0, 1100.0, 1500.0], 0.35)
    sub_1 = [c + o for c, o in zip(circuit, own, strict=True)]
    return {
        "circuit": _noise(rng, circuit),
        "sub_1": _noise(rng, sub_1),
        "main_2": _noise(rng, [b + s for b, s in zip(base_2, sub_1, strict=True)], 20.0),
    }


def test_a_sub_panel_is_the_direct_parent_over_a_confirmed_grandparent() -> None:
    series = _sub_panel()
    info = tr.analyse(series)
    # Kept under main_2 directly, the circuit is counted twice in main_2.
    assert not info.fits_capacity("main_2", ["sub_1", "circuit"])
    edges = [
        # The grandparent sees every circuit step: STRUCTURAL on counts alone.
        _edge("main_2", "circuit", STRUCTURAL, support=40),
        _edge("sub_1", "circuit", PROVISIONAL, support=6),
        _edge("main_2", "sub_1", PROVISIONAL, support=6),
    ]
    view = _quiet(_view(edges, require_direction=True), "g", aligned=series)
    assert _life(view, "sub_1", "circuit") == "structural"
    assert _life(view, "main_2", "circuit") == "provisional"
    assert _life(view, "main_2", "sub_1") == "structural"
    assert view.structure_decisions["circuit"] == "sub_1"


# --------------------------------------------------------------------------- #
# KCL for one child below the 300 W bar.
# --------------------------------------------------------------------------- #


def _rack_house(n: int = 3000, seed: int = 41) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    circuit = _steps(rng, n, [0.0, 150.0, 300.0], 0.04)
    base = _steps(rng, n, [130.0, 180.0, 230.0, 280.0], 0.15)
    return {
        "circuit": _noise(rng, circuit, 2.0),
        "leg": _noise(rng, [b + c for b, c in zip(base, circuit, strict=True)], 2.0),
        # A constant ~230 W load that is NOT on ``leg`` (it outdraws it).
        "rack": _noise(rng, [234.0] * n, 3.0),
    }


def test_a_load_that_outdraws_its_candidate_parent_alone_is_never_assigned_to_it() -> None:
    series = _rack_house()
    info = tr.analyse(series)
    assert info.fits_capacity("leg", ["rack"])  # the 300 W bar never sees it ...
    assert not info.fits_alone("leg", "rack")  # ... KCL at 50 W does
    edges = [
        _edge("leg", "rack", PROVISIONAL, support=4),
        _edge("leg", "circuit", PROVISIONAL, support=6),
    ]
    view = _quiet(_view(edges, require_direction=True), "g", aligned=series)
    # The rack no longer poisons the leg's capacity: the circuit confirms.
    assert _life(view, "leg", "circuit") == "structural"
    assert view.structure_decisions.get("rack") != "leg"


# --------------------------------------------------------------------------- #
# A sparsely sampled second meter on a main.
# --------------------------------------------------------------------------- #


def _sparse_meter(n: int = 6000, seed: int = 51) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    load = _steps(rng, n, [0.0, 400.0, 900.0], 0.004)
    base = _steps(rng, n, [500.0, 2500.0, 5000.0, 7500.0], 0.002)
    # Small loads step often: the main has many more steps than the meter sees.
    small = _steps(rng, n, [0.0, 60.0, 120.0], 0.12)
    main = _noise(rng, [b + v + m for b, v, m in zip(base, load, small, strict=True)], 10.0)
    # The second meter reads +60 W and reports every 120 s (12 grid steps).
    meter: list[float | None] = []
    held = main[0] + 60.0
    for i, value in enumerate(main):
        if i % 12 == 0:
            held = value + 60.0
        meter.append(held)
    return {"load": _noise(rng, load, 3.0), "main": main, "meter": meter}


def test_a_sparsely_sampled_second_meter_is_a_duplicate_never_a_parent() -> None:
    series = _sparse_meter()
    info = tr.analyse(series)
    assert ("main", "meter") in {(a, b) for a, b, _r, _o in info.duplicates}
    # The 30 s symmetric-twin test cannot see it (the meter has mostly not
    # reported yet) ...
    assert not info.symmetric_twins("main", "meter")
    edges = [
        _edge("main", "load", STRUCTURAL, support=30),
        # Coincidences with the sparse meter's lumped steps.
        _edge("meter", "load", STRUCTURAL, support=60),
    ]
    # ... the 5 min one does, and the ledger gives the two no direction.
    assert info.classify(edges).duplicates == (("main", "meter"),)
    resolved, _transitions = tr.resolve_structure(edges, series, analysis=info)
    assert _life(resolved, "main", "load") == "structural"
    assert _life(resolved, "meter", "load") == "provisional"


# --------------------------------------------------------------------------- #
# Structural confirmations build on each other within one settle.
# --------------------------------------------------------------------------- #


def _screen_house() -> dict[str, list[float | None]]:
    rng = random.Random(61)
    n = 3000
    # A small load (no regression fit: its steps are under 50 W), so only the
    # ledger's counts and the nesting can place it.
    screen = _steps(rng, n, [0.0, 40.0], 0.03)
    partner = [40.0 - s for s in screen]
    big = _steps(rng, n, [0.0, 800.0, 1600.0], 0.03)
    own = _steps(rng, n, [100.0, 300.0, 600.0], 0.08)
    base_2 = _steps(rng, n, [50.0, 250.0, 500.0, 800.0, 1100.0, 1500.0], 0.5)
    sub_1 = [b + o + s + p for b, o, s, p in zip(big, own, screen, partner, strict=True)]
    return {
        "big": _noise(rng, big),
        "screen": _noise(rng, screen, 2.0),
        "sub_1": _noise(rng, sub_1),
        "main_2": _noise(rng, [b + s for b, s in zip(base_2, sub_1, strict=True)], 30.0),
    }


def test_a_settle_resolves_a_grandparent_found_in_the_same_settle() -> None:
    series = _screen_house()
    edges = [
        _edge("main_2", "sub_1", PROVISIONAL, support=6),
        _edge("sub_1", "big", PROVISIONAL, support=6),
        _edge("main_2", "big", STRUCTURAL, support=40),
        # Both parents of the screen are STRUCTURAL on counts; the earlier
        # settle decided the grandparent (the sub-panel was not known yet).
        _edge("main_2", "screen", STRUCTURAL, support=60),
        _edge("sub_1", "screen", STRUCTURAL, support=30),
    ]
    view = _quiet(
        _view(edges, require_direction=True, structure_decisions={"screen": "main_2"}),
        "g",
        aligned=series,
    )
    assert _life(view, "main_2", "sub_1") == "structural"
    assert _life(view, "sub_1", "screen") == "structural"
    assert _life(view, "main_2", "screen") == "provisional"
    assert view.structure_decisions["screen"] == "sub_1"


# --------------------------------------------------------------------------- #
# The structural memory.
# --------------------------------------------------------------------------- #


def test_memory_keeps_the_last_sample_per_bucket_holds_and_drops_late_samples() -> None:
    memory = StructureMemory(step_s=30.0, window_s=3600.0)
    t = 1_000_020.0  # bucket-aligned (30 s)
    memory.record("a", t, 100.0)
    memory.record("a", t + 10, 110.0)  # same bucket: the last one wins
    memory.record("a", t + 95, 200.0)  # two quiet buckets in between: held
    memory.record("a", t - 60, 999.0)  # late: never rewrites the past
    memory.record("b", t + 35, None)  # absent is absent, never 0 W
    series = memory.series(t + 125)
    assert series.step_s == 30.0
    assert list(series["a"]) == [110.0, 110.0, 110.0, 200.0, 200.0]
    assert math.isnan(series["b"][0]) and math.isnan(series["b"][1])
    assert math.isnan(series["b"][-1])


def test_memory_window_is_bounded_and_a_restart_gap_is_absent() -> None:
    memory = StructureMemory(step_s=30.0, window_s=3600.0)
    t = 3_000_000.0
    for i in range(400):  # 200 min, far over the 60 min window
        memory.record("a", t + 30 * i, float(i))
    series = memory.series(t + 30 * 399)
    assert len(series["a"]) == 121 and series["a"][-1] == 399.0
    restored = StructureMemory.from_json(memory.to_json(), step_s=30.0, window_s=3600.0)
    assert list(restored.series(t + 30 * 399)["a"]) == list(series["a"])
    # The process was down for 10 min: that gap is absent, not held.
    restored.record("a", t + 30 * 420, 500.0)
    after = restored.series(t + 30 * 420)["a"]
    assert after[-1] == 500.0 and math.isnan(after[-2]) and after[-22] == 399.0


def test_memory_is_seeded_from_a_replay_and_live_samples_win() -> None:
    memory = StructureMemory(step_s=30.0, window_s=7 * 86400.0)
    start = 4_000_020.0
    replay = AlignedSeries(
        {"a": [1.0] * 100, "b": [2.0] * 100}, step_s=30.0, start_ts=start
    )
    memory.record("a", start + 30 * 90, 7.0)  # live sample inside the replay span
    added = memory.seed(replay)
    # Replay index i is the value at start + i * 30 s: the bucket before it.
    series = memory.series(start + 30 * 98)
    assert added > 0
    assert series["a"][-1] == 7.0  # live wins where both exist
    assert series["a"][0] == 1.0 and series["b"][0] == 2.0  # the past comes from the replay
    assert len(series["a"]) == len(series["b"]) == 100


def test_structural_fits_match_on_the_memory_grid() -> None:
    # A 30 s memory grid gives the same structural answer as the 10 s grid.
    fine = _sub_panel(n=6000)
    coarse = AlignedSeries(
        {name: values[2::3] for name, values in fine.items()}, step_s=30.0
    )
    a, b = tr.analyse(fine), tr.analyse(coarse)
    assert b.span == 1 and a.span == 3
    fa, fb = a.sfit("sub_1", "circuit"), b.sfit("sub_1", "circuit")
    assert fa is not None and fb is not None
    assert abs(fa.r - fb.r) < 0.05 and abs(fa.slope - fb.slope) < 0.1


def test_the_runtime_memory_reaches_beyond_six_hours_and_survives_a_restart(
    tmp_path: Path,
) -> None:
    now = [T0]

    def core() -> EngineCore:
        db = Database(tmp_path / "memory.db")
        engine = EngineCore(
            EngineConfig(database_path=str(tmp_path / "memory.db"), ha_token="x"),
            db,
            clock=lambda: now[0],
        )
        engine._wire_v3_services()  # noqa: SLF001 - the production start path
        return engine

    first = core()
    for i in range(0, 2 * 24 * 120):  # two days, one sample per 30 s
        at = T0 + timedelta(seconds=30 * i)
        first._t5_record("feed", at, 100.0 + (i % 7) * 10.0)  # noqa: SLF001
    now[0] = T0 + timedelta(days=2)
    series = first._t5_aligned_series()  # noqa: SLF001
    assert series is not None and len(series["feed"]) >= 2 * 24 * 120 - 1  # not 6 h
    first._persist_structure_memory()  # noqa: SLF001 - also done on stop and every 30 min

    now[0] = T0 + timedelta(days=2, minutes=5)
    second = core()
    restored = second._t5_aligned_series()  # noqa: SLF001
    assert restored is not None and len(restored["feed"]) >= 2 * 24 * 120
