"""Regression gates against the **real** installation's captured topology.

These tests read ``data/wattmosaic.db`` -- the local capture of the reference
Home Assistant install -- and skip cleanly when it is absent. The database is
opened ``mode=ro`` and never written.

Why they exist
--------------
The simulator tests prove the engine behaves correctly on a house with the
*shape* of this install. They cannot prove anything about the install itself,
because the simulator's topology is a synthetic stand-in. The defects these
tests guard are invisible on the simulator and obvious on the real capture:

* The shipped topology declares **six** independent main CTs (load-manager L1/L2,
  main-panel L1/L2, sub-panel L1/L2). Physically there is one service feed, one
  main panel fed from it, and one sub panel fed from the main panel. Summing all
  six counts most of the house two to three times, so ``main_power_w`` is roughly
  2.5x the true whole-home draw.
* The nested-CT check in ``tests.simulation.metrics`` could not classify a
  nested ``main`` as a sub feed, so on a topology that declares its panel tree
  with nested mains ``nested_check_records`` was 0 and
  ``nested_double_count_records == 0`` proved nothing. That is the vacuous-test
  hole these tests close.

Defect IDs
----------
``E2-1`` is the shipped-topology double count (independent mains, no declared
panel tree). It is the defect ``test_real_replay_reports_whole_home_once``
currently xfails on. The engine-fix agent owns it; these tests are the gate that
must flip to XPASS when it is fixed.

``E2-2`` is the companion solver defect: on real data the six independent feeds
make the bounded least-squares problem ill-conditioned enough that
``DisaggregationEngine.infer`` **does not terminate** (measured >10 min on a
one-minute window). ``test_shipped_replay_terminates`` pins it. Any tool that
replays the capture must therefore impose a hard deadline; see
``tools/audit/replay_capture.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.simulation.metrics import evaluate, panel_channel_map

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The capture is git-ignored and local-only, so the path is overridable. A
#: worktree has no ``data/`` directory; point ``WATTMOSAIC_REAL_DB`` at the main
#: checkout's copy to run these gates there. Read-only in every case.
REAL_DB = Path(
    os.environ.get("WATTMOSAIC_REAL_DB", REPO_ROOT / "data" / "wattmosaic.db")
).expanduser()

pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=(
        "real capture unavailable: data/wattmosaic.db is local-only and "
        "git-ignored; run tools/audit/collect_samples.py to produce one, or set "
        "WATTMOSAIC_REAL_DB to an existing capture"
    ),
)

#: The replay window.  Short on purpose: the shipped topology does not terminate
#: on this data, so a larger window only makes the failure slower.  One tick per
#: audited 15.7 s cadence over five minutes is ~19 windows, enough for a mean.
REPLAY_MINUTES = 5
REPLAY_TICK_S = 15.7
#: Deadline for a replay that is expected to finish.  The nested variant takes
#: ~0.5 s; this is 40x headroom, not a performance assertion.
REPLAY_TIMEOUT_S = 20.0


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def shipped_topology():
    """The topology exactly as stored, read-only."""
    from tools.audit.replay_capture import load_topology

    return load_topology(REAL_DB, nested=False)


@pytest.fixture(scope="module")
def nested_topology():
    """The topology with the panel tree declared (a hypothesis under test)."""
    from tools.audit.replay_capture import load_topology

    return load_topology(REAL_DB, nested=True)


@pytest.fixture(scope="module")
def capture(shipped_topology):
    """The tail of ``ct_samples`` mapped onto the shipped topology's ct ids."""
    from tools.audit.replay_capture import aligner_key, load_capture_db

    entity_to_ct = {str(ch.entity_id): ch.ct_id for ch in shipped_topology.channels}
    entity_to_ct.update(
        {aligner_key(ch.entity_id): ch.ct_id for ch in shipped_topology.channels}
    )
    return load_capture_db(REAL_DB, minutes=REPLAY_MINUTES, entity_to_ct=entity_to_ct)


@pytest.fixture(scope="module")
def shipped_replay(capture, shipped_topology):
    """Replay with a hard deadline; ``None`` means the solver did not stop.

    The shipped topology is known not to terminate (E2-2), so the test does not
    *assume* a result: it records the timeout as the finding.  This keeps the
    suite fast and makes the failure mode visible instead of a hung CI job.
    """
    from tools.audit.replay_capture import ReplayTimeout, _Deadline, replay

    try:
        with _Deadline(REPLAY_TIMEOUT_S, hard_exit=False):
            return replay(
                capture, shipped_topology, variant="as-shipped", tick_s=REPLAY_TICK_S
            )
    except ReplayTimeout:
        return None


@pytest.fixture(scope="module")
def nested_replay(capture, nested_topology):
    from tools.audit.replay_capture import replay

    return replay(capture, nested_topology, variant="nested", tick_s=REPLAY_TICK_S)


# --------------------------------------------------------------------------- #
# The topology itself
# --------------------------------------------------------------------------- #


