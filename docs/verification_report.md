# Verification Report

Generated: `2026-11-10T18:41:46.226945+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.25 |  |
| secret_scan | **PASS** | 0.49 |  |
| lint | **PASS** | 0.06 |  |
| unit_tests | **PASS** | 10.05 |  |
| integration_tests | **PASS** | 3.98 |  |
| simulation_tests | **PASS** | 16.53 |  |
| end_to_end_tests | **PASS** | 61.96 |  |
| manifest_valid | **PASS** | 0.1 |  |
| release_valid | **PASS** | 0.07 |  |
| docs_fresh | **PASS** | 0.62 |  |
| deployment_valid | **PASS** | 3.25 |  |
| hacs_valid | **PASS** | 0.06 |  |
| config_valid | **PASS** | 1.07 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
