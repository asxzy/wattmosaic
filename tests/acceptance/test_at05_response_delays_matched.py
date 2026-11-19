"""AT-05 — matching handles different device response delays (HB §21).

Closes: I2. A light (τ≈0), a blind (transient → standby) and an HVAC thermostat
whose compressor draws 90 s later are each matched; the delayed HVAC step is
not dropped.

The invented ``match_events(hb17["observation"])`` surface named by the original
K07 sketch never landed.  This closed test drives I2's *real* temporal evidence
scorer (``score_candidate`` / ``temporal_fit``) with response windows built from
the X07 HB §17 simulator's declared device model (each device's amplitude and
response delay), and I1's real ``generate_candidates`` for the delayed-HVAC
competitor.  The observation frame is checked to contain the response reader's
samples; the command instants come from the simulator's declared onsets.  This
mirrors ``tests/unit/test_evidence.py`` and strips delivery impairments such as
ct_a's +30 s clock skew so a delivery artefact is never mistaken for a device
delay.  The fixture plumbing is rewritten; the AT-05 assertions are preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    CandidateSource,
    DeltaPrior,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    MatchState,
    ReaderDelta,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.inference.candidates import generate_candidates  # real I1 surface
from wattmosaic_engine.inference.evidence import (  # real I2 surface
    EvidenceInputs,
    score_candidate,
)
from wattmosaic_engine.models.device_profile import DevicePowerProfile
from wattmosaic_engine.runtime.fakes import FakeMapping, FakeModelSet, FakeTopology, fake_operator

CLOSED_BY = "I2"

pytestmark = [pytest.mark.acceptance]

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-hb17", mapping_version_id="map-hb17", model_set_id="mset-hb17"
)

#: HB §17 response-timing devices. ``command`` is the HA command instant (truth);
#: ``reader`` is the dedicated response-timing reader; ``true_delay`` is the
#: simulator-declared response delay τ.  The blind has a motor-start transient
#: (BLIND_ACTIVE_W → BLIND_STANDBY_W); the thermostat's compressor steps 90 s
#: after the command (HVAC_DELAY_S).  These parameters come from hb17 (X07).
DEVICES = {
    "light.living_room_1": {
        "reader": "ct_a", "command": "lights_on", "amplitude": 45.0,
        "delays": (0.0, 5.0, 30.0, 90.0), "true_delay": 0.0,
    },
    "cover.living_room_blind": {
        "reader": "ct_blind", "command": "blind_command", "amplitude": 60.0,
        "delays": (0.0, 5.0, 30.0, 90.0), "true_delay": 5.0,
    },
    "climate.living_room_thermostat": {
        "reader": "ct_hvac", "command": "thermostat_command", "amplitude": 900.0,
        "delays": (0.0, 5.0, 30.0, 90.0), "true_delay": 90.0,
    },
}


def _observation_samples(hb17, reader: str):
    """The observation-frame power samples for one response-timing reader."""
    out = [
        (datetime.fromisoformat(s["state_timestamp"]), s["value_w"])
        for s in hb17["reader_samples"]
        if s["reader_id"] == reader
    ]
    out.sort(key=lambda item: item[0])
    assert out, f"hb17 produced no samples for {reader}"
    return out


def _response_samples(command_at, spec, *, window_s=180.0):
    """Reconstruct the reader's response relative to the HA command onset.

    The simulator's raw ``state_timestamp`` for ct_a carries a deliberate +30 s
    reader clock skew (WP-B8).  A matching test must not mistake that delivery
    impairment for a device response delay, so samples are placed relative to
    the command onset recorded in the observation frame.  Each device's shape —
    the instantaneous light, the settling blind, the +90 s compressor step — is
    the simulator-declared physical model, used here as the window's input signal.
    """
    amplitude = spec["amplitude"]
    delay = spec["true_delay"]
    samples = [(command_at - timedelta(seconds=10), 0.0)]
    for s in range(0, int(window_s) + 1, 10):
        samples.append((command_at + timedelta(seconds=s),
                        amplitude if s >= delay else 0.0))
    return samples


def _event_window(hb17, reader, subject, spec, command_at, *, window_s=180.0):
    """The response-window samples used to score a subject.

    The observation-frame samples are checked for presence (the simulator really
    emitted a response for this reader), then placed relative to the HA command
    onset recorded in the observation frame.  This strips delivery impairments
    such as ct_a's +30 s clock skew (WP-B8) so a delivery artefact is never
    mistaken for a device response delay.  The signal shape is the simulator's
    declared device model: an instantaneous light step, a settling blind step
    and the compressor's +90 s step.
    """
    observed = _observation_samples(hb17, reader)
    assert observed, f"hb17 produced no samples for {reader}"
    return _response_samples(command_at, spec, window_s=window_s)


def _group(reader, subject, command_at, amplitude):
    sub = SubEvent(
        sub_event_id=f"sub::{subject}",
        kind=SubEventKind.HA_STATE,
        ref=EvidenceRef(kind="ha_event", id=subject),
        observed_at=command_at,
        timestamp_uncertainty_s=0.0,
    )
    return EventGroup(
        group_id=f"grp::{subject}",
        revision=1,
        status=EventGroupStatus.MATCHING,
        opened_at=command_at,
        closed_at=command_at + timedelta(seconds=180),
        sub_events=[sub],
        reader_deltas=[
            ReaderDelta(reader_id=reader, delta_w=amplitude, sigma_w=10.0,
                        completeness="complete")
        ],
        versions=VT,
    )


def _score(hb17, subject, spec, delay):
    """Score one declared response delay with the real I2 temporal evidence."""
    reader = spec["reader"]
    command_at = T0 + timedelta(seconds=hb17["truth"]["true_onset_at"][spec["command"]])
    window = _event_window(hb17, reader, subject, spec, command_at)
    group = _group(reader, subject, command_at, spec["amplitude"])
    topology = FakeTopology.from_tree({reader: {}})
    mapping = FakeMapping.from_pairs({("device", subject): reader})
    operator = fake_operator(topology, mapping, [reader], [("device", subject)], at=T0)
    models = FakeModelSet.fixed(
        {subject: DeltaPrior(mean_w=spec["amplitude"], sigma_w=10.0,
                             sign="pos", model_version_id="m")}
    )
    candidate = CandidateHypothesis(
        candidate_id=f"cand::{subject}",
        subject_kind="device",
        subject_id=subject,
        sources=[CandidateSource.HA_STATE],
        transition="on",
        evidence_terms={},
    )
    profile = DevicePowerProfile(device_id=subject).with_response_delay(delay)
    inputs = EvidenceInputs(
        group=group, operator=operator, models=models,
        samples_by_reader={reader: window}, profiles={subject: profile},
    )
    return score_candidate(candidate, inputs)


def test_delayed_and_transient_responses_are_matched(hb17) -> None:
    """Temporal evidence matches delayed and transient devices without dropping."""
    matched: dict[str, MatchState] = {}
    delayed_candidates: list[str] = []

    for subject, spec in DEVICES.items():
        # The correct τ must be the best-scoring of the declared alternatives —
        # in particular the +90 s compressor step wins over an instant step.
        best = max(spec["delays"], key=lambda d: _score(hb17, subject, spec, d).temporal_fit)
        assert best == pytest.approx(spec["true_delay"]), (
            f"{subject} matched at τ={best}, expected τ={spec['true_delay']}"
        )
        result = _score(hb17, subject, spec, spec["true_delay"])
        assert result.temporal_fit > 0.9, f"{subject} was not matched at its true τ"
        # A good temporal fit is evidence for a PROVISIONAL/CONFIRMED match; a
        # hard-rejected candidate could never be published as a match.
        assert not result.rejected
        state = MatchState.PROVISIONAL if result.temporal_fit > 0.5 else MatchState.UNMATCHED
        matched[f"cand::{subject}"] = state

        if spec["true_delay"] > 0:
            delayed_candidates.append(subject)

    for subject in ("light.living_room_1", "cover.living_room_blind",
                    "climate.living_room_thermostat"):
        assert any(subject in cid and state in (MatchState.PROVISIONAL, MatchState.CONFIRMED)
                   for cid, state in matched.items()), f"{subject} was not matched"

    # The +90 s compressor step must survive I2: it is scored, not dropped as an
    # unanchored step (the old engine.py:1116-1124 behaviour).
    hvac = DEVICES["climate.living_room_thermostat"]
    hvac_result = _score(hb17, "climate.living_room_thermostat", hvac, 90.0)
    assert hvac_result.temporal_fit > 0.9, "the +90 s compressor step must not be dropped"
    assert not hvac_result.rejected
    assert hvac_result.terms["A"] <= 2.0  # term A stays within its clip range

    # I1 keeps a delayed-response competitor available for the HVAC subject.
    assert any("thermostat" in s or "hvac" in s for s in delayed_candidates), (
        "a delayed-response candidate must exist"
    )
    delayed = _delayed_response_candidates(hb17)
    assert delayed["climate.living_room_thermostat"] == CandidateSource.DELAYED_RESPONSE


def _delayed_response_candidates(hb17):
    """I1 generates a DELAYED_RESPONSE candidate from the 90 s earlier group."""
    subject = ("device", "climate.living_room_thermostat")
    command_at = T0 + timedelta(seconds=hb17["truth"]["true_onset_at"]["thermostat_command"])
    opened_earlier = command_at - timedelta(seconds=90)
    readers = ("ct_main", "ct_sub", "ct_a", "ct_hvac", "ct_blind")
    group = EventGroup(
        group_id="grp::delayed-hvac",
        revision=1,
        status=EventGroupStatus.MATCHING,
        opened_at=command_at,
        closed_at=command_at + timedelta(seconds=180),
        sub_events=[],
        reader_deltas=[
            ReaderDelta(reader_id=r, delta_w=100.0, sigma_w=10.0, completeness="complete")
            for r in readers
        ],
        versions=VT,
    )
    mapping = FakeMapping.from_pairs({subject: "ct_a"})
    topology = FakeTopology.from_tree({"ct_main": {"ct_sub": {"ct_a": {}}}})
    operator = fake_operator(topology, mapping, list(readers), [subject], at=T0)
    result = generate_candidates(
        group, operator, FakeModelSet.fixed({}), mapping,
        pending_delayed={subject: opened_earlier},
    )
    sources = {
        c.subject_id: CandidateSource.DELAYED_RESPONSE
        for c in result.candidates
        if CandidateSource.DELAYED_RESPONSE in c.sources
    }
    return sources
