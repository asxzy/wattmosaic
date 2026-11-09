# Verification Report

Generated: `2026-11-09T20:15:25.577294+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.2 |  |
| secret_scan | **PASS** | 0.43 |  |
| lint | **PASS** | 0.09 |  |
| unit_tests | **PASS** | 8.71 |  |
| integration_tests | **PASS** | 2.51 |  |
| simulation_tests | **PASS** | 12.94 |  |
| end_to_end_tests | **PASS** | 50.44 |  |
| manifest_valid | **PASS** | 0.08 |  |
| release_valid | **PASS** | 0.06 |  |
| docs_fresh | **PASS** | 0.49 |  |
| deployment_valid | **PASS** | 2.38 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.7 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
