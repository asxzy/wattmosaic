#!/usr/bin/env bash
# =============================================================================
# Build a self-contained Home Assistant App directory for WattMosaic.
#
# The Supervisor builds a local app with the *app folder* as the Docker build
# context.  The app therefore needs a copy of the engine package, pyproject.toml
# and the pinned constraints next to its Dockerfile.  This script stages that
# directory without touching the sources:
#
#   dist/addon/wattmosaic_engine/
#     ├── config.yaml          (option/schema contract, read by the Supervisor)
#     ├── Dockerfile           (addon/Dockerfile, unmodified)
#     ├── run.sh, gateway.py, healthcheck.py, discovery.py
#     ├── constraints.txt
#     ├── icon.png, logo.png
#     ├── README.md
#     ├── pyproject.toml
#     └── engine/wattmosaic_engine/...
#
# Install with one of:
#   scripts/package_addon.sh --install root@homeassistant   (rsync over SSH)
#   scripts/package_addon.sh && cp -r dist/addon/wattmosaic_engine /local_apps/
#
# Options:
#   --out DIR        staging parent (default: dist/addon)
#   --install HOST   rsync the result to HOST:{/local_apps,/addons}/wattmosaic_engine
#                    (probed; 2026.11 moved the mount to /local_apps)
#   --keep           do not delete an existing staging directory first
#   --check          stage into a temp dir and only verify the layout
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_PARENT="${REPO_ROOT}/dist/addon"
APP_NAME="wattmosaic_engine"
INSTALL_HOST=""
KEEP=0
CHECK_ONLY=0

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_PARENT="$2"; shift 2 ;;
    --install) INSTALL_HOST="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  OUT_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/wm-addon.XXXXXX")"
  trap 'rm -rf "${OUT_PARENT}"' EXIT
fi

STAGE="${OUT_PARENT}/${APP_NAME}"

if [[ "${KEEP}" -eq 0 && -d "${STAGE}" ]]; then
  rm -rf "${STAGE}"
fi
mkdir -p "${STAGE}"

copy() {
  local src="$1" dst="$2"
  [[ -e "${src}" ]] || { echo "missing: ${src}" >&2; exit 1; }
  # Nested destinations (e.g. engine/wattmosaic_engine) need their parents;
  # ``cp -R`` creates only the final component.
  mkdir -p "$(dirname "${dst}")"
  cp -R "${src}" "${dst}"
}

# App metadata and entrypoint assets.
for f in config.yaml Dockerfile run.sh gateway.py healthcheck.py discovery.py \
         constraints.txt icon.png logo.png README.md; do
  copy "${REPO_ROOT}/addon/${f}" "${STAGE}/${f}"
done

# The ingress panel.  It is static (HTML/CSS/JS/SVG) and has no build step, so
# staging it is a plain directory copy.  The Dockerfile serves it from /app/ui.
copy "${REPO_ROOT}/addon/ui" "${STAGE}/ui"

# Python package sources (the Supervisor has no network access to PyPI beyond
# the build, and must not clone the repo).
copy "${REPO_ROOT}/engine/wattmosaic_engine" "${STAGE}/engine/wattmosaic_engine"

# Packaging metadata the Dockerfile installs from.  README.md is required by
# pyproject.toml ([project].readme); the app README doubles as that file.
copy "${REPO_ROOT}/pyproject.toml" "${STAGE}/pyproject.toml"
cp "${REPO_ROOT}/addon/README.md" "${STAGE}/README.md"
copy "${REPO_ROOT}/LICENSE" "${STAGE}/LICENSE"

# Drop local caches/bytecode so the staged tree is clean and reproducible.
find "${STAGE}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${STAGE}" -name '*.py[co]' -delete 2>/dev/null || true

# Remove the prebuilt-image reference from the *staged* copy.
#
# In the repository, `image:` tells the Supervisor to pull the published
# multi-arch image (the repository app folder cannot build on its own, because
# it has no engine/).  A staged copy is the opposite case: it is self-contained
# by construction, so the Supervisor must build it locally.  Leaving `image:`
# in would make a copy-install silently pull instead of build, which defeats
# the point of staging and breaks the offline/no-registry install path.
sed -i.bak '/^image: /d' "${STAGE}/config.yaml" && rm -f "${STAGE}/config.yaml.bak"

chmod 0755 "${STAGE}/run.sh" "${STAGE}/discovery.py"

# Sanity checks: the build context must be self-sufficient.
required=(config.yaml Dockerfile run.sh gateway.py healthcheck.py discovery.py
          constraints.txt icon.png logo.png pyproject.toml README.md
          ui/index.html ui/app.css ui/app.js ui/mark.svg
          engine/wattmosaic_engine/main.py)
missing=0
for f in "${required[@]}"; do
  if [[ ! -e "${STAGE}/${f}" ]]; then
    echo "staging incomplete: ${f} missing" >&2
    missing=1
  fi
done
[[ "${missing}" -eq 0 ]] || exit 1

echo "staged app: ${STAGE}"
du -sh "${STAGE}" 2>/dev/null || true

if [[ -n "${INSTALL_HOST}" ]]; then
  if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync is required for --install" >&2
    exit 1
  fi
  # Home Assistant 2026.11 renamed the local-apps mount from /addons to
  # /local_apps (Supervisor migrates addons/{core,data,local,git} to
  # apps/{core,data,local,git} on disk).  The Terminal & SSH app follows the
  # new map, so probe for it instead of assuming either name.
  REMOTE_DIR="/local_apps"
  if ! ssh -o BatchMode=yes "${INSTALL_HOST}" "test -d /local_apps" 2>/dev/null; then
    REMOTE_DIR="/addons"
  fi
  echo "installing to ${INSTALL_HOST}:${REMOTE_DIR}/${APP_NAME}/"
  rsync -az --delete \
    -e "ssh -o BatchMode=yes" \
    "${STAGE}/" "${INSTALL_HOST}:${REMOTE_DIR}/${APP_NAME}/"
  echo "done. In Home Assistant: Settings > Apps > App store > Check for updates."
fi
