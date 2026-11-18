"""AT-20 — UI explains source and uncertainty of every displayed estimate (HB §21).

Closes: U2. For every rendered number in the panel model there is a status text,
identifiability and a provenance drill; HA attributes carry status, bounds and
versions (HB §18).
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by U2"),
]


def test_every_rendered_number_has_status_and_provenance(hb17, panel_model) -> None:
    """No naked number: each displayed estimate is explained and drillable."""
    from wattmosaic_engine.mapping import project_matrix_payload  # target surface (U2)

    payload = project_matrix_payload(hb17["observation"])
    model = panel_model(payload)

    rendered = model["rendered_numbers"]
    assert rendered, "the panel rendered no numbers"
    for item in rendered:
        assert item["status_text"], f"missing status for {item['id']}"
        assert item["identifiability"], f"missing identifiability for {item['id']}"
        assert item["bounds"] is not None, f"missing bounds for {item['id']}"
        assert item["provenance_drill"], f"missing provenance drill for {item['id']}"

    ha = model["ha_attributes"]
    for attrs in ha.values():
        assert "status" in attrs and "identifiability" in attrs
        assert "lower_w" in attrs and "upper_w" in attrs
        for key in ("topology_version_id", "mapping_version_id", "model_version_id"):
            assert key in attrs
