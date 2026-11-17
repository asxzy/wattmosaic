# TASK-CHANGE-002 — Four version identifiers, immutable history, rollback and reconciliation

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-CHANGE-002`
**Title.** Four version identifiers, immutable history, rollback and reconciliation
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-CHANGE-003`, `REQ-CHANGE-004`, `REQ-CHANGE-006`, `REQ-CHANGE-007`

**Description.**
Make the four version identifiers travel with every derived artefact and its
validity interval, keep the history immutable, allow a rollback that creates a
new version, and reconcile a change against the inference history.

**Expected output.**
- Every derived artefact names all four version identifiers with validity intervals.
- A previous version stays byte-identical and readable.
- A restore creates a new version whose parent is the version in force.
- A change response lists the affected device and record counts and rewrites no record.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | the four version identifiers |
| `engine/wattmosaic_engine/electrical/topology.py` | the version store, the rollback |
| `engine/wattmosaic_engine/storage/database.py` | the version columns, the immutable history |
| `engine/wattmosaic_engine/api/server.py` | the change reconciliation route |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The versioned topology | `TASK-TOPOLOGY-003` | the version, the diff, the parent |
| The configuration event | `TASK-CHANGE-001` | the change being reconciled |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The four version identifiers | every derived artefact, the API | with validity intervals |
| The change reconciliation | `GET /changes` | affected counts, no rewrite |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-003` | the versioned topology and the diff |
| `TASK-CHANGE-001` | the configuration event being reconciled |

**Acceptance criteria.**
- [ ] Every derived artefact names all four version identifiers with validity intervals.
- [ ] A previous version stays byte-identical and readable.
- [ ] A restore creates a new version whose parent is the version in force.
- [ ] A change response lists the affected device and record counts.
- [ ] No record is rewritten by a reconciliation.
- [ ] The version identifiers keep the existing integer type.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-inference-records::test_four_version_identifiers_are_present` (new) | `pytest tests/unit` | all four are present |
| `T11`, `T12` · `UT-topology-persistence::test_previous_version_stays_byte_identical` | scenarios | the history is immutable |
| `UT-topology-persistence::test_rollback_creates_a_new_version` (new) | `pytest tests/unit` | the rollback creates a version |
| `T12` · `UT-inference-records::test_change_lists_the_affected_records` | scenario `T12` | the counts are listed |

**Builder assignment.**
Unassigned. Wave 3, segment 3.3. Write owner of `electrical/topology.py` for
wave 3.

**Blockers.**
- The `contracts.py` and `api/server.py` writes are **write requests** to
  `TASK-CONFIDENCE-001` and `TASK-MAPPING-001`, the wave-3 owners. Not
  blockers.
