# Test Scenario Matrix

Owner: Domain & Test Inventory (Subagent B).
Companion document: [`domain_requirements_inventory.md`](domain_requirements_inventory.md)
(concept → contract mapping and the proposed additions that several gaps depend on).
Basis: 2177 tests passing (`python -m pytest tests`). Every row below was checked
against the actual test function names, not against docs.

Legend: **COVERED** = an existing test asserts this and would fail if the behaviour
regressed. **PARTIAL** = the mechanism is tested, but not for the stated scenario
(usually the units exist while the end-to-end case is absent). **MISSING** = no
test asserts it at all.

Fixtures referenced:
`mains()/shared_branch_topo()/separate_branch_topo()/_dedicated_pair_topo()` and
`window(...)` (`tests/unit/test_disaggregation.py`),
`_dedicated_topology()` (`tests/unit/test_electrical.py`),
`_house()/_noisy()` (`tests/unit/test_nesting.py`),
`_row_watts()/_history()/_switch_patterns()` (`tests/unit/test_group_fit.py`),
`_co_toggling_pair()/_correlate()/_cycle()` (`tests/unit/test_correlation.py`),
`_chan()/_window()/_aliased_topology()` (`tests/unit/test_reconciliation_split.py`),
`_rich_config()` (`tests/unit/test_storage_config.py`),
`_paired_topology()/_one_leg_topology()` (`tests/simulation/test_leg_split_asymmetric.py`),
`tmp_db_path`/`sample_topology`/`fast_scenario`/`household_scenario` (`tests/conftest.py`),
`make_sample`/`binary_samples`/`series` (`tests/unit/conftest.py`),
`REAL_HOME_TOPOLOGY` + `SimulatorConfig` (`tools/simulator`).

---

## Group A — Basic loads

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| A1 120 V device | COVERED | `test_disaggregation.py::test_10_l1_l2_phase_relationship`; `test_electrical.py::test_phase_coefficients_l1_l2_both` | `separate_branch_topo()`, `mains()` (`Phase.L1`) | — |
| A2 240 V device | COVERED | `test_disaggregation.py::test_09_double_pole_240v_appliance_total_not_two_appliances`; `test_electrical.py::test_direct_double_pole_sums_both_legs`; `test_leg_split_asymmetric.py::test_paired_legs_sum_to_the_device_power` | `_dedicated_pair_topo()`, `_paired_topology()` | — |
| A3 Device with a power entity | PARTIAL | `test_device_links.py::test_state_key_never_maps_a_power_entity_to_device_state`; `test_topology_bootstrap.py::test_discovery_never_proposes_wattmosaic_own_estimates`; `test_topology_bootstrap.py::test_discovery_ignores_an_isolated_smart_plug` | `_topology()`, `discover_candidate_topology(states, registries)` | Only the *negative* is tested: a device power entity must not become a CT or a state. Add a test where a declared `sensor.<device>_power` is the **only** evidence and must yield a value with a distinct measurement basis, and a test that the same entity cannot also be read as a CT (no double count). |
| A4 ON/OFF-only device | COVERED | `test_disaggregation.py::test_01_single_device_activation`, `::test_02_single_device_deactivation`; `test_device_attachment.py::test_single_on_claimant_owns_the_whole_row`, `::test_state_pin_does_not_require_a_learnt_model` | `separate_branch_topo()`, `states(...)`, `shared_branch_topo(2)` | — |
| A5 Dynamic power | COVERED | `test_disaggregation.py::test_18_variable_power_consumption_tracks_measurement` | `shared_branch_topo(1)` over 12/45/180/640/1200 W | — |
| A6 Startup transient | MISSING | Only `test_models.py::test_binary_model_median_ignores_inrush_outlier` (model fitting, not the engine) | `make_sample(event=9000.0)` | Add a simulator profile with a multi-second inrush (`ExpectedPower.startup_w` is currently unused by the simulator) and a test that one 5× spike does not inflate the steady-state estimate, does not create a phantom device, and does not move the residual. |
| A7 Standby | COVERED | `test_disaggregation.py::test_17_standby_power_is_preserved`; `test_device_attachment.py::test_stated_standby_is_allowed_while_off`; simulator `SimDeviceType.STANDBY` | `separate_branch_topo()`, `model(..., standby_w=…)` | — |

