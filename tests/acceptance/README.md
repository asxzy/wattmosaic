# HB §21 acceptance suite (K07)

Twenty mandatory acceptance tests from `handbook.md` §21, one file per
requirement, all written now as `@pytest.mark.xfail(strict=True)` against the
HB §17 reference house (`tools.simulator.hb17`, X07, seed 17).  Each reason
names the **single closing task** that removes that file's xfail marker. A test
may only be un-xfailed once it passes unchanged; V1 verifies no markers remain.

CI picks the suite up through the normal `pytest -q` run. `pytest tests/acceptance
-q` must report exactly **20 xfailed, 0 xpassed**. Strictness means an accidental
pass fails the run and forces the marker's removal, so progress is always visible.

Tests assert **semantics**, never merely HTTP 200 (HB §21). Where a v3 public
surface does not exist yet, the test imports it lazily inside the test body (or
uses the `engine_harness` / `api_client` / `panel_model` fixtures in this
directory), so collection always succeeds and the test xfails strictly.

## File → closing task

| File | Requirement (HB §21) | Closes (remove xfail) |
|---|---|---|
| `test_at01_nested_no_double_count.py` | Nested readers, no double counting | M1 |
| `test_at02_parent_residual_not_containment.py` | Parent residual ≠ child containment evidence | T5 |
| `test_at03_nonzero_standby_modeled.py` | Stable non-zero standby is modeled | L2 |
| `test_at04_unknown_without_ha.py` | Unknown devices detected without HA | T6 |
| `test_at05_response_delays_matched.py` | Matching handles different response delays | I2 |
| `test_at06_joint_inference.py` | Multiple simultaneous devices jointly inferred | I3 |
| `test_at07_group_not_fabricated.py` | Non-identifiable → group, no fabricated values | I4 |
| `test_at08_plug_anchor.py` | Smart-plug measurements are stronger anchors | I3 |
| `test_at09_covariance_nested.py` | Covariance accounted for across nested readers | I3 |
| `test_at10_unknown_alternatives.py` | Unknown-load alternatives remain during matching | I1 |
| `test_at11_cells_track_evidence.py` | Mapping cells change with distinct evidence | M2 |
| `test_at12_columns_multi_level.py` | Matrix columns reflect multi-level readers | U1 |
| `test_at13_no_duplicate_secondary_columns.py` | No duplicate reader columns for secondary edges | M1 |
| `test_at14_physical_load_counted_once.py` | One physical load counted once | I5 |
| `test_at15_plug_relocation.py` | Plug relocation changes mapping, not power model | T7 |
| `test_at16_reader_reconciliation.py` | Reader/calibration change triggers reconciliation | T7 |
| `test_at17_missing_not_zero.py` | Missing data does not become zero | D3 |
| `test_at18_late_event_revision.py` | Late events revise provisional results | I6 |
| `test_at19_replay_newer_model.py` | Historical replay with a newer model | L7 |
| `test_at20_ui_explains_estimates.py` | UI explains source and uncertainty | U2 |

Closing tasks: M1, T5, L2, T6, I2, I3, I4, I1, M2, U1, I5, T7, D3, I6, L7, U2
(each `implementation-plan.md` §3 task id). The meta-test in
`test_closing_task_ids.py` asserts every reason names an existing task id.
