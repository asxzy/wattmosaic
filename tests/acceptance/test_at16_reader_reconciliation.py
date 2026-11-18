"""AT-16 — reader relocation or calibration change triggers reconciliation (HB §21).

Closes: T7. A CT polarity flip → SUSPECTED ConfigurationEvent → affected cells
SUSPECTED_CHANGE → new topology version after N events → prior version readable.
A calibration change is not retroactive.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by T7"),
]


def test_polarity_flip_reconciles_and_calibration_is_not_retroactive(hb17) -> None:
    """Declared hardware changes create new versions, never rewrite history."""
    from wattmosaic_engine.contracts import ChangeStatus, MappingStatus
    from wattmosaic_engine.versioning import VersionService  # target surface (T7)

    svc = VersionService.from_hb17(hb17["observation"])
    before = svc.current()

    ev = svc.declare_ct_direction_change("ct_c", polarity="inverted")
    assert ev.status == ChangeStatus.SUSPECTED
    cells = svc.affected_cells(ev.event_id)
    assert cells and all(c.mapping.mapping_status == MappingStatus.SUSPECTED_CHANGE for c in cells)

    for _ in range(3):
        svc.observe_confirming_event(ev.event_id)
    after = svc.current()
    assert after.topology_version_id != before.topology_version_id
    assert svc.as_of(before.topology_version_id) is not None

    prior_value = svc.snapshot_value_at("ct_c", at=svc.origin)
    svc.declare_calibration_change("ct_c", multiplier=1.05)
    assert svc.snapshot_value_at("ct_c", at=svc.origin) == prior_value
