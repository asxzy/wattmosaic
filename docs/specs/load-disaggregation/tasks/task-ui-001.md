# TASK-UI-001 — Panel vocabulary: inventory, quality, the four-state label and the view states

**Task ID.** `TASK-UI-001`
**Title.** Panel vocabulary: inventory, quality, the four-state label and the view states
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-UI-001`, `REQ-UI-002`, `REQ-UI-007`, `REQ-UI-013`

**Description.**
Give the panel its vocabulary: the reader inventory, the measurement with its
quality and provenance, the four-state power label, and the six view states
every view must handle.

**Expected output.**
- The reader inventory lists every declared field.
- Every measurement shows value, unit, the three timestamps, quality and its four-state label.
- Every view has loading, empty, error, stale, offline and degraded states.
- A missing render is missing; it is never `0 W`.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `addon/ui/app.js` | `el()`, the `STRINGS` table, the view states |
| `tools/dev/check_panel.py` | the static panel gate |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The four-state label | `TASK-FOUNDATION-002` | `measured` / `estimated` / `allocated` / `unknown` |
| The reader declaration | `TASK-MEASURE-002` | `resolution_w`, `reliability`, `expected_interval_s` |
| The three timestamps | `TASK-MEASURE-003` | preserved, never conflated |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The panel strings | `tools/dev/check_panel.py` | every label in the `STRINGS` table |
| The rendered view states | the operator | loading, empty, error, stale, offline, degraded |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the four-state label must exist before it is rendered |

**Acceptance criteria.**
- [ ] The reader inventory lists every declared field.
- [ ] Every measurement shows value, unit, the three timestamps, quality and its four-state label.
- [ ] Every view has the loading, empty, error, stale, offline and degraded states.
- [ ] A missing render is missing and never `0 W`.
- [ ] All DOM is built with `el()` and inserted as text; the router's route list stays closed.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-panel::test_reader_inventory_lists_every_field` (new) | `pytest tests/unit` | the inventory is complete |
| `UT-panel::test_measurement_shows_quality_and_provenance` (new) | `pytest tests/unit` | the measurement is complete |
| `UT-panel::test_four_state_labels_are_used` (new) | `pytest tests/unit` | all four labels appear |
| `UT-panel::test_view_states_never_render_a_zero` (new) | `pytest tests/unit` | no fabricated zero |

**Builder assignment.**
Unassigned. Wave 4, segment 4.1. Write owner of `addon/ui/app.js` and
`tools/dev/check_panel.py` for wave 4.

**Blockers.**
- None. The `TASK-CONFIDENCE-001` write to `addon/ui/app.js` lands in wave 3
  and is complete before this task starts.
