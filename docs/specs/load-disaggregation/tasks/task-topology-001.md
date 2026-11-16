# TASK-TOPOLOGY-001 — Typed edge contract and the lifecycle vocabulary

**Task ID.** `TASK-TOPOLOGY-001`
**Title.** Typed edge contract and the lifecycle vocabulary
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TOPO-001`, `REQ-TOPO-002`

**Description.**
Give every topology edge exactly one relation kind and an explicit lifecycle,
and make it impossible for a lifecycle state to imply confirmation.

**Expected output.**
- Every edge resolves to exactly one relation kind: containment, shared load,
  leg pairing or correlation — never a blend.
- The eight lifecycle states exist as a separate vocabulary from `TopologyStatus`.
- Only `CONFIRMED` reads as established; a candidate always requires confirmation.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | `TopologyRelation`, `RelationKind`, the lifecycle vocabulary |
| `engine/wattmosaic_engine/disaggregation/association.py` | the edge production |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `TopologyRelation` | `contracts.py` | the existing shape |
| `TopologyStatus` | `contracts.py` | three members; not extended |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The typed edge | `electrical/hierarchy.py`, `disaggregation/mapping.py` | one kind, one lifecycle state |
| The lifecycle vocabulary | `api/server.py`, `GET /relations` | additive field |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] Every edge resolves to exactly one relation kind.
- [ ] The eight lifecycle states exist and are distinct from `TopologyStatus`.
- [ ] Only `CONFIRMED` reads as established.
- [ ] A candidate always carries `requires_confirmation = true`.
- [ ] A lifecycle state never implies a relation kind, and vice versa.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T13` · `UT-association::test_relation_kind_is_single_and_typed` (new) | scenario `T13` | one kind per edge |
| `T12` · `UT-association::test_edge_lifecycle_states` (new) | scenario `T12` | the eight states exist |

**Builder assignment.**
Unassigned. Wave 2, segment 2.1. Write owner of `contracts.py` and
`disaggregation/association.py` for wave 2.

**Blockers.**
- None. The `contracts.py` writes requested by `TASK-EVENT-001`,
  `TASK-DISAGG-002`, `TASK-POWER-001` and `TASK-POWER-002` in wave 2 are
  **write requests** to this task, not blockers.
