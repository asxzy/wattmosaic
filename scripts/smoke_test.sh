#!/usr/bin/env bash
# =============================================================================
# Post-deploy smoke test for WattMosaic.
#
# Runs a fixed set of checks against a *running* deployment and exits NON-ZERO
# if ANY check fails. Every check runs even after a failure, so one invocation
# reports the full picture; the final exit status is the aggregate.
#
# This is deliberately NOT the App watchdog probe (addon/healthcheck.py probes
# /healthz only). The smoke test covers the full post-deploy surface: the
# gateway/engine health, status, the ingress panel, applied migrations, the
# residual/matrix/snapshot/topology/devices endpoints and the metrics scrape.
#
# Usage:
#   scripts/smoke_test.sh [BASE_URL]
#
#   BASE_URL defaults to ${WM_SMOKE_BASE_URL:-http://127.0.0.1:8787}.
#   WM_API_TOKEN, when set, is sent as ``X-API-Token`` on every API call.
#   WM_DATABASE_PATH (default /data/wattmosaic.db) is used for the "migrations
#   current" check via PRAGMA user_version compared with LATEST_VERSION.
#
# Dependencies: curl, and the repo venv's python (for JSON assertions and the
# migration check). No new runtime dependency is introduced.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${1:-${WM_SMOKE_BASE_URL:-http://127.0.0.1:8787}}"
BASE_URL="${BASE_URL%/}"
TOKEN="${WM_API_TOKEN:-}"
DB_PATH="${WM_DATABASE_PATH:-/data/wattmosaic.db}"

# Choose a python that can import the engine package for the migration check.
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

FAILURES=0

# Shared curl options. -f makes HTTP >=400 a curl failure (so a 401/404/500 is
# a FAIL, never a silent success); timeouts keep a hung endpoint from stalling
# the whole run.
CURL_OPTS=(-fsS --connect-timeout 5 --max-time 15)
if [[ -n "${TOKEN}" ]]; then
  CURL_OPTS+=(-H "X-API-Token: ${TOKEN}")
fi

# Record one check's verdict. Any FAIL increments FAILURES; nothing is ever
# swallowed, so the aggregate exit code is non-zero if this check failed.
pass() { printf 'PASS  %s\n' "$1"; }
fail() {
  printf 'FAIL  %s: %s\n' "$1" "$2" >&2
  FAILURES=$((FAILURES + 1))
}

# Fetch a URL into a temp file. Prints the path on stdout; returns non-zero on
# a transport or HTTP error (the caller records the FAIL).
fetch() {
  local url="$1" out
  out="$(mktemp "${TMPDIR:-/tmp}/wm-smoke.XXXXXX")"
  if ! curl "${CURL_OPTS[@]}" "${url}" -o "${out}"; then
    rm -f "${out}"
    return 1
  fi
  printf '%s' "${out}"
}

# Assert, via python, that a JSON file satisfies a small expression. Avoids a
# jq dependency. EXPR is evaluated with ``data`` bound to the parsed payload.
json_ok() {
  local file="$1" expr="$2"
  PYTHONPATH="${REPO_ROOT}/engine" "${PYTHON}" -c \
    "import json,sys; data=json.load(open(sys.argv[1])); sys.exit(0 if (${expr}) else 1)" \
    "${file}"
}

# --------------------------------------------------------------------------- #
# Checks. Each prints exactly one PASS/FAIL line and never exits the script.
# --------------------------------------------------------------------------- #

check_health() {
  local url="${BASE_URL}/api/v1/health" body
  if ! body="$(fetch "${url}")"; then fail health "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, dict) and data.get('status') in ('ok', 'degraded')"; then
    fail health "unexpected health payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass health
}

check_status() {
  local url="${BASE_URL}/api/v1/status" body
  if ! body="$(fetch "${url}")"; then fail status "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, dict) and 'status' in data"; then
    fail status "unexpected status payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass status
}

check_panel() {
  # The gateway serves the ingress panel index at /. Non-ingress clients get a
  # small JSON descriptor; either a JSON descriptor or HTML proves reachability.
  local url="${BASE_URL}/" body
  if ! body="$(fetch "${url}")"; then fail panel "GET ${url} failed"; return; fi
  if ! grep -qiE 'wattmosaic|<!doctype html|<html' "${body}"; then
    fail panel "index did not look like the WattMosaic panel: $(head -c 200 "${body}")"
    rm -f "${body}"; return
  fi
  rm -f "${body}"; pass panel
}