## Group B — Refoss factor (×1 / ×2)

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| B1 ×1 (default) | COVERED | `test_electrical.py::test_default_multiplier_is_one_and_changes_nothing`; `test_nesting_wizard.py::test_default_multiplier_is_one` | `_ch(...)` default, wizard payload | — |
| B2 ×2 | COVERED | `test_electrical.py::test_scale_w_applies_the_declared_multiplier_once`, `::test_direct_measurement_applies_the_multiplier_and_reports_the_full_load`, `::test_main_power_applies_a_multiplier_on_a_nested_feed` | `Topology` built inline with `measurement_multiplier=2.0` | — |
| B3 Single leg with two-leg reporting | PARTIAL | `test_electrical.py::test_matrix_scales_the_target_but_not_the_raw_reading`, `::test_step_detection_reports_the_step_in_the_load_not_the_leg` | inline `CTChannel(measurement_multiplier=2.0)` | Tests prove the arithmetic applies once. No test starts from the physical case (“one CT clamped, monitor publishes the whole 240 V load”) because the simulator cannot emit it. Add a channel whose reported value equals the *device* total while `phase=L1`, and assert the result is one device at that total — not 2× and not split. |
| B4 Factor changes over time | MISSING | — | — | Needs a factor validity interval (`MeasurementFactor`). Test: multiplier 1.0 → 2.0 at t=X; readings before X must not be doubled, readings after must. Currently `measurement_multiplier` is a single mutable float, so history is unreconstructable by construction. |
| B5 Missing factor | MISSING | — | — | Test: a 240 V single-leg channel with no declared multiplier and a reading far below the sibling device total must be *flagged* (unverified basis), not silently trusted as a conductor reading. Today the default 1.0 is indistinguishable from a deliberate ×1. |
| B6 Incorrect factor metadata | PARTIAL | `test_nesting.py::test_a_half_scaled_leg_is_not_treated_as_a_duplicate`; `test_nesting_wizard.py::test_a_multiplier_outside_the_bounds_is_rejected_by_validation` | `_house()`, wizard bounds 0.01–100 | Refuses bad multipliers and refuses to read a half-scaled leg as a duplicate, but no test asserts a *wrong ×2* is detected from physics (e.g. the doubled value exceeds the parent feed). Add: wrong ×2 → child exceeds parent → containment fails and the row is not presented as verified. |
| B7 Factor CT beside a normal CT | PARTIAL | `test_electrical.py::test_matrix_scales_the_target_but_not_the_raw_reading`; `::test_main_power_applies_a_multiplier_on_a_nested_feed` | inline mixed `Topology` | Mixed scaling is exercised per-row but never in a reconciliation: no test asserts the ×2 row and a normal row on the same panel sum to a valid main total with the residual unchanged. |
| B8 Unbalanced L1/L2 | COVERED | `test_leg_split_asymmetric.py` (all 5: `test_paired_legs_sum…`, `test_declared_split_does_not_perturb…`, `test_single_leg_row_is_solved_through_the_declared_split`, `test_declared_split_recovers_the_missing_leg`, `test_wrong_split_errs_in_the_direction_of_the_mistake`); `test_electrical.py::test_asymmetric_pairing_is_reported` | `_paired_topology(split_l1)`, `_one_leg_topology(split_l1)` | — |
| B9 Historical factor reconstruction | MISSING | — | — | Same blocker as B4. Test: given stored readings + a factor timeline, re-running an old window reproduces the old watts; without a timeline the reconstruction must report “unknown basis”, never assume 1.0. |

