# Troubleshooting

Ordered from "first thing to check" outward. Every command is read-only.

## 1. Quick triage

```bash
# Is the engine alive?
curl -s http://localhost:8787/api/v1/health | jq

# Is it talking to Home Assistant?
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/health | jq '{status,ha_connected,database}'

# What does the engine think the channels are doing?
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/quality | jq
```

If `ha_connected` is `false`, go to §2. If `status` is `degraded`, read the
`details` object — it names the failing subsystem.

---

## 2. Engine cannot connect to Home Assistant

Symptom: `ha_connected: false`, log shows `ha_auth_failed` or repeated
`ha_connect_retry`.

| Cause | Check | Fix |
| --- | --- | --- |
| Wrong host/port | `curl -s -o /dev/null -w '%{http_code}' http://<ha>:8123/api/` | Must return `200`. `HA_HOST` without a port defaults to `8123`. |
| Token invalid/expired | `curl -s -H "Authorization: Bearer $HA_TOKEN" http://<ha>:8123/api/` | `401` means regenerate the long-lived token. |
| Token leaked into a log | — | Rotate it immediately; tokens are read-only but still sensitive. |
| App cannot reach HA | Inside the App container: `curl http://homeassistant:8123/api/` | Use `http://homeassistant:8123`, not the LAN IP, from the App. |
| Self-signed TLS | Log shows certificate verification error | Set `WM_HA_VERIFY_SSL=false` **only** on a trusted local network. |
| HA restarting | `state` in `/api/config` | Wait; the engine reconnects automatically with backoff up to 60 s. |

The engine never gives up: it retries with jittered exponential backoff
indefinitely. A persistent failure is configuration, not a transient bug.

---

## 3. Integration shows entities as unavailable

1. Confirm the engine is up (§1).
2. Confirm the integration's **scan interval** has elapsed.
3. Check the result age: entities become unavailable when results exceed
   `stale_after` (default 120 s) or the engine is unreachable for
   `unavailable_after` (default 120 s).
4. Check the integration's diagnostics download (Settings → Devices & Services →
   WattMosaic → ⋮ → Download diagnostics). It shows engine health, contract
   version, last error and topology status.

This behaviour is intentional. A stale value presented as a current measurement
would be a correctness bug.

### A single device is unavailable while its neighbours are fine

Usually an **idle circuit on a push-on-change sensor**, not a fault.

Many power integrations publish only when the value changes. A circuit that is
genuinely off stops publishing, so its last state sits at `0.0 W` and stops
advancing. Once the reading is older than `stale_after`, the engine reports the
channel as `missing`, and any device measured only by that channel becomes
`unavailable`.

Confirm it from the engine:

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/quality \
  | jq '.channels | to_entries[] | select(.value.status == "missing") | .key'
```

The channel is named there as `missing_sample (no sample in window)`. If the
device draws power again, its state publishes immediately — no restart or
reconfiguration is needed.

WattMosaic does **not** invent a `0 W` reading from silence: a held value is not
a new measurement. See ADR-0015.

---

## 4. `contract_mismatch` on setup

The integration and engine disagree on the API contract version (shown in the
`X-WattMosaic-Contract-Version` header).

- Update both to the same release. The versions are released together.
- Do not edit the version header to force compatibility; the schemas may have
  changed incompatibly.

---

## 5. Power values look wrong

### 5.1 Every device reads zero

- Check that the CT entities actually update: `/api/v1/quality` reports
  `median_interval_s` per channel. All-null intervals mean no subscription data.
- Check `WM_STALE_AFTER_S`: if your CT integration's poll is slower than this on
  your installation, readings will be marked stale. A typical polling CT
  integration reports ~15.7 s median; if yours is slower, raise the threshold.

### 5.2 Main power roughly doubles the sum of branches

This is the classic nested-CT double-count. Verify in
`GET /api/v1/topology` that a branch CT is not also listed as an independent
main, and that panel B is not modelled as a sibling of panel A when
it is physically downstream. See `docs/electrical_model.md` §nesting.

### 5.3 One device absorbs all the residual

It should not. If it does, inspect the device's `explanation` in
`GET /api/v1/devices/{id}`:

- `ambiguous_concurrent_events` — the solver could not separate them. This is
  correct behaviour.
- A suspiciously high confidence with a large `unknown_residual_w` indicates a
  topology error, not an algorithm error. Re-check CT roles.

### 5.4 Negative power appears

Negative means reverse flow (solar export, or an inverted CT). This is preserved
by design. If a CT is physically reversed, set its polarity to `inverted` in the
integration options.

---

## 6. A device never gets a model

- `model_status: pending` means too few accepted samples. Check
  `/api/v1/models/{device_id}` for sample counts and rejection reasons.
- If most samples are rejected with `concurrent_events`, the device usually runs
  at the same time as others. That is an electrical measurement limitation, not
  a bug. The engine will not fabricate a model from confounded data.
- If samples are rejected with `stale_measurement`, your CT cadence is slower than
  expected; raise `WM_STALE_AFTER_S`.

Sample rejection reasons are enumerated and each one is surfaced in diagnostics.

---

## 7. Energy counters reset or jump

- Energy is integrated over actual elapsed time. A gap longer than
  `stale_after` deliberately accrues **no** energy rather than guessing.
- On engine restart the last incomplete bucket is recomputed from stored results,
  so counters continue rather than reset.
- If the Home Assistant entity loses state restoration, it resumes from the
  engine's stored total on the next snapshot.

---

## 8. Database problems

```bash
# Integrity and size
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/status | jq '.database'