check_migrations() {
  # The engine applies migrations at start. Confirm the on-disk database is at
  # the latest schema: PRAGMA user_version == LATEST_VERSION. Falls back to the
  # diagnostics endpoint's database.schema_current when the DB path is not
  # reachable from this host (e.g. smoke run off-container).
  if [[ -f "${DB_PATH}" ]]; then
    local version latest
    version="$(PYTHONPATH="${REPO_ROOT}/engine" "${PYTHON}" -c \
      "import sqlite3; print(sqlite3.connect('file:${DB_PATH}?mode=ro', uri=True).execute('PRAGMA user_version').fetchone()[0])" 2>/dev/null || true)"
    latest="$(PYTHONPATH="${REPO_ROOT}/engine" "${PYTHON}" -c \
      "from wattmosaic_engine.storage import LATEST_VERSION; print(LATEST_VERSION)" 2>/dev/null || true)"
    if [[ -n "${version}" && -n "${latest}" && "${version}" == "${latest}" ]]; then
      pass migrations; return
    fi
    fail migrations "schema ${version:-unknown} != latest ${latest:-unknown} (${DB_PATH})"; return
  fi
  # Off-container fallback: diagnostics reports database.schema_current.
  local url="${BASE_URL}/api/v1/diagnostics" body
  if ! body="$(fetch "${url}")"; then
    fail migrations "DB ${DB_PATH} unavailable and diagnostics unreachable"; return
  fi
  if ! json_ok "${body}" "isinstance(data, dict) and (data.get('database') or {}).get('schema_current') is True"; then
    fail migrations "diagnostics did not report database.schema_current: $(head -c 200 "${body}")"
    rm -f "${body}"; return
  fi
  rm -f "${body}"; pass migrations
}

check_residuals() {
  local url="${BASE_URL}/api/v1/residuals" body
  if ! body="$(fetch "${url}")"; then fail residuals "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, dict)"; then
    fail residuals "unexpected residuals payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass residuals
}

check_matrix() {
  # The v3 measurement matrix (subjects x readers) replaced the legacy /mapping.
  local url="${BASE_URL}/api/v1/matrix" body
  if ! body="$(fetch "${url}")"; then fail matrix "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, dict)"; then
    fail matrix "unexpected matrix payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass matrix
}

check_snapshot() {
  # The v3 InferenceSnapshot. A freshly started engine that has not published a
  # snapshot yet answers 404 by contract ("not yet computed"); that is reachable
  # and correct, so it passes. Any other HTTP error or a malformed body fails.
  local url="${BASE_URL}/api/v1/snapshot" body code
  body="$(mktemp "${TMPDIR:-/tmp}/wm-smoke.XXXXXX")"
  local opts=(-sS --connect-timeout 5 --max-time 15)
  if [[ -n "${TOKEN}" ]]; then opts+=(-H "X-API-Token: ${TOKEN}"); fi
  if ! code="$(curl "${opts[@]}" -o "${body}" -w '%{http_code}' "${url}")"; then
    rm -f "${body}"; fail snapshot "GET ${url} failed"; return
  fi
  if [[ "${code}" == "404" ]]; then
    rm -f "${body}"; pass snapshot; return
  fi
  if [[ "${code}" != "200" ]]; then
    rm -f "${body}"; fail snapshot "GET ${url} returned HTTP ${code}"; return
  fi
  if ! json_ok "${body}" "isinstance(data, dict) and 'device_estimates' in data"; then
    fail snapshot "unexpected snapshot payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass snapshot
}

check_topology() {
  # The v3 topology read: lifecycle-tagged edges plus the active version.
  local url="${BASE_URL}/api/v1/topology/v3" body
  if ! body="$(fetch "${url}")"; then fail topology "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, dict)"; then
    fail topology "unexpected topology payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass topology
}

check_devices() {
  local url="${BASE_URL}/api/v1/devices" body
  if ! body="$(fetch "${url}")"; then fail devices "GET ${url} failed"; return; fi
  if ! json_ok "${body}" "isinstance(data, (list, dict))"; then
    fail devices "unexpected devices payload: $(head -c 200 "${body}")"; rm -f "${body}"; return
  fi
  rm -f "${body}"; pass devices
}

check_metrics() {
  local url="${BASE_URL}/api/v1/metrics" body
  if ! body="$(fetch "${url}")"; then fail metrics "GET ${url} failed"; return; fi
  if ! grep -q 'wattmosaic_' "${body}"; then
    fail metrics "metrics scrape had no wattmosaic_ series: $(head -c 200 "${body}")"
    rm -f "${body}"; return
  fi
  rm -f "${body}"; pass metrics
}

main() {
  echo "smoke: base URL ${BASE_URL}"
  check_health
  check_status
  check_panel
  check_migrations
  check_residuals
  check_matrix
  check_snapshot
  check_topology
  check_devices
  check_metrics
  if [[ "${FAILURES}" -gt 0 ]]; then
    echo "smoke: ${FAILURES} check(s) FAILED" >&2
    return 1
  fi
  echo "smoke: all checks passed"
  return 0
}

main "$@"
