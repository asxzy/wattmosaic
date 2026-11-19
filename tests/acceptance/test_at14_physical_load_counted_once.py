"""AT-14 — one physical load visible to many Readers counted once (HB §21).

Closes: I5. The observable TV (a single-subject plug reporting 3 W standby in
HB §17 step 8) is seen by the plug, Circuit A, Subpanel and Main.  The snapshot
publishes the TV exactly once with ``MEASURED`` status, and the reconciliation
counts that one physical load once in ``identified_w`` — not four times from
the four rows that observe it.

The snapshot is the one the live runtime publishes (``engine_harness``: the
real fast/event/steady loops over the HB §17 observation stream).  The user's
declarations are the only configuration: the TV on its smart plug, the plug a
confirmed single-subject endpoint (DEC-05) plugged into Circuit A, and the
plug's HA switch linked to the TV.  No ground truth is read.

The plug's samples carry the simulator's sub-watt perturbation and the steady
loop publishes a MEASURED level when it first sees it (re-publishing only on a
>3 sigma move), so the replay starts at the first record whose plug sample
agrees with the plug's own HA-reported standby (``power_w_observation``) to the
report's display resolution; the published value is that real reading.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import EstimateStatus, InferenceMethod

CLOSED_BY = "I5"

#: The HA display precision of the plug's reported power attribute.
_REPORT_RESOLUTION_W = 0.01


def _declare_tv_on_plug(start: Any):
    def configure(core: Any) -> None:
        at = start - timedelta(days=1)
        core.db.repos.entities.upsert_endpoint(
            {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": "sensor.tv_plug_power",
             "scope": "single_subject", "scope_status": "confirmed", "scope_subject_id": "tv",
             "enabled": True, "created_at": at}
        )
        core.db.repos.entities.add_entity_link(
            {"entity_id": "switch.tv_plug", "target_kind": "device", "target_id": "tv",
             "role": "state", "valid_from": at, "declared_by": "user"}
        )

    return configure


def _published_snapshot(hb17: Any, engine_harness: Any) -> Any:
    """The snapshot the live runtime publishes for the HB §17 stream."""
    observation = hb17["observation"]
    reported = next(
        msg["attributes"]["power_w_observation"]
        for record in observation
        for msg in record["ha_messages"]
        if msg["entity_id"] == "switch.tv_plug" and msg.get("state") == "standby"
    )
    first = next(
        index
        for index, record in enumerate(observation)
        if abs(record["reader_samples"]["ct_plug"]["value_w"] - reported) <= _REPORT_RESOLUTION_W
    )
    stream = observation[first:]
    harness = engine_harness(
        {"house": hb17["house"], "observation": stream},
        devices={"tv": "ct_plug"},
        parents={"ct_plug": "ct_a"},  # the plug is plugged into Circuit A
        configure=_declare_tv_on_plug(hb17["house"].start_time),
        run=False,
    )
    try:
        harness.feed(stream[0])
        harness.heartbeat()  # the steady loop publishes the plug's reading
        harness.run(stream[1:])
        harness.settle()
        return harness.snapshot
    finally:
        harness.close()


def test_tv_counted_once_in_snapshot_reconciliation(hb17, engine_harness) -> None:
    """Four readers observing one TV still yield one physical contribution."""
    snapshot = _published_snapshot(hb17, engine_harness)
    tv_estimates = [e for e in snapshot.device_estimates if e.subject_id == "tv"]
    assert len(tv_estimates) == 1

    tv = tv_estimates[0]
    # MEASURED comes only from a confirmed single-subject endpoint (DEC-05):
    # the plug's 3 W standby, never collapsed to 0 (HB §17 step 8).
    assert tv.status == EstimateStatus.MEASURED
    assert tv.method == InferenceMethod.DIRECT_READING
    assert tv.point_w == pytest.approx(3.0, abs=0.01)
    assert tv.point_w > 0.0
    assert tv.source_reader_ids, "the TV must list at least one contributing reader"

    # The nested CTs observe the same physical load as EXPECTED entries, but
    # provenance never duplicates one reader and the reconciliation counts the
    # TV once, not once per observing reader.
    assert len(tv.source_reader_ids) == len(set(tv.source_reader_ids))
    recon_tv = snapshot.reconciliation.identified_w
    assert recon_tv is not None
    assert recon_tv == pytest.approx(3.0, abs=0.01)
    # Not 4× the plug reading (the DEF-04 / AT-14 double-count defect).
    assert recon_tv != pytest.approx(12.0)


def test_reconciliation_terms_use_null_semantics(hb17, engine_harness) -> None:
    """Absent terms are None, never 0.0; the identity is never fabricated."""
    snapshot = _published_snapshot(hb17, engine_harness)
    recon = snapshot.reconciliation
    # main and identified are real; group/unknown may be absent (HB §18).
    assert recon.main_w is not None
    assert recon.identified_w is not None
    if recon.group_w is None or recon.unknown_w is None:
        assert recon.balance_error_w is None
