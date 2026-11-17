# TASK-CHANGE-001 — Configuration events and the hypothesis lifecycle

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-CHANGE-001`
**Title.** Configuration events and the hypothesis lifecycle
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-CHANGE-001`, `REQ-CHANGE-002`, `REQ-CHANGE-005`

**Description.**
Emit a named, traceable event for each of the eight configuration change
kinds, keep a suspected change a hypothesis until it is validated, and
preserve the uncertainty when the evidence is insufficient.

**Expected output.**
- Each of the eight change kinds emits a named event with before/after state, timestamp and resulting version.
- A suspected change leaves the live configuration untouched until validated.
- An unresolved change publishes the missing evidence and applies no silent default.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/audit.py` | the event emission, the hypothesis lifecycle |
| `engine/wattmosaic_engine/storage/migrations.py` | the configuration-event table |
| `engine/wattmosaic_engine/contracts.py` | the configuration-event model |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The typed relations | `TASK-TOPOLOGY-001` | the before/after state |
| The storage schema | `TASK-STORAGE-002` | the table and the version columns |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The configuration event | `GET /changes`, the panel `#/topology` | additive route and a new table |
| The hypothesis | `disaggregation/audit.py` | not applied until validated |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-001` | the typed relations the change is about |
| `TASK-STORAGE-002` | the schema and the version columns |

**Acceptance criteria.**
- [ ] Each of the eight change kinds emits a named event with before/after state, timestamp and resulting version.
- [ ] A suspected change leaves the live configuration untouched until validated.
- [ ] An unresolved change publishes the missing evidence.
- [ ] No silent default is applied when the evidence is insufficient.
- [ ] The configuration event is a distinct artefact, not a relation kind.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T11`, `T12` · `UT-topology-persistence::test_configuration_event_per_change_kind` (new) | scenarios | all eight kinds emit |
| `T11`, `T12` · `UT-hierarchy::test_suspected_change_does_not_alter_the_active_topology` (new) | scenarios | the live configuration is untouched |
| `UT-hierarchy::test_refused_with_a_reason_when_evidence_is_insufficient` (new) | `pytest tests/unit` | the missing evidence is named |

**Builder assignment.**
Unassigned. Wave 3, segment 3.2. Write owner of the configuration-event
emission in `disaggregation/audit.py` for wave 3.

**Blockers.**
- The `contracts.py` and `storage/migrations.py` writes are **write requests**
  to `TASK-CONFIDENCE-001` and `TASK-STORAGE-002`, the wave-3 owners. Not
  blockers.
