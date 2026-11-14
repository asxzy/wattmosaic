"""End-to-end: one HA event travels the whole inference chain, on the real rig.

Every module below already had its own unit tests.  What none of them could
prove is that the *live* process calls them in dependency order over the same
window, so that an artifact exists at the end:

``HA event -> isolation -> association -> circuit -> residual -> record``

This file runs the production callbacks (``EngineCore.on_sample`` /
``on_event`` / ``run_inference_once``) with a seeded simulator and asserts the
chain's *products*, not its internals: a verdict, a candidate association, a
classified circuit, a residual and an append-only record that cites them.

It also covers the two failures that only appear when the pieces are wired:

* an event analysed on the cycle it arrived can never be ``ISOLATED`` (its
  window has not elapsed and the post-event readings do not exist yet), so the
  pass must *hold* it -- and a hold that never drains is the same as a drop;
* association needs ``min_events`` (>= 2) independent events, but a pass sees at
  most one transition per device, so evidence has to accumulate across passes
  or no relation is ever emitted.

Reproducibility is asserted the same way an operator would check it: run the
same seeded raw data twice and diff the artifacts.  A wall clock, a row id or a
UUID in the identity would make that diff non-empty.
"""

from __future__ import annotations

import datetime as dt

import pytest
from wattmosaic_engine.disaggregation import audit as audit_module
from wattmosaic_engine.disaggregation.audit import (
    REFUSED_MULTI_CT_SCOPE,
    AuditConfig,
    AuditPass,
)

from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel

from .test_pipeline_e2e import _feed_production, _production_core

#: 30 simulated minutes is the shortest span in which the seeded household both
#: switches a device and produces a second clean instance of the same switch --
#: the minimum for ``AssociationConfig.min_events``.  Shorter runs legitimately
#: produce a verdict and a record but no association, which is why the shorter
#: cases below assert those and not more.
MINUTES = 30

#: Seed fixed so the assertions name real devices rather than "some device".
SEED = 5


def _records(minutes: float = MINUTES, seed: int = SEED) -> list[dict]:
    return list(
        HouseholdScenario(
            SimulatorConfig(
                seed=seed, minutes=minutes, interval=15, quality=QualityLevel.GOOD
            )
        ).records()
    )


def _enum(value) -> str:
    return str(getattr(value, "value", value))


@pytest.fixture(scope="module")
def driven(tmp_path_factory):
    """One production rig driven through the whole scenario, read by several tests.

    Module scoped because the rig *is* the experiment: re-running the simulator
    per test would triple the wall time and, worse, let each test assert against
    a different run while claiming to describe the same chain.
    """
    core, db = _production_core(tmp_path_factory.mktemp("full_chain"))
    try:
        _feed_production(core, _records())
        yield core, db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def test_a_live_run_produces_every_artifact_in_the_chain(driven) -> None:
    """event -> verdict -> relation -> circuit -> residual -> record, all live."""
    core, _db = driven
    backend = core.inference_backend
    report = backend.audit_report()
    assert report is not None, "no analysis pass ran"

    # 1. Isolation: events arrived, verdicts were reached, and *not* all of them
    #    were isolated -- a run that reported 100 % clean would be a red flag.
    isolation = core.isolation_status()
    counts = isolation["counts"]
    assert counts["analysed"] > 0
    assert counts["isolated"] > 0, "no event ever reached ISOLATED"
    assert counts["contaminated"] > 0, (
        "contamination was never detected; the isolator is answering 'clean' "
        "to everything, which means it is not looking at competitors"
    )
    assert counts["insufficient_data"] == 0, "held events must still mature"

    # 2. Association: a candidate edge, with the evidence that justified it.
    associations = [r for r in report.relations if _enum(r.kind) == "device_ct_association"]
    assert associations, "no device->CT association was derived"
    for relation in associations:
        assert relation.status == "candidate", "a candidate must never self-confirm"
        assert relation.requires_confirmation is True
        evidence = relation.evidence
        assert evidence["stats"]["n_events"] >= 2, "min_events was not reached"
        assert evidence["supporting"], "a relation must cite the events behind it"

    # 3. Circuits: classified from the associations, never from nothing.
    circuits = core.circuits()
    assert circuits, "no circuit was classified"
    known = {_enum(c["classification"]) for c in circuits}
    assert known - {"unknown"}, "every circuit stayed unknown"
    for circuit in circuits:
        # A class is never born confirmed: it is either still a candidate or was
        # promoted because a residual was checked and did not contradict it.
        assert _enum(circuit["classification_status"]) in {"candidate", "confirmed"}
        assert circuit["device_ids"], "a circuit must name the devices it explains"
    assert any(
        _enum(c["classification_status"]) == "candidate" for c in circuits
    ), "every circuit self-confirmed, which the classifier must never do"

    # 4. Residual: either computed for a single-CT scope, or refused with the
    #    module's own reason code.  Never a fabricated zero.
    assert report.counts["residuals"] > 0
    refused = report.refused
    for key, reason in refused.items():
        assert key.startswith("residual:")
        assert reason, "a refusal must carry a reason code"
    multi_ct = [c for c in circuits if len(c["ct_ids"]) > 1]
    if multi_ct:
        assert REFUSED_MULTI_CT_SCOPE in refused.values(), (
            "a multi-CT scope must be refused rather than summed"
        )

    # 5. Records: append-only, cite the artifacts above, and carry the version.
    device_id = circuits[0]["device_ids"][0]
    records = core.inference_records(device_id, limit=5000)
    assert records, "no InferenceRecord was written for a device that has a circuit"
    assert [r["revision"] for r in records] == list(range(1, len(records) + 1))
    for index, record in enumerate(records):
        assert record["algorithm_version"] == audit_module.DEFAULT_ALGORITHM_VERSION
        assert record["record_id"].startswith("rec_")
        if index == 0:
            assert record["previous_record_id"] is None
        else:
            assert record["previous_record_id"] == records[index - 1]["record_id"]
            assert record["revision"] == records[index - 1]["revision"] + 1


