# TASK-STORAGE-002 — Evidence immutability, batched writes, indexes and version identifiers

**Task ID.** `TASK-STORAGE-002`
**Title.** Evidence immutability, batched writes, indexes and version identifiers
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-STORAGE-003`, `REQ-STORAGE-004`, `REQ-STORAGE-005`, `REQ-STORAGE-006`, `REQ-STORAGE-007`

**Description.**
Make the L2 evidence append-only with strictly increasing revisions, batch the
writes, protect historical evidence from a version change, index the time and
entity access paths, and carry the four version identifiers on every record.

**Expected output.**
- L2 evidence rows are append-only, with a strictly increasing revision.
- One transaction per table per flush.
- No version deletes an earlier version's rows.
- The time and entity access paths use an index.
- Every record names all four versions.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/storage/migrations.py` | the columns, the indexes, the new numbered migration |
| `engine/wattmosaic_engine/storage/database.py` | the append-only guard, the batched writer |
| `engine/wattmosaic_engine/main.py` | the writer loop |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The tier registry | `TASK-STORAGE-001` | decides which tables are L2 evidence |
| The four version identifiers | `TASK-CHANGE-002` | the names and their types |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The appended evidence row | `api/server.py` | immutable, revisioned |
| The version columns | every derived artefact | all four present |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-STORAGE-001` | the tier registry and the retention policy |

**Acceptance criteria.**
- [ ] L2 evidence is append-only with strictly increasing revisions.
- [ ] A flush is one transaction per table.
- [ ] No version deletes an earlier version's rows.
- [ ] The time and the entity access paths use an index.
- [ ] Every record names all four version identifiers.
- [ ] The migration is forward-only, additive and idempotent.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-inference-records::test_evidence_rows_are_append_only` (new) | `pytest tests/unit` | an update is refused |
| `UT-health-probe-cost::test_flush_batches_one_transaction_per_table` (new) | `pytest tests/unit` | one transaction per table |
| `T10` · `UT-storage-config::test_audit_tables_are_exempt_from_prune` | scenario `T10` | the evidence survived |
| `UT-storage-config::test_query_plan_uses_an_index` (new) | `pytest tests/unit` | the plan uses the index |
| `UT-inference-records::test_record_carries_all_four_versions` (new) | `pytest tests/unit` | all four are present |

**Builder assignment.**
Unassigned. Wave 3, segment 3.1. Write owner of `storage/migrations.py`,
`storage/database.py` and `main.py` for wave 3.

**Blockers.**
- The four version identifier names are settled by `TASK-CHANGE-002`, which
  runs in wave 3 segment 3.3 — after this task. **Assumption:** the four
  identifiers follow the naming in `14-requirement-traceability.md` and the
  existing integer type. The orchestrator confirms the names before the
  migration is written.
