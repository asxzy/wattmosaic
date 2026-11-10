# Verification Report

Generated: `2026-11-10T01:33:19.743665+00:00`

**Required gates passed: 12/12**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.18 |  |
| secret_scan | **PASS** | 0.35 |  |
| lint | **PASS** | 0.06 |  |
| unit_tests | **PASS** | 8.27 |  |
| integration_tests | **PASS** | 2.14 |  |
| simulation_tests | **PASS** | 12.08 |  |
| end_to_end_tests | **PASS** | 40.73 |  |
| manifest_valid | **PASS** | 0.07 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.42 |  |
| deployment_valid | **PASS** | 2.12 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.25 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
