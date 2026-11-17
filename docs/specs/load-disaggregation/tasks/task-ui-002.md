# TASK-UI-002 — Panel views: topology, residual, model, mapping and identifiability

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-UI-002`
**Title.** Panel views: topology, residual, model, mapping and identifiability
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-UI-003`, `REQ-UI-004`, `REQ-UI-005`, `REQ-UI-006`, `REQ-UI-008`

**Description.**
Render the five substantive views inside the existing routes: the drawn
topology, the residual decomposition, the device model, the mapping matrix
and the identifiability status.

**Expected output.**
- Each drawn edge carries its status and its confidence, and a candidate is visually distinct.
- The residual view shows the signed value, the baseline, the dynamic part and the age.
- The mapping view lists every cell.
- An ambiguous group shows its minimum, its maximum and its members, not a watt figure.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `addon/ui/app.js` | the five views |
| `tools/dev/check_panel.py` | the static panel gate for the new strings |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The panel vocabulary | `TASK-UI-001` | the strings and the view states |
| The edge status and confidence | `TASK-CONFIDENCE-002` | the per-edge evidence |
| The mapping cell and coverage | `TASK-MAPPING-001` | the nine fields and the coverage block |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The rendered views | the operator | inside the existing five routes |
| The panel gate checks | `tools/dev/check_panel.py` | the new strings are checked |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-UI-001` | the vocabulary and the view states |

**Acceptance criteria.**
- [ ] Each drawn edge carries its status and its confidence.
- [ ] A candidate edge is visually distinct from a confirmed one.
- [ ] The residual view shows the signed value, the baseline, the dynamic part and the age.
- [ ] The mapping view lists every cell.
- [ ] An ambiguous group shows its minimum, its maximum and its members, and no watt figure.
- [ ] No new hash route is introduced.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-panel::test_topology_edge_shows_status_and_confidence` (new) | `pytest tests/unit` | the edge is annotated |
| `UT-panel::test_residual_view_shows_the_signed_decomposition` (new) | `pytest tests/unit` | the decomposition is shown |
| `UT-panel::test_device_model_detail_is_visible` (new) | `pytest tests/unit` | the model is visible |
| `UT-panel::test_mapping_view_lists_every_cell` (new) | `pytest tests/unit` | every cell is listed |
| `UT-panel::test_ambiguous_group_shows_a_range` (new) | `pytest tests/unit` | the range, not a number |

**Builder assignment.**
Unassigned. Wave 4, segment 4.2. Write owner of the view functions in
`addon/ui/app.js` for wave 4.

**Blockers.**
- None. The `addon/ui/app.js` write is a **write request** to `TASK-UI-001`,
  the wave-4 owner of that file.
