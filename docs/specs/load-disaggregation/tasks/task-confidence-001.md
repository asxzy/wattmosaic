# TASK-CONFIDENCE-001 — Confidence dimensions, the calibration flag and tier monotonicity

**Task ID.** `TASK-CONFIDENCE-001`
**Title.** Confidence dimensions, the calibration flag and tier monotonicity
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-CONFIDENCE-001`, `REQ-CONFIDENCE-002`, `REQ-CONFIDENCE-003`, `REQ-CONFIDENCE-004`

**Description.**
Keep the confidence vector a vector, keep it explicitly uncalibrated, use only
the five existing member names, and make a tier fall only with a recorded
reason.

**Expected output.**
- No aggregate field, no `__float__`, no arithmetic on the vector.
- `confidence_is_calibrated` is `false`.
- Only the five existing member names are used; an unassessed dimension is `null`.
- A tier never falls without a recorded reason.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | `ConfidenceVector`, `ConfidenceDimension` |
| `engine/wattmosaic_engine/disaggregation/confidence.py` | `compute_confidence` |
| `engine/wattmosaic_engine/disaggregation/tiers.py` | `evidence_tier`, `ConfidenceTier` |
| `addon/ui/app.js` | the panel must not present a blended score |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `ConfidenceVector` | `contracts.py` | the five member names, unchanged |
| The published result | `TASK-DISAGG-002` | the source, quality and tier |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The confidence vector | `api/server.py`, the panel | five dimensions, one may be `null` |
| The evidence tier | `disaggregation/tiers.py` | monotonic, with a reason |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] No aggregate field, no `__float__` and no arithmetic on the vector.
- [ ] `confidence_is_calibrated` is `false`.
- [ ] Only the five existing member names are used.
- [ ] An unassessed dimension is `null`, never `0.0`.
- [ ] A tier never falls without a recorded reason.
- [ ] The panel never presents a blended confidence score.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-contracts::test_confidence_vector_has_no_aggregate_or_float` (new) | `pytest tests/unit` | no aggregate exists |
| `UT-contracts::test_confidence_is_calibrated_is_false` (new) | `pytest tests/unit` | the flag is `false` |
| `UT-contracts::test_confidence_uses_only_the_five_member_names` (new) | `pytest tests/unit` | the names are unchanged |
| `UT-electrical::test_confidence_bounds_and_direct_dominance` | `pytest tests/unit` | the bounds and the dominance hold |

**Builder assignment.**
Unassigned. Wave 3, segment 3.1. Write owner of `contracts.py` and
`addon/ui/app.js` for wave 3.

**Blockers.**
- None. The `contracts.py` writes requested by `TASK-CHANGE-001` and
  `TASK-CHANGE-002` in wave 3 are **write requests** to this task.
