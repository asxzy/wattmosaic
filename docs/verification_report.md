# Verification Report

Generated: `2026-11-08T23:54:40.426671+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.21 |  |
| secret_scan | **PASS** | 0.43 |  |
| unit_tests | **PASS** | 8.97 |  |
| integration_tests | **PASS** | 2.85 |  |
| simulation_tests | **PASS** | 15.29 |  |
| end_to_end_tests | **PASS** | 52.03 |  |
| manifest_valid | **PASS** | 0.09 |  |
| docs_fresh | **PASS** | 0.55 |  |
| deployment_valid | **PASS** | 2.8 |  |
| hacs_valid | **PASS** | 0.05 |  |
| config_valid | **PASS** | 0.86 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
