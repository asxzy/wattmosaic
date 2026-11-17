# TASK-FOUNDATION-002 — Runtime boundary, the four-state label and one owner per wave

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-FOUNDATION-002`
**Title.** Runtime boundary, the four-state label and one owner per wave
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-FOUNDATION-004`, `REQ-FOUNDATION-005`, `REQ-FOUNDATION-006`

**Description.**
State the runtime boundary in code, add the machine-readable four-state power
label, and publish the one-file-one-owner matrix. This task proves that the
target adds no runtime dependency, no database, no service and no message bus.

**Expected output.**
- One process, one database file, no new dependency: asserted by a test.
- A four-state label — `measured`, `estimated`, `allocated`, `unknown` — on
  every published device result.
- The ownership matrix published in the implementation plan.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `pyproject.toml` | the dependency list; unchanged in substance |
| `engine/wattmosaic_engine/main.py` | the single-process, single-loop boundary |
| `engine/wattmosaic_engine/contracts.py` | the power-state label field |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the label is set on every result |
| `docs/specs/load-disaggregation/15-implementation-plan.md` | the one-file-one-owner matrix |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The bumped contract | `TASK-FOUNDATION-001` | additive and optional |
| The inference result | `disaggregation/engine.py` | the label is a field on it |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `power_state` label | `api/server.py`, `addon/ui/app.js` | one of the four states, never prose |
| Ownership matrix | the orchestrator | one write owner per file per wave |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-001` | consumes the bumped contract |

**Acceptance criteria.**
- [ ] No new runtime dependency, database, service or message bus.
- [ ] Every device result exposes exactly one machine-readable power-state label.
- [ ] The label is one of `measured`, `estimated`, `allocated`, `unknown`.
- [ ] The implementation plan names one write owner per file per wave.
- [ ] The plan's write-request list is a message to the orchestrator, not an uncoordinated edit.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-app-startup::test_one_process_one_database` (new) | `pytest tests/unit` | one process, one SQLite file |
| `UT-disaggregation::test_four_state_label_matches_source_and_basis` (new) | `pytest tests/unit` | the label follows the source and the basis |
| `MANUAL-wave-ownership-review` | review | no file appears twice in one wave's owner column |

**Builder assignment.**
Unassigned. Wave 0, segment 0.2. Write owner of `main.py`,
`disaggregation/engine.py` and the implementation plan for wave 0.

**Blockers.**
- The `contracts.py` write is a **write request** to `TASK-FOUNDATION-001`,
  the wave-0 owner of that file. Not a blocker, but it is not a free edit.