## Group C — CT relationships

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| C1 1 device / 1 CT | COVERED | `test_disaggregation.py::test_05_dedicated_ct_measurement_is_never_replaced_by_model`; `test_electrical.py::test_resolve_direct_uses_dedicated_ct_only` | `separate_branch_topo()`, `_dedicated_topology()` | — |
| C2 1 device / many CTs | COVERED | `test_disaggregation.py::test_09…`, `::test_a_genuine_double_pole_pair_still_sums_both_legs`; `test_electrical.py::test_direct_double_pole_sums_both_legs` | `_dedicated_pair_topo()`, `_dedicated_topology()` (`ct_tub_l1`,`ct_tub_l2`) | — |
| C3 Many devices / 1 CT | COVERED | `test_disaggregation.py::test_06_shared_branch_ct_attributes_only_from_branch`; `test_device_attachment.py::test_declaring_a_shared_ct_gives_every_device_an_attribution_row` | `shared_branch_topo(2)` / `shared_branch_topo(3)`, `window(...)` | — |
| C4 Many CTs on the same load | COVERED | `test_nesting.py::test_two_monitors_on_one_wire_are_a_duplicate_not_a_nesting`; `test_electrical.py::test_an_aliased_main_contributes_once`, `::test_an_alias_chain_contributes_once_through_the_whole_chain` | `_house()`, inline alias chain | — |
| C5 Main/sub overlap | COVERED | `test_disaggregation.py::test_07_nested_branch_ct_does_not_double_count`, `::test_main_power_is_never_double_counted_with_nested_panels`; `test_electrical.py::test_nested_main_sums_only_the_outermost_feed`, `::test_three_level_nesting_sums_only_the_root` | `separate_branch_topo()`, three-level inline tree | — |
| C6 Independent CTs with similar responses | COVERED | `test_nesting.py::test_two_loads_active_at_different_times_are_not_a_duplicate`, `::test_two_feeds_that_merely_track_each_other_are_not_a_duplicate` | `_added()`, `_noisy()` series | — |
| C7 Negative CT values | COVERED | `test_disaggregation.py::test_11_reversed_ct_polarity`, `::test_negative_export_is_preserved_in_reconciliation`; `test_electrical.py::test_negative_direct_reading_is_clipped_to_zero`, `::test_negative_direct_reading_on_a_paired_ct_keeps_the_signed_sum`, `::test_main_power_preserves_negative_export` | `window(...)` with negative gaps, `Polarity.INVERTED` | — |
| C8 Missing samples | COVERED | `test_disaggregation.py::test_12_missing_sensor_samples_do_not_abort_or_fabricate`, `::test_direct_measurement_missing_channel_is_not_zero`; `test_synchronization.py::test_window_marks_missing_channels`; `test_electrical.py::test_a_missing_feed_in_a_conflict_group_is_not_silently_dropped` | `window({"branch_fridge": None, ...})` | — |
| C9 Reversed orientation / direction change | PARTIAL | `test_disaggregation.py::test_11_reversed_ct_polarity`; `test_electrical.py::test_reversed_polarity_is_applied`; `test_quality.py::test_a_load_row_declared_normal_but_negative_is_reported` | inline `Polarity.INVERTED`, negative readings | Static inversion is covered. A **change** mid-run (a CT that reads positive then negative, or polarity flipping after a re-clamp) is never tested; the engine would need to flag it rather than clip one half. Add a two-phase window and assert a quality issue plus no silent 0 W. |

## Group D — Dedicated circuits

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| D1 EV charger | PARTIAL | `test_accuracy.py::test_every_dedicated_device_is_within_tolerance` (`vehicle_charger` ceiling 2.0), `::test_direct_devices_are_detected` | `REAL_HOME_TOPOLOGY`, `SimulatorConfig(seed=202, minutes=60, interval=15)` | The device exists and its MAE is pinned, but no EV-specific behaviour (taper, rate change) is asserted. Add a variable-rate scenario for `vehicle_charger` and assert the tracked value follows the rate rather than the last learned constant. |
| D2 Hot tub | PARTIAL | `test_electrical.py::test_direct_double_pole_sums_both_legs` and `::test_a_genuine_double_pole_pair_still_sums_both_legs` use the literal device `tub` (paired 240 V dedicated); `test_state_entity_mapping.py::test_topology_carries_state_overrides_for_the_spa` | `_dedicated_topology()` (device `tub`), `dev_spa` mapping dict | The electrical shape and the state semantics exist; there is no spa cycle (heat / idle / jet) test and no spa profile in the simulator. Add a `hot_tub` profile with a heating stage + standby and pin its per-device error. |
| D3 A/C | PARTIAL | `test_accuracy.py::test_every_dedicated_device_is_within_tolerance` (`air_conditioner` 80.6 W, `upper_ac` 73.3 W, `central_hvac` 14.0 W), `::test_concurrency_is_stratified_not_averaged` | `REAL_HOME_TOPOLOGY`, `HVACProfile` | Pinned error only. No test of a compressor duty cycle against a dedicated CT, and no test that an A/C ON signal with unchanged power is not credited. |
| D4 Dedicated circuit with standby | COVERED | `test_disaggregation.py::test_17_standby_power_is_preserved`; `test_device_attachment.py::test_stated_standby_is_allowed_while_off`; simulator `hot_water_tank`/`monitor`/`printer` (`STANDBY`) | `separate_branch_topo()`, `model(standby_w=…)` | — |
| D5 Dedicated circuit, dynamic | PARTIAL | `test_disaggregation.py::test_18…` (branch, not dedicated); `test_accuracy.py` per-device ceilings for `vehicle_charger`/`humidifier_1` | `shared_branch_topo(1)`, `VariableSpeedProfile` | The dynamic-tracking assertion is on a *branch* row. Add the same sweep on a `DEDICATED_DEVICE` channel and assert `source == DIRECT_MEASURED` throughout (no fallback to a model). |
| D6 Falsely appearing multi-load | COVERED | `test_topology_bootstrap.py::test_a_single_channel_circuit_is_not_declared_a_private_measurement`; `test_device_taxonomy.py::test_monitoring_is_derived_from_the_dedicated_channel_only`; `test_energy_quality.py::test_sole_occupant_branch_is_shared_not_dedicated` | `discover_candidate_topology`, inline topology | — |
| D7 Insufficient evidence | COVERED | `test_disaggregation.py::test_16_unidentifiable_concurrent_loads_are_ambiguous_not_fabricated`, `::test_16c_ambiguous_concurrent_loads_with_models_stay_unknown`; `test_electrical.py::test_identifiability_rank_and_ambiguous_group` | `shared_branch_topo(2)`, `registry([...])` | — |

