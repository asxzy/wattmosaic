# Verification Report

Generated: `2026-11-09T20:06:56.085757+00:00`

**Required gates passed: 11/11**

| Gate | Status | Duration (s) | Notes |
| --- | --- | --- | --- |
| contract_schema_sync | **PASS** | 0.17 |  |
| secret_scan | **PASS** | 0.33 |  |
| unit_tests | **PASS** | 8.16 |  |
| integration_tests | **PASS** | 2.38 |  |
| simulation_tests | **PASS** | 12.92 |  |
| end_to_end_tests | **PASS** | 45.22 |  |
| manifest_valid | **PASS** | 0.06 |  |
| release_valid | **PASS** | 0.05 |  |
| docs_fresh | **PASS** | 0.43 |  |
| deployment_valid | **PASS** | 4.24 |  |
| hacs_valid | **PASS** | 0.03 |  |
| config_valid | **FAIL** | 0.29 | requires HA_TOKEN in environment |

## Failures

### config_valid (exit 2)

```text
{
  "ok": false,
  "problems": [
    "HA token is not configured (HA_TOKEN)"
  ],
  "config": {
    "ha_url": "<redacted-url>",
    "ha_token": "",
    "api_host": "<redacted-ip>",
    "api_port": 8787,
    "api_token": "",
    "database_path": "data/wattmosaic.db",
    "learning_enabled": true,
    "stale_after_s": 120.0,
    "inference_interval_s": 5.0,
    "log_level": "INFO"
  },
  "contract_version": "1.1.0",
  "engine_version": "0.1.2"
}
```


## How this is produced

```bash
python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md
```

A gate is only `PASS` if the command exited 0 in the orchestrator's own run.
Agent reports are not accepted as evidence.
