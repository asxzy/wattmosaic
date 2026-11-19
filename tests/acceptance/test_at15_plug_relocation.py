"""AT-15 — smart-plug relocation changes mappings without relearning power (HB §21).

Closes: T7. After the move: mapping version +1, old mapping edge RETIRED with
valid_until, new edge PROVISIONAL→CONFIRMED, and the TV power-model version id
is unchanged.
"""

from __future__ import annotations

import pytest

CLOSED_BY = "T7"

pytestmark = [pytest.mark.acceptance]


def test_plug_move_versions_mapping_not_model(hb17) -> None:
    """The plug move is a topology/mapping event, never a model change."""
    from wattmosaic_engine.contracts import EdgeLifecycleV3
    from wattmosaic_engine.versioning import VersionService  # target surface (T7/D1)

    svc = VersionService.from_hb17(hb17["observation"])
    before = svc.current()
    model_version_before = svc.model_version_id("tv")

    # HB §17 step 9: the plug moves in truth only — model/HA entity unchanged.
    assert hb17["house"].truth["true_circuit"]["tv"] == "ct_c"
    svc.apply_configuration_event(
        kind="smart_plug_relocation", subject="tv", moved_to="ct_c"
    )
    after = svc.current()

    assert int(after.mapping_version_id.split("-")[-1]) == int(
        before.mapping_version_id.split("-")[-1]
    ) + 1
    assert after.topology_version_id == before.topology_version_id

    old_edge = svc.mapping_edge("tv", "ct_a")
    assert old_edge.lifecycle == EdgeLifecycleV3.RETIRED
    assert old_edge.valid_until is not None

    new_edge = svc.mapping_edge("tv", "ct_c")
    assert new_edge.lifecycle in (
        EdgeLifecycleV3.PROVISIONAL,
        EdgeLifecycleV3.CONFIRMED,
    )

    assert svc.model_version_id("tv") == model_version_before
