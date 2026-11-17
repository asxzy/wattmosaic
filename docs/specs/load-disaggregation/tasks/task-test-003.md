# TASK-TEST-003 — The T01–T15 builders, the matrix reconciliation and the performance and accuracy harnesses

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-TEST-003`
**Title.** The T01–T15 builders, the matrix reconciliation and the performance and accuracy harnesses
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TEST-003`, `REQ-TEST-004`, `REQ-TEST-009`, `REQ-TEST-010`

**Description.**
Give every canonical scenario a named deterministic builder, reconcile the
scenario matrix with the requirement matrix, assert every stated performance
budget, and keep the accuracy claim honest.

**Expected output.**
- Each of T01–T15 has a named deterministic builder, and the ground truth is never visible to the engine path.
- Every scenario-dependent requirement names both the `T##` identifier and the existing matrix row.
- Every stated budget is asserted by a test.
- No artefact claims a real-house per-device accuracy.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `tools/simulator/scenarios.py` (new) | the T01–T15 builders |
| `tests/simulation/test_simulator_fixtures.py` (new) | the determinism check |
| `tests/unit/test_simulator_fixtures.py` (new) | the builder coverage check |
| `tests/unit/test_health_probe_cost.py` | the health-probe budget |
| `tests/unit/test_nesting_perf.py` | the nesting budget |
| `tests/simulation/test_accuracy.py` | the accuracy strata |
| `docs/test_scenario_matrix.md` | the scenario-to-requirement reconciliation |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The scenario catalogue | `14-requirement-traceability.md` §7 | the `T##` identifiers per requirement |
| The simulator strata | `tools/simulator/` | the existing `LabHouse` pattern |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The scenario builders | `tests/simulation/`, `tests/unit/` | deterministic, ground truth separated |
| The scenario matrix | `docs/test_scenario_matrix.md` | the `T##` ↔ requirement reconciliation |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TEST-001` | the verification vehicle and the gate surface |

**Acceptance criteria.**
- [ ] Each of T01–T15 has a named deterministic builder.
- [ ] The ground truth is never visible to the engine path.
- [ ] Every scenario-dependent requirement names both the `T##` identifier and the existing matrix row.
- [ ] Every stated budget is asserted by a test.
- [ ] No artefact claims a real-house per-device accuracy; the accuracy claim is stratified and simulator-derived.
- [ ] The `tests/simulation/test_accuracy.py` write is a **write request** to
      `TASK-TEST-002`, the wave-4 owner of that directory.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T01`–`T15` · `SIM-simulator-fixtures::test_every_scenario_builder_is_deterministic` (new) | `pytest tests/simulation` | every builder is deterministic |
| `UT-simulator-fixtures::test_every_scenario_id_has_a_builder` (new) | `pytest tests/unit` | every `T##` has a builder |
| `UT-nesting-perf::test_every_stated_budget_is_asserted` (new) | `pytest tests/unit` | the budgets are asserted |
| `SIM-accuracy::test_accuracy_is_stratified_and_simulator_derived` (new) | `pytest tests/simulation` | the claim is honest |

**Builder assignment.**
Unassigned. Wave 4, segment 4.2. Write owner of `tools/simulator/scenarios.py`
and of the new test files for wave 4.

**Blockers.**
- None.
