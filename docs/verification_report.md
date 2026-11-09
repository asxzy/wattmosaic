# Verification Report

Generated: `2026-11-09T18:08:49.948407+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.19 |  |
| secret_scan | **PASS** | 0.41 |  |
| unit_tests | **PASS** | 9.34 |  |
| integration_tests | **PASS** | 3.06 |  |
| simulation_tests | **PASS** | 14.17 |  |
| end_to_end_tests | **PASS** | 62.61 |  |
| manifest_valid | **PASS** | 0.11 |  |
| docs_fresh | **PASS** | 0.57 |  |
| deployment_valid | **PASS** | 3.0 |  |
| hacs_valid | **PASS** | 0.04 |  |
| config_valid | **PASS** | 0.83 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
