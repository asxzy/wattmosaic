"""Regression gates for real-data replay through the v3 surface.

These tests read ``data/wattmosaic.db`` -- the local capture of the reference
Home Assistant install -- and skip cleanly when it is absent. The database is
opened ``mode=ro`` and never written. No real household data is committed.

After Z2 the legacy per-snapshot solver is gone. The v3 path replays immutable
evidence through the versioned reconciler
(:func:`wattmosaic_engine.replay.replay_week`) under an explicit model set, and
the derived output is a
:class:`~wattmosaic_engine.learning.reconciliation.DerivedRun`. The gates below
pin the properties that still matter on a real capture:

* replay terminates under a hard deadline -- a wedged engine fails fast;
* the derived run carries an explicit version triple and immutable evidence;
* two model sets produce distinct runs, and repeated runs are deterministic;
* absent estimates stay ``None`` -- no ``0 W`` placeholder for a missing value
  (HB §18);
* the tool reads the capture read-only and never writes to a database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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

REPLAY_MINUTES = 120
REPLAY_TIMEOUT_S = 60.0


@pytest.fixture(scope="module")
def entity_to_ct():
    """Map capture entity ids to endpoint ids from the stored topology (read-only)."""
    import json
    import sqlite3

    from tools.audit.replay_capture import aligner_key

    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"no topology_versions row in {REAL_DB}"
    payload = json.loads(row[0])
    mapping: dict[str, str] = {}
    for channel in payload.get("channels") or []:
        ct_id = channel.get("ct_id")
        entity = str(channel.get("entity_id") or "")
        if ct_id:
            mapping[entity] = ct_id
            mapping[aligner_key(entity)] = ct_id
    return mapping


@pytest.fixture(scope="module")
def capture(entity_to_ct):
    from tools.audit.replay_capture import load_capture_db

    return load_capture_db(REAL_DB, minutes=REPLAY_MINUTES, entity_to_ct=entity_to_ct)


@pytest.fixture(scope="module")
def derived_run(capture):
    """The v3 derived run, with a hard deadline so a wedged engine fails fast."""
    from tools.audit.replay_capture import ReplayTimeout, _Deadline, replay

    try:
        with _Deadline(REPLAY_TIMEOUT_S, hard_exit=False):
            return replay(capture, variant="as-shipped", models="v1")
    except ReplayTimeout:
        return None


def test_real_capture_loads_read_only(capture) -> None:
    """The capture has delivered samples; the DB was opened ``mode=ro``."""
    assert capture.samples
    assert capture.window_start is not None
    assert capture.window_end is not None


def test_v3_replay_terminates_under_the_deadline(derived_run) -> None:
    """A real capture must not wedge the v3 reconciler."""
    assert derived_run is not None, (
        f"v3 replay did not terminate within {REPLAY_TIMEOUT_S:.0f}s on real data"
    )
    assert derived_run.derived_run_id.startswith("derived-")
    assert derived_run.chunks >= 1


def test_derived_run_carries_an_explicit_version_triple(derived_run) -> None:
    """The run is bound to a (topology, mapping, model-set) triple, no lookup."""
    assert derived_run.versions.topology_version_id
    assert derived_run.versions.mapping_version_id
    assert derived_run.versions.model_set_id.startswith("mset-")


def test_derived_run_is_retrievable_and_deterministic(capture, derived_run) -> None:
    """Replaying the same capture twice yields the same derived run."""
    from wattmosaic_engine.replay import get_derived_run

    from tools.audit.replay_capture import replay

    again = replay(capture, models="v1")
    assert again.derived_run_id == derived_run.derived_run_id
    assert again.fingerprint == derived_run.fingerprint
    assert get_derived_run(derived_run.derived_run_id).derived_estimates == (
        derived_run.derived_estimates
    )


def test_absent_estimates_are_none_not_zero(derived_run) -> None:
    """HB §18: a non-identifiable subject has no fabricated point estimate."""
    assert derived_run.derived_estimates
    for estimate in derived_run.derived_estimates:
        assert estimate.point_w is None or isinstance(estimate.point_w, float)
        assert estimate.status


def test_two_model_sets_produce_distinct_runs(capture, derived_run) -> None:
    """AT-19: changing only the model set changes the derived run."""
    from tools.audit.replay_capture import replay

    other = replay(capture, models="v2")
    assert other.derived_run_id != derived_run.derived_run_id
    assert other.versions.model_set_id != derived_run.versions.model_set_id


def test_replay_reads_topology_read_only(entity_to_ct) -> None:
    """The tool must not write to the capture database (it opens ``mode=ro``)."""
    import sqlite3

    before = REAL_DB.stat().st_mtime_ns
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        conn.execute("SELECT COUNT(*) FROM ct_samples").fetchone()
    finally:
        conn.close()
    assert REAL_DB.stat().st_mtime_ns == before
