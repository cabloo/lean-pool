"""A pool with TLS on this machine, for the tests in ``test_tls_live.py``.

Real HAProxy processes run the generated configurations: one for the pool proxy and one for
each Lean server box's TLS front. The certificates are throwaway ones made by the project's own
certificate code. Everything listens on loopback::

    client --HTTPS--> pool proxy --plain, loopback--> cache --> the proxy's checkers door
                          |
                          +--mutual TLS--> box front --plain--> fake Lean server
                          |
                          +--agent tunnel (loopback) --mutual TLS--> box front --plain--> agent

Every box is at 127.0.0.1, so nothing but the name in its certificate tells one from another.
Nothing here is used unless HAProxy is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from live_pool import WORKERS, LivePool, RunningHaproxy, free_port
from tls_support import ThrowawayAuthority, strict_client_context

from leanpool.haproxy import BoxSettings, LeanServer, PoolSettings, TlsSettings, render_box_config

PROXY_CLIENT_NAME = "lean-pool-proxy"
FRONT_DOOR_NAME = "pool.test"
_EXCHANGE_SECONDS = 10.0


def free_port_run(count: int) -> int:
    """Return the first of ``count`` consecutive ports that were free a moment ago."""
    while True:
        first = free_port()
        with contextlib.ExitStack() as held:
            try:
                for port in range(first, first + count):
                    listener = held.enter_context(socket.socket())
                    listener.bind(("127.0.0.1", port))
            except OSError:
                continue
            return first


@dataclass
class PoolCertificates:
    """One pool's authority, its front door's certificate and the proxy's client certificate."""

    authority: ThrowawayAuthority
    front_door_pem: Path
    proxy_client_pem: Path

    @classmethod
    def create(cls, directory: Path) -> PoolCertificates:
        authority = ThrowawayAuthority.create(directory)
        return cls(
            authority=authority,
            front_door_pem=authority.server(FRONT_DOOR_NAME, addresses=("127.0.0.1",)),
            proxy_client_pem=authority.client(PROXY_CLIENT_NAME),
        )

    @property
    def authority_file(self) -> Path:
        return self.authority.certificate_path

    def client_context(self, client_pem: Path | None = None) -> ssl.SSLContext:
        """What a client that trusts this pool uses, optionally presenting a certificate."""
        context = strict_client_context(self.authority_file)
        if client_pem is not None:
            context.load_cert_chain(str(client_pem))
        return context

    def session(self) -> aiohttp.ClientSession:
        """An HTTP session that trusts this pool's authority and nothing else."""
        connector = aiohttp.TCPConnector(limit=0, ssl=self.client_context())
        return aiohttp.ClientSession(connector=connector)


@dataclass
class BoxFront:
    """A Lean server box's TLS front: a real HAProxy on what ``render-box`` generates."""

    directory: Path
    name: str
    server_pem: Path
    authority_file: Path
    lean_upstream_port: int
    agent_upstream_port: int
    lean_timeout_seconds: int = 60
    server_wait_seconds: int = 60
    margin_seconds: int = 30
    lean_port: int = field(default_factory=free_port)
    agent_port: int = field(default_factory=free_port)
    _haproxy: RunningHaproxy | None = None

    @property
    def settings(self) -> BoxSettings:
        return BoxSettings(
            server_pem=str(self.server_pem),
            ca_file=str(self.authority_file),
            proxy_client_name=PROXY_CLIENT_NAME,
            lean_upstream=f"127.0.0.1:{self.lean_upstream_port}",
            agent_upstream=f"127.0.0.1:{self.agent_upstream_port}",
            lean_port=self.lean_port,
            agent_port=self.agent_port,
            lean_timeout_seconds=self.lean_timeout_seconds,
            server_wait_seconds=self.server_wait_seconds,
            margin_seconds=self.margin_seconds,
        )

    async def start(self) -> None:
        self._haproxy = RunningHaproxy(self.directory, name=f"box-{self.name}")
        await self._haproxy.start(render_box_config(self.settings), self.lean_port)

    def stop(self) -> None:
        if self._haproxy is not None:
            self._haproxy.stop()

    @property
    def log(self) -> str:
        assert self._haproxy is not None, "the box's front has not been started"
        return self._haproxy.log


@dataclass(kw_only=True)
class TlsLivePool(LivePool):
    """The pool proxy with TLS, in front of the cache and of the boxes' TLS fronts."""

    certificates: PoolCertificates
    boxes: list[BoxFront]
    agent_tunnel_port: int = 0
    lean_timeout_seconds: int = 60
    server_wait_seconds: int = 60
    margin_seconds: int = 30

    def __post_init__(self) -> None:
        if self.agent_tunnel_port == 0:
            self.agent_tunnel_port = free_port_run(max(1, len(self.boxes)))

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.public_port}"

    @property
    def settings(self) -> PoolSettings:
        return dataclasses.replace(
            super().settings,
            lean_timeout_seconds=self.lean_timeout_seconds,
            server_wait_seconds=self.server_wait_seconds,
            margin_seconds=self.margin_seconds,
            tls=TlsSettings(
                front_door_pem=str(self.certificates.front_door_pem),
                ca_file=str(self.certificates.authority_file),
                client_pem=str(self.certificates.proxy_client_pem),
                agent_tunnel_port=self.agent_tunnel_port,
            ),
        )

    @property
    def servers(self) -> list[LeanServer]:
        """Each box as a list entry: its name, and the loopback ports of its TLS front."""
        return [
            LeanServer(box.name, "127.0.0.1", box.lean_port, WORKERS, box.agent_port)
            for box in self.boxes
        ]

    async def close(self) -> None:
        await super().close()
        for box in self.boxes:
            box.stop()


@dataclass
class Exchange:
    """What one TLS connection brought back: the bytes received, or why there were none."""

    received: bytes
    error: str = ""


async def exchange(
    port: int, context: ssl.SSLContext, server_hostname: str, request: bytes = b""
) -> Exchange:
    """Open a TLS connection to a loopback port, send ``request`` and read until it closes.

    In TLS 1.3 a client finishes its handshake before the server has looked at the client's
    certificate, so a server's refusal shows as an alert or a closed connection on the first
    read, not as a failed connect. Either way nothing is received.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                "127.0.0.1", port, ssl=context, server_hostname=server_hostname
            ),
            _EXCHANGE_SECONDS,
        )
    except (ssl.SSLError, OSError) as error:
        return Exchange(b"", f"connect: {error}")
    try:
        writer.write(request)
        await writer.drain()
        return Exchange(await asyncio.wait_for(reader.read(), _EXCHANGE_SECONDS))
    except (ssl.SSLError, OSError) as error:
        return Exchange(b"", f"read: {error}")
    finally:
        writer.close()
        with contextlib.suppress(ssl.SSLError, OSError):
            await writer.wait_closed()
