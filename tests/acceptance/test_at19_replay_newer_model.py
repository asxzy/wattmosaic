"""AT-19 — historical evidence replayable with a newer model version (HB §21).

Closes: L7. Replay one week with model set v1 and v2: derived outputs differ,
raw samples and evidence are byte-identical, both result sets are retrievable
by ``derived_run_id``.

The scenario (topology, mapping, HA entity links and the two model sets) is
written into a real database through the engine's declaration paths by
``tools.simulator.replay_scenario``; the replay rebuilds everything from that
database's stored versions.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.acceptance]

CLOSED_BY = "L7"


def test_replay_with_new_model_differs_but_evidence_is_immutable(hb17, tmp_path) -> None:
    """Replay is pure: only the model set changes; evidence never does."""
    from wattmosaic_engine.replay import replay_week  # target surface (L7)

    from tools.simulator.replay_scenario import build_replay_db

    raw_bytes = hb17["observation"]
    evidence_bytes = hb17["house"].observation_view()
    db_path = tmp_path / "replay.db"
    triples = build_replay_db(db_path, raw_bytes)

    run_v1 = replay_week(raw_bytes, triple=triples["v1"], db_path=db_path)
    run_v2 = replay_week(raw_bytes, triple=triples["v2"], db_path=db_path)

    assert run_v1.derived_run_id != run_v2.derived_run_id
    assert run_v1.derived_estimates != run_v2.derived_estimates
    assert run_v1.raw_samples_bytes == raw_bytes
    assert run_v2.raw_samples_bytes == raw_bytes
    assert run_v1.evidence_bytes == evidence_bytes == run_v2.evidence_bytes

    from wattmosaic_engine.replay import get_derived_run  # target surface (L7)

    assert get_derived_run(run_v1.derived_run_id).derived_estimates == run_v1.derived_estimates
    assert get_derived_run(run_v2.derived_run_id).derived_estimates == run_v2.derived_estimates
