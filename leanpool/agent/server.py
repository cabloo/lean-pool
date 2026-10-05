"""The usage agent: a TCP server that tells HAProxy how much headroom this box has.

HAProxy connects every few seconds (``agent-check``), reads one line and closes. The agent
never computes anything on a connection: a background sampler reads ``/proc`` on a fixed
interval and each connection is answered from the latest sample. That keeps the CPU share a
measurement over a known interval (two readings) rather than whatever happened between two
connections, and makes answering cost nothing however many proxies ask.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from leanpool.agent.headroom import BoxState, agent_reply
from leanpool.agent.readings import (
    CpuTimes,
    idle_share,
    parse_available_memory_bytes,
    parse_cpu_times,
    parse_one_minute_load,
)

_LINGER_SECONDS = 2.0


@dataclass(frozen=True)
class AgentSettings:
    """How the agent listens, what it measures and where it reads the measurements.

    The three paths exist so tests can point the agent at fixture files; on a real box they stay
    at their ``/proc`` defaults.
    """

    host: str = "0.0.0.0"
    port: int = 18200
    sample_interval_seconds: float = 5.0
    memory_floor_bytes: int = 2 * 1024**3
    use_load_average: bool = False
    stat_path: Path = Path("/proc/stat")
    meminfo_path: Path = Path("/proc/meminfo")
    loadavg_path: Path = Path("/proc/loadavg")


class UsageSampler:
    """Keeps the box's latest measurements and the reply they amount to."""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self._previous_cpu_times: CpuTimes | None = None
        self._state: BoxState | None = None

    def sample(self) -> None:
        """Read the ``/proc`` files once and replace the current state.

        The CPU share compares this reading with the previous one, so it is unknown after the
        first call and known from the second.
        """
        cpu_times = parse_cpu_times(self._settings.stat_path.read_text())
        cpu_idle_share = (
            None
            if self._previous_cpu_times is None
            else idle_share(self._previous_cpu_times, cpu_times)
        )
        self._previous_cpu_times = cpu_times
        self._state = BoxState(
            cpu_idle_share=cpu_idle_share,
            available_memory_bytes=parse_available_memory_bytes(
                self._settings.meminfo_path.read_text()
            ),
            load_per_core=self._load_per_core(cpu_times.cores),
        )

    def reply(self) -> str:
        """Return the agent-check line for the latest sample."""
        if self._state is None:
            raise RuntimeError("sample() must run before reply()")
        return agent_reply(self._state, self._settings.memory_floor_bytes)

    def _load_per_core(self, cores: int) -> float | None:
        if not self._settings.use_load_average:
            return None
        return parse_one_minute_load(self._settings.loadavg_path.read_text()) / cores


class UsageAgent:
    """The sampler and the TCP server together."""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self.sampler = UsageSampler(settings)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        """Take the first reading and start listening.

        The first reading happens here so that an unreadable ``/proc`` file stops the agent at
        once with a clear error instead of leaving a port open that answers nothing useful.
        """
        self.sampler.sample()
        self._server = await asyncio.start_server(
            self._answer, self._settings.host, self._settings.port
        )

    @property
    def port(self) -> int:
        """The port the agent is listening on (useful when it was started on port 0)."""
        if self._server is None:
            raise RuntimeError("the agent has not been started")
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def sample_forever(self) -> None:
        """Sample on the configured interval until cancelled.

        A failed reading is not caught: the agent stops, its supervisor restarts it, and until
        then HAProxy keeps the server's last weight. A sampler that swallowed the error would go
        on reporting a stale weight as if it were current.
        """
        while True:
            await asyncio.sleep(self._settings.sample_interval_seconds)
            self.sampler.sample()

    async def close(self) -> None:
        """Stop listening."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Write one agent-check line and close; whatever the peer sent is ignored.

        The reply is followed by end-of-stream, and the socket is closed only once the peer has
        closed its side (or stayed silent for ``_LINGER_SECONDS``). Closing at once while the
        peer's own bytes (HAProxy's ``agent-send``) were still unread would reset the
        connection, and the peer could lose the reply it had not yet read.

        A peer that has already gone (a check that timed out, a proxy that is stopping) makes
        any of these steps fail, with errors that are not all ``ConnectionError``: ending the
        stream on a connection the peer closed raises a plain ``OSError`` ("not connected").
        None of them matters, since nobody is left to read the reply, so every ``OSError`` is
        ignored. A timeout is one too.
        """
        with contextlib.suppress(OSError):
            writer.write(self.sampler.reply().encode("ascii"))
            writer.write_eof()
            await writer.drain()
            await asyncio.wait_for(reader.read(), timeout=_LINGER_SECONDS)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def run_agent(settings: AgentSettings, stop: asyncio.Event) -> None:
    """Serve until ``stop`` is set. Raises if a reading fails."""
    agent = UsageAgent(settings)
    await agent.start()
    sampling = asyncio.create_task(agent.sample_forever())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({sampling, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if sampling.done():
            sampling.result()
    finally:
        sampling.cancel()
        stopping.cancel()
        await agent.close()