# From the host
sqlite3 /data/wattmosaic.db "PRAGMA integrity_check; PRAGMA user_version;"
```

| Symptom | Meaning | Action |
| --- | --- | --- |
| `database: corrupt` | integrity check failed | Stop the engine, restore a backup. Do not delete the file first — recover what you can. |
| Database grows without bound | Retention not running or set too high | Lower `WM_RETENTION_DAYS_RAW`; retention runs on a timer. |
| `database is locked` | Another process opened the file | Run only one engine instance per database. |

The engine refuses to start on a corrupt database rather than silently losing
history.

---

## 9. App does not start

1. **Logs** tab of the App: look for `config_invalid` with the specific problem.
2. Confirm `ha_token` is set in App options (it is a password field and is not
   shown after saving).
3. Confirm `database_path` is `/data/...` so it is inside the persistent volume.
4. Confirm the architecture is supported (`amd64` or `aarch64`).
5. `watchdog: true` means the Supervisor restarts a hung App; repeated restarts
   usually indicate a missing token or an unreachable HA URL.

### 9.1 The Supervisor says "Timeout while waiting for app … to start"

This message means the container ran but never reported healthy within 120
seconds. Check the App log for one of these:

| Log line | Cause | Fix |
| --- | --- | --- |
| `server rejected WebSocket connection: HTTP 401` repeating, and Supervisor logs `No API token provided for /core:8123/api/websocket` | The Core proxy URL was mangled by a default-port heuristic: `http://supervisor/core` became `http://supervisor/core:8123`. | Fixed in 0.1.1 and later. On an older image, set `ha_token` to a long-lived user token; the direct `http://homeassistant:8123` path is unaffected. |
| `unhealthy: engine rejected WM_API_TOKEN (401)` | The container health probe tried to authenticate directly, but `WM_API_TOKEN` is set inside PID 1 and is not visible to a Docker `HEALTHCHECK` exec. | Fixed in 0.1.1 and later; the probe now uses the gateway's `/healthz`. |
| `engine_starting` present but `ha_connected` never appears | The App cannot reach Home Assistant at `ha_url`. | Verify `ha_url` is `http://homeassistant:8123` (no trailing path). |
| Engine is healthy but ingestion stalls for ~1 s at a time, and `/api/v1/health` takes seconds | A whole-file `PRAGMA integrity_check` was running on the health path. On a 392 MB database it costs ~1.2 s (6.8 s cold), and the 5 s probe polls it. | Fixed in 0.1.2. `health` now uses a cheap liveness query and reports the startup scan's cached verdict. |

An empty `api_token` is **not** a startup problem: the API is then served only
to loopback and the Supervisor's internal network, which is enough for the
integration and the ingress panel. Set it if you publish a host port.

If you are upgrading from 0.1.0, install the new App version and restart it; the
database and options are preserved.

---

## 10. Working with support

Collect these **before** asking for help:

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/diagnostics > diagnostics.json
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/status > status.json
```

`/api/v1/diagnostics` is designed to be shareable: tokens and secrets are
redacted. Still skim it before posting publicly.

Never paste `.env`, raw tokens, or the `Authorization` header into an issue.
