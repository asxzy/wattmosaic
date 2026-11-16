# TASK-RESIDUAL-002 — Drift events, the unknown-residual hand-off and the non-redistribution guard

**Task ID.** `TASK-RESIDUAL-002`
**Title.** Drift events, the unknown-residual hand-off and the non-redistribution guard
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-RESIDUAL-005`, `REQ-RESIDUAL-006`, `REQ-RESIDUAL-007`

**Description.**
Detect residual drift, hand an unexplained residual change to the learning
system as a decision with a reason, and prove that a residual is never
redistributed onto a device.

**Expected output.**
- A sustained baseline shift emits a named drift event.
- An unexplained residual change reaches the learning ledger with a decision and a reason code.
- No device power changes because a residual was distributed.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/models/residual_power.py` | the drift detector |
| `engine/wattmosaic_engine/disaggregation/events.py` | the residual event |
| `engine/wattmosaic_engine/learning/pipeline.py` | the learning hand-off |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the non-redistribution guard |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `ResidualPower` and its history | `TASK-RESIDUAL-001` | signed, decomposed |
| The event kind vocabulary | `TASK-EVENT-001` | three kinds; the residual event is one of them |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The residual drift event | `disaggregation/events.py`, the API | named, with before/after |
| The learning ledger entry | `learning/pipeline.py` | a decision and a reason code |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-RESIDUAL-001` | the residual model and its history |
| `TASK-EVENT-001` | the event kind and window vocabulary |

**Acceptance criteria.**
- [ ] A sustained baseline shift emits a named event and rewrites no stored point.
- [ ] An unexplained residual change reaches the learning ledger with a decision and a reason code.
- [ ] No device power changes because a residual was distributed.
- [ ] The drift event is classified, not dropped silently.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-residual-power::test_residual_drift_emits_a_named_event` (new) | `pytest tests/unit` | the named event fires |
| `T10` · `UT-learning::test_unknown_residual_event_reaches_the_ledger` | scenario `T10` | the ledger entry exists |
| `T04` · `IT-safety-invariants::test_residual_is_never_redistributed` | scenario `T04` | no device power moved |

**Builder assignment.**
Unassigned. Wave 3, segment 3.2. Write owner of `models/residual_power.py`,
`disaggregation/events.py` and `disaggregation/engine.py` for wave 3.

**Blockers.**
- None. The dependency on `TASK-EVENT-001` is a wave-2 predecessor, so this
  task starts in wave 3.
