"""Fixtures: in-process fake Lean servers and a cache service in front of them."""

from __future__ import annotations

import dataclasses
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from admit_support import write_cases
from aiohttp.test_utils import TestClient, TestServer
from fake_lean_server import Behaviour, FakeLeanServer, always, lean_answer
from support import API_KEY, PIN, CacheClient, StartCache, StartLeanServer

from leanpool.cache import CacheSettings, create_application


@pytest.fixture
async def start_lean_server() -> AsyncIterator[StartLeanServer]:
    """Start fake Lean servers on request; all of them are closed after the test."""
    servers: list[FakeLeanServer] = []

    async def start(
        behaviour: Behaviour | None = None,
        api_key: str | None = API_KEY,
        host: str = "127.0.0.1",
        port: int | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> FakeLeanServer:
        server = FakeLeanServer(behaviour=behaviour or always(lean_answer()), api_key=api_key)
        await server.start(host, port, ssl_context)
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.close()


@pytest.fixture
def cases_directory(tmp_path: Path) -> Path:
    """An admission test's cases: one that verifies and two that must be rejected."""
    return write_cases(tmp_path / "cases")


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    """A file holding the pool's API key, as the admission test is given it."""
    path = tmp_path / "api-key"
    path.write_text(f"{API_KEY}\n")
    return path


@pytest.fixture
async def start_cache(tmp_path: Path) -> AsyncIterator[StartCache]:
    """Start cache services on request; they share one database file unless told otherwise."""
    clients: list[CacheClient] = []

    async def start(upstream_url: str, **overrides: Any) -> CacheClient:
        settings = dataclasses.replace(
            CacheSettings(
                pin=PIN,
                api_key=API_KEY,
                database_path=tmp_path / "cache.sqlite3",
                upstream_url=upstream_url,
                upstream_timeout_seconds=10.0,
            ),
            **overrides,
        )
        client = TestClient(TestServer(create_application(settings)))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()
