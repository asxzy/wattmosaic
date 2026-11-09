# Verification Report

Generated: `2026-11-09T17:29:43.602073+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.2 |  |
| secret_scan | **PASS** | 0.43 |  |
| unit_tests | **PASS** | 8.69 |  |
| integration_tests | **PASS** | 3.11 |  |
| simulation_tests | **PASS** | 13.78 |  |
| end_to_end_tests | **PASS** | 56.19 |  |
| manifest_valid | **PASS** | 0.11 |  |
| docs_fresh | **PASS** | 0.55 |  |
| deployment_valid | **PASS** | 2.74 |  |
| hacs_valid | **PASS** | 0.04 |  |
| config_valid | **PASS** | 0.77 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
