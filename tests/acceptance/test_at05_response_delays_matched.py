"""AT-05 — matching handles different device response delays (HB §21).

Closes: I2. A light (τ≈0), a blind (transient → standby) and an HVAC thermostat
whose compressor draws 90 s later are each matched; the delayed HVAC step is
not dropped.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I2"),
]


def test_delayed_and_transient_responses_are_matched(hb17) -> None:
    """Temporal evidence matches delayed and transient devices without dropping."""
    from wattmosaic_engine.contracts import MatchState
    from wattmosaic_engine.inference import match_events  # target surface (I2)

    result = match_events(hb17["observation"])
    matches: dict[str, MatchState] = result.matches

    for subject in ("light.living_room_1", "cover.living_room_blind",
                    "climate.living_room_thermostat"):
        assert any(
            subject in cid and state in (MatchState.PROVISIONAL, MatchState.CONFIRMED)
            for cid, state in matches.items()
        ), f"{subject} was not matched"

    assert result.hvac_step_dropped is False, "the +90 s compressor step must not be dropped"
    assert any(
        "hvac" in s or "thermostat" in s for s in result.delayed_candidates
    ), "a delayed-response candidate must exist"
