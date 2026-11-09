# Verification Report

Generated: `2026-11-09T06:35:31.430879+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.14 |  |
| secret_scan | **PASS** | 0.38 |  |
| unit_tests | **PASS** | 7.8 |  |
| integration_tests | **PASS** | 2.03 |  |
| simulation_tests | **PASS** | 11.6 |  |
| end_to_end_tests | **PASS** | 44.16 |  |
| manifest_valid | **PASS** | 0.07 |  |
| docs_fresh | **PASS** | 0.38 |  |
| deployment_valid | **PASS** | 2.0 |  |
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
