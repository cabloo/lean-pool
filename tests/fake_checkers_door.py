"""An in-process stand-in for the generated HAProxy ``checkers`` backend.

The cache forwards a miss to one address: the proxy's loopback listener, which balances over
the Lean servers. Tests cannot run HAProxy, so this stand-in applies the rules the generated
configuration states (``leanpool/haproxy/config.py``, backend ``checkers``), reading the retry
count and the retried statuses from the generator itself:

* requests are spread over the servers;
* a connection failure, or a reply with one of ``CHECKER_RETRY_STATUSES`` (a crashed worker's
  500, or a gateway error), is retried on the next server, at most ``CHECKER_RETRIES`` times
  (``retries``, ``retry-on``, ``option redispatch 1``);
* anything else is passed through untouched and unretried, in particular a 200 carrying a
  Lean timeout.

It is test scaffolding, not part of lean-pool. What the end-to-end tests show with it is the
cache's side of the contract; HAProxy's own behaviour is not exercised by them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from leanpool.haproxy.config import CHECKER_RETRIES, CHECKER_RETRY_STATUSES

_FORWARDED_HEADERS = ("Authorization", "Content-Type", "X-Lean-Pool-Hop")


@dataclass
class FakeCheckersDoor:
    """Forwards ``POST /api/check`` to one of ``server_urls``, failing over like the proxy."""

    server_urls: list[str]
    _next_server: int = 0
    _server: TestServer | None = None
    _session: aiohttp.ClientSession | None = field(default=None)

    async def start(self) -> None:
        application = web.Application()
        application.router.add_post("/api/check", self._handle_check)
        self._session = aiohttp.ClientSession()
        self._server = TestServer(application)
        await self._server.start_server()

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()
        if self._session is not None:
            await self._session.close()

    @property
    def url(self) -> str:
        assert self._server is not None, "the fake door has not been started"
        return str(self._server.make_url("")).rstrip("/")

    async def _handle_check(self, request: web.Request) -> web.Response:
        body = await request.read()
        headers = {
            name: request.headers[name] for name in _FORWARDED_HEADERS if name in request.headers
        }
        first = self._next_server
        self._next_server = (first + 1) % len(self.server_urls)
        last_failure = web.Response(status=503, text="no server is available")
        for attempt in range(1 + CHECKER_RETRIES):
            server_url = self.server_urls[(first + attempt) % len(self.server_urls)]
            reply = await self._forward(server_url, body, headers)
            if reply is None:
                continue
            if reply.status not in CHECKER_RETRY_STATUSES:
                return reply
            last_failure = reply
        return last_failure

    async def _forward(
        self, server_url: str, body: bytes, headers: dict[str, str]
    ) -> web.Response | None:
        """Return the server's reply, or None when it could not be reached at all."""
        assert self._session is not None
        try:
            async with self._session.post(
                f"{server_url}/api/check", data=body, headers=headers
            ) as response:
                return web.Response(
                    status=response.status,
                    body=await response.read(),
                    headers={"Content-Type": response.headers.get("Content-Type", "text/plain")},
                )
        except aiohttp.ClientError:
            return None
