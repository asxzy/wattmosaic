# Troubleshooting (contract 3.0)

Ordered from "first thing to check" outward. Every command is read-only.

## 1. Quick triage

```bash
# Is the engine alive?
curl -s http://localhost:8787/healthz

# Is it talking to Home Assistant, and which contract does it speak?
curl -s -H "X-API-Token: $WM_API_TOKEN" \
  http://localhost:8787/api/v1/health | jq '{status,ha_connected,contract_version,details}'

# Which responses carry the 3.0 contract marker?
curl -sI -H "X-API-Token: $WM_API_TOKEN" \
  http://localhost:8787/api/v1/health | grep -i x-wattmosaic-contract-version

# Event-loop health and endpoint quality
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/runtime-metrics | jq
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/quality | jq
```

If `ha_connected` is `false`, go to §2. If `status` is `degraded`, read the
`details` object — it names the failing subsystem. If the integration reports a
contract mismatch, upgrade the engine and integration together: both are at
**3.0.0** and no 2.x form is accepted.

## 2. Engine cannot connect to Home Assistant

Symptom: `ha_connected: false`, log shows `ha_auth_failed` or repeated
`ha_connect_retry`.

| Cause | Check | Fix |
| --- | --- | --- |
| Wrong host/port | `curl -s -o /dev/null -w '%{http_code}' http://<ha>:8123/api/` | Must return `200`. `HA_HOST` without a port defaults to `8123`. |
| Token invalid/expired | `curl -s -H "Authorization: Bearer $HA_TOKEN" http://<ha>:8123/api/` | `401` means regenerate the long-lived token. |
| App cannot reach HA | Inside the App container: `curl http://homeassistant:8123/api/` | Use the supervisor hostname, not the LAN IP. |
| Self-signed TLS | Log shows certificate verification error | Set `WM_HA_VERIFY_SSL=false` **only** on a trusted local network. |
| HA restarting | `state` in `/api/config` | Wait; the engine reconnects with jittered backoff. |

The engine never gives up: it retries with jittered exponential backoff
indefinitely. A persistent failure is configuration, not a transient bug.

## 3. A device shows `unknown` instead of watts

This is usually correct behaviour, not a fault. The HA power sensor publishes a
point only for `MEASURED`, or `ESTIMATED` + `IDENTIFIABLE`; everything else is
`None` (DEC-25).

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" \
  http://localhost:8787/api/v1/devices/<device_id> | jq
```

| `estimate_status` / `identifiability` | Meaning | Action |
| --- | --- | --- |
| `UNKNOWN`, `insufficient_evidence` | no accepted endpoints or no events yet | Wait for state changes; verify endpoints are accepted |
| `ESTIMATED`, `non_identifiable` | two devices share a row with no separating evidence | Check topology edges; the group sum is the honest answer |
| `ESTIMATED`, `group_identifiable` | member of a non-separable group | Read the group sensor, not a per-member number |
| `MEASURED` expected but absent | single-subject scope not confirmed | Confirm the plug/appliance-monitor scope |

Past 0 W history is not rewritten; `unknown` going forward leaves a gap in HA
statistics. That is intentional.

## 4. Nothing is being inferred

- Are any HA power entities **accepted as measurement endpoints**? Only accepted
  endpoints are ingested; discovery lists candidates but does not ingest until
  you accept them.
- Are events arriving? Check `/api/v1/runtime-metrics` for group counters. A
  `groups_dropped` count means the event queue (`WM_EVENT_QUEUE_MAX`) is full.
- Is the topology absent? `details.topology.configured = false` in health means
  `WM_TOPOLOGY_BOOTSTRAP` found nothing — configure endpoints and review edges.
- `WM_INFERENCE_INTERVAL_S` no longer drives inference: it is accepted and
  ignored. Do not expect changing it to change cadence (DEC-03).

## 5. Learning is blocked

| Engine health `details.learning_status` | Meaning | Action |
| --- | --- | --- |
| `samples_seen: 0`, `samples_rejected: 0` | dead intake — no state change reached the engine | Check `state_links` and `devices_without_state_entity` in diagnostics; links must resolve to runtime entities, never power sensors |
| `samples_seen: N > 0`, most rejected as `ambiguous_topology` | alive but blocked | Confirm the relevant topology edges / endpoint scopes |
| many `concurrent_events` | fixtures co-toggle | expected for scene-driven lights; use group estimates |
| `samples_accepted > 0` | learning works | nothing |

Legacy whole-circuit models are read-only (`LEGACY_ABSOLUTE`) and retrained from
delta evidence; a device may stay `model_status: pending` while its events are
always contaminated.

## 6. Migration / upgrade problems

The engine automatically writes a pre-migration backup before any migration
≥ 13 (`<db>.pre-vNNN.<timestamp>.bak`, newest three kept). There are **no
down-migrations**: rollback means reinstalling the previous engine version and
restoring the matching backup. If a migration fails mid-apply, `user_version` is
left unchanged. Legacy tables were archived (renamed `*_legacy`, read-only) by
migration v025. See [`docs/deployment.md`](deployment.md).

## 7. Contract / integration mismatch

- Every `/api/v1` response carries `X-WattMosaic-Contract-Version: 3.0.0`. An
  integration refusing to start against the engine means the two are on
  different contracts.
- `/mapping`, `PUT /topology`, `POST /topology/confirm`, `/confidence_tiers` and
  `/relations` were removed. Clients must use `/matrix`, the typed topology
  routes and the snapshot's estimate statuses.
- A stale panel showing old wording needs a fresh image (the gateway serves
  static `ui/` assets from the image).

## 8. Common container issues

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Container exits immediately, `no Home Assistant token` | `ha_token` empty and no `SUPERVISOR_TOKEN` | Set `ha_token`, or keep it empty with `homeassistant_api: true` in App mode |
| `no Home Assistant URL` | Standalone run without a URL | Set `HA_URL` / `HA_HOST` in `.env` |
| `integrity_check` fails at start | corrupt DB or bad restore | Restore a backup; copy the file aside before deleting anything |
| Database grows unexpectedly | raw retention too high | Lower `WM_RETENTION_DAYS_RAW`; evidence/version tiers are operator-bounded |
| Watchdog restarts repeatedly | `/healthz` returns 503 | Check engine logs; `/healthz` only turns 200 when `/api/v1/health` answers |
| `groups_dropped` climbing | event loop cannot keep up | Reduce endpoint churn or raise `WM_EVENT_QUEUE_MAX` |

### Collecting a diagnostic bundle

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/healthz
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/health | jq
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/diagnostics | jq > diagnostics.json
curl -s -H "X-API-Token: $WM_API_TOKEN" localhost:8787/api/v1/runtime-metrics | jq > runtime.json
docker logs --tail 200 <container> 2>&1 | tail -200
```

Diagnostics never include tokens; verify before sharing.