## Group E — Shared circuits

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| E1 Multiple lighting | COVERED | `test_device_attachment.py::test_three_lamps_one_row_still_resolve_when_one_is_on`; `test_correlation.py::test_two_fixtures_on_one_row_are_both_placed_with_measured_watts`; `test_accuracy.py` ceilings for `lighting_1/2/3` | `shared_branch_topo(3)`, `_co_toggling_pair()`, `REAL_HOME_TOPOLOGY` | — |
| E2 Multiple plugs | PARTIAL | `test_correlation.py::test_two_fixtures_on_one_row_are_both_placed_with_measured_watts`; simulator `garden_outlet`, `eave_outlet`, `utility_1..4` | `_co_toggling_pair()` | Generic shared-row separation is proven; no scenario that is explicitly “two sockets on one breaker” with one always-on phantom load and an unknown count. Add a row with 2 known + 1 undeclared socket and assert the residual absorbs the third while the two known devices keep their measured watts. |
| E3 Multiple appliances | COVERED | `test_disaggregation.py::test_03_multiple_simultaneous_activation_separable`; `test_device_attachment.py::test_two_on_claimants_stay_unknown` | `shared_branch_topo(2)`, `window(...)` | — |
| E4 Independent changes | COVERED | `test_correlation.py::test_fixtures_sharing_a_row_but_switching_independently_are_both_placed`; `test_disaggregation.py::test_16b_concurrent_but_separable_is_not_marked_ambiguous` | `_correlate(...)`, `shared_branch_topo(2)` | — |
| E5 Overlapping schedules | COVERED | `test_disaggregation.py::test_03…`, `::test_16b…`; `test_simulator.py::test_simultaneous_events_exist_and_are_simultaneous` | `shared_branch_topo`, `SimulatorConfig(simultaneous_event_batches=6)` | — |
| E6 Unmonitored load | COVERED | `test_disaggregation.py::test_08_unknown_load_is_preserved_as_residual`; `test_electrical.py::test_unobserved_device_is_listed_not_silently_zero`; `test_accuracy.py::test_unknown_residual_is_preserved` | `separate_branch_topo()`, `unknown_load_l1/l2` | — |

## Group F — Residuals

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| F1 Fixed baseline | COVERED | `test_group_fit.py::test_the_constant_baseline_is_excluded_from_the_coefficients`; `test_device_attachment.py::test_everything_off_leaves_the_row_in_the_residual` | `_history(...)`, `shared_branch_topo(2)` | — |
| F2 Standby | COVERED | `test_disaggregation.py::test_17_standby_power_is_preserved`; `test_aggregation.py::test_run_does_not_open_below_the_power_floor` | `model(standby_w=…)`, `binary_samples` | — |
| F3 Dynamic | PARTIAL | `test_disaggregation.py::test_18…`, `::test_a_residual_with_no_transition_is_reported_not_fabricated` | `shared_branch_topo(1)` | Tracking is asserted; the residual under a *continuously varying* unmetered load is not. Add a ramp on an unknown load and assert the residual follows it each window without being pushed into a device. |
| F4 Positive residual | COVERED | `test_disaggregation.py::test_08…`; `test_reconciliation_split.py::test_normal_case_spread_fits_inside_the_residual`, `::test_split_does_not_change_any_power_value` | `_window(...)`, `_infer(...)` | — |
| F5 Negative residual | COVERED | `test_reconciliation_split.py::test_a_negative_residual_contains_no_disagreement_either`; `test_disaggregation.py::test_negative_export_is_preserved_in_reconciliation` | negative readings, alias legs | — |
| F6 Near-zero residual | COVERED | `test_reconciliation_split.py::test_zero_residual_contains_no_disagreement_but_still_reports_the_spread`, `::test_a_clean_residual_is_not_annotated` | zero-residual records | — |
| F7 New device explains part of the residual | PARTIAL | `test_auto_placement.py::test_a_configured_device_absent_from_topology_is_materialised_on_its_row`, `::test_both_off_leaves_the_baseline_unattributed` | `tmp_db_path`, configured device + row | Devices are materialised but no test asserts the residual *decreases by exactly the new device's power* in the next window. This is the requirement's “new device explains part, residual must shrink, not be silently reused”. |
| F8 Cannot disaggregate | COVERED | `test_disaggregation.py::test_16…`, `::test_16c…`; `test_electrical.py::test_unobserved_device_is_listed_not_silently_zero` | `shared_branch_topo(2)`, models | — |
| F9 Model conflict | PARTIAL | `test_disaggregation.py::test_05_dedicated_ct_measurement_is_never_replaced_by_model`, `::test_16c…`; `test_learning.py::test_compare_models_requires_margin_and_respects_mae_guard` | `registry([model("tv", on_w=900.0)])` | “Direct wins over model” is proven. The engine *does* cap confidence and append `state_direct_measurement_conflict` (`disaggregation/engine.py:1533`), but `grep` finds **no test** referencing that flag, so no test asserts the conflict is surfaced, only that the higher-priority source won. Add: state OFF + dedicated CT 900 W → flag present, confidence capped at 0.6, model not retrained on that window. |
| F10 Duplicate CT not subtracted twice | COVERED | `test_electrical.py::test_an_aliased_main_contributes_once`, `::test_an_alias_chain_contributes_once_through_the_whole_chain`; `test_nesting.py::test_two_monitors_on_one_wire_are_a_duplicate_not_a_nesting`; `test_reconciliation_split.py::test_an_alias_that_shadows_only_part_of_the_feed_is_not_compared`; `test_panel_tree.py::test_real_fixture_duplicate_feed_is_merged_not_nested` | alias topologies, real fixture | — |

