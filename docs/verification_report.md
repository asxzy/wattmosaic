# Verification Report

Generated: `2026-11-08T23:26:10.975477+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.16 |  |
| secret_scan | **PASS** | 0.37 |  |
| unit_tests | **PASS** | 8.11 |  |
| integration_tests | **PASS** | 2.06 |  |
| simulation_tests | **PASS** | 12.31 |  |
| end_to_end_tests | **PASS** | 43.21 |  |
| manifest_valid | **PASS** | 0.06 |  |
| docs_fresh | **PASS** | 0.4 |  |
| deployment_valid | **PASS** | 2.07 |  |
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