def test_shipped_topology_declares_nested_parents(shipped_topology, nested_topology) -> None:
    """The captured wiring must be expressible as a panel tree.

    The *shipped* topology declares no parents at all -- that is defect E2-1.
    What this test pins is that the install genuinely has a sub panel fed from a
    main panel, so the nested variant describes real wiring and does not invent
    it: the sub-panel feeds are mains with a ``parent_ct_id`` pointing at a root
    feed.
    """
    # As shipped: every main is independent.  Asserted so a "fix" that only
    # edits the local database cannot pass unnoticed.
    shipped_map = panel_channel_map(shipped_topology)
    assert shipped_map["sub"] == [], (
        "the shipped topology now declares nested feeds; update defect E2-1's "
        "status and re-pin test_real_replay_reports_whole_home_once"
    )
    assert len(shipped_map["root"]) == 6, shipped_map["root"]

    # The nested variant must be a real tree, not a relabel.
    nested_map = panel_channel_map(nested_topology)
    assert nested_map["sub"], "nested topology must classify at least one sub feed"
    assert len(nested_map["root"]) == 2, nested_map["root"]

    by_id = {ch.ct_id: ch for ch in nested_topology.channels}
    for sub_id in nested_map["sub"]:
        channel = by_id[sub_id]
        assert channel.parent_ct_id is not None, f"sub feed {sub_id} has no parent_ct_id"
        assert channel.parent_ct_id in by_id, (
            f"sub feed {sub_id} points at unknown parent {channel.parent_ct_id}"
        )
        assert channel.parent_ct_id in nested_map["root"], (
            f"sub feed {sub_id} parent {channel.parent_ct_id} is not a root feed"
        )


def test_nested_check_is_not_vacuous(nested_topology, capture) -> None:
    """``nested_check_records`` must be > 0 or the double-count guard proves nothing.

    This is the hole that existed before: ``panel_channel_map`` classified only
    non-``main`` intermediate nodes as sub feeds, so a topology declaring its
    panel tree with nested mains produced ``sub == []``, the check never ran, and
    ``nested_double_count_records == 0`` was true for the trivial reason that
    nothing was ever examined.
    """
    panel = panel_channel_map(nested_topology)
    assert panel["sub"], "the nested topology must expose sub feeds"
    records = _capture_records(capture)

    # An honest predictor: report the root feeds only.  The check must still
    # *run* on every record where the sub feed carries load.
    def root_only(record: dict) -> dict:
        latest = record["latest_measurements"]
        total = sum(
            float(latest[ct]["value_w"])
            for ct in panel["root"]
            if ct in latest and latest[ct].get("value_w") is not None
        )
        return {
            "devices": {},
            "reconciliation": {"main_power_w": total, "unknown_residual_w": total},
        }

    report = evaluate(records, root_only, panel_channels=panel)
    assert report.invariants["nested_check_records"] > 0, (
        "nested_check_records is 0: the sub-panel feed was never examined, so "
        "nested_double_count_records == 0 is vacuous"
    )
    # And an honest root-only claim is not flagged.
    assert report.invariants["nested_double_count_records"] == 0


def test_parentless_topology_double_count_is_caught(shipped_topology, capture) -> None:
    """Strip the parents and the guard must still catch the double count.

    With every main declared independent (the shipped shape) a predictor that
    sums every main feed is "correct" by its own topology and wrong physically.
    The harness must still flag it, which requires the panel split to be derived
    from *measurements* when no parent is declared.  Otherwise the guard is
    vacuous on exactly the topology that needs it most.
    """
    stripped = shipped_topology.model_copy(
        update={
            "channels": [
                ch.model_copy(update={"parent_ct_id": None, "alias_of_ct_id": None})
                for ch in shipped_topology.channels
            ]
        }
    )
    records = _capture_records(capture)
    series = _series_by_channel(records)
    panel = panel_channel_map(stripped, series)
    assert panel["sub"], (
        "a parentless topology still has intermediate feeds (the sub-panel mains); "
        "panel_channel_map must find them from measurements or the check is vacuous"
    )
    assert len(panel["root"]) < 6, panel["root"]

    def sum_every_main(record: dict) -> dict:
        latest = record["latest_measurements"]
        total = sum(
            float(latest[ct]["value_w"])
            for ct in panel["root"] + panel["sub"]
            if ct in latest and latest[ct].get("value_w") is not None
        )
        return {
            "devices": {},
            "reconciliation": {"main_power_w": total, "unknown_residual_w": total},
        }

    report = evaluate(records, sum_every_main, panel_channels=panel)
    assert report.invariants["nested_check_records"] > 0
    assert report.invariants["nested_double_count_records"] > 0, (
        "summing every main feed was not flagged as a nested double count"
    )