## Group G — HA events & automation

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| G1 Single isolated event | COVERED | `test_disaggregation.py::test_01…`; `test_event_anchor.py::test_anchor_prefers_near_step`; `test_event_intake.py::test_declared_events_are_persisted` | `window(transitions=[...])`, `tmp_path` | — |
| G2 Two devices in the window | COVERED | `test_disaggregation.py::test_03…`, `::test_16b…`; `test_simulator.py::test_simultaneous_events_exist_and_are_simultaneous` | `shared_branch_topo(2)`, simultaneous batches | — |
| G3 Automation multi-change | COVERED | `test_correlation.py::test_fixtures_switched_by_one_scene_are_withheld_not_counted_twice`; `test_placement_discovery.py::test_config_flags_that_co_toggle_cannot_become_a_giant_regressor`; `test_group_fit.py::test_perfectly_collinear_members_collapse_to_one_group` | `_co_toggling_pair()`, `_switch_patterns()` | — |
| G4 One physical device / multiple entities | PARTIAL | `test_state_entity_mapping.py::test_state_episodes_collapses_attribute_updates`; `test_device_taxonomy.py::test_select_runtime_entities_splits_loads_from_modes`; `test_device_links.py::test_state_entities_come_from_device_metadata`; `test_storage_config.py::test_save_load_round_trip_preserves_rich_configuration` (two `state_entity_ids` on one device) | `_state_rows()`, `_rich_config()` | Many entities on one device are parsed and classified, but no test asserts the *power* consequence: two load-state entities of one device firing together must credit the device once, and one of them plus a sibling must not double-count. |
| G5 Another device changes in the analysis window | COVERED | `test_device_attachment.py::test_a_concurrent_sibling_change_blocks_step_attribution`; `test_disaggregation.py::test_16…` | `shared_branch_topo(2)` with two transitions | — |
| G6 Out-of-order arrival | COVERED | `test_synchronization.py::test_out_of_order_sampling_stays_ordered`; `test_event_anchor.py::test_tracker_never_rewrites_history_for_out_of_order_cycles`; `test_aggregation.py::test_out_of_order_and_duplicate_results_never_double_count` | `ObservationWindow` with reversed times, `tmp_db_path` | — |
| G7 Event time vs receive time differ | COVERED | `test_synchronization.py::test_window_age_uses_state_timestamp_not_arrival`; `test_ingestion.py::test_measurement_normalization_and_timestamps`; `test_state_lag.py::test_transition_is_not_backdated_by_the_state_lag` | `make_sample(...)` with a distinct `received_at`, `state_lag_s={DEVICE: 60.0}` | — |
| G8 Overlapping event groups | PARTIAL | `test_correlation.py::test_transitions_inside_the_dedup_gap_are_one_event`; `test_state_entity_mapping.py::test_additive_pair_is_reported_as_confounded`; `test_event_anchor.py::test_default_path_matches_legacy_explicit_arguments` | `_cycle(...)`, dedup gap constant | Dedupe *within* one entity and additive confounds are covered. Two devices whose event windows partially overlap in time (not identical) are never tested as an event *group*. Add overlapping, non-identical windows and assert neither device is attributed the other's step. |
| G9 Insufficient data | COVERED | `test_correlation.py::test_a_single_coincidence_is_refused`, `::test_three_agreeing_pairs_alone_are_not_enough_without_agreement`; `test_event_intake.py::test_a_repeated_state_is_not_a_transition` | `_correlate(...)`, `tmp_path` | — |
| G10 Configurable offsets | COVERED | `test_event_anchor.py::test_grace_widens_the_anchor_window`, `::test_anchor_window_is_used_not_the_observation_horizon`, `::test_negative_or_non_finite_anchor_windows_raise`; `test_electrical.py::test_a_configured_skew_limit_reaches_the_direct_path` | engine kwargs (`anchor_grace_s`, `anchor_before_s`, `anchor_after_s`) and `DisaggregationEngine(max_leg_skew_s=…)` | — |

