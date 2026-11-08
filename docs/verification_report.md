# Verification Report

Generated: `2026-11-08T04:38:32.089542+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| secret_scan | **PASS** | 0.35 |  |
| unit_tests | **PASS** | 8.17 |  |
| integration_tests | **PASS** | 2.97 |  |
| simulation_tests | **PASS** | 15.26 |  |
| end_to_end_tests | **PASS** | 45.71 |  |
| manifest_valid | **PASS** | 0.07 |  |
| docs_fresh | **PASS** | 0.48 |  |
| deployment_valid | **PASS** | 2.17 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.28 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
