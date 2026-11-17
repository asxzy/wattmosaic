# TASK-LEARNING-001 — Sample intake, the rejection ledger and per-device thresholds

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-LEARNING-001`
**Title.** Sample intake, the rejection ledger and per-device thresholds
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-LEARNING-001`, `REQ-LEARNING-002`, `REQ-LEARNING-006`

**Description.**
Accept learning samples only from isolated events inside a window, keep an
enumerated reason code on every rejection, and allow a threshold to be
overridden per device with the resolved source published.

**Expected output.**
- Only isolated events produce samples.
- Every rejection keeps an enumerated reason code.
- A per-device override changes that device's threshold and the resolved source is published.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/learning/pipeline.py` | the sample intake, the rejection ledger |
| `engine/wattmosaic_engine/disaggregation/configuration.py` | `EffectiveConfig`, the resolved threshold |
| `engine/wattmosaic_engine/disaggregation/isolation.py` | the isolation gate for the sample |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The isolation verdict and window | `TASK-EVENT-002` | the only accepted evidence |
| The correlation result | `TASK-EVENT-001` | the group and the lag |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The learning sample | `learning/pipeline.py` | from an isolated event only |
| The rejection reason | the API, the delivery report | an enumerated code |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-EVENT-002` | the isolation verdict and the cross-reader vector |

**Acceptance criteria.**
- [ ] Only isolated events produce samples.
- [ ] Every rejection keeps an enumerated reason code.
- [ ] A per-device override changes that device's threshold only.
- [ ] The resolved threshold source is published.
- [ ] A contaminated event never produces a sample.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-learning::test_only_isolated_events_create_samples` (new) | `pytest tests/unit` | the gate holds |
| `UT-learning-reporting::test_rejections_carry_reason_codes` (new) | `pytest tests/unit` | the code is present |
| `UT-learning::test_per_device_threshold_override` (new) | `pytest tests/unit` | the override is scoped |

**Builder assignment.**
Unassigned. Wave 3, segment 3.1. Write owner of `learning/pipeline.py`,
`disaggregation/configuration.py` and `disaggregation/isolation.py` for wave 3.

**Blockers.**
- None.