## Group H — Dynamic behaviour

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| H1 HVAC modulation | PARTIAL | `test_accuracy.py::test_every_dedicated_device_is_within_tolerance` (`air_conditioner`, `central_hvac`, `upper_central_hvac`), `::test_concurrency_is_stratified_not_averaged`; `HVACProfile` in `tools/simulator/behavior.py` | `REAL_HOME_TOPOLOGY`, seed 202 | Error is pinned per device but no test asserts modulation is *tracked* (value moves with the profile) rather than a constant. Add a window where HVAC power ramps within the fully-measured case and assert per-sample error, not just MAE. |
| H2 EV rate change | MISSING | — | — | `vehicle_charger` is `VARIABLE_SPEED` but no test changes its rate. Add a step from e.g. 3.6 kW to 11 kW and assert the dedicated channel follows immediately, the energy integral matches, and no device run is split incorrectly. |
| H3 Fridge cycling | PARTIAL | `test_disaggregation.py::test_19_multi_stage_appliance_cycle`; `test_models.py::test_time_varying_model_fits_stage_profile_and_cycle_energy`; `test_accuracy.py` ceilings for `refrigerator_1/2`, `freezer` | `model(stages=[...])`, `series([...])`, `RefrigeratorProfile` | Staged cycles are tested generically and the fridge MAE is pinned, but there is no test that a fridge's on/off duty cycle is not misread as a device *event* stream driving learning samples. |
| H4 Motor startup | MISSING | — | — | `DeviceType.MOTOR`/`PUMP` exist in the contract but `grep` finds no motor/pump profile in `tools/simulator/`, so the simulator cannot produce one. Add a pump/motor profile with inrush + steady state and assert the steady state is learned, the inrush is excluded from the model, and the inrush does not enter the energy integral. |
| H5 State ON while power changes | COVERED | `test_disaggregation.py::test_unavailable_state_is_not_treated_as_off`, `::test_18…`; `test_state_lag.py::test_engine_never_zeroes_a_directly_measured_load`; `test_device_taxonomy.py::test_mode_only_device_never_reports_its_expected_watts` | `window(previous_states=…)`, `state_lag_s` | — |
| H6 Delayed CT response | COVERED | `test_disaggregation.py::test_13_delayed_sensor_update_is_flagged_not_used_as_fresh`, `::test_a_pair_whose_legs_were_sampled_far_apart_is_excluded`; `test_state_lag.py` (all 7) | `window(ages=…)`, `state_lag_s={DEVICE: 60.0}` | — |
| H7 Irregular sampling | COVERED | `test_synchronization.py::test_out_of_order_sampling_stays_ordered`; `test_disaggregation.py::test_observation_cadence_drives_event_windows`, `::test_cadence_is_derived_from_observed_history_not_hardcoded`; `test_simulator.py::test_stale_and_missing_are_observable` | `SimulatorConfig(channel_intervals=…)`, `cadence_s` | — |
| H8 Noise / spikes | COVERED | `test_disaggregation.py::test_15_noise_and_drift_stay_within_tolerance`, `::test_step_detection_does_not_flag_noise_only`, `::test_per_channel_noise_floor_reaches_detect_changes`; `test_correlation.py::test_a_step_inside_the_noise_floor_is_refused` | `_noisy()` seeds, `noise_floor_w` | — |
| H9 Missing intervals | COVERED | `test_disaggregation.py::test_12…`; `test_synchronization.py::test_window_marks_missing_channels`; `test_aggregation.py::test_gap_accrues_no_energy`; `test_energy_quality.py::test_measurement_age_130s_accrues_zero_energy` | outage windows, `stale_after_s=120` | — |

