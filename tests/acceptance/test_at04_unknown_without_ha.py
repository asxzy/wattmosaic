"""AT-04 — unknown devices detected without HA (HB §21).

Closes: T6. The fridge compressor has no HA entity; it still produces an event
group, an unknown-scope candidate and an Unknown Load Profile that reaches
CHARACTERISED after five cycles.
"""

from __future__ import annotations

import pytest

CLOSED_BY = "T6"

pytestmark = [pytest.mark.acceptance]


def test_fridge_without_ha_becomes_characterised_unknown_profile(hb17) -> None:
    """Reader-only evidence is enough to detect and characterise the fridge."""
    from wattmosaic_engine.contracts import CandidateSource, UnknownLoadProfileV3
    from wattmosaic_engine.realtime import run_unknown_detection  # target surface (T6)

    result = run_unknown_detection(hb17["observation"])

    assert any(
        e.status.value in ("detected", "matching", "provisional", "confirmed")
        for e in result.event_groups
    ), "the fridge transition must create an event group without any HA event"
    assert any(
        CandidateSource.UNKNOWN_SCOPE in c.sources for c in result.candidates
    ), "a candidate must carry the unknown_scope source"

    profiles: list[UnknownLoadProfileV3] = result.unknown_profiles
    fridge = next(p for p in profiles if p.scope_endpoint_id == "ct_b")
    for _ in range(5):
        profiles = run_unknown_detection(hb17["observation"], profiles=profiles)
    fridge = next(p for p in profiles if p.scope_endpoint_id == "ct_b")
    assert fridge.lifecycle == "characterised"
    assert fridge.event_count >= 5
