# HB §21 acceptance suite (K07 / K07b)

Twenty mandatory acceptance tests from `handbook.md` §21, one file per
requirement, against the HB §17 reference house (`tools.simulator.hb17`, X07,
seed 17). Each file is in exactly one of two states:

- **Open** — exactly one strict marker
  `@pytest.mark.xfail(strict=True, reason="closed by <TASK>")`. The named task
  closes the requirement by removing that marker. A strict xfail means an
  accidental pass fails CI and forces the marker's removal, so progress stays
  visible.
- **Closed** — zero xfail markers and a module-level constant
  `CLOSED_BY = "<TASK>"`. The named task has landed the real, passing test,
  which executes unchanged.

In both cases the task id must exist in `implementation-plan.md` §3. There are
exactly **20** files, one per AT number (AT-01 … AT-20). The meta-test in
`test_closing_task_ids.py` enforces this contract and checks each file's state
and task id against the table below.

CI picks the suite up through the normal `pytest -q` run. `pytest tests/acceptance
-q` reports one passing test per closed AT (currently **2 passed**, AT-12 and
AT-03), **18 xfailed and 0 xpassed**, plus the passing meta-tests.

Tests assert **semantics**, never merely HTTP 200 (HB §21). Where a v3 public
surface does not exist yet, the test imports it lazily inside the test body (or
uses the `engine_harness` / `api_client` / `panel_model` fixtures in this
directory), so collection always succeeds and the test xfails strictly.

## File → closing task / state

| File | Requirement (HB §21) | Closes (remove xfail) | State |
|---|---|---|---|
| `test_at01_nested_no_double_count.py` | Nested readers, no double counting | M1 | open |
| `test_at02_parent_residual_not_containment.py` | Parent residual ≠ child containment evidence | T5 | **closed** (CLOSED_BY = "T5") |
| `test_at03_nonzero_standby_modeled.py` | Stable non-zero standby is modeled | L2 | **closed** (CLOSED_BY = "L2") |
| `test_at04_unknown_without_ha.py` | Unknown devices detected without HA | T6 | open |
| `test_at05_response_delays_matched.py` | Matching handles different response delays | I2 | open |
| `test_at06_joint_inference.py` | Multiple simultaneous devices jointly inferred | I3 | closed |
| `test_at07_group_not_fabricated.py` | Non-identifiable → group, no fabricated values | I4 | closed |
| `test_at08_plug_anchor.py` | Smart-plug measurements are stronger anchors | I3 | closed |
| `test_at09_covariance_nested.py` | Covariance accounted for across nested readers | I3 | closed |
| `test_at10_unknown_alternatives.py` | Unknown-load alternatives remain during matching | I1 | open |
| `test_at11_cells_track_evidence.py` | Mapping cells change with distinct evidence | M2 | **closed** (CLOSED_BY = "M2") |
| `test_at12_columns_multi_level.py` | Matrix columns reflect multi-level readers | U1 | **closed** (CLOSED_BY = "U1") |
| `test_at13_no_duplicate_secondary_columns.py` | No duplicate reader columns for secondary edges | M1 | open |
| `test_at14_physical_load_counted_once.py` | One physical load counted once | I5 | closed |
| `test_at15_plug_relocation.py` | Plug relocation changes mapping, not power model | T7 | **closed** (CLOSED_BY = "T7") |
| `test_at16_reader_reconciliation.py` | Reader/calibration change triggers reconciliation | T7 | **closed** (CLOSED_BY = "T7") |
| `test_at17_missing_not_zero.py` | Missing data does not become zero | D3 | open |
| `test_at18_late_event_revision.py` | Late events revise provisional results | I6 | closed |
| `test_at19_replay_newer_model.py` | Historical replay with a newer model | L7 | open |
| `test_at20_ui_explains_estimates.py` | UI explains source and uncertainty | U2 | open |

**AT-12 is closed by U1.** U1's real, passing assertions (which execute the
shipped `matrix-cell.js` header renderer under node) were folded into
`test_at12_columns_multi_level.py`, which now carries `CLOSED_BY = "U1"` and no
xfail marker. The duplicate module `test_at12_multilevel_columns.py` was removed.

Closing tasks: M1, T5, L2, T6, I2, I3, I4, I1, M2, U1, I5, T7, D3, I6, L7, U2
(each an `implementation-plan.md` §3 task id).
