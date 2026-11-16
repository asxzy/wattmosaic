# TASK-MEASURE-003 — Timestamps, repeat suppression and cadence derivation

**Task ID.** `TASK-MEASURE-003`
**Title.** Timestamps, repeat suppression and cadence derivation
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-MEASURE-008`, `REQ-MEASURE-009`, `REQ-MEASURE-010`

**Description.**
Keep the three timestamps distinct, stop a repeated value from counting as new
evidence, and derive the sampling cadence from the observed history instead of
assuming two seconds.

**Expected output.**
- `state_timestamp`, `received_at` and `physical_timestamp` preserved and never conflated.
- A repeated value updates the current value but not the evidence count.
- The cadence derived from observed history, published per reader.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/synchronization/` | `SampleAligner`, `ChannelHealth` |
| `engine/wattmosaic_engine/config/settings.py` | the cadence settings |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `Measurement` | `TASK-MEASURE-001` | carries the three timestamps |
| The reader history | `synchronization/` | the observed cadence |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| Resolved cadence | `disaggregation/isolation.py`, `disaggregation/configuration.py` | the window source |
| `repeated` flag | `storage/database.py` | not new evidence |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-MEASURE-001` | the measurement contract must be settled |

**Acceptance criteria.**
- [ ] The three timestamps are preserved and never conflated.
- [ ] Staleness is measured from the state timestamp, not the arrival time.
- [ ] A repeated value is not new evidence; the evidence count does not move.
- [ ] The cadence is derived from the observed history; a hard-coded 2 s interval is refused by a static gate.
- [ ] The publication states the source of the resolved window.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-synchronization::test_window_age_uses_state_timestamp_not_arrival` | `pytest tests/unit` | staleness uses the state timestamp |
| `UT-event-intake::test_a_repeated_state_is_not_a_transition` | `pytest tests/unit` | a repeat is not a transition |
| `T14` · `UT-synchronization::test_cadence_is_derived_from_observed_history_not_hardcoded` | scenario `T14` | the cadence comes from the history |

**Builder assignment.**
Unassigned. Wave 1, segment 1.2. Write owner of `config/settings.py` for
wave 1.

**Blockers.**
- The `config/settings.py` write for the retention settings is a **write
  request** to `TASK-STORAGE-001`, which shares the file in wave 1. Not a
  blocker; the request is filed, not an edit.
