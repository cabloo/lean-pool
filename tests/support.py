"""Shared constants and small helpers for the cache tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient
from fake_lean_server import FakeLeanServer

API_KEY = "pool-key"
PIN = "lean-4.27.0/mathlib-test"
AUTHORIZED = {"Authorization": f"Bearer {API_KEY}"}

CacheClient = TestClient[web.Request, web.Application]
StartLeanServer = Callable[..., Awaitable[FakeLeanServer]]
StartCache = Callable[..., Awaitable[CacheClient]]


def check_body(*codes: str, timeout: float | None = 60, **extra: Any) -> dict[str, Any]:
    """A check request with one snippet per code, ids ``attempt-0``, ``attempt-1``, ..."""
    body: dict[str, Any] = {
        "snippets": [{"id": f"attempt-{index}", "code": code} for index, code in enumerate(codes)],
        **extra,
    }
    if timeout is not None:
        body["timeout"] = timeout
    return body


async def check(
    client: CacheClient, *codes: str, identifier: str | None = None, **extra: Any
) -> dict[str, Any]:
    """Send a check that must succeed and return its first result."""
    body = check_body(*codes, **extra)
    if identifier is not None:
        body["snippets"][0]["id"] = identifier
    async with client.post("/api/check", json=body, headers=AUTHORIZED) as response:
        assert response.status == 200, await response.text()
        reply: dict[str, Any] = await response.json()
    first_result: dict[str, Any] = reply["results"][0]
    return first_result


async def status(client: CacheClient) -> dict[str, int]:
    """Read the cache's ``/status``."""
    async with client.get("/status", headers=AUTHORIZED) as response:
        assert response.status == 200
        counters: dict[str, int] = await response.json()
    return counters