def test_the_record_cites_the_evidence_that_produced_it(driven) -> None:
    """A record is an argument: it names the events, relations and circuits."""
    core, _db = driven
    report = core.inference_backend.audit_report()
    devices = sorted(
        {
            str(r.subject_id)
            for r in report.relations
            if _enum(r.kind) == "device_ct_association"
        }
    )
    assert devices
    citing = [
        record
        for device_id in devices
        for record in core.inference_records(device_id, limit=5000)
        if any(ref.startswith("relation:") for ref in record["evidence_refs"])
    ]
    assert citing, "no record cited the association that explains the device"
    for record in citing:
        assert record["algorithm_version"]
        assert record["explanation"], "a record without an explanation is a number"
        # The evidence dict is the machine-readable half of the same claim.
        assert "association_ids" in record["evidence"]
        assert record["evidence"]["association_ids"], (
            "a record cited a relation in evidence_refs but listed none in evidence"
        )


# --------------------------------------------------------------------------- #
# Events are held, not analysed on arrival
# --------------------------------------------------------------------------- #
def _pass(tmp_path, **overrides) -> AuditPass:
    from wattmosaic_engine.storage.database import Database

    config = AuditConfig(**overrides)
    return AuditPass(Database(tmp_path / "held.db"), config=config)


def _event(at: dt.datetime, device_id: str, entity_id: str) -> dict:
    return {
        "device_id": device_id,
        "entity_id": entity_id,
        "new_state": "on",
        "previous_state": "off",
        "occurred_at": at,
    }


def test_an_event_is_held_until_its_analysis_window_elapses(tmp_path) -> None:
    """Analysing on arrival yields ``insufficient_data`` forever; holding does not."""
    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=True)
    device = topology.devices[0]
    channel = topology.channels[0]
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = _pass(tmp_path)

    early = pass_.analyze(
        _window(at),
        topology=topology,
        results=[],
        events=[_event(at, device.device_id, channel.entity_id)],
        now=at,
    )
    assert early.isolations == [], "an event was analysed before its window elapsed"
    assert early.stages["pending_events"]["waiting"] == 1

    # Still not ready: the power window (60 s) has not passed either.
    halfway = pass_.analyze(
        _window(at + dt.timedelta(seconds=30)),
        topology=topology,
        results=[],
        events=[],
        now=at + dt.timedelta(seconds=30),
    )
    assert halfway.isolations == []
    assert halfway.stages["pending_events"]["waiting"] == 1

    # Now it is ready, and the queue drained rather than growing.
    late = pass_.analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[],
        events=[],
        now=at + dt.timedelta(minutes=5),
    )
    assert len(late.isolations) == 1
    assert late.isolations[0].event_id
    # No tracker history was supplied, so the honest verdict is insufficient
    # data -- but it is a *verdict*, not a silently absent event.
    assert late.isolations[0].status == "insufficient_data"
    assert late.stages["pending_events"]["waiting"] == 0


def test_an_event_older_than_the_retention_horizon_is_counted_not_analysed(
    tmp_path,
) -> None:
    """A hold that outlives the history is a drop; the counter makes it visible."""
    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=True)
    device = topology.devices[0]
    channel = topology.channels[0]
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = _pass(tmp_path, event_max_age_s=300.0)

    pass_.analyze(
        _window(at),
        topology=topology,
        results=[],
        events=[_event(at, device.device_id, channel.entity_id)],
        now=at,
    )
    report = pass_.analyze(
        _window(at + dt.timedelta(hours=2)),
        topology=topology,
        results=[],
        events=[],
        now=at + dt.timedelta(hours=2),
    )
    assert report.isolations == []
    assert pass_.events_expired == 1


