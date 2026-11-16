# TASK-FOUNDATION-001 — Specification status and the additive contract workflow

**Task ID.** `TASK-FOUNDATION-001`
**Title.** Specification status and the additive contract workflow
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-FOUNDATION-001`, `REQ-FOUNDATION-002`, `REQ-FOUNDATION-003`

**Description.**
Carry the declared status onto every specification file, make the additive
contract workflow executable, and prove that a payload produced before the
change still validates. This task owns the **single contract bump of wave 0**
and the `contracts.py` write slot for that wave.

**Expected output.**
- Every file in the specification set carries `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`.
- One `CONTRACT_VERSION` bump, one `data/schemas/` regeneration, no drift.
- A `GATE-spec_status` entry in `tools/dev/verify.py`.
- The change protocol recorded in `docs/interface_contracts.md`.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `docs/specs/load-disaggregation/README.md` | specification status header |
| `engine/wattmosaic_engine/contracts.py` | `CONTRACT_VERSION`, the additive payload fields |
| `tools/dev/export_schemas.py` | schema regeneration |
| `tools/dev/verify.py` | `GATE-spec_status`, `GATE-contract_schema_sync` |
| `docs/interface_contracts.md` | the change protocol |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `CONTRACT_VERSION` | `contracts.py` | `2.0.0` today; the target's first bump moves it forward |
| Pre-change payload | the shipped producer | must still validate after the bump |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| Regenerated `data/schemas/` | CI drift check | additive and optional fields only |
| `GATE-spec_status` | the orchestrator | fails when a spec file lacks the status header |

**Dependencies.**

| Task | Reason |
| --- | --- |
| — | none; this is the first task |

**Acceptance criteria.**
- [ ] Every specification file carries the approved status header.
- [ ] A pre-change payload still validates against the bumped contract.
- [ ] `CONTRACT_VERSION` is bumped exactly once and `data/schemas/` regenerates with no diff.
- [ ] `GATE-contract_schema_sync` passes.
- [ ] No field was removed, renamed or made mandatory.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-contracts::test_pre_change_payload_still_validates` (new) | `pytest tests/unit` | an old payload validates |
| `GATE-contract_schema_sync` | `python tools/dev/verify.py:contract_schema_sync` | schemas match the contracts |
| `GATE-spec_status` (new) | `python tools/dev/verify.py:spec_status` | every spec file carries the status |

**Builder assignment.**
Unassigned. Wave 0, segment 0.1. Write owner of `contracts.py` and
`tools/dev/verify.py` for wave 0.

**Blockers.**
- `OD-38` — the specification set is not approved. **RESOLVED** (2026-11-16):
  the orchestrator's review gate passed and the set carries
  `SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION`. The blocker is cleared; the task
  still starts `NOT_STARTED` and proceeds on the §6 defaults of
  `13-open-decisions-and-assumptions.md`.
