# Verification Report

Generated: `2026-11-08T22:27:16.498015+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.37 |  |
| unit_tests | **PASS** | 8.67 |  |
| integration_tests | **PASS** | 3.05 |  |
| simulation_tests | **PASS** | 12.77 |  |
| end_to_end_tests | **PASS** | 43.13 |  |
| manifest_valid | **PASS** | 0.07 |  |
| docs_fresh | **PASS** | 0.42 |  |
| deployment_valid | **PASS** | 2.3 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.64 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
