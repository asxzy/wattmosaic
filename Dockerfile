# syntax=docker/dockerfile:1.7
#
# WattMosaic Engine — standalone image (generic Docker hosts, non-HA-OS).
#
# Build from the repository root:
#     docker build -t wattmosaic-engine:0.1.2 .
#
# The Home Assistant App image lives in addon/Dockerfile and is functionally
# identical at runtime; only the build context differs (the App build context is
# addon/, because the Supervisor mounts the app directory at /addon).  Keep the
# two runtime stages in sync — scripts/validate_addon.sh fails if they diverge.
#
# Runtime layout:
#   /opt/venv          engine + pinned dependencies (no build tools)
#   /app               run.sh, gateway.py, healthcheck.py
#   /data              persistent SQLite database (volume)
#   :8787              gateway (API + unauthenticated /healthz)
#   :8788 (loopback)   engine API, requires X-API-Token

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --------------------------------------------------------------------------- #
# Stage 1 — builder: resolve pinned wheels into a relocatable virtualenv
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade \
        "pip==25.0.1" "setuptools==80.9.0" "wheel==0.45.1"

COPY addon/constraints.txt ./
COPY pyproject.toml README.md ./
COPY engine ./engine

# --only-binary=:all: guarantees the final image never needs a toolchain and
# makes an accidental sdist a build error instead of a silent slowdown.
RUN /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: \
        -c constraints.txt . \
 && /opt/venv/bin/pip check

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="WattMosaic Engine" \
      org.opencontainers.image.description="Multi-CT load disaggregation engine for WattMosaic" \
      org.opencontainers.image.version="0.1.2" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/asxzy/wattmosaic"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    WM_DATABASE_PATH="/data/wattmosaic.db" \
    WM_API_HOST="127.0.0.1" \
    WM_API_PORT="8788" \
    WM_GATEWAY_PORT="8787" \
    WM_GATEWAY_ENABLED="true" \
    WM_LOG_LEVEL="INFO"

RUN groupadd --gid 10001 wattmosaic \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin wattmosaic \
 && install -d -o 10001 -g 10001 -m 0750 /data /app

COPY --from=builder /opt/venv /opt/venv
COPY addon/run.sh addon/gateway.py addon/healthcheck.py addon/discovery.py /app/
COPY addon/ui /app/ui

RUN chmod 0755 /app/run.sh /app/gateway.py /app/healthcheck.py /app/discovery.py

WORKDIR /app
VOLUME ["/data"]
EXPOSE 8787

# Runs as root only to fix /data ownership, then drops to UID 10001
# (no_new_privs, all capabilities dropped). See addon/run.sh.
# Keep the timing in step with addon/Dockerfile: the Supervisor gives an app
# only 120 s to leave STARTUP.
HEALTHCHECK --interval=5s --timeout=8s --start-period=60s --retries=3 \
    CMD ["python3", "/app/healthcheck.py"]

ENTRYPOINT ["/app/run.sh"]
CMD []
