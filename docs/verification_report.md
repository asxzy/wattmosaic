# Verification Report

Generated: `2026-11-09T00:05:16.232671+00:00`

**Required gates passed: 10/10**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.28 |  |
| secret_scan | **PASS** | 0.44 |  |
| unit_tests | **PASS** | 7.83 |  |
| integration_tests | **PASS** | 2.13 |  |
| simulation_tests | **PASS** | 12.52 |  |
| end_to_end_tests | **PASS** | 45.36 |  |
| manifest_valid | **PASS** | 0.08 |  |
| docs_fresh | **PASS** | 0.48 |  |
| deployment_valid | **PASS** | 2.49 |  |
| hacs_valid | **PASS** | 0.17 |  |
| config_valid | **PASS** | 0.76 | requires HA_TOKEN in environment |

## Failures

None.

## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
