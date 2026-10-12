"""A whole pool on this machine with a real HAProxy, for the tests in ``test_haproxy_live.py``.

The pool is the generated configuration, run by the ``haproxy`` found on ``PATH``, in front of
the real cache service and in-process fake Lean servers. Everything listens on loopback: the
one listener the generated configuration puts on every address, the public port, is moved to
loopback before HAProxy is started. Nothing here is used unless HAProxy is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer
from fake_lean_server import FakeLeanServer
from support import API_KEY, AUTHORIZED, PIN

from leanpool.cache import CacheSettings, create_application
from leanpool.haproxy import (
    LeanServer,
    PoolSettings,
    maximum_pool_metrics_bytes,
    render_haproxy_config,
    render_pool_metrics,
    write_pool_metrics,
)
from leanpool.signals import PRIORITY_HEADER, PoolCapacity, capacity_from_headers

HAPROXY = shutil.which("haproxy")
WORKERS = 4
_STARTUP_SECONDS = 30.0
_WILDCARD_BIND = re.compile(r"^(\s*bind) :(\d+)", re.MULTILINE)
# The pool's configuration answers GET /metrics with HAProxy's Prometheus exporter, which is a
# build option. A test of the pool on an HAProxy built without it fails with these words: it is
# not skipped, because the pool itself would not start there.
MISSING_EXPORTER = (
    f"the haproxy on PATH ({HAPROXY}) was built without the Prometheus exporter "
    "(`haproxy -vv` does not list prometheus-exporter under its services; build it with "
    "USE_PROMEX=1). The pool's configuration answers GET /metrics with that exporter, and an "
    "HAProxy without it refuses the configuration"
)


@functools.cache
def haproxy_has_exporter() -> bool:
    """Whether the ``haproxy`` on ``PATH`` lists the Prometheus exporter among its services."""
    assert HAPROXY is not None
    built = subprocess.run([HAPROXY, "-vv"], capture_output=True, text=True, check=False).stdout
    services = [line for line in built.splitlines() if line.startswith("Available services :")]
    return any("prometheus-exporter" in line for line in services)


# Every port a test was given, so that no two tests and no two listeners of one test get the
# same one: a port is released before it is returned, and the system may well hand it out again.
_HANDED_OUT: set[int] = set()


def free_port() -> int:
    """Return a port that was free a moment ago, and that no test was given before."""
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port: int = listener.getsockname()[1]
        if port not in _HANDED_OUT:
            _HANDED_OUT.add(port)
            return port


def free_port_run(count: int) -> int:
    """Return the first of ``count`` consecutive ports, free a moment ago and given to no test."""
    while True:
        first = free_port()
        run = range(first, first + count)
        if _HANDED_OUT.intersection(run[1:]):
            continue
        with contextlib.ExitStack() as held:
            try:
                for port in run:
                    listener = held.enter_context(socket.socket())
                    listener.bind(("127.0.0.1", port))
            except OSError:
                continue
        _HANDED_OUT.update(run)
        return first


async def eventually(condition: Callable[[], Awaitable[bool]], seconds: float = 30.0) -> None:
    """Wait until ``await condition()`` is true; fail after ``seconds``."""
    deadline = time.monotonic() + seconds
    while not await condition():
        assert time.monotonic() < deadline, "the expected state was not reached in time"
        await asyncio.sleep(0.1)


def on_loopback(config: str) -> str:
    """Make every listener of a generated configuration listen on loopback only.

    The generated ``bind :PORT`` listens on every address of the machine, which is right for a
    proxy and wrong for a test. Only the listening address changes; every other character is
    the generated configuration.
    """
    return _WILDCARD_BIND.sub(r"\1 127.0.0.1:\2", config)


@dataclass
class RunningHaproxy:
    """One HAProxy process on a configuration, logging to a file beside it."""

    directory: Path
    name: str = "haproxy"
    _process: subprocess.Popen[bytes] | None = None
    # Processes a reload replaced: each ends by itself once the requests it holds have ended.
    _replaced: list[subprocess.Popen[bytes]] = field(default_factory=list)

    async def start(self, config: str, port: int) -> None:
        """Run HAProxy on ``config`` and wait until it accepts connections on ``port``."""
        assert HAPROXY is not None
        path = self.directory / f"{self.name}.cfg"
        path.write_text(on_loopback(config))
        with (self.directory / f"{self.name}.log").open("wb") as log:
            self._process = subprocess.Popen(
                [HAPROXY, "-db", "-f", str(path)], stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + _STARTUP_SECONDS
        while not _accepts_connections(port):
            assert self._process.poll() is None, f"HAProxy exited:\n{self.log}"
            assert time.monotonic() < deadline, f"HAProxy did not start:\n{self.log}"
            await asyncio.sleep(0.05)

    def reload(self, config: str) -> None:
        """Hand over to a new HAProxy on ``config`` without dropping a request.

        The new process (``-sf``) binds the same ports, then tells the running one to stop
        listening and to end once the requests it holds have ended. That is the hand-over a
        master process makes to a new worker when a deployment reloads it (``kill -HUP``); it
        is done by hand here so that the test needs no master process.
        """
        assert HAPROXY is not None
        assert self._process is not None, "HAProxy has not been started"
        path = self.directory / f"{self.name}.cfg"
        path.write_text(on_loopback(config))
        replaced = self._process
        with (self.directory / f"{self.name}.log").open("ab") as log:
            self._process = subprocess.Popen(
                [HAPROXY, "-db", "-f", str(path), "-sf", str(replaced.pid)],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self._replaced.append(replaced)

    def stop(self) -> None:
        processes = [*self._replaced, *([] if self._process is None else [self._process])]
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._process = None
        self._replaced.clear()

    @property
    def log(self) -> str:
        """Everything this HAProxy has written so far."""
        return (self.directory / f"{self.name}.log").read_text(errors="replace")


@dataclass
class CheckReply:
    """What a client got for one check through the pool."""

    status: int
    body: Any
    seconds: float
    capacity: PoolCapacity | None = None  # what the answer's headers said about the pool

    @property
    def result(self) -> dict[str, Any]:
        """The single result of a successful check."""
        assert self.status == 200, f"HTTP {self.status}: {self.body}"
        result: dict[str, Any] = self.body["results"][0]
        return result


@dataclass
class LivePool:
    """HAProxy with the generated configuration, the cache, and fake Lean servers."""

    directory: Path
    lean_servers: list[FakeLeanServer]
    public_port: int = field(default_factory=free_port)
    checkers_port: int = field(default_factory=free_port)
    stats_port: int = field(default_factory=free_port)
    cache_port: int = field(default_factory=free_port)
    # The name of the pool's facts file for GET /metrics/pool, beside the configuration. With
    # none the configuration has no rule for that path, as it has none without --pool-metrics.
    pool_metrics_file: str | None = None
    _haproxy: RunningHaproxy | None = None
    _cache: TestServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.public_port}"

    @property
    def settings(self) -> PoolSettings:
        return PoolSettings(
            public_port=self.public_port,
            checkers_port=self.checkers_port,
            stats_port=self.stats_port,
            cache_address=f"127.0.0.1:{self.cache_port}",
            pool_metrics_file=self.pool_metrics_file,
        )

    @property
    def cache_settings(self) -> CacheSettings:
        return CacheSettings(
            pin=PIN,
            api_key=API_KEY,
            database_path=self.directory / "cache.sqlite3",
            port=self.cache_port,
            upstream_url=f"http://127.0.0.1:{self.checkers_port}",
            upstream_timeout_seconds=60.0,
        )

    @property
    def servers(self) -> list[LeanServer]:
        """The fake Lean servers as list entries, by address. No agent listens on their ports."""
        return [
            LeanServer(f"lean-{index}", "127.0.0.1", server.port, WORKERS, free_port())
            for index, server in enumerate(self.lean_servers)
        ]

    def render(self, servers: Sequence[LeanServer] | None = None) -> str:
        return render_haproxy_config(self.servers if servers is None else servers, self.settings)

    def write_pool_metrics(
        self,
        servers: Sequence[LeanServer] | None = None,
        certificate_not_after: Mapping[str, int] | None = None,
    ) -> str:
        """Write the pool's facts beside the configuration, as ``render --pool-metrics`` does.

        HAProxy reads the file when it reads the configuration, so this comes before a start
        or a reload. Returns the text written.
        """
        assert self.pool_metrics_file is not None, "this pool was given no facts file"
        text = render_pool_metrics(
            self.servers if servers is None else servers, certificate_not_after
        )
        write_pool_metrics(
            self.directory / self.pool_metrics_file,
            text,
            maximum_pool_metrics_bytes(self.settings),
        )
        return text

    async def start_cache(self) -> None:
        self._cache = TestServer(create_application(self.cache_settings), port=self.cache_port)
        await self._cache.start_server()

    async def stop_cache(self) -> None:
        """Stop the cache: from now on a connection to it is refused."""
        assert self._cache is not None
        await self._cache.close()
        self._cache = None

    async def start_haproxy(self, config: str) -> None:
        """Run HAProxy on ``config`` and wait until it accepts connections.

        Fails, with the reason, on an HAProxy built without the Prometheus exporter.
        """
        assert haproxy_has_exporter(), MISSING_EXPORTER
        self._haproxy = RunningHaproxy(self.directory)
        await self._haproxy.start(config, self.public_port)

    def reload_haproxy(self, config: str) -> None:
        """Hand the running HAProxy's work over to a new one on ``config``."""
        assert self._haproxy is not None, "HAProxy has not been started"
        self._haproxy.reload(config)

    async def start(self, servers: Sequence[LeanServer] | None = None) -> None:
        """Start the cache and HAProxy with the configuration generated for the fake servers.

        ``servers`` is the list to render instead of ``self.servers`` (one of them paused, say).
        A pool with a facts file has it written first, from the same list.
        """
        await self.start_cache()
        if self.pool_metrics_file is not None:
            self.write_pool_metrics(servers)
        await self.start_haproxy(self.render(servers))

    async def close(self) -> None:
        if self._haproxy is not None:
            self._haproxy.stop()
        if self._cache is not None:
            await self._cache.close()

    @property
    def log(self) -> str:
        """Everything HAProxy has written so far."""
        assert self._haproxy is not None, "HAProxy has not been started"
        return self._haproxy.log

    async def logged(self, text: str, times: int) -> None:
        """Wait until HAProxy's log contains ``text`` exactly ``times`` times.

        HAProxy logs a request when it ends, which can be a moment after the client has its
        reply; the log names the backend and server that answered (``cache/checkers_door``).
        """

        async def reached() -> bool:
            count = self.log.count(text)
            assert count <= times, f"{text!r} is logged {count} times, expected {times}"
            return count == times

        await eventually(reached, seconds=10.0)

    async def check(
        self,
        session: aiohttp.ClientSession,
        code: str,
        *,
        priority: str | None = None,
        **extra: Any,
    ) -> CheckReply:
        """Send one check to the pool's public port, as a client would.

        ``priority`` is sent as the ``X-Lean-Priority`` header, whatever its value.
        """
        body = {"snippets": [{"id": "attempt", "code": code}], "timeout": 60, **extra}
        headers = (
            dict(AUTHORIZED) if priority is None else {**AUTHORIZED, PRIORITY_HEADER: priority}
        )
        started = time.monotonic()
        async with session.post(f"{self.url}/api/check", json=body, headers=headers) as reply:
            content_type = reply.headers.get("Content-Type", "")
            payload = await reply.json() if "json" in content_type else await reply.text()
            return CheckReply(
                reply.status,
                payload,
                time.monotonic() - started,
                capacity_from_headers(reply.headers),
            )

    async def health(self, session: aiohttp.ClientSession) -> tuple[int, Any, PoolCapacity | None]:
        """``GET /health``: its status, its body, and what its headers say about the pool."""
        async with session.get(f"{self.url}/health") as reply:
            return reply.status, await reply.json(), capacity_from_headers(reply.headers)

    async def server_state(self, backend: str, server: str) -> dict[str, str]:
        """One server's row of HAProxy's statistics (``status``, ``weight``, ...)."""
        url = f"http://127.0.0.1:{self.stats_port}/;csv"
        async with aiohttp.ClientSession() as session, session.get(url) as reply:
            lines = (await reply.text()).strip().splitlines()
        columns = lines[0].removeprefix("# ").split(",")
        rows = [dict(zip(columns, line.split(","), strict=False)) for line in lines[1:]]
        return next(row for row in rows if (row["pxname"], row["svname"]) == (backend, server))


def _accepts_connections(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
        return True
    return False
