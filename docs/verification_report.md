# Verification Report

Generated: `2026-11-08T22:29:42.003000+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.19 |  |
| secret_scan | **PASS** | 0.33 |  |
| unit_tests | **PASS** | 8.3 |  |
| integration_tests | **PASS** | 2.13 |  |
| simulation_tests | **PASS** | 12.59 |  |
| end_to_end_tests | **PASS** | 43.2 |  |
| manifest_valid | **PASS** | 0.12 |  |
| docs_fresh | **PASS** | 0.5 |  |
| deployment_valid | **PASS** | 2.47 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **PASS** | 0.74 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