# --------------------------------------------------------------------------- #
# Evidence accumulates across passes
# --------------------------------------------------------------------------- #
def test_association_evidence_survives_the_pass_that_observed_it(driven) -> None:
    """``min_events`` (>= 2) is unreachable if each pass throws evidence away.

    A pass sees at most one transition per device, so a finder rebuilt per cycle
    would observe one event and never emit.  The buffer is what makes the
    repeatability rule satisfiable, and it must be idempotent: re-analysing the
    same event during a reanalysis pass must not count it twice.
    """
    core, _db = driven
    pass_ = core.inference_backend.audit
    observations = pass_._accumulated_observations()
    assert len(observations) > 0, "every observation was discarded after its pass"
    keys = [(row.device_id, row.event_id, row.ct_id) for row in observations]
    assert len(keys) == len(set(keys)), "the same (event, CT) was counted twice"
    multi = {
        device
        for device, event, _ct in keys
        if len({e for d, e, _ in keys if d == device}) >= 2
    }
    assert multi, "no device produced two events, so min_events could not be met"


# --------------------------------------------------------------------------- #
# Stored-history reanalysis
# --------------------------------------------------------------------------- #
def test_reanalysis_reaches_isolated_from_stored_history_alone(tmp_path) -> None:
    """A restart must be able to reach a verdict for an event that already matured.

    The live queue deliberately holds events until their window elapses, so an
    event that matured while the process was down is only reachable from stored
    raw samples.  This is the pass that does it -- using the *aligner* key
    (``sensor.x_power`` -> ``x_power``), which is not the topology ``ct_id``.
    """
    from wattmosaic_engine.storage.database import Database

    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=True)
    db = Database(tmp_path / "reanalysis.db")
    try:
        base = dt.datetime(2026, 1, 1, 6, 0, tzinfo=dt.UTC)
        rows = []
        for tick in range(40):
            stamp = base + dt.timedelta(seconds=15 * tick)
            for channel in topology.channels:
                watts = 1000.0 if tick < 8 else 5000.0
                rows.append(
                    {
                        "ct_id": channel.entity_id.split(".", 1)[-1],
                        "entity_id": channel.entity_id,
                        "value_w": watts,
                        "raw_value": watts,
                        "unit": "W",
                        "state_timestamp": stamp,
                        "received_at": stamp,
                        "physical_timestamp": stamp,
                        "quality": "good",
                        "repeated": False,
                        "sequence": 0,
                    }
                )
        assert db.insert_ct_samples(rows) == len(rows)

        device = topology.devices[0]
        event_at = base + dt.timedelta(seconds=120)
        db.insert_device_event(
            {
                "event_id": "evt-reanalysis",
                "device_id": device.device_id,
                "entity_id": topology.channels[0].entity_id,
                "event_type": "state_changed",
                "previous_state": "off",
                "new_state": "on",
                "attributes": {},
                "occurred_at": event_at,
                "received_at": event_at,
            }
        )

        pass_ = AuditPass(db, config=AuditConfig())
        report = pass_.reanalyze(
            topology=topology,
            lookback_s=3600.0,
            now=base + dt.timedelta(seconds=600),
        )
        assert report.mode == "reanalysis"
        assert report.errors == {}
        assert report.stages["series"]["source"] == "stored_samples"
        assert len(report.isolations) == 1
        verdict = report.isolations[0]
        assert verdict.status == "isolated", verdict.reason
        assert verdict.reason == "isolated"
        coverage = verdict.evidence["coverage"]
        assert coverage["sufficient"] is True
        # The original instant is preserved, not re-stamped at analysis time.
        assert verdict.occurred_at == event_at
        assert report.counts["observations"] > 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
def _artifacts(core) -> tuple:
    report = core.inference_backend.audit_report()
    return (
        sorted((r.relation_id, r.algorithm_version) for r in report.relations),
        sorted(
            (c.circuit_id, _enum(c.classification), _enum(c.measurement_basis))
            for c in report.circuits
        ),
        sorted((r.record_id, r.revision) for r in report.records),
        sorted((round(r.residual_w, 6), _enum(r.kind)) for r in report.residuals),
    )


def test_same_raw_data_and_algorithm_version_reproduce_identical_records(
    tmp_path_factory,
) -> None:
    """Two runs of one seeded scenario must produce byte-identical artifacts.

    This is the property that makes "replay under a new ``algorithm_version``
    and diff against the old one" a meaningful operation.  It fails the moment a
    UUID, a row id or the wall clock participates in an identity.
    """
    outcomes = []
    for index in range(2):
        core, db = _production_core(tmp_path_factory.mktemp(f"replay_{index}"))
        try:
            _feed_production(core, _records(minutes=20))
            outcomes.append(_artifacts(core))
        finally:
            db.close()

    relations, circuits, records, residuals = outcomes[0]
    assert records, "the replay produced no records at all"
    assert outcomes[0] == outcomes[1], (
        "identical raw data produced different artifacts; something outside the "
        "inference is part of the identity"
    )
    # And the identity itself is content-addressed, not allocated.
    for record_id, _revision in records:
        assert record_id.startswith("rec_")
        assert len(record_id) == len("rec_") + 24


def _window(at: dt.datetime):
    from wattmosaic_engine.models.protocols import SimpleWindow

    return SimpleWindow(at=at, values={"ct_l1": 100.0})
