"""Z7 — a declared plug relocation takes effect in the live runtime.

HB §17 relocated-TV house (``relocated_tv`` variant, 15-minute lead-in) through
the real three-loop runtime, seeded with the user's declared topology/mapping
(``tests/support/hb17_seed``; the TV and the lights on Circuit A).  At step 9
the user declares the move on the runtime's own D1 service:
``apply_configuration_event(kind="smart_plug_relocation", subject="tv",
moved_to="ct_c")``.

Before Z7 a group opened before the declaration never closed (P3 re-emitted
the TV's OFF step every confirmation period, chaining one group for the rest
of the stream) and kept being re-inferred under the superseded mapping: every
later event result was stamped with the old mapping version, the TV and the
lights were published at about -1.94 W on Circuit A / Subpanel / Main, and no
TV x Circuit C cell ever appeared.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import EventGroupStatus

from tests.support.hb17_seed import start_hb17_harness


def _version_number(version_id: str) -> int:
    return int(str(version_id).rsplit("-", 1)[-1])


class _Relocated:
    def __init__(self) -> None:
        from tools.simulator.hb17 import build

        self.house = house = build(17, variant="relocated_tv", minutes=60, lead_in_minutes=15)
        observation = house.observation_view()
        start = house.start_time

        def configure(core: Any) -> None:
            # The plug's switch controls the TV, and the plug is a declared
            # single-subject endpoint for it (DEC-05).
            core.db.repos.entities.add_entity_link(
                {"entity_id": "switch.tv_plug", "target_kind": "device", "target_id": "tv",
                 "role": "state", "valid_from": start - timedelta(days=1), "declared_by": "user"}
            )
            core.db.repos.entities.upsert_endpoint(
                {"endpoint_id": "ct_plug", "kind": "smart_plug", "entity_id": "sensor.tv_plug_power",
                 "scope": "single_subject", "scope_status": "confirmed", "scope_subject_id": "tv",
                 "enabled": True, "created_at": start - timedelta(days=1)}
            )

        self.harness = harness = start_hb17_harness(
            {"house": house, "observation": observation}, configure=configure, run=False
        )
        self.core = core = harness.core
        self.negative_snapshot: list[tuple[str, float, str]] = []
        self.negative_cells: list[tuple[str, str, float]] = []
        self.tv_on_c: list[float | None] = []

        def watch(_record: Any, _published: Any) -> None:
            # What a client reads after every record: the snapshot and the matrix.
            snapshot = harness.snapshot
            for estimate in snapshot.device_estimates if snapshot is not None else ():
                if estimate.point_w is not None and estimate.point_w < 0.0:
                    self.negative_snapshot.append(
                        (estimate.subject_id, estimate.point_w, estimate.versions.mapping_version_id)
                    )
            for cell in core.matrix_payload().cells:
                power = cell.power.estimated_power_w
                if cell.row_kind == "device" and power is not None and power < 0.0:
                    self.negative_cells.append((cell.row_id, cell.reader_id, power))
                if cell.row_kind == "device" and (cell.row_id, cell.reader_id) == ("tv", "ct_c"):
                    self.tv_on_c.append(power)

        move = house.step_at(9).at_s
        harness.run([r for r in observation if float(r["t_s"]) < move], watch)
        self.before = core.version_service.current()
        self.open_at_move = [
            g for g in core.grouping_engine.groups()
            if g.status not in (EventGroupStatus.CONFIRMED, EventGroupStatus.UNRESOLVED)
        ]
        self.n_before = len(harness.results)
        core.version_service.apply_configuration_event(
            kind="smart_plug_relocation", subject="tv", moved_to="ct_c"
        )
        self.declared = core.version_service.current()
        harness.run([r for r in observation if float(r["t_s"]) >= move], watch)
        harness.settle()
        self.after_results = harness.results[self.n_before:]


@pytest.fixture(scope="module")
def relocated():
    state = _Relocated()
    yield state
    state.harness.close()


def test_declaration_promotes_a_new_mapping_version(relocated: _Relocated) -> None:
    assert relocated.declared.mapping_version_id != relocated.before.mapping_version_id
    view = relocated.core.version_service.mapping_view(relocated.declared.mapping_version_id)
    assert view.most_specific_reader(("device", "tv")) == "ct_c"


def test_results_after_the_declaration_carry_the_new_mapping(relocated: _Relocated) -> None:
    declared = _version_number(relocated.declared.mapping_version_id)
    events = [r for r in relocated.after_results if r.event_group_id is not None]
    # The TV keeps toggling on Circuit C after the move: real event results.
    assert len(events) >= 6
    stale = [
        (r.event_group_id, r.versions.mapping_version_id)
        for r in relocated.after_results
        if _version_number(r.versions.mapping_version_id) < declared
    ]
    assert stale == []


def test_groups_open_at_the_declaration_are_closed_under_their_own_mapping(
    relocated: _Relocated,
) -> None:
    repo = relocated.core.db.repos.evidence
    for group in relocated.open_at_move:
        stored = repo.latest_group(group.group_id)
        assert stored is not None
        assert stored.versions.mapping_version_id == relocated.before.mapping_version_id
        assert stored.status in (EventGroupStatus.CONFIRMED, EventGroupStatus.UNRESOLVED)
        assert stored.revision == group.revision
    # No result after the declaration re-infers a pre-declaration group.
    closed = {g.group_id for g in relocated.open_at_move}
    assert not [r for r in relocated.after_results if r.event_group_id in closed]


def test_tv_cells_appear_on_circuit_c(relocated: _Relocated) -> None:
    assert relocated.tv_on_c, "a TV x Circuit C cell is published after the move"
    assert any(
        power is not None and power > 100.0 for power in relocated.tv_on_c
    ), "the TV's ~116 W ON steps on Circuit C reach its Circuit C cell"
    assert any(
        c.row_kind == "device" and (c.row_id, c.reader_id) == ("tv", "ct_c")
        for c in relocated.core.matrix_payload().cells
    )
    # The TV's post-move events are explained on Circuit C, not on Circuit A.
    tv_readers = {
        tuple(e.source_reader_ids)
        for r in relocated.after_results
        if r.event_group_id is not None
        for e in r.device_estimates
        if e.subject_id == "tv" and e.point_w is not None
    }
    assert tv_readers
    assert all("ct_c" in readers and "ct_a" not in readers for readers in tv_readers)


def test_no_negative_device_power_is_published(relocated: _Relocated) -> None:
    """HB sign rules: a consumer's published power is never negative."""
    assert relocated.negative_snapshot == []
    assert relocated.negative_cells == []