## Group I — Identifiability / ambiguity

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| I1 Two devices always switch together | COVERED | `test_group_fit.py::test_perfectly_collinear_members_collapse_to_one_group`; `test_electrical.py::test_identifiability_rank_and_ambiguous_group`; `test_placement_discovery.py::test_a_two_fixture_group_still_reports_co_toggling_after_one_is_placed_solo` | `_switch_patterns()`, `_history(...)` | — |
| I2 Strongly correlated power | COVERED | `test_group_fit.py::test_a_near_singular_design_is_dropped_rather_than_amplified`; `test_nesting.py::test_two_feeds_that_merely_track_each_other_are_not_a_duplicate` | `_design_for_test(...)`, `_added()` | — |
| I3 One device never isolated | COVERED | `test_group_fit.py::test_a_blind_fixture_never_gets_a_coefficient_larger_than_the_row`, `::test_a_fixture_below_the_response_floor_is_not_reported`, `::test_a_row_with_too_few_transitions_is_rejected` | `_blind_fixture_design(...)` | — |
| I4 Multiple devices explain the same CT change | COVERED | `test_disaggregation.py::test_16…`, `::test_16c…`; `test_correlation.py::test_two_rows_responding_equally_leaves_the_answer_unknown`, `::test_a_second_real_response_leaves_the_answer_unknown` | `shared_branch_topo(2)`, `_correlate(...)` | — |
| I5 Insufficient independent evidence | COVERED | `test_correlation.py::test_a_single_coincidence_is_refused`, `::test_two_corroborations_are_below_the_default_but_usable_on_request`; `test_group_fit.py::test_a_row_that_barely_beats_its_null_is_rejected` | `_correlate(...)`, threshold kwargs | — |
| I6 Conflicting observations | PARTIAL | `test_correlation.py::test_an_inconsistent_switch_is_refused_even_with_enough_agreements`, `::test_a_row_that_falls_when_the_light_comes_on_is_refused`; `test_group_fit.py::test_a_duty_cycle_confound_does_not_pass_the_rotated_null` | `_correlate(...)`, `_duty_light(...)` | Intra-signal conflicts are covered. No test asserts a *conflict between two independent sources* (metric says 900 W, state says OFF) produces an explicit conflict state rather than one silently winning. |
| I7 Unobserved loads | COVERED | `test_disaggregation.py::test_08…`; `test_electrical.py::test_unobserved_device_is_listed_not_silently_zero`; `test_topology_bootstrap.py` (unknown loads in discovery) | `unknown_load_l1/l2` | — |
| I8 Topology undeterminable | COVERED | `test_nesting.py::test_every_candidate_requires_confirmation`, `::test_an_idle_feed_is_never_nested_under_the_whole_home`; `test_nesting_wizard.py::test_two_candidates_are_never_narrowed_to_one`; `test_electrical.py::test_a_genuinely_ambiguous_same_leg_pair_still_refuses`, `::test_three_way_same_leg_ambiguity_still_refuses` | `_house()`, wizard proposals, same-leg feeds | — |

## Group J — Learning & regression