# --------------------------------------------------------------------------- #
# Replay through the production path
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=False,
    reason=(
        "E2-1/E2-2: the shipped topology declares six independent mains instead of "
        "one service feed with a nested sub panel, so main_power_w sums root+panel+sub "
        "(measured 3943 W vs 1600 W on a 120 min window, ratio 2.46) and the "
        "ill-conditioned system does not terminate on real data. Fixing E2-1 must "
        "make this XPASS; if the fix only re-parents the CTs in the local database "
        "this test will still fail, because it builds both variants from the stored "
        "topology."
    ),
)
def test_real_replay_reports_whole_home_once(shipped_replay, nested_replay) -> None:
    """Both topology variants must report the same whole-home power.

    They describe the same house. The as-shipped variant sums every main feed;
    the nested variant sums only the service feed. If the two disagree by more
    than 5%, one of them is counting part of the house twice -- and since the
    nested one is the physically correct tree, the as-shipped number is wrong.

    The as-shipped run is also the control for the solver hang: ``shipped_replay``
    is ``None`` when it exceeded its deadline, which is itself a failure of the
    shipped topology and is reported as such.
    """
    assert shipped_replay is not None, (
        "the shipped topology did not terminate within "
        f"{REPLAY_TIMEOUT_S:.0f}s on real data; the six independent mains make the "
        "bounded solver ill-conditioned (E2-2)"
    )
    shipped_main = shipped_replay.main_power_w
    nested_main = nested_replay.main_power_w
    assert nested_main > 0.0, "nested replay inferred no main power"
    ratio = shipped_main / nested_main
    assert 0.95 <= ratio <= 1.05, (
        f"as-shipped main {shipped_main:.1f} W vs nested {nested_main:.1f} W "
        f"(ratio {ratio:.3f}); the shipped topology counts the panel tree more "
        "than once (E2-1)"
    )


def test_nested_replay_terminates_and_balances(nested_replay) -> None:
    """The physically correct tree must terminate and conserve power.

    The positive control for the whole harness: on the same capture, declaring
    the panel tree makes the solver well-conditioned and the reconciliation
    balance ``main - accounted = residual`` hold.
    """
    assert nested_replay.n_windows > 0
    assert nested_replay.main_power_w > 0.0
    assert nested_replay.residual_w >= -1.0, (
        f"residual {nested_replay.residual_w:.1f} W is negative: the engine "
        "claimed more device power than the whole-home feed carries"
    )
    balance = nested_replay.main_power_w - nested_replay.accounted_w
    assert balance == pytest.approx(nested_replay.residual_w, abs=1.0)
    assert nested_replay.invariants["nested_check_records"] > 0
    assert nested_replay.invariants["nested_double_count_records"] == 0


def test_shipped_replay_terminates(shipped_replay) -> None:
    """E2-2: the shipped topology must terminate on real data within the deadline.

    Currently it does not, so this is an xfail that names the defect. It flips to
    XPASS when the engine's conditioning fix lands.
    """
    if shipped_replay is None:
        pytest.xfail(
            f"E2-2: shipped topology did not terminate within {REPLAY_TIMEOUT_S:.0f}s"
        )
    assert shipped_replay.n_windows > 0


def test_shipped_replay_double_counts_relative_to_nested(
    shipped_replay, nested_replay
) -> None:
    """Quantify E2-1: the shipped total exceeds the nested total materially.

    Not an xfail, because it asserts the *defect exists* rather than that it is
    fixed. It fails when E2-1 is fixed, and that is intentional: the message says
    which test to update.
    """
    if shipped_replay is None:
        pytest.skip("shipped topology did not terminate; see E2-2")
    assert shipped_replay.main_power_w > nested_replay.main_power_w * 1.05, (
        "the shipped topology no longer over-reports whole-home power; E2-1 may "
        "be fixed -- update this test and remove the xfail on "
        "test_real_replay_reports_whole_home_once"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _capture_records(capture) -> list[dict]:
    """Turn a real capture into ``metrics.evaluate``-shaped records.

    A real house has no ground truth, so ``ground_truth.devices`` stays empty and
    only the fields the *nested* invariants need are filled: a
    ``latest_measurements`` snapshot per inference instant.  ``timestamp`` is the
    instant, so ``evaluate`` can compute the window.
    """
    by_tick: dict[str, dict] = {}
    for sample in capture.samples:
        key = sample.received_at.replace(microsecond=0).isoformat()
        slot = by_tick.setdefault(
            key,
            {
                "timestamp": key,
                "latest_measurements": {},
                "ground_truth": {"devices": {}, "main_total_w": 0.0, "unmetered_w": 0.0},
            },
        )
        slot["latest_measurements"][sample.ct_id] = {
            "value_w": sample.value_w,
            "quality": sample.quality,
            "repeated": sample.repeated,
        }
    return [by_tick[key] for key in sorted(by_tick)]


def _series_by_channel(records: list[dict]) -> dict[str, list[float | None]]:
    """Per-channel aligned series, for the measurement-derived panel split."""
    channels: dict[str, list[float | None]] = {}
    for record in records:
        for ct_id, sample in record["latest_measurements"].items():
            channels.setdefault(ct_id, []).append(sample.get("value_w"))
    # Every series must be equally long for a positional comparison; missing
    # samples are gaps, not zeros.
    length = max((len(v) for v in channels.values()), default=0)
    for ct_id, values in channels.items():
        channels[ct_id] = values + [None] * (length - len(values))
    return channels
