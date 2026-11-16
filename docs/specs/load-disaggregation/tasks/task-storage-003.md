# TASK-STORAGE-003 — Recovery, migration safety and replay

**Task ID.** `TASK-STORAGE-003`
**Title.** Recovery, migration safety and replay
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-STORAGE-010`, `REQ-STORAGE-011`, `REQ-STORAGE-012`

**Description.**
Make the engine recover cleanly after a restart, make every migration
forward-only, additive and idempotent, and make a stored window replay
identically under the same algorithm version.

**Expected output.**
- A restart preserves every record count and refuses to run on a corrupt file.
- Every migration applies twice with no effect the second time.
- A stored window replays to the same result under the same algorithm version.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/storage/migrations.py` | the forward-only migration set |
| `engine/wattmosaic_engine/storage/database.py` | the integrity check |
| `engine/wattmosaic_engine/main.py` | the startup recovery path |
| `tools/audit/replay_capture.py` | the replay harness |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The migration set | `TASK-STORAGE-002` | forward-only, additive, idempotent |
| The stored window | `storage/database.py` | the replay input |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The replay result | `tests/simulation/test_replay_capture.py` | identical to the original run |
| The integrity verdict | `main.py` | refuses to run on corruption |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-STORAGE-002` | the schema and the version columns must be in place |

**Acceptance criteria.**
- [ ] A restart preserves every record count.
- [ ] A corrupt database file is refused with a named reason, not a crash.
- [ ] Migrations are forward-only, additive and idempotent; applying twice is a no-op.
- [ ] A stored window replays identically under the same algorithm version.
- [ ] The replay records the algorithm version it ran under.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `E2E-storage::test_restart_preserves_every_record_count` (new) | `pytest tests/end_to_end` | the counts survive |
| `UT-migrations::test_migrations_are_idempotent` (new) | `pytest tests/unit` | the second apply changes nothing |
| `T12` · `SIM-replay-capture::test_replay_is_identical_under_the_same_algorithm_version` | scenario `T12` | the replay matches |

**Builder assignment.**
Unassigned. Wave 3, segment 3.6. Write owner of
`tools/audit/replay_capture.py` for wave 3.

**Blockers.**
- None.
