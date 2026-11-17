# TASK-TOPOLOGY-003 — Versioned topology, change detection and reconciliation

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-TOPOLOGY-003`
**Title.** Versioned topology, change detection and reconciliation
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TOPO-008`, `REQ-TOPO-009`

**Description.**
Make the topology a versioned object with an identifier, a time, a status, a
parent and a diff; detect a change, state it, reconcile it, and keep the
version every stored record was produced under.

**Expected output.**
- A topology version carrying an identifier, a time, a status, a parent and a diff.
- A change detected, stated and reconciled.
- Every stored record keeps the version it was produced under.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/electrical/topology.py` | `TopologyGraph` and the version store |
| `engine/wattmosaic_engine/storage/database.py` | the version rows |
| `engine/wattmosaic_engine/disaggregation/audit.py` | the change detection and reconciliation |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The typed edge | `TASK-TOPOLOGY-001` | the versioned graph |
| The `topology_versions` table | `storage/migrations.py` | the existing table, reused |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The topology version | `GET /topology/versions`, `GET /topology/diff` | additive read routes |
| The change reconciliation | `disaggregation/audit.py` | the affected records |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-001` | the typed edge contract |

**Acceptance criteria.**
- [ ] A topology version carries an identifier, a time, a status, a parent and a diff.
- [ ] A change is detected, stated and reconciled.
- [ ] Every stored record keeps the version it was produced under.
- [ ] A previous version stays readable and byte-identical.
- [ ] No historical relation is rewritten.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T11`, `T12` · `UT-topology-persistence::test_topology_version_diff` (new) | scenarios | the diff is correct |
| `T11`, `T12` · `UT-inference-records::test_records_keep_their_topology_version` (new) | scenarios | the version travels with the record |

**Builder assignment.**
Unassigned. Wave 2, segment 2.2. Write owner of `electrical/topology.py` and
`disaggregation/audit.py` for wave 2.

**Blockers.**
- The `storage/database.py` write is a **write request** to `TASK-STORAGE-001`,
  the wave-1 owner of that file. Not a blocker; the request is filed.
