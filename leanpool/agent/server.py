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
import os
from dataclasses import dataclass
from pathlib import Path

from leanpool.agent.headroom import (
    BoxState,
    agent_reply,
    fitting_slots,
    next_slots,
    with_installer_version,
)
from leanpool.agent.readings import (
    CpuTimes,
    ReadingError,
    busy_cpus,
    elapsed_seconds,
    idle_share,
    parse_available_memory_bytes,
    parse_cpu_times,
    parse_cpu_usage_microseconds,
    parse_one_minute_load,
    threads_of_each_core,
)

_LINGER_SECONDS = 2.0
_MICROSECONDS_PER_SECOND = 1_000_000


def _clock_ticks_per_second() -> int:
    """The unit of ``/proc/stat``'s counters on this machine (100 on every common Linux)."""
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return 100


@dataclass(frozen=True)
class AgentSettings:
    """How the agent listens, what it measures and where it reads the measurements.

    The three paths exist so tests can point the agent at fixture files; on a real box they stay
    at their ``/proc`` defaults.

    The agent states open slots only when it is given BOTH ``workers`` (the Lean server's worker
    count, the most it ever states) and ``lean_cpu_stat_path`` (the ``cpu.stat`` file of the
    control group the Lean server runs in, from which its own CPU use is read). With either
    missing its replies are what they were before slots existed. ``cpu_reserve`` is how many
    cores are left free beside the box's other work before a check is counted as fitting, and
    ``cpu_topology_path`` is where the kernel says which logical CPUs share a core.
    ``slots_beside_other_work`` is the box's measured limit on checks while it also runs other
    work (None: no such limit). ``installer_version`` is the version of the installer that set
    the box up; with it every reply ends `` # version V`` (see ``with_installer_version``).
    """

    host: str = "0.0.0.0"
    port: int = 18200
    sample_interval_seconds: float = 5.0
    memory_floor_bytes: int = 2 * 1024**3
    use_load_average: bool = False
    stat_path: Path = Path("/proc/stat")
    meminfo_path: Path = Path("/proc/meminfo")
    loadavg_path: Path = Path("/proc/loadavg")
    workers: int | None = None
    cpu_reserve: float = 1.0
    lean_cpu_stat_path: Path | None = None
    cpu_topology_path: Path = Path("/sys/devices/system/cpu")
    slots_beside_other_work: int | None = None
    installer_version: str | None = None
    clock_ticks_per_second: int = _clock_ticks_per_second()

    @property
    def states_slots(self) -> bool:
        """Whether this agent was told how to measure open slots."""
        return self.workers is not None and self.lean_cpu_stat_path is not None


@dataclass(frozen=True)
class CpuTopology:
    """The physical cores the kernel describes, as each one's number of threads (most first)."""

    threads_of_each_core: tuple[int, ...]

    @property
    def threads(self) -> int:
        """The logical CPUs described."""
        return sum(self.threads_of_each_core)

    @property
    def cores(self) -> int:
        """The physical cores described."""
        return len(self.threads_of_each_core)


def cpu_topology_of(topology_path: Path) -> CpuTopology | None:
    """Return the box's cores as the kernel describes them, or None where it does not.

    Each ``cpu<N>/topology`` under ``topology_path`` names the logical CPUs that share N's core.
    None (which the slots then read as one thread a core) when nothing can be read there: an
    older kernel, a box whose ``/sys`` is not the host's. A CPU that is offline has no such file
    and is not counted.
    """
    lists = []
    try:
        directories = sorted(topology_path.glob("cpu[0-9]*"))
    except OSError:
        return None
    for directory in directories:
        for name in ("core_cpus_list", "thread_siblings_list"):
            try:
                lists.append((directory / "topology" / name).read_text())
                break
            except OSError:
                continue
    counted = threads_of_each_core(lists)
    return CpuTopology(counted) if counted else None


class UsageSampler:
    """Keeps the box's latest measurements and the reply they amount to."""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self._previous_cpu_times: CpuTimes | None = None
        self._previous_lean_usage: int | None = None
        self._slots: int | None = None
        self._state: BoxState | None = None
        self._topology = cpu_topology_of(settings.cpu_topology_path)

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
        if self._settings.states_slots:
            self._slots = self._sampled_slots(cpu_times)
        self._previous_cpu_times = cpu_times
        self._state = BoxState(
            cpu_idle_share=cpu_idle_share,
            available_memory_bytes=parse_available_memory_bytes(
                self._settings.meminfo_path.read_text()
            ),
            load_per_core=self._load_per_core(cpu_times.cores),
            open_slots=self._slots,
        )

    def reply(self) -> str:
        """Return the agent-check line for the latest sample."""
        if self._state is None:
            raise RuntimeError("sample() must run before reply()")
        return with_installer_version(
            agent_reply(self._state, self._settings.memory_floor_bytes),
            self._settings.installer_version,
        )

    def _sampled_slots(self, cpu_times: CpuTimes) -> int | None:
        """Return the slots to state after this reading. Called before the reading is kept.

        Three cases leave the measurement out, and none of them stops the agent:

        * the Lean server's ``cpu.stat`` cannot be read (its container is being made again, or
          this box gives the agent no such file): the agent states the server's whole worker
          count. HAProxy keeps the last limit it was told, so saying nothing would leave an
          earlier, lower limit standing on a server nobody is measuring;
        * there is no interval yet (the first reading, or the first after the file came back);
        * a counter went backwards (the control group was made again, the box's counters were
          restored): the slots stated before stand until the next whole interval.
        """
        workers = self._settings.workers
        path = self._settings.lean_cpu_stat_path
        assert workers is not None
        assert path is not None
        try:
            lean_usage = parse_cpu_usage_microseconds(path.read_text())
        except (OSError, ReadingError):
            self._previous_lean_usage = None
            return workers
        previous_cpu_times, previous_lean_usage = (
            self._previous_cpu_times,
            self._previous_lean_usage,
        )
        self._previous_lean_usage = lean_usage
        if previous_cpu_times is None or previous_lean_usage is None:
            return self._slots
        busy = busy_cpus(previous_cpu_times, cpu_times)
        seconds = elapsed_seconds(
            previous_cpu_times, cpu_times, self._settings.clock_ticks_per_second
        )
        if busy is None or seconds is None or lean_usage < previous_lean_usage:
            return self._slots
        lean_cpus = (lean_usage - previous_lean_usage) / _MICROSECONDS_PER_SECOND / seconds
        # A topology counts only when it describes the very CPUs /proc/stat counts: where the two
        # disagree (a /sys that is not the host's) fitting_slots takes every thread for a core.
        fits = fitting_slots(
            cpu_times.cores,
            self._settings.cpu_reserve,
            busy,
            lean_cpus,
            workers,
            None if self._topology is None else self._topology.threads_of_each_core,
            self._settings.slots_beside_other_work,
        )
        return next_slots(self._slots, fits)

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
