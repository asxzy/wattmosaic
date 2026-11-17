# TASK-TEST-002 — API surface, replay and migration harnesses

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-TEST-002`
**Title.** API surface, replay and migration harnesses
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TEST-002`, `REQ-TEST-005`, `REQ-TEST-008`

**Description.**
Prove the new HTTP surface follows the contract, that a stored window replays
through the production path, and that every prior schema version migrates
without losing a row.

**Expected output.**
- A new route returns the contract header, rejects a bad token with `401`, and returns `400` for a validation failure.
- A stored window replays identically through the production path.
- Every prior schema version migrates with no row loss.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `tests/integration/` | the HTTP surface tests |
| `tests/end_to_end/` | the whole-cycle tests over persisted state |
| `tests/simulation/` | the replay tests |
| `engine/wattmosaic_engine/api/facade.py` | `NullFacade` must answer every new method |
| `tools/audit/replay_capture.py` | the replay harness |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The additive routes | the task that owns each route | the contract header, the token compare, `400` |
| The migration set | `TASK-STORAGE-003` | forward-only, additive, idempotent |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The facade method set | `api/server.py` | every new method has a `NullFacade` answer |
| The replay result | `tests/simulation/test_replay_capture.py` | identical to the original run |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TEST-001` | the verification vehicle and the gate surface |

**Acceptance criteria.**
- [ ] A new route returns the contract header.
- [ ] A bad token is rejected with `401`.
- [ ] A validation failure returns `400`, never `422`.
- [ ] A stored window replays identically through the production path.
- [ ] Every prior schema version migrates with no row loss.
- [ ] `NullFacade` answers for every new facade method, so a missing backend does not `500`.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `IT-engine-api-contract::test_new_routes_follow_the_contract` (new) | `pytest tests/integration` | the header, `401` and `400` |
| `SIM-replay-capture::test_replay_matches_the_original_run` (new) | `pytest tests/simulation` | the replay matches |
| `UT-migrations::test_every_prior_version_migrates_without_row_loss` (new) | `pytest tests/unit` | no row is lost |

**Builder assignment.**
Unassigned. Wave 4, segment 4.1. Write owner of `tests/integration/`,
`tests/end_to_end/` and `tools/audit/replay_capture.py` for wave 4.

**Blockers.**
- `OD-36` — nine live-capture tests skip without a local `WATTMOSAIC_REAL_DB`
  and never run in CI. **Assumption:** the skip is recorded in the release
  note with its reason; it is not deleted to make CI green.
