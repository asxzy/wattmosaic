# TASK-TEST-001 — Unit-rule coverage, regression and negative verification

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-TEST-001`
**Title.** Unit-rule coverage, regression and negative verification
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-TEST-001`, `REQ-TEST-006`, `REQ-TEST-007`

**Description.**
Carry the verification vehicle for every wave: prove that every new rule has a
test that failed against the pre-change tree, prove that the existing suite
stays green and the type-check delta is zero, and prove every new guard with a
negative-verification record.

**Expected output.**
- Every new rule has a test that failed against the pre-change tree, with the failing log kept.
- The suite result is not smaller than the baseline and the `mypy engine` error count does not increase.
- Every new guard has a negative-verification record; repeated families have an automated harness.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `tests/unit/` | the new rule tests |
| `tools/dev/negverify_nesting.py` | the negative-verification harness |
| `tools/dev/verify.py` | `GATE-verify-all`, `GATE-negverify` |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The task list and its test IDs | `14-requirement-traceability.md` §6, §7 | the vehicle per requirement |
| The baseline suite result and the baseline `mypy` error count | the pre-change tree | the comparison point |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The red-then-green record | the delivery report, the orchestrator | one failing log per new rule |
| The negative-verification record | `GATE-negverify` | one entry per new guard |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-001` | the gate surface and the contract workflow |

**Acceptance criteria.**
- [ ] Every new rule has a test that failed against the pre-change tree; the failing log is kept.
- [ ] The suite result is not smaller than the baseline.
- [ ] The `mypy engine` error count does not increase.
- [ ] Every new guard has a negative-verification record.
- [ ] Repeated guard families have an automated harness.
- [ ] No existing test is weakened, skipped or deleted to make a check pass.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `MANUAL-red-then-green-record` | delivery-report artefact | the failing log per new rule |
| `GATE-verify-all` | `python tools/dev/verify.py:verify_all` | the suite is green, the type-check delta is zero |
| `GATE-negverify` | `python tools/dev/verify.py:negverify` | every guard has a negative record |

**Builder assignment.**
Unassigned. Starts in wave 0 and runs **continuously** alongside every wave.
Write owner of `tools/dev/verify.py` and `tools/dev/negverify_nesting.py` from
wave 1 onward.

**Blockers.**
- The `tools/dev/verify.py` write in wave 0 is a **write request** to
  `TASK-FOUNDATION-001`, the wave-0 owner. Not a blocker.
- The red-then-green record is a **process artefact**: it exists only if the
  builder captures the failing run before the fix. It is not a test.
