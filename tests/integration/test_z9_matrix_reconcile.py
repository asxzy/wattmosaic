"""Z9 — matrix cells are reconciled on a mapping promotion (HB §17 step 10).

A plug relocation promotes a new mapping version that drops the old
``subject -> reader`` edge.  Before Z9 the reconciler was never called from the
runtime (and its ``.values()`` on a tuple meant it could never see an edge), so
the retired subject x reader cells stayed live in the ``/matrix`` payload.  This
test drives the real three-loop runtime with non-HB17 names and asserts:

* the current matrix payload has no ``dishwasher x kitchen_ckt`` cell after the
  promotion;
* that cell is still present in the append-only history (provenance);
* the ``dishwasher x laundry_ckt`` cell exists after the next inference result;
* a rollback (a new version copying the old edge set) restores the old cell;
* the HTTP ``GET /api/v1/matrix`` surface serves the same current view.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.core.mapping_api import MappingApiMixin
from wattmosaic_engine.storage.database import Database

from tests.support.hb17_seed import start_hb17_harness

T0 = datetime(2026, 5, 1, tzinfo=UTC)
FEED = "feed_l1"
KITCHEN = "kitchen_ckt"
LAUNDRY = "laundry_ckt"
DISHWASHER = "dishwasher"
MOVE_AT = 800.0  # seconds into the synthetic stream
_STEP = 10


def _records(count: int = 150):
    """A synthetic stream: the dishwasher toggles on KITCHEN, then on LAUNDRY."""
    for index in range(count):
        t = _STEP * (index + 1)
        on = 1 <= (index % 20) <= 8
        watts = 1775.0 if on else 2.0
        moved = t >= MOVE_AT
        values = {
            FEED: 300.0 + watts,
            KITCHEN: 300.0 if moved else watts,
            LAUNDRY: watts if moved else 300.0,
        }
        stamp = (T0 + timedelta(seconds=t)).isoformat()
        observed = (T0 + timedelta(seconds=t - 5)).isoformat()
        received = (T0 + timedelta(seconds=t - 5, milliseconds=-50)).isoformat()
        samples = {
            reader: {
                "reader_id": reader,
                "entity_id": f"sensor.{reader}_power",
                "value_w": value,
                "value": value,
                "state_timestamp": observed,
                "received_at": received,
                "last_reported": observed,
                "measurement_window_ms": 1000.0,
                "quality": "good",
                "sequence": index,
            }
            for reader, value in values.items()
        }
        yield {"timestamp": stamp, "t_s": t, "ha_messages": [], "reader_samples": samples}


class _MatrixFacade(MappingApiMixin):
    def __init__(self, db: Database) -> None:
        self.db = db
        self._clock = lambda: T0  # noqa: E731 - deterministic, injected


def _cells(core: Any) -> set[tuple[str, str]]:
    return {
        (cell.row_id, cell.reader_id)
        for cell in core.matrix_payload().cells
        if cell.row_kind == "device"
    }


def _history_for(core: Any, reader: str) -> list[Any]:
    return list(core.db.repos.mapping.cell_history(("device", DISHWASHER), reader))


class _Relocated:
    def __init__(self) -> None:
        observation = list(_records())
        self.harness = start_hb17_harness(
            {"house": None, "observation": observation},
            devices={DISHWASHER: KITCHEN},
            parents={KITCHEN: FEED, LAUNDRY: FEED},
            run=False,
        )
        self.core = self.harness.core
        self.harness.run([record for record in observation if float(record["t_s"]) < MOVE_AT])
        self.old_mapping_version = self.core.version_service.current().mapping_version_id
        self.before_cells = _cells(self.core)
        self.old_history_len = len(_history_for(self.core, KITCHEN))

        self.core.version_service.apply_configuration_event(
            kind="smart_plug_relocation", subject=DISHWASHER, moved_to=LAUNDRY
        )
        self.new_mapping_version = self.core.version_service.current().mapping_version_id
        self.harness.run([record for record in observation if float(record["t_s"]) >= MOVE_AT])
        self.harness.settle()
        self.after_cells = _cells(self.core)

    def close(self) -> None:
        self.harness.close()


@pytest.fixture(scope="module")
def relocated():
    state = _Relocated()
    yield state
    state.close()


def test_promotion_retires_the_old_cell_from_the_current_matrix(relocated: _Relocated) -> None:
    assert relocated.new_mapping_version != relocated.old_mapping_version
    assert (DISHWASHER, KITCHEN) in relocated.before_cells
    current = _cells(relocated.core)
    assert (DISHWASHER, KITCHEN) not in current, "the retired cell must not appear in /matrix"


def test_the_retired_cell_stays_in_history_and_provenance(relocated: _Relocated) -> None:
    history = _history_for(relocated.core, KITCHEN)
    assert len(history) > relocated.old_history_len
    assert history[-1]["mapping_status"] == "retired"
    # The old observation is never rewritten.
    assert any(row["mapping_status"] == "confirmed" for row in history[:-1])


def test_the_new_cell_appears_after_the_next_result(relocated: _Relocated) -> None:
    assert (DISHWASHER, LAUNDRY) in relocated.after_cells


def test_http_matrix_payload_agrees_with_the_current_store(relocated: _Relocated) -> None:
    facade = _MatrixFacade(relocated.core.db)

    async def check(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/matrix", headers={"X-API-Token": "z9-token"}) as response:
            assert response.status == 200
            payload = await response.json()
        cells = {
            (cell["row_id"], cell["reader_id"])
            for cell in payload["cells"]
            if cell["row_kind"] == "device"
        }
        assert (DISHWASHER, KITCHEN) not in cells
        assert (DISHWASHER, LAUNDRY) in cells

    async def run() -> None:
        server = APIServer(facade, host="127.0.0.1", port=0, api_token="z9-token")
        await server.start()
        try:
            async with aiohttp.ClientSession() as session:
                await check(session, f"http://127.0.0.1:{server.port}/api/v1")
        finally:
            await server.stop()

    asyncio.run(run())


def test_rollback_restores_the_old_cell(relocated: _Relocated) -> None:
    core = relocated.core
    versions = core.version_service
    # The pre-promotion version (the user's declared dishwasher -> kitchen edge).
    old_id = relocated.old_mapping_version
    assert old_id == "map-2"
    # A rollback copies the old version's edge set into a NEW active version.
    versions.rollback("mapping", old_id)
    core.drain_background()

    current = _cells(core)
    assert (DISHWASHER, KITCHEN) in current, "rollback restores the retired cell"
    # The history keeps both the retirement and the restoration (append-only).
    history = _history_for(core, KITCHEN)
    assert any(row["mapping_status"] == "retired" for row in history)
    assert history[-1]["mapping_status"] == "confirmed"


def test_reconciler_ignores_non_mapping_events(relocated: _Relocated) -> None:
    """A topology-only promotion never retires a mapping cell on its own."""
    core = relocated.core
    before = len(core.matrix_reconciler.store.audit_log)
    versions = core.version_service
    topo = versions.current().topology_version_id
    # Re-promoting the active topology is idempotent and changes no matrix cell.
    versions.promote_topology(topo, "no-op")
    core.drain_background()
    assert len(core.matrix_reconciler.store.audit_log) == before
