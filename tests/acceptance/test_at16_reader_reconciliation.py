"""AT-16 — reader relocation or calibration change triggers reconciliation (HB §21).

Closes: T7. A CT polarity flip → SUSPECTED ConfigurationEvent → affected cells
SUSPECTED_CHANGE → new topology version after N events → prior version readable.
A calibration change is not retroactive.

The house runs through the live runtime (``engine_harness``): the user's
declared topology/mapping is seeded through D1 (``tests/support/hb17_seed``),
the TV's post-relocation cycles on Circuit C produce the real M2 cells the
polarity flip marks, and the calibration check reads the snapshots the runtime
itself stored before and after the declaration.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial

import pytest

CLOSED_BY = "T7"

pytestmark = [pytest.mark.acceptance]


def test_polarity_flip_reconciles_and_calibration_is_not_retroactive(
    hb17_relocated, engine_harness
) -> None:
    """Declared hardware changes create new versions, never rewrite history."""
    from wattmosaic_engine.contracts import ChangeStatus, MappingStatus

    from tests.support.hb17_seed import stored_reading_at

    house = hb17_relocated["house"]
    observation = hb17_relocated["observation"]

    def link_tv(core) -> None:
        # The user's HA link: the plug's switch controls the TV.
        core.db.repos.entities.add_entity_link(
            {"entity_id": "switch.tv_plug", "target_kind": "device", "target_id": "tv",
             "role": "state", "valid_from": house.start_time - timedelta(days=1),
             "declared_by": "user"}
        )

    # The TV on Circuit C, where AT-15's step-9 relocation put it.
    harness = engine_harness(hb17_relocated, devices={"tv": "ct_c"}, configure=link_tv, run=False)
    cut = house.step_at(9).at_s + 1000.0  # three post-move TV cycles
    harness.run([r for r in observation if float(r["t_s"]) < cut])
    svc = harness.core.version_service
    origin = harness.now
    snapshot_value_at = partial(stored_reading_at, harness.db)

    try:
        before = svc.current()

        ev = svc.declare_ct_direction_change("ct_c", polarity="inverted")
        assert ev.status == ChangeStatus.SUSPECTED
        cells = svc.affected_cells(ev.event_id)
        assert cells and all(
            c.mapping.mapping_status == MappingStatus.SUSPECTED_CHANGE for c in cells
        )

        for _ in range(3):
            svc.observe_confirming_event(ev.event_id)
        after = svc.current()
        assert after.topology_version_id != before.topology_version_id
        assert svc.as_of(before.topology_version_id) is not None

        prior_value = snapshot_value_at("ct_c", at=origin)
        assert prior_value is not None  # a real stored reading, not None == None
        svc.declare_calibration_change("ct_c", multiplier=1.05)
        # The runtime keeps running and storing snapshots after the declaration.
        harness.run([r for r in observation if float(r["t_s"]) >= cut])
        assert harness.db.repos.inference.latest_snapshot()["observed_at"] > origin.isoformat()
        assert snapshot_value_at("ct_c", at=origin) == prior_value
    finally:
        harness.close()
