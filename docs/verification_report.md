# Verification Report

Generated: `2026-11-18T23:44:09.659410+00:00`

**Required gates passed: 12/13**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.39 |  |
| spec_status | **PASS** | 0.05 |  |
| secret_scan | **PASS** | 1.27 |  |
| lint | **PASS** | 0.1 |  |
| unit_tests | **PASS** | 47.67 |  |
| integration_tests | **PASS** | 8.03 |  |
| simulation_tests | **PASS** | 51.27 |  |
| end_to_end_tests | **FAIL** | 119.7 |  |
| manifest_valid | **PASS** | 0.09 |  |
| release_valid | **PASS** | 0.03 |  |
| docs_fresh | **PASS** | 0.71 |  |
| deployment_valid | **PASS** | 3.35 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **FAIL** | 1.51 | requires HA_TOKEN in environment |

## Failures

### end_to_end_tests (exit 1)

```text
                        )
                    last_state = state
                    asyncio.run(core.run_inference_once())
            finally:
                aligner_module.utcnow = original
    
            rows = db._conn.execute(
                "SELECT device_id, supporting_ct_ids_json, rejection_reason, notes_json "
                "FROM learning_samples WHERE device_id = ?",
                (device.device_id,),
            ).fetchall()
            assert rows, "the declared event produced no learning sample at all"
            row = dict(rows[-1])
            assert row["supporting_ct_ids_json"] not in ("", "[]"), row
            assert "no_supporting_ct" not in row["notes_json"], row
            # Candidate topology is refused by policy, not for lack of measurement.
>           assert row["rejection_reason"] == "ambiguous_topology", row
E           AssertionError: {'device_id': 'underfloor_loop', 'notes_json': '["window_s:62.8", "cadence_s:15.7", "state:on", "mode_variable:state",...:4", "topology_quality:candidate"]', 'rejection_reason': None, 'supporting_ct_ids_json': '["ct_main_underfloor_loop"]'}
E           assert None == 'ambiguous_topology'

tests/end_to_end/test_event_intake.py:502: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  wattmosaic.integration:inference.py:93 topology declares 6 entity(ies) for more than one device; their samples will be rejected as ambiguous
=========================== short test summary info ============================
FAILED tests/end_to_end/test_event_intake.py::test_linked_event_produces_an_acceptable_sample_shape
```

### config_valid (exit 2)

```text
    "default_sample_interval_s": 10.0,
    "inference_interval_s": 5.0,
    "log_level": "INFO",
    "history_lookback_s": 86400.0,
    "audit_enabled": true,
    "audit_isolation_enabled": true,
    "audit_association_enabled": true,
    "audit_circuits_enabled": true,
    "audit_residual_enabled": true,
    "audit_hierarchy_enabled": true,
    "audit_records_enabled": true,
    "analysis_offset_s": 0.0,
    "isolation_window_s": 30.0,
    "isolation_power_window_s": 60.0,
    "isolation_by_class": {},
    "isolation_by_device": {},
    "isolation_device_classes": {},
    "audit_algorithm_version": "disagg@2.0",
    "reanalysis_interval_s": 0.0,
    "reanalysis_lookback_s": 21600.0,
    "train_interval_s": 0.0
  },
  "contract_version": "2.4.0",
  "engine_version": "0.2.1-dev.42"
}
```


## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
