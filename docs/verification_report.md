# Verification Report

Generated: `2026-11-10T01:55:39.383208+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| secret_scan | **PASS** | 0.41 |  |
| lint | **PASS** | 0.04 |  |
| unit_tests | **PASS** | 8.58 |  |
| integration_tests | **PASS** | 2.57 |  |
| simulation_tests | **PASS** | 12.0 |  |
| end_to_end_tests | **PASS** | 45.09 |  |
| manifest_valid | **PASS** | 0.07 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.42 |  |
| deployment_valid | **PASS** | 2.11 |  |
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
