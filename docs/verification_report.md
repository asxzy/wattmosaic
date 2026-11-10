# Verification Report

Generated: `2026-11-10T07:40:34.767844+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| secret_scan | **PASS** | 0.49 |  |
| lint | **PASS** | 0.04 |  |
| unit_tests | **PASS** | 8.46 |  |
| integration_tests | **PASS** | 2.41 |  |
| simulation_tests | **PASS** | 12.42 |  |
| end_to_end_tests | **PASS** | 45.87 |  |
| manifest_valid | **PASS** | 0.07 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.43 |  |
| deployment_valid | **PASS** | 2.16 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.59 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
