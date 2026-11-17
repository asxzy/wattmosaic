# TASK-DISAGG-001 — Joint inference, identifiability and group estimates

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-DISAGG-001`
**Title.** Joint inference, identifiability and group estimates
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-DISAGG-001`, `REQ-DISAGG-002`, `REQ-DISAGG-003`, `REQ-DISAGG-004`

**Description.**
Solve jointly over every available source, compute and publish the
identifiability of the system, and publish a range or a group estimate where
separation is impossible — never a fabricated split.

**Expected output.**
- The result names every source used and every source available but unused, with a reason.
- The rank, the null space and the condition number are published.
- An inseparable set publishes `min_w` and `max_w` and no per-device watt figure.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/engine.py` | `DisaggregationEngine`, the joint inference |
| `engine/wattmosaic_engine/electrical/solver.py` | `analyse_identifiability`, `augment_with_unknown_slack` |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The measurement matrix | `electrical/matrix.py` | the coefficients |
| The signed residual | `TASK-RESIDUAL-001` | the slack |
| The cadence and window | `TASK-MEASURE-003` | the aligned window |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The inference result | `api/server.py`, the panel | sources used, sources unused, identifiability |
| The group estimate | `api/server.py` | `min_w`, `max_w`, the members |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-MEASURE-003` | the aligned window and the cadence |
| `TASK-RESIDUAL-001` | the residual slack |

**Acceptance criteria.**
- [ ] The result names every source used and every source available but unused, with a reason.
- [ ] The rank, the null space and the condition number are published.
- [ ] An inseparable set publishes `min_w` and `max_w` and no per-device watt figure.
- [ ] Simultaneous device changes are handled without a fabricated split.
- [ ] The solver's unknown slack is used for the unexplained remainder, not for a device.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `E2E-full-inference-chain::test_result_names_every_source_used_and_unused` (new) | `pytest tests/end_to_end` | both lists are present |
| `T06`, `T07` · `UT-disaggregation::test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated` | scenarios | no fabricated split |
| `T07` · `UT-electrical::test_identifiability_rank_and_ambiguous_group` | scenario `T07` | the rank is published |
| `T07` · `UT-electrical::test_ambiguous_group_publishes_min_and_max` (new) | scenario `T07` | the range is published |

**Builder assignment.**
Unassigned. Wave 2, segment 2.2. Write owner of
`disaggregation/engine.py` and `electrical/solver.py` for wave 2.

**Blockers.**
- None. The `disaggregation/engine.py` writes requested by `TASK-DISAGG-002`
  and `TASK-POWER-002` in wave 2 are **write requests** to this task.
