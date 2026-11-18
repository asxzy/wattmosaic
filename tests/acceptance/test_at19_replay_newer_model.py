"""AT-19 — historical evidence replayable with a newer model version (HB §21).

Closes: L7. Replay one week with model set v1 and v2: derived outputs differ,
raw samples and evidence are byte-identical, both result sets are retrievable
by ``derived_run_id``.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by L7"),
]


def test_replay_with_new_model_differs_but_evidence_is_immutable(hb17) -> None:
    """Replay is pure: only the model set changes; evidence never does."""
    from wattmosaic_engine.replay import replay_week  # target surface (L7)

    raw_bytes = hb17["observation"]
    evidence_bytes = hb17["house"].observation_view()

    run_v1 = replay_week(raw_bytes, model_set="v1")
    run_v2 = replay_week(raw_bytes, model_set="v2")

    assert run_v1.derived_run_id != run_v2.derived_run_id
    assert run_v1.derived_estimates != run_v2.derived_estimates
    assert run_v1.raw_samples_bytes == raw_bytes
    assert run_v2.raw_samples_bytes == raw_bytes
    assert run_v1.evidence_bytes == evidence_bytes == run_v2.evidence_bytes

    from wattmosaic_engine.replay import get_derived_run  # target surface (L7)

    assert get_derived_run(run_v1.derived_run_id).derived_estimates == run_v1.derived_estimates
    assert get_derived_run(run_v2.derived_run_id).derived_estimates == run_v2.derived_estimates
