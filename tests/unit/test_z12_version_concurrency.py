"""Z12 — version-number allocation is atomic under concurrency.

Root cause reproduced here: ``propose_mapping``/``propose_topology`` used to read
the current versions, compute ``max(number) + 1`` and INSERT in separate steps.
Any concurrent writer (the loop thread vs. the background executor that runs
the Z9 matrix reconciler on a version-change notification) could interleave
between the read and the insert, so both computed the same ``map-<n>`` and one
died with ``UNIQUE constraint failed: mapping_versions.version_id``.  The read
itself also ran on the shared connection without the writer lock and could
come back empty while another thread used the connection, yielding ``map-1``
(with the active ``map-1`` as its own parent).

* The deterministic tests park each proposer inside the allocation window --
  between computing the number and inserting the row (the injected clock is
  called exactly there, to stamp ``created_at``) -- with a barrier.  Without an
  atomic allocator both threads reach the barrier holding the same number.
  With it the second proposer cannot enter the window until the first has
  committed, so the barrier times out and both succeed with distinct numbers.
* The stress test runs concurrent propose+promote and lock-free reader threads
  and checks the version chain stays well-formed.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    MappingEdge,
    Observability,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.versioning import VersionService
from wattmosaic_engine.versioning.service import VersionConflictError

T0 = datetime(2026, 4, 2, tzinfo=UTC)
#: How long a proposer parked in the allocation window waits for its peer.  The
#: atomic allocator makes the peer wait on the lock instead, so this elapses.
WINDOW_TIMEOUT_S = 1.0


class _WindowClock:
    """A fixed clock that, once armed, parks each caller thread on a barrier once.

    The service reads the clock between allocating a version number and
    inserting the row, so a parked thread holds an allocated-but-uninserted
    number -- the exact window of the Z12 race.
    """

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=WINDOW_TIMEOUT_S)
        self.armed = False
        self._seen: set[int] = set()
        self._guard = threading.Lock()

    def __call__(self) -> datetime:
        if self.armed:
            ident = threading.get_ident()
            with self._guard:
                first = ident not in self._seen
                self._seen.add(ident)
            if first:
                # A broken barrier means the peer is (correctly) blocked outside
                # the window by the atomic allocator.
                with contextlib.suppress(threading.BrokenBarrierError):
                    self.barrier.wait()
        return T0


def _topo_edge(edge_id: str, parent: str, child: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id, relation=RelationType.CONTAINS, source_endpoint_id=parent,
        target_endpoint_id=child, lifecycle=EdgeLifecycleV3.CONFIRMED, declared_by="user",
        beta=1.0, beta_basis="unity", evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=T0, pinned=True,
    )


def _map_edge(edge_id: str, subject: str, endpoint: str) -> MappingEdge:
    return MappingEdge(
        mapping_edge_id=edge_id, subject_kind="device", subject_id=subject, endpoint_id=endpoint,
        observability=Observability.DIRECT, lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user", evidence=EdgeEvidence(support=8, independent_causes=2),
        valid_from=T0,
    )


def _service(db: Database, clock: Callable[[], datetime]) -> VersionService:
    return VersionService(
        topology_repo=db.repos.topology_v3,
        mapping_repo=db.repos.mapping,
        clock=clock,
        model_set_id="mset-z12",
    )


def _run_parallel(targets: list[Callable[[], Any]]) -> tuple[list[Any], list[BaseException]]:
    results: list[Any] = [None] * len(targets)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(index: int, fn: Callable[[], Any]) -> None:
        try:
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(i, fn), name=f"z12-{i}")
        for i, fn in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "proposer deadlocked"
    return results, errors


def _mapping_rows(db: Database) -> list[dict[str, Any]]:
    with db._lock:
        rows = db._conn.execute(
            "SELECT version_id, number, parent_version_id, status FROM mapping_versions "
            "ORDER BY number"
        ).fetchall()
    return [dict(row) for row in rows]


def _topology_rows(db: Database) -> list[dict[str, Any]]:
    with db._lock:
        rows = db._conn.execute(
            "SELECT version_id, number, parent_version_id, status FROM topology_versions_v3 "
            "ORDER BY number"
        ).fetchall()
    return [dict(row) for row in rows]


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "z12.db")
    try:
        yield database
    finally:
        database.close()


# --------------------------------------------------------------------------- #
# Deterministic interleavings (threads + barrier in the allocation window).
# --------------------------------------------------------------------------- #


def test_concurrent_mapping_proposals_get_distinct_numbers(db: Database) -> None:
    clock = _WindowClock(parties=2)
    svc = _service(db, clock)
    before = {row["version_id"] for row in _mapping_rows(db)}
    clock.armed = True

    results, errors = _run_parallel(
        [
            lambda: svc.propose_mapping([_map_edge("me-a", "kettle", "ckt_a")], None),
            lambda: svc.propose_mapping([_map_edge("me-b", "toaster", "ckt_b")], None),
        ]
    )

    assert errors == []
    assert len(set(results)) == 2
    rows = _mapping_rows(db)
    new = [row for row in rows if row["version_id"] not in before]
    assert sorted(row["version_id"] for row in new) == sorted(results)
    numbers = [row["number"] for row in rows]
    assert numbers == sorted(set(numbers))
    for row in new:
        assert row["parent_version_id"] != row["version_id"]
        assert row["version_id"] == f"map-{row['number']}"


def test_concurrent_topology_proposals_get_distinct_numbers(db: Database) -> None:
    clock = _WindowClock(parties=2)
    svc = _service(db, clock)
    before = {row["version_id"] for row in _topology_rows(db)}
    clock.armed = True

    results, errors = _run_parallel(
        [
            lambda: svc.propose_topology([_topo_edge("te-a", "main", "ckt_a")], None),
            lambda: svc.propose_topology([_topo_edge("te-b", "main", "ckt_b")], None),
        ]
    )

    assert errors == []
    assert len(set(results)) == 2
    rows = _topology_rows(db)
    new = [row for row in rows if row["version_id"] not in before]
    assert sorted(row["version_id"] for row in new) == sorted(results)
    numbers = [row["number"] for row in rows]
    assert numbers == sorted(set(numbers))
    for row in new:
        assert row["parent_version_id"] != row["version_id"]


def test_concurrent_rollback_and_propose_do_not_collide(db: Database) -> None:
    svc = _service(db, lambda: T0)
    first = svc.propose_mapping([_map_edge("me-a", "kettle", "ckt_a")], None)
    svc.promote_mapping(first, "seed")

    clock = _WindowClock(parties=2)
    svc._clock = clock  # same service, now parking callers in the window
    clock.armed = True
    results, errors = _run_parallel(
        [
            lambda: svc.rollback("mapping", "map-1"),
            lambda: svc.propose_mapping([_map_edge("me-b", "toaster", "ckt_b")], None),
        ]
    )

    assert errors == []
    assert len(set(results)) == 2
    rows = _mapping_rows(db)
    assert [row["number"] for row in rows] == list(range(1, len(rows) + 1))
    assert sum(1 for row in rows if row["status"] == "active") == 1


def test_proposal_on_an_empty_table_is_not_its_own_parent(db: Database) -> None:
    """The ``"map-1"`` read fallback must never become a stored parent link."""
    with db._lock:
        db._conn.execute("DELETE FROM mapping_versions")
    svc = _service(db, lambda: T0)

    version_id = svc.propose_mapping([_map_edge("me-a", "kettle", "ckt_a")], None)

    assert version_id == "map-1"
    (row,) = _mapping_rows(db)
    assert row["parent_version_id"] is None
    svc.promote_mapping(version_id, "first")
    assert svc.current().mapping_version_id == "map-1"


# --------------------------------------------------------------------------- #
# Stress: concurrent propose + promote with concurrent readers.
# --------------------------------------------------------------------------- #


def test_stress_concurrent_propose_promote_and_reads(db: Database) -> None:
    svc = _service(db, lambda: T0)
    rounds, writers = 25, 4
    stop = threading.Event()
    read_failures: list[str] = []
    promoted: list[str] = []
    conflicts: list[str] = []
    guard = threading.Lock()

    def writer(index: int) -> None:
        for step in range(rounds):
            if index % 2 == 0:
                version = svc.propose_mapping(
                    [_map_edge(f"me-{index}-{step}", f"dev{index}", f"ckt{step % 3}")], None
                )
                promote = svc.promote_mapping
            else:
                version = svc.propose_topology(
                    [_topo_edge(f"te-{index}-{step}", "main", f"ckt{step % 3}")], None
                )
                promote = svc.promote_topology
            try:
                promote(version, "stress")
            except VersionConflictError:
                with guard:
                    conflicts.append(version)
            else:
                with guard:
                    promoted.append(version)

    def reader() -> None:
        # The Z9 reconciler's read path: it must never observe an empty table.
        while not stop.is_set():
            rows = svc._mapping_versions()
            if not rows:
                read_failures.append("empty mapping_versions read")
            svc.current()

    readers = [threading.Thread(target=reader, name=f"z12-reader-{i}") for i in range(2)]
    for thread in readers:
        thread.start()
    try:
        _, errors = _run_parallel([lambda i=i: writer(i) for i in range(writers)])
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=30)

    assert errors == []
    assert read_failures == []
    assert promoted  # progress was made; conflicts are the documented outcome
    for rows, prefix in ((_mapping_rows(db), "map"), (_topology_rows(db), "topo")):
        numbers = [row["number"] for row in rows]
        assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
        ids = {row["version_id"] for row in rows}
        assert all(row["version_id"] == f"{prefix}-{row['number']}" for row in rows)
        assert sum(1 for row in rows if row["status"] == "active") == 1
        for row in rows:
            assert row["parent_version_id"] != row["version_id"]
            assert row["parent_version_id"] is None or row["parent_version_id"] in ids
    expected_new = writers // 2 * rounds
    assert len(_mapping_rows(db)) >= expected_new
    assert len(_topology_rows(db)) >= expected_new
