# Verification Report

Generated: `2026-11-09T16:54:52.068263+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.4 |  |
| unit_tests | **PASS** | 8.77 |  |
| integration_tests | **PASS** | 2.36 |  |
| simulation_tests | **PASS** | 13.07 |  |
| end_to_end_tests | **PASS** | 48.67 |  |
| manifest_valid | **PASS** | 0.07 |  |
| docs_fresh | **PASS** | 0.44 |  |
| deployment_valid | **PASS** | 2.15 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.6 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
