# Verification Report

Generated: `2026-11-19T23:02:19.283136+00:00`

**Required gates passed: 13/13**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| spec_status | **PASS** | 0.05 |  |
| secret_scan | **PASS** | 1.06 |  |
| lint | **PASS** | 0.1 |  |
| unit_tests | **PASS** | 66.88 |  |
| integration_tests | **PASS** | 25.81 |  |
| simulation_tests | **PASS** | 29.17 |  |
| end_to_end_tests | **PASS** | 4.83 |  |
| manifest_valid | **PASS** | 0.05 |  |
| release_valid | **PASS** | 0.03 |  |
| docs_fresh | **PASS** | 0.39 |  |
| deployment_valid | **PASS** | 2.92 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **FAIL** | 0.54 | requires HA_TOKEN in environment |

## Failures

### config_valid (exit 2)

```text
    "evidence_retention_days": 400,
    "evidence_raw_retention_days": 30,
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
  "contract_version": "3.0.0",
  "engine_version": "0.2.1-dev.42"
}
```


## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