| Row | Status | Existing test(s) | Fixture | Gap remediation |
| --- | --- | --- | --- | --- |
| J1 New data raises confidence | PARTIAL | `test_learning.py::test_candidate_promotes_only_when_it_beats_active_model`, `::test_trainer_produces_candidate_with_validation_metrics`; `test_learning_reporting.py::test_the_verdict_improves_monotonically_with_accepted_samples`; `test_models.py::test_registry_round_trips_persisted_model_and_estimates` | `db` (`tmp_db_path`), `binary_samples(count=32)` | Model-level confidence improves, and learning status improves. **No** test asserts `InferenceResult.confidence` for a device rises after more accepted samples arrive. Add: same window, two model versions, assert per-dimension confidence moves up and the old record is not overwritten. |
| J2 Conflicting data lowers confidence | PARTIAL | `test_electrical.py::test_confidence_penalties_reduce_score`, `::test_confidence_bounds_and_direct_dominance`; `test_learning.py::test_compare_models_requires_margin_and_respects_mae_guard` | `ConfidenceInputs(...)` directly | Penalties are unit-tested on the pure function. No end-to-end test shows a device's confidence dropping across cycles when evidence contradicts it, and none asserts *which* dimension dropped. |
| J3 Association invalidated | PARTIAL | `test_device_attachment.py::test_removing_the_configuration_retracts_the_projected_ct`, `::test_retraction_leaves_a_wizard_assigned_ct_alone`; `test_state_entity_mapping.py::test_report_withdraws_a_mapping_on_a_duplicated_conductor`; `test_correlation.py::test_merge_is_idempotent_and_withdraws_only_auto_placements` | `tmp_db_path` + `DeviceConfiguration`, `_report(...)` | Retraction on *declaration removal* and on duplicate-conductor evidence is covered. Nothing tests an association being invalidated *by measurement evidence* while the declaration stands (e.g. a claimed dedicated CT turns out to feed two loads). |
| J4 New device discovered | PARTIAL | `test_auto_placement.py::test_a_configured_device_absent_from_topology_is_materialised_on_its_row`; `test_correlation.py::test_placement_creates_a_shared_device_on_the_branch_row`, `::test_auto_device_id_is_stable_and_distinct_from_discovery`; `test_device_attachment.py::test_declared_dedicated_ct_becomes_a_direct_measurement` | `tmp_db_path`, `plan_placements`, topology fixtures | Materialisation from config and auto-placement are covered; the *discovery* lifecycle (a new device appears from evidence, gets a stable id, appears in the snapshot, and its power starts being reported) has no single test. |
| J5 Residual partially explained | MISSING | Closest: `test_auto_placement.py::test_both_off_leaves_the_baseline_unattributed` | `shared_branch_topo(2)` | Add: residual R before a device is declared, R − P immediately after, with the same window; assert the invariant still holds (`accounted + residual == main`), the delta equals the new device's power, and the residual is *not* carried forward as unexplained. |
| J6 Topology changes | PARTIAL | `tests/integration/test_confirm_topology_e2e.py` (7 tests); `test_topology_persistence.py` (8); `test_panel_tree.py::test_persisted_topology_still_yields_the_same_tree`; `test_reconciliation_persistence.py::test_migration_7_adds_the_split_columns_in_place` | `tmp_path`, real engine presses | Confirmation, persistence and migration are thorough. No test asserts that changing the topology *after* inferences exist re-derives those inferences / marks them stale, or that old records keep their `topology_version`. |
| J7 Factor changes | MISSING | — | — | Same root cause as B4/B9: a factor is a mutable float, so no test can exist. Needs `MeasurementFactor` intervals first, then: change ×1 → ×2, assert the next record's `measurement_basis` is `FACTOR_ADJUSTED`, the previous record is unchanged, and re-reading history reproduces both values. |
| J8 Algorithm version change | MISSING | `test_group_fit.py::test_the_frozen_signature_is_unchanged` is a threshold hash, not a record; `test_release_contract.py::test_the_served_engine_version_is_the_package_version` is health metadata | `REAL_HOME_TOPOLOGY`, `HealthResponse` | Add `algorithm_version` to a persisted record and test: a version bump does not rewrite existing records; a replay under v2 can be diffed against v1 for the same window; an old record with an unknown version is reported as such rather than silently re-interpreted. |
| J9 Historical results reproducible | MISSING | — | — | Requires the versioned record (J8) plus immutable evidence references. Test: store window W under v1, replay W under v1 → identical `power_w`, `confidence` and `reason`; then assert the stored record is byte-identical after a schema migration (additive-only rule, like migration 7). |

---

## Tally

| Group | COVERED | PARTIAL | MISSING | Rows |
| --- | --- | --- | --- | --- |
| A Basic loads | 5 | 1 | 1 | 7 |
| B Refoss factor | 3 | 3 | 3 | 9 |
| C CT relationships | 8 | 1 | 0 | 9 |
| D Dedicated circuits | 3 | 4 | 0 | 7 |
| E Shared circuits | 5 | 1 | 0 | 6 |
| F Residuals | 7 | 3 | 0 | 10 |
| G HA events / automation | 8 | 2 | 0 | 10 |
| H Dynamic | 5 | 2 | 2 | 9 |
| I Identifiability / ambiguity | 7 | 1 | 0 | 8 |
| J Learning / regression | 0 | 5 | 4 | 9 |
| **Total** | **51** | **23** | **10** | **84** |

### Biggest gaps, by consequence

1. **Group J is the weakest group (0 COVERED).** Learning and regression are
   tested at the *model* level, never at the *inference record* level, because
   `InferenceResult` has no algorithm version, no previous/next pair, and no
   per-dimension confidence. J5/J7/J8/J9 are structural, not test-writing gaps.
2. **Group B (3 MISSING) is entirely about the factor's timeline.** ×1 and ×2
   arithmetic is well covered; “which multiplier was in force at time t” cannot
   be expressed, so change / missing / historical reconstruction cannot be
   tested without the contract addition.
3. **PARTIAL clusters on the same three seams:** (a) entity roles —
   A3, G4 assume `state_entity_ids` is enough; (b) residual lifecycle — F7/J5
   assume a residual can be *explained*, not just reported; (c) conflict
   surfacing — F9/I6 assert one source wins but never that the conflict is
   recorded.
4. **A6 (startup transient) and H4 (motor startup) are MISSING although the
   contract already has `ExpectedPower.startup_w`** — the field exists and is
   never used by the simulator or asserted by any test.
