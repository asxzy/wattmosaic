"""AT-20 — UI explains source and uncertainty of every displayed estimate (HB §21).

Closes: U2. For every rendered number in the panel model there is a status text,
identifiability and a provenance drill; HA attributes carry status, bounds and
versions (HB §18).

This requirement is CLOSED by U2: it carries no xfail marker and holds the real,
passing assertions that execute the shipped panel ES modules under node through
``tests/unit/panel_harness.py``.  The HB §17 reference-house matrix payload is
the deterministic fixture (the same K05 shape GET /api/v1/matrix publishes), and
``panel_model`` runs U2's real ``v3ViewModel`` from ``view-overview.js``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.panel_harness import DOM_SHIM, lit, node_path, run_esm

CLOSED_BY = "U2"

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "matrix" / "hb17_payload.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    """The deterministic HB §17 matrix payload (K05 shape)."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def panel_model():
    """Run U2's real v3ViewModel over the payload via the panel ESM harness."""

    def _model(matrix_payload: dict) -> dict:
        if not node_path():
            pytest.skip("node is not installed; the panel does not need it at runtime")
        program = (
            "import { v3ViewModel } from './view-overview.js';\n"
            f"const d = {lit(matrix_payload)};\n"
            "console.log(JSON.stringify(v3ViewModel(d)));\n"
        )
        return run_esm(program, shim=DOM_SHIM)

    return _model


def test_every_rendered_number_has_status_and_provenance(payload, panel_model) -> None:
    """No naked number: each displayed estimate is explained and drillable."""
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
