"""Async HTTP client for the WattMosaic engine API.

The client is intentionally **pure Python**: it takes an injected
``aiohttp.ClientSession`` (Home Assistant supplies one via
``async_get_clientsession``) and an injectable sleep function, so the whole
module is testable without Home Assistant or a live engine.

Security
--------
* The API token is only ever placed in the ``X-API-Token`` request header.
* It is never included in URLs, logs, exception messages or ``repr``.
* All exception messages pass through :func:`util.sanitize_text`.

Contract
--------
Every response is validated against the authoritative contract in
``engine/wattmosaic_engine/contracts.py`` (version ``1.0.0``).  A missing
``X-WattMosaic-Contract-Version`` header is tolerated (older engine), a
different *major* version raises :class:`EngineContractMismatch`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .const import (
    API_BASE_PATH,
    CONTRACT_VERSION,
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REQUEST_TIMEOUT,
    HEADER_API_TOKEN,
    HEADER_CONTRACT_VERSION,
)
from .util import is_token_configured, sanitize_text

__all__ = [
    "EngineError",
    "EngineUnavailable",
    "EngineAuthError",
    "EngineProtocolError",
    "EngineContractMismatch",
    "EngineClient",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EngineError(Exception):
    """Base class for all engine client errors.

    ``str(error)`` is always safe to log: the token is scrubbed and only the
    request *path* (never the full URL with query string) is included.
    """

    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        status: int | None = None,
        payload: Any = None,
        secret: str | None = None,
    ) -> None:
        self.path = path
        self.status = status
        self.payload = payload
        self.message = sanitize_text(message, secret)
        prefix = f"{path}: " if path else ""
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{prefix}{self.message}{suffix}")

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"{type(self).__name__}({self.message!r}, path={self.path!r}, status={self.status!r})"


class EngineUnavailable(EngineError):
    """The engine could not be reached, timed out, or returned 5xx/429."""


class EngineAuthError(EngineError):
    """The engine rejected the API token (HTTP 401/403)."""


class EngineProtocolError(EngineError):
    """The response was not valid JSON or did not match the expected shape."""


class EngineContractMismatch(EngineError):
    """The engine speaks an incompatible contract major version."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

SleepCallable = Callable[[float], Awaitable[Any]]

