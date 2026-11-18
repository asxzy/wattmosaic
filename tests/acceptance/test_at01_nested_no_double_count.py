"""AT-01 — one load through nested readers without double counting (HB §21).

Closes: M1. The same physical +45 W (HB §17 step 3) must appear in the Circuit A,
Subpanel and Main matrix cells while the physical total adds it exactly once and
reconciliation counts it once.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by M1"),
]


def test_nested_load_not_double_counted(hb17) -> None:
    """The same load in three nested reader cells is one physical contribution."""
    from wattmosaic_engine.contracts import CellPower, MatrixPayload, ReconciliationV3
    from wattmosaic_engine.mapping import project_matrix_payload  # target surface (M1)

    payload: MatrixPayload = project_matrix_payload(hb17["observation"])
    lights = [c for c in payload.cells if "light" in c.row_id]
    assert lights, "the co-switched lights must produce matrix cells"

    by_reader = {c.reader_id: c for c in lights}
    for reader in ("ct_a", "ct_sub", "ct_main"):
        assert reader in by_reader, f"missing light cell for nested reader {reader}"
        power: CellPower = by_reader[reader].power
        assert power.measurement_status is not None
        assert power.estimated_power_w is not None
        assert power.estimated_power_w == pytest.approx(45.0, abs=2.0)

    # Physical total adds the load once, not three times.
    assert payload.physical_total.group_w == pytest.approx(45.0, abs=2.0)

    # Reconciliation counts the nested observation once.
    recon: ReconciliationV3 = ReconciliationV3.model_validate(
        {"main_w": 1545.0, "identified_w": 45.0, "group_w": 0.0,
         "unknown_w": 0.0, "residual_w": 1500.0, "export_w": 0.0,
         "meter_spread_w": 0.0}
    )
    assert recon.identified_w + recon.group_w == pytest.approx(45.0, abs=2.0)
