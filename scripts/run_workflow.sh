#!/usr/bin/env bash
# =============================================================================
# Unified end-to-end WattMosaic workflow: build -> migrate -> smoke -> observe.
#
# One command drives the full post-change verification path against a running
# deployment. Each stage must succeed before the next begins (``set -e``).
#
# Usage:
#   scripts/run_workflow.sh [BASE_URL]
#
# Stages:
#   1. build     stage a self-contained App with scripts/package_addon.sh --check
#   2. migrate   apply pending DB migrations to WM_DATABASE_PATH (default
#                /data/wattmosaic.db) using the venv python; forward-only and
#                idempotent. Skip with WM_SKIP_MIGRATE=1 for a remote host.
#   3. smoke     run scripts/smoke_test.sh BASE_URL; exits non-zero on any FAIL.
#   4. observe   poll /api/v1/health for WM_OBSERVE_COUNT times (default 5)
#                at WM_OBSERVE_INTERVAL seconds (default 5), failing if the
#                engine leaves ok/degraded or becomes unreachable.
#
# Environment:
#   WM_API_TOKEN        sent as X-API-Token on API calls (both smoke + observe)
#   WM_DATABASE_PATH    database file for the migrate stage
#   WM_SKIP_MIGRATE=1   skip local migration (e.g. DB lives on another host)
#   WM_SKIP_BUILD=1     skip the local staging build check
#   WM_OBSERVE_COUNT    health polls in the observe stage (default 5)
#   WM_OBSERVE_INTERVAL seconds between polls (default 5)
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${1:-${WM_SMOKE_BASE_URL:-http://127.0.0.1:8787}}"
BASE_URL="${BASE_URL%/}"
TOKEN="${WM_API_TOKEN:-}"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

CURL_OPTS=(-fsS --connect-timeout 5 --max-time 15)
if [[ -n "${TOKEN}" ]]; then
  CURL_OPTS+=(-H "X-API-Token: ${TOKEN}")
fi

banner() { printf '\n== %s ==\n' "$1"; }

banner 'build'
if [[ "${WM_SKIP_BUILD:-0}" == '1' ]]; then
  echo 'build: skipped (WM_SKIP_BUILD=1)'
else
  bash "${REPO_ROOT}/scripts/package_addon.sh" --check
fi

banner 'migrate'
if [[ "${WM_SKIP_MIGRATE:-0}" == '1' ]]; then
  echo 'migrate: skipped (WM_SKIP_MIGRATE=1)'
else
  DB_PATH="${WM_DATABASE_PATH:-/data/wattmosaic.db}"
  if [[ ! -f "${DB_PATH}" ]]; then
    echo "migrate: ${DB_PATH} not present locally; set WM_SKIP_MIGRATE=1 for a remote DB" >&2
    exit 1
  fi
  PYTHONPATH="${REPO_ROOT}/engine" "${PYTHON}" -c \
    "from wattmosaic_engine.storage import LATEST_VERSION; from wattmosaic_engine.storage.database import Database; db=Database('${DB_PATH}'); assert db.schema_version==LATEST_VERSION, (db.schema_version, LATEST_VERSION); print('migrate: schema', db.schema_version, 'current')"
fi

banner 'smoke'
bash "${REPO_ROOT}/scripts/smoke_test.sh" "${BASE_URL}"

banner 'observe'
COUNT="${WM_OBSERVE_COUNT:-5}"
INTERVAL="${WM_OBSERVE_INTERVAL:-5}"
for ((i = 1; i <= COUNT; i++)); do
  body="$(mktemp "${TMPDIR:-/tmp}/wm-observe.XXXXXX")"
  if ! curl "${CURL_OPTS[@]}" "${BASE_URL}/api/v1/health" -o "${body}"; then
    echo "observe ${i}/${COUNT}: health unreachable" >&2
    rm -f "${body}"
    exit 1
  fi
  state="$(PYTHONPATH="${REPO_ROOT}/engine" "${PYTHON}" -c \
    "import json,sys; print(json.load(open(sys.argv[1])).get('status','unknown'))" "${body}")"
  rm -f "${body}"
  echo "observe ${i}/${COUNT}: ${state}"
  if [[ "${state}" != 'ok' && "${state}" != 'degraded' ]]; then
    echo "observe: engine left ok/degraded (${state})" >&2
    exit 1
  fi
  if [[ "${i}" -lt "${COUNT}" ]]; then sleep "${INTERVAL}"; fi
done

echo 'workflow: build -> migrate -> smoke -> observe all succeeded'
