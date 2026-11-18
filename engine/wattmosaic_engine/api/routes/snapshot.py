"""Snapshot and confidence-tier routes."""

from __future__ import annotations

import asyncio

from aiohttp import web

from ..server import _facade, _ok, maybe_await


async def snapshot(request: web.Request) -> web.Response:
    # Same whole-table latest-result read as ``devices`` and ``topology_tree``.
    return _ok(await maybe_await(await asyncio.to_thread(_facade(request).snapshot)))


async def confidence_tiers(request: web.Request) -> web.Response:
    return _ok(await maybe_await(_facade(request).confidence_tiers()))


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_get(f"{base}/snapshot", snapshot)
    router.add_get(f"{base}/confidence_tiers", confidence_tiers)


__all__ = ["confidence_tiers", "register", "snapshot"]
