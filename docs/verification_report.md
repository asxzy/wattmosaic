# Verification Report

Generated: `2026-11-10T15:21:16.727986+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.41 |  |
| lint | **PASS** | 0.04 |  |
| unit_tests | **PASS** | 8.6 |  |
| integration_tests | **PASS** | 2.38 |  |
| simulation_tests | **PASS** | 12.32 |  |
| end_to_end_tests | **PASS** | 46.24 |  |
| manifest_valid | **PASS** | 0.07 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.42 |  |
| deployment_valid | **PASS** | 2.17 |  |
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
