# TASK-LEARNING-002 — Promotion, factor timing and measurement precedence

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-LEARNING-002`
**Title.** Promotion, factor timing and measurement precedence
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-LEARNING-003`, `REQ-LEARNING-004`, `REQ-LEARNING-005`, `REQ-LEARNING-007`

**Description.**
Train, validate and promote a model only when it beats the active one by the
margin, resolve a factor against the reading's timestamp, and keep a
measurement above a model.

**Expected output.**
- A candidate that does not beat the active model by the margin is not activated, and its decision publishes its metrics.
- A factor is resolved against the reading's timestamp, never retroactively.
- A measurement outranks a model; the conflict is flagged and the window is not used for training.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/learning/pipeline.py` | the promotion gate |
| `engine/wattmosaic_engine/electrical/factors.py` | `MeasurementFactor`, the timestamp resolution |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the measurement-over-model precedence |
| `engine/wattmosaic_engine/storage/database.py` | the learning decision row |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The learning sample | `TASK-LEARNING-001` | the intake |
| The unknown-residual event | `TASK-RESIDUAL-002` | the second learning source |
| `FactorRegistry.basis_for` | `electrical/factors.py` | a **frozen interface** — its signature may not change without a contract bump |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The promotion decision | `storage/database.py` | with its metrics and a reason |
| The resolved factor | `electrical/factors.py` | the reading's timestamp decides |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-LEARNING-001` | the sample intake |
| `TASK-RESIDUAL-002` | the unknown-residual learning source |

**Acceptance criteria.**
- [ ] A candidate that does not beat the active model by the margin is not activated.
- [ ] The promotion decision publishes its metrics and its reason.
- [ ] A factor is resolved against the reading's timestamp and never applied retroactively.
- [ ] A measurement outranks a model.
- [ ] The conflict is flagged and the window is not used for training.
- [ ] `FactorRegistry.basis_for` is not changed in signature.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-learning::test_candidate_promotes_only_when_it_beats_active_model` | `pytest tests/unit` | the margin holds |
| `T10` · `UT-learning::test_unknown_residual_event_enters_the_pipeline` (new) | scenario `T10` | the source is accepted |
| `UT-factors::test_factor_is_resolved_against_the_reading_timestamp` (new) | `pytest tests/unit` | the timing is correct |
| `UT-learning::test_measurement_disagrees_with_model_is_recorded` (new) | `pytest tests/unit` | the conflict is recorded |

**Builder assignment.**
Unassigned. Wave 3, segment 3.4. Write owner of `electrical/factors.py` for
wave 3.

**Blockers.**
- None. The `learning/pipeline.py`, `disaggregation/engine.py` and
  `storage/database.py` writes are **write requests** to their wave-3 owners.
