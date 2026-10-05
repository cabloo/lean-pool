"""An in-process stand-in for a Kimina Lean Server.

It speaks the part of Kimina's HTTP interface lean-pool depends on: ``POST /api/check`` (with
the optional Bearer key, Kimina's ``debug`` rule for diagnostics and one status for the whole
request) and ``GET /health``. What it answers for a snippet is up to the test.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

# What a test returns for one snippet: the result's fields without its id, or a whole HTTP
# response when the server is to fail the request.
SnippetReply = dict[str, Any] | web.Response
Behaviour = Callable[[dict[str, Any]], Awaitable[SnippetReply]]

_LARGEST_REQUEST_BYTES = 16 * 1024**2

DIAGNOSTICS = {"repl_uuid": "fake-repl", "cpu_max": 1.0, "memory_max": 123456}


def lean_answer(
    *messages: tuple[str, str], seconds: float = 0.25, sorries: int = 0
) -> dict[str, Any]:
    """A definitive Lean answer with the given (severity, text) messages."""
    return {
        "time": seconds,
        "response": {
            "env": 0,
            "messages": [{"severity": severity, "data": text} for severity, text in messages],
            "sorries": [{"goal": "⊢ False"}] * sorries,
        },
        "diagnostics": dict(DIAGNOSTICS),
    }


def lean_timeout(seconds: int = 60) -> dict[str, Any]:
    """Kimina's reply when Lean exceeded the timeout: HTTP 200 with an error in the body."""
    return {
        "time": seconds,
        "error": f"Lean REPL command timed out in {seconds} seconds",
        "diagnostics": {"repl_uuid": "fake-repl"},
    }


def repl_error(text: str = "Could not parse the command") -> dict[str, Any]:
    """Kimina's reply when the REPL itself rejected the command."""
    return {"time": 0.01, "response": {"message": text}}


def always(reply: dict[str, Any]) -> Behaviour:
    """A behaviour that answers every snippet with a copy of ``reply``."""

    async def behaviour(_snippet: dict[str, Any]) -> SnippetReply:
        return dict(reply)

    return behaviour


def failing_with(status: int, text: str = "the fake server failed") -> Behaviour:
    """A behaviour that fails every request with an HTTP status."""

    async def behaviour(_snippet: dict[str, Any]) -> SnippetReply:
        return web.Response(status=status, text=text)

    return behaviour


@dataclass
class RecordedRequest:
    """One ``/api/check`` request as the fake server received it.

    Header names are matched without regard to case, as HTTP requires: a proxy may change their
    case on the way (HAProxy sends them in lower case).
    """

    body: dict[str, Any]
    headers: CIMultiDict[str]


@dataclass
class FakeLeanServer:
    """A fake Lean server. ``requests`` records every check request that passed the key check,
    and ``health_checks`` counts the ``GET /health`` requests answered.
    """

    behaviour: Behaviour = field(default_factory=lambda: always(lean_answer()))
    api_key: str | None = None
    requests: list[RecordedRequest] = field(default_factory=list)
    health_checks: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    _server: TestServer | None = None

    async def start(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Listen on ``host``, on ``port`` or on any free port; over TLS when given a context."""
        application = web.Application(client_max_size=_LARGEST_REQUEST_BYTES)
        application.router.add_post("/api/check", self._handle_check)
        application.router.add_get("/health", self._handle_health)
        self._server = TestServer(application, host=host, port=port)
        await self._server.start_server(ssl=ssl_context)

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    @property
    def url(self) -> str:
        assert self._server is not None, "the fake server has not been started"
        return str(self._server.make_url("")).rstrip("/")

    @property
    def port(self) -> int:
        assert self._server is not None, "the fake server has not been started"
        port = self._server.port
        assert port is not None
        return port

    @property
    def checked_codes(self) -> list[str]:
        """The code of every snippet received, in arrival order."""
        return [
            snippet["code"] for request in self.requests for snippet in request.body["snippets"]
        ]

    async def _handle_health(self, _request: web.Request) -> web.Response:
        self.health_checks += 1
        return web.json_response({"status": "ok"})

    async def _handle_check(self, request: web.Request) -> web.Response:
        if self.api_key is not None:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if token != self.api_key:
                return web.json_response({"detail": "Invalid API key"}, status=401)
        body = await request.json()
        self.requests.append(RecordedRequest(body=body, headers=CIMultiDict(request.headers)))
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            replies = await asyncio.gather(
                *(self.behaviour(snippet) for snippet in body["snippets"])
            )
        finally:
            self.in_flight -= 1
        results = []
        for snippet, reply in zip(body["snippets"], replies, strict=True):
            if isinstance(reply, web.Response):
                return reply
            result = {"id": snippet["id"], **reply}
            if not body.get("debug", False) and "response" in result:
                result.pop("diagnostics", None)
            results.append(result)
        return web.json_response({"results": results})