#: HTTP status codes worth retrying (server-side / throttling).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class EngineClient:
    """Thin, defensive async client for ``/api/v1``.

    Parameters
    ----------
    session:
        An ``aiohttp.ClientSession``-like object exposing
        ``request(method, url, **kwargs)`` returning an async context manager
        whose ``__aenter__`` yields a response with ``.status``, ``.headers``,
        ``.text()`` and ``.json()``.
    base_url:
        Engine origin, e.g. ``http://192.168.1.10:8787``.  A trailing
        ``/api/v1`` is tolerated and stripped.
    api_token:
        Optional shared token sent as ``X-API-Token``.
    """

    def __init__(
        self,
        session: Any,
        base_url: str,
        *,
        api_token: str | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        contract_version: str = CONTRACT_VERSION,
        sleep: SleepCallable | None = None,
    ) -> None:
        from .util import normalise_base_url

        normalised = normalise_base_url(base_url)
        if normalised is None:
            raise ValueError("invalid engine base URL")
        self._session = session
        self._base_url = normalised
        self._api_token = api_token.strip() if is_token_configured(api_token) else None
        self._timeout = max(0.1, float(timeout))
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._contract_version = contract_version
        self._sleep = sleep or asyncio.sleep

    # -- introspection ------------------------------------------------------ #

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token_configured(self) -> bool:
        return self._api_token is not None

    def __repr__(self) -> str:
        """Never leak the token, not even in debuggers/tracebacks."""
        token = "configured" if self._api_token else "none"
        return f"EngineClient(base_url={self._base_url!r}, token={token})"

    # -- low level ---------------------------------------------------------- #

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{API_BASE_PATH}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {
            HEADER_CONTRACT_VERSION: self._contract_version,
            "Accept": "application/json",
        }
        if self._api_token:
            headers[HEADER_API_TOKEN] = self._api_token
        return headers

    def _check_contract(self, headers: Mapping[str, Any], path: str) -> None:
        raw = None
        for key, value in headers.items():
            if str(key).lower() == HEADER_CONTRACT_VERSION.lower():
                raw = value
                break
        if not raw:
            # Older engines may not send the header; stay backwards compatible.
            return
        remote = str(raw).strip()
        if remote == self._contract_version:
            return
        remote_major = remote.split(".", 1)[0]
        local_major = self._contract_version.split(".", 1)[0]
        if remote_major != local_major:
            raise EngineContractMismatch(
                f"engine contract {remote} is incompatible with integration "
                f"contract {self._contract_version}",
                path=path,
                payload={"engine": remote, "integration": self._contract_version},
            )

    def _decode(self, path: str, status: int, body: str) -> Any:
        if not body.strip():
            if status == 204:
                return {}
            raise EngineProtocolError("empty response body", path=path, status=status)
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as err:
            raise EngineProtocolError(
                f"invalid JSON response: {err}", path=path, status=status
            ) from None
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        expect: type | tuple[type, ...] = dict,
    ) -> Any:
        """Perform a request with bounded retries and typed errors."""
        url = self._url(path)
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if json_body is not None:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        # aiohttp accepts a float timeout on request(); everything else is
        # tolerant of an unknown kwarg being ignored by test doubles.
        kwargs["timeout"] = self._timeout

        attempt = 0
        last_error: EngineError | None = None

        while attempt <= self._max_retries:
            if attempt:
                delay = self._backoff_base * (2 ** (attempt - 1))
                if delay:
                    await self._sleep(delay)
            attempt += 1
            try:
                async with self._session.request(method, url, **kwargs) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    headers = dict(getattr(response, "headers", {}) or {})
                    body = await response.text()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # network layer: timeout, DNS, reset, ...
                last_error = EngineUnavailable(
                    f"engine unreachable: {type(err).__name__}: {err}",
                    path=path,
                    secret=self._api_token,
                )
                continue

            if status in (401, 403):
                raise EngineAuthError(
                    "engine rejected the API token" if self._api_token else "engine requires an API token",
                    path=path,
                    status=status,
                    payload=self._safe_decode(body),
                )

            if status >= 400:
                payload = self._safe_decode(body)
                detail = self._error_detail(payload) or f"HTTP {status}"
                error_type: type[EngineError] = (
                    EngineUnavailable if status in _RETRYABLE_STATUS else EngineProtocolError
                )
                error = error_type(detail, path=path, status=status, payload=payload)
                if status in _RETRYABLE_STATUS and attempt <= self._max_retries:
                    last_error = error
                    continue
                raise error

            self._check_contract(headers, path)

            payload = self._decode(path, status, body)
            if not isinstance(payload, expect):
                raise EngineProtocolError(
                    f"unexpected response type {type(payload).__name__}",
                    path=path,
                    status=status,
                    payload=None,
                )
            return payload

        raise last_error or EngineUnavailable("engine unreachable", path=path)

    def _safe_decode(self, body: str) -> Any:
        try:
            return json.loads(body) if body else None
        except (ValueError, TypeError):
            return None

    def _error_detail(self, payload: Any) -> str | None:
        if not isinstance(payload, Mapping):
            return None
        error = payload.get("error")
        detail = payload.get("detail")
        if isinstance(error, str) and isinstance(detail, str):
            return sanitize_text(f"{error}: {detail}", self._api_token)
        if isinstance(error, str):
            return sanitize_text(error, self._api_token)
        if isinstance(detail, str):
            return sanitize_text(detail, self._api_token)
        return None

    # -- endpoints ---------------------------------------------------------- #

    async def health(self) -> dict[str, Any]:
        """``GET /health`` -> ``HealthResponse``."""
        return await self._request("GET", "/health")

    async def status(self) -> dict[str, Any]:
        """``GET /status`` -> engine + DB + learning summary."""
        return await self._request("GET", "/status")

    async def snapshot(self) -> dict[str, Any]:
        """``GET /snapshot`` -> ``SnapshotResponse``."""
        return await self._request("GET", "/snapshot")

    async def devices(self) -> Any:
        """``GET /devices`` -> device list (plain list or ``{"devices": [...]}``)."""
        return await self._request("GET", "/devices", expect=(dict, list))

    async def device(self, device_id: str) -> dict[str, Any]:
        """``GET /devices/{device_id}``."""
        return await self._request("GET", f"/devices/{_quote(device_id)}")

    async def topology(self) -> dict[str, Any]:
        """``GET /topology`` -> ``Topology``."""
        return await self._request("GET", "/topology")

    async def put_topology(self, topology: Mapping[str, Any]) -> dict[str, Any]:
        """``PUT /topology``.  The engine keeps it ``candidate`` by default."""
        return await self._request("PUT", "/topology", json_body=dict(topology))

    async def confirm_topology(self, version: int | None = None) -> dict[str, Any]:
        """``POST /topology/confirm``.  Only an explicit user action confirms."""
        body: dict[str, Any] = {}
        if version is not None:
            body["version"] = int(version)
        return await self._request("POST", "/topology/confirm", json_body=body)

    async def quality(self) -> dict[str, Any]:
        """``GET /quality`` -> ``DataQualityReport``."""
        return await self._request("GET", "/quality")

    async def diagnostics(self) -> dict[str, Any]:
        """``GET /diagnostics`` -> diagnostics payload."""
        return await self._request("GET", "/diagnostics")

    async def models(self, device_id: str) -> dict[str, Any]:
        """``GET /models/{device_id}`` -> versions + active model."""
        return await self._request("GET", f"/models/{_quote(device_id)}")

    async def command(
        self,
        command: str,
        *,
        device_id: str | None = None,
        sample_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /commands`` -> ``ServiceResult``."""
        body: dict[str, Any] = {
            "command": command,
            "parameters": dict(parameters or {}),
        }
        if device_id is not None:
            body["device_id"] = device_id
        if sample_id is not None:
            body["sample_id"] = sample_id
        return await self._request("POST", "/commands", json_body=body)

    async def export_device(self, device_id: str) -> dict[str, Any]:
        """``GET /export/{device_id}`` -> device data export."""
        return await self._request("GET", f"/export/{_quote(device_id)}")

    async def metrics(self) -> str:
        """``GET /metrics`` -> Prometheus-style text."""
        url = self._url("/metrics")
        try:
            async with self._session.request(
                "GET", url, headers=self._headers(), timeout=self._timeout
            ) as response:
                status = int(getattr(response, "status", 0) or 0)
                text = await response.text()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise EngineUnavailable(
                f"engine unreachable: {type(err).__name__}: {err}",
                path="/metrics",
                secret=self._api_token,
            ) from None
        if status >= 400:
            raise EngineProtocolError(
                f"metrics request failed: HTTP {status}", path="/metrics", status=status
            )
        return text

    # -- composed helpers --------------------------------------------------- #

    async def probe(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch health + snapshot together (used by the config flow)."""
        health = await self.health()
        snapshot = await self.snapshot()
        return health, snapshot


def _quote(value: str) -> str:
    """Percent-encode a path segment without importing urllib in callers."""
    from urllib.parse import quote

    return quote(str(value), safe="")


def describe_engine_error(error: BaseException) -> str:
    """Map a client error to a stable config-flow error key (no secrets)."""
    if isinstance(error, EngineAuthError):
        return "invalid_auth"
    if isinstance(error, EngineContractMismatch):
        return "contract_mismatch"
    if isinstance(error, EngineUnavailable):
        return "cannot_connect"
    if isinstance(error, EngineProtocolError):
        return "invalid_response"
    return "unknown"


def token_status_label(token: Any) -> str:
    """Human-readable token status that never echoes the secret."""
    return "configured" if is_token_configured(token) else "not configured"


def redact_token_for_display(token: Any) -> str:
    """Return ``configured``/``not configured`` — never the token itself.

    Used by the options flow and diagnostics so a token can be acknowledged
    without ever being echoed back into the UI.
    """
    return token_status_label(token)
