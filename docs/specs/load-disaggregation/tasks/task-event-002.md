# TASK-EVENT-002 — Cross-reader vector, late data, duplicates and incomplete groups

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-EVENT-002`
**Title.** Cross-reader vector, late data, duplicates and incomplete groups
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-EVENT-005`, `REQ-EVENT-006`, `REQ-EVENT-007`, `REQ-EVENT-008`, `REQ-EVENT-009`

**Description.**
Publish the per-reader effect of an event, classify late data instead of
dropping it, count a re-delivered event once, accept an incomplete group as a
valid outcome, and keep the observed time and the ingestion time apart.

**Expected output.**
- The event publishes its per-reader effect vector: before, after, delta, quality.
- A late event is classified and rewrites no published record.
- A re-delivered event adds no row.
- An incomplete group names its missing part.
- `occurred_at` is never mutated.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/isolation.py` | the effect vector |
| `engine/wattmosaic_engine/disaggregation/events.py` | the event record |
| `engine/wattmosaic_engine/ingestion/ha_source.py` | the re-delivery handling |
| `engine/wattmosaic_engine/synchronization/` | the out-of-order handling |
| `engine/wattmosaic_engine/storage/database.py` | the duplicate identity column |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The event kind and window | `TASK-EVENT-001` | the vocabulary |
| The per-reader delta | `ingestion/correlation.py` | the correlation step already computes it |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The effect vector | `EventIsolation` | published, additive field |
| The late-data classification | the API, the panel | `late` is a state, not a drop |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-EVENT-001` | the event kind, window and grouping vocabulary |

**Acceptance criteria.**
- [ ] The event publishes its per-reader effect vector.
- [ ] A late event is classified and rewrites no published record.
- [ ] A re-delivered event adds no row.
- [ ] An incomplete group names its missing part and credits no device.
- [ ] `occurred_at` is never mutated.
- [ ] The observed time and the ingestion time are both preserved.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T01`, `T02` · `UT-correlation::test_event_publishes_its_cross_reader_vector` (new) | scenarios | the vector is present |
| `T14` · `UT-synchronization::test_out_of_order_sampling_stays_ordered` | scenario `T14` | the order is kept |
| `T12` · `E2E-event-intake::test_duplicate_event_is_counted_once` (new) | scenario `T12` | one row, not two |
| `T14` · `UT-event-isolation::test_incomplete_group_names_its_missing_part` (new) | scenario `T14` | the missing part is named |
| `UT-state-lag::test_transition_is_not_backdated_by_the_state_lag` | `pytest tests/unit` | `occurred_at` is untouched |

**Builder assignment.**
Unassigned. Wave 2, segment 2.4. Write owner of `disaggregation/events.py`
and `storage/database.py` for wave 2.

**Blockers.**
- The `storage/database.py` write is a **write request** to `TASK-STORAGE-001`,
  the wave-1 owner of that file. Not a blocker.
