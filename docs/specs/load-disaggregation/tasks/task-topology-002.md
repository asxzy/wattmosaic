# TASK-TOPOLOGY-002 — Containment evidence bar, violations and residual awareness

**Task ID.** `TASK-TOPOLOGY-002`
**Title.** Containment evidence bar, violations and residual awareness
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TOPO-003`, `REQ-TOPO-004`, `REQ-TOPO-005`, `REQ-TOPO-006`, `REQ-TOPO-007`

**Description.**
Hold the containment evidence bar where it is: a coincident response is
overlap only, a tolerance violation is recorded and never raised, residual
evidence alone cannot promote an edge, and nested children contribute once.

**Expected output.**
- A single coincident response yields an overlap, never a containment.
- A power-conservation violation within tolerance is recorded and never raised.
- Residual evidence alone cannot promote an edge to containment.
- Nested children and alias chains contribute exactly once.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/electrical/hierarchy.py` | `HierarchyFinder`, `ValidationResult` |
| `engine/wattmosaic_engine/electrical/matrix.py` | `MeasurementMatrix`, the nesting asymmetry |
| `engine/wattmosaic_engine/disaggregation/association.py` | the residual-aware evidence path |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The typed edge | `TASK-TOPOLOGY-001` | one kind, one lifecycle state |
| The signed residual | `TASK-RESIDUAL-001` | the residual-awareness input |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `ValidationResult` | `disaggregation/audit.py` | the recorded violation |
| The containment decision | `electrical/topology.py` | overlap, never containment by coincidence |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-001` | the typed edge contract |

**Acceptance criteria.**
- [ ] A single coincident response yields overlap only.
- [ ] A violation within tolerance is recorded and never raised.
- [ ] Residual evidence alone cannot promote an edge.
- [ ] Nested children and alias chains contribute once.
- [ ] Parent and child are treated asymmetrically.
- [ ] Exact parent–child power conservation is never enforced.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T01`, `T02` · `UT-disaggregation::test_07_nested_branch_ct_does_not_double_count` | scenarios | the nesting does not double count |
| `T01` · `UT-hierarchy::test_hot_tub_equal_deltas_yield_overlap_only` | scenario `T01` | overlap, not containment |
| `T03` · `UT-hierarchy::test_violation_within_tolerance_is_recorded` (new) | scenario `T03` | the violation is recorded |
| `T03` · `UT-residual-power::test_residual_only_evidence_does_not_promote` (new) | scenario `T03` | no promotion from residual alone |
| `T02`, `T06` · `UT-electrical::test_three_level_nesting_sums_only_the_root` | scenarios | only the root is summed |

**Builder assignment.**
Unassigned. Wave 2, segment 2.2. Write owner of `electrical/hierarchy.py` and
`electrical/matrix.py` for wave 2.

**Blockers.**
- None. The `disaggregation/association.py` write is a **write request** to
  `TASK-TOPOLOGY-001`, the wave-2 owner of that file.
