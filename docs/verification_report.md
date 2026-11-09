# Verification Report

Generated: `2026-11-09T19:27:25.391105+00:00`

**Required gates passed: 11/11**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.38 |  |
| unit_tests | **PASS** | 9.21 |  |
| integration_tests | **PASS** | 3.32 |  |
| simulation_tests | **PASS** | 15.52 |  |
| end_to_end_tests | **PASS** | 55.31 |  |
| manifest_valid | **PASS** | 0.09 |  |
| release_valid | **PASS** | 0.06 |  |
| docs_fresh | **PASS** | 0.51 |  |
| deployment_valid | **PASS** | 3.36 |  |
| hacs_valid | **PASS** | 0.06 |  |
| config_valid | **PASS** | 1.27 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
