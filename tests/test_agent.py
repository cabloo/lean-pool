"""The usage agent's measurements, its formula and its replies."""

from __future__ import annotations

import asyncio
import dataclasses
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from leanpool.agent import (
    AgentSettings,
    BoxState,
    CpuTimes,
    UsageAgent,
    UsageSampler,
    agent_reply,
    fitting_slots,
    headroom_percent,
    idle_share,
    memory_share,
    next_slots,
    parse_cpu_times,
    with_installer_version,
)
from leanpool.agent.cli import main, parse_settings
from leanpool.agent.readings import (
    ReadingError,
    busy_cpus,
    elapsed_seconds,
    parse_available_memory_bytes,
    parse_cpu_usage_microseconds,
    parse_one_minute_load,
    threads_of_each_core,
)
from leanpool.agent.server import CpuTopology, cpu_topology_of, run_agent

GIBIBYTE = 1024**3
FLOOR = 2 * GIBIBYTE
PLENTY = 64 * GIBIBYTE
# HAProxy reads a series of words; these are the two replies the agent may give.
AGENT_CHECK_LINE = re.compile(r"(drain|ready up( (?P<percent>\d{1,3})%)?)\n")

REAL_STAT = """\
cpu  10132153 290696 3084719 46828483 16683 0 25195 0 175628 0
cpu0 1393280 32966 572056 13343292 6130 0 17875 0 23933 0
cpu1 1335498 31619 569430 13379398 3814 0 3441 0 23420 0
intr 199292 0 0 0
ctxt 1990473
btime 1062191376
procs_running 1
"""


def stat_text(busy: int, idle: int, cores: int = 4, iowait: int = 0) -> str:
    lines = [f"cpu  {busy} 0 0 {idle} {iowait} 0 0 0 0 0"]
    lines += [f"cpu{core} 0 0 0 0 0 0 0 0 0 0" for core in range(cores)]
    return "\n".join([*lines, "intr 1 2 3", "ctxt 4"]) + "\n"


def meminfo_text(available_bytes: int) -> str:
    return (
        "MemTotal:       131072000 kB\n"
        "MemFree:          1024000 kB\n"
        f"MemAvailable:   {available_bytes // 1024} kB\n"
        "Buffers:           204800 kB\n"
    )


class Box:
    """A box whose ``/proc`` files the test writes."""

    def __init__(
        self,
        directory: Path,
        use_load_average: bool = False,
        sample_interval_seconds: float = 5.0,
    ) -> None:
        self.settings = AgentSettings(
            host="127.0.0.1",
            port=0,
            sample_interval_seconds=sample_interval_seconds,
            memory_floor_bytes=FLOOR,
            use_load_average=use_load_average,
            stat_path=directory / "stat",
            meminfo_path=directory / "meminfo",
            loadavg_path=directory / "loadavg",
        )
        self._busy = self._idle = 0
        self.set_memory(PLENTY)
        self.set_load(0.0)
        self.advance(busy=0, idle=0)

    def advance(self, busy: int, idle: int) -> None:
        """Let the CPU counters grow by this many busy and idle ticks."""
        self._busy += busy
        self._idle += idle
        self.settings.stat_path.write_text(stat_text(self._busy, self._idle))

    def set_memory(self, available_bytes: int) -> None:
        self.settings.meminfo_path.write_text(meminfo_text(available_bytes))

    def set_load(self, one_minute: float) -> None:
        self.settings.loadavg_path.write_text(f"{one_minute:.2f} 1.00 1.00 2/345 6789\n")


def percent_of(reply: str) -> int:
    match = AGENT_CHECK_LINE.fullmatch(reply)
    assert match is not None, f"not an agent-check line: {reply!r}"
    assert match["percent"] is not None, f"no percentage in {reply!r}"
    return int(match["percent"])


def sampled_reply(box: Box, busy: int, idle: int) -> str:
    """The reply after two readings ``busy + idle`` ticks apart."""
    sampler = UsageSampler(box.settings)
    sampler.sample()
    box.advance(busy=busy, idle=idle)
    sampler.sample()
    return sampler.reply()


def test_cpu_times_are_read_from_the_aggregate_line_and_cores_are_counted() -> None:
    times = parse_cpu_times(REAL_STAT)
    assert times.cores == 2
    # idle + iowait; guest time (the ninth column) is already inside user time.
    assert times.idle_ticks == 46828483 + 16683
    assert times.total_ticks == 10132153 + 290696 + 3084719 + 46828483 + 16683 + 0 + 25195 + 0


def test_time_waiting_for_disk_counts_as_idle() -> None:
    before = parse_cpu_times(stat_text(busy=0, idle=0))
    after = parse_cpu_times(stat_text(busy=50, idle=20, iowait=30))
    assert idle_share(before, after) == 0.5


@pytest.mark.parametrize(
    "text", ["", "intr 1 2 3\n", "cpu  1 2\ncpu0 1 2\n", "cpu  a b c d\ncpu0\n"]
)
def test_an_unreadable_stat_file_is_an_error(text: str) -> None:
    with pytest.raises(ReadingError):
        parse_cpu_times(text)


def test_the_cpu_share_is_the_idle_part_of_the_interval_between_two_readings() -> None:
    earlier = CpuTimes(idle_ticks=9_000, total_ticks=10_000, cores=4)
    later = CpuTimes(idle_ticks=9_100, total_ticks=10_400, cores=4)
    # Since boot the box was 90% idle; in the interval that counts, only 25%.
    assert idle_share(earlier, later) == 0.25


def test_there_is_no_cpu_share_without_an_interval() -> None:
    reading = CpuTimes(idle_ticks=9_000, total_ticks=10_000, cores=4)
    assert idle_share(reading, reading) is None
    counters_reset = CpuTimes(idle_ticks=10, total_ticks=20, cores=4)
    assert idle_share(reading, counters_reset) is None


def test_available_memory_is_read_in_bytes() -> None:
    assert parse_available_memory_bytes(meminfo_text(3 * GIBIBYTE)) == 3 * GIBIBYTE
    with pytest.raises(ReadingError, match="MemAvailable"):
        parse_available_memory_bytes("MemTotal: 1 kB\n")
    with pytest.raises(ReadingError, match="MemAvailable"):
        parse_available_memory_bytes("MemAvailable: lots\n")


def test_the_one_minute_load_is_read() -> None:
    assert parse_one_minute_load("3.25 2.00 1.00 2/345 6789\n") == 3.25
    with pytest.raises(ReadingError):
        parse_one_minute_load("\n")


def test_the_percentage_falls_as_cpu_idle_falls() -> None:
    percents = [
        headroom_percent(BoxState(cpu_idle_share=share, available_memory_bytes=PLENTY), FLOOR)
        for share in (1.0, 0.75, 0.5, 0.25, 0.1, 0.0)
    ]
    assert percents == [100, 75, 50, 25, 10, 1]


def test_the_percentage_falls_as_available_memory_approaches_the_floor() -> None:
    percents = [
        headroom_percent(BoxState(cpu_idle_share=1.0, available_memory_bytes=available), FLOOR)
        for available in (8 * FLOOR, 2 * FLOOR, int(1.5 * FLOOR), int(1.25 * FLOOR), FLOOR)
    ]
    assert percents == [100, 100, 50, 25, 1]


def test_memory_does_not_lower_the_weight_while_there_is_plenty() -> None:
    assert memory_share(2 * FLOOR, FLOOR) == 1.0
    assert memory_share(100 * FLOOR, FLOOR) == 1.0
    assert memory_share(FLOOR, FLOOR) == 0.0
    assert memory_share(FLOOR // 2, FLOOR) == 0.0


def test_the_scarcest_resource_decides() -> None:
    state = BoxState(cpu_idle_share=0.8, available_memory_bytes=int(1.3 * FLOOR))
    assert headroom_percent(state, FLOOR) == 30


def test_memory_below_the_floor_answers_drain() -> None:
    state = BoxState(cpu_idle_share=1.0, available_memory_bytes=FLOOR - 1)
    assert agent_reply(state, FLOOR) == "drain\n"
    assert agent_reply(BoxState(cpu_idle_share=None, available_memory_bytes=0), FLOOR) == "drain\n"


def test_memory_at_the_floor_is_the_lowest_weight_not_a_drain() -> None:
    state = BoxState(cpu_idle_share=1.0, available_memory_bytes=FLOOR)
    assert agent_reply(state, FLOOR) == "ready up 1%\n"


@pytest.mark.parametrize("cpu_idle_share", [0.0, 0.004, 0.5, 0.999, 1.0])
@pytest.mark.parametrize("available", [0, FLOOR - 1, FLOOR, 3 * GIBIBYTE, PLENTY])
@pytest.mark.parametrize("load_per_core", [None, 0.0, 0.5, 7.0])
def test_every_reply_is_a_valid_agent_check_line(
    cpu_idle_share: float, available: int, load_per_core: float | None
) -> None:
    reply = agent_reply(BoxState(cpu_idle_share, available, load_per_core), FLOOR)
    match = AGENT_CHECK_LINE.fullmatch(reply)
    assert match is not None
    if match["percent"] is not None:
        assert 1 <= int(match["percent"]) <= 100


def test_a_busy_cpu_never_drains_the_server() -> None:
    state = BoxState(cpu_idle_share=0.0, available_memory_bytes=PLENTY)
    assert agent_reply(state, FLOOR) == "ready up 1%\n"


def test_one_reading_gives_no_percentage(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.advance(busy=900, idle=100)
    sampler = UsageSampler(box.settings)
    sampler.sample()
    assert sampler.reply() == "ready up\n"


def test_the_percentage_comes_from_two_readings_not_from_the_totals(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.advance(busy=100, idle=9_900)  # 99% idle since boot
    assert percent_of(sampled_reply(box, busy=300, idle=100)) == 25


def test_readings_with_no_time_between_them_give_no_percentage(tmp_path: Path) -> None:
    box = Box(tmp_path)
    assert sampled_reply(box, busy=0, idle=0) == "ready up\n"


def test_the_sampler_answers_drain_when_memory_runs_out_and_recovers(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.set_memory(FLOOR - 4096)
    assert sampled_reply(box, busy=0, idle=100) == "drain\n"
    box.set_memory(PLENTY)
    # "ready" in the next reply is what cancels the drain on HAProxy's side.
    assert sampled_reply(box, busy=0, idle=100) == "ready up 100%\n"


def test_a_reply_cannot_be_asked_for_before_the_first_reading(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="sample"):
        UsageSampler(Box(tmp_path).settings).reply()


def test_the_load_average_is_ignored_unless_enabled(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.set_load(3.0)  # three of four cores claimed
    assert percent_of(sampled_reply(box, busy=0, idle=100)) == 100


def test_the_load_average_per_core_limits_the_percentage_when_enabled(tmp_path: Path) -> None:
    box = Box(tmp_path, use_load_average=True)
    box.set_load(3.0)
    assert percent_of(sampled_reply(box, busy=0, idle=100)) == 25
    box.set_load(40.0)
    assert percent_of(sampled_reply(box, busy=0, idle=100)) == 1


async def read_reply(port: int, send: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if send:
        writer.write(send)
    reply = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    return reply


async def reply_becomes(port: int, expected: bytes) -> None:
    """Wait until the agent answers ``expected``; fail if it has not within five seconds."""
    async with asyncio.timeout(5):
        while await read_reply(port) != expected:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("reset", [False, True], ids=["closes", "resets"])
async def test_a_peer_that_is_gone_before_the_reply_does_not_disturb_the_agent(
    tmp_path: Path, reset: bool
) -> None:
    """Seen with a real HAProxy: a check that gave up leaves a connection that is already gone,
    and ending the stream on it fails with "not connected", which is not a ConnectionError.
    """
    agent = UsageAgent(Box(tmp_path).settings)
    await agent.start()
    unhandled: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: unhandled.append(context)
    )
    try:
        for _ in range(20):
            connection = socket.create_connection(("127.0.0.1", agent.port))
            if reset:
                linger_for_zero_seconds = struct.pack("ii", 1, 0)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger_for_zero_seconds)
            connection.close()
        await asyncio.sleep(0.2)
        assert await read_reply(agent.port) == b"ready up\n"
    finally:
        await agent.close()
    assert unhandled == []


async def test_each_connection_gets_one_line_and_is_closed(tmp_path: Path) -> None:
    box = Box(tmp_path)
    agent = UsageAgent(box.settings)
    await agent.start()
    try:
        assert await read_reply(agent.port) == b"ready up\n"
        box.advance(busy=60, idle=40)
        agent.sampler.sample()
        # reader.read() returning means the agent closed the connection after its one line.
        assert await read_reply(agent.port) == b"ready up 40%\n"
        assert await read_reply(agent.port, send=b"whatever agent-send says\n") == b"ready up 40%\n"
    finally:
        await agent.close()


async def test_the_background_sampler_keeps_the_reply_current(tmp_path: Path) -> None:
    box = Box(tmp_path, sample_interval_seconds=0.01)
    agent = UsageAgent(box.settings)
    await agent.start()
    sampling = asyncio.create_task(agent.sample_forever())
    try:
        box.set_memory(FLOOR - 1)
        await reply_becomes(agent.port, b"drain\n")
        box.set_memory(PLENTY)
        # The fixture's CPU counters stand still, so a recovered box has no percentage to give.
        await reply_becomes(agent.port, b"ready up\n")
    finally:
        sampling.cancel()
        await agent.close()


async def test_the_agent_stops_with_an_error_when_a_reading_fails(tmp_path: Path) -> None:
    box = Box(tmp_path, sample_interval_seconds=0.01)
    stop = asyncio.Event()
    running = asyncio.create_task(run_agent(box.settings, stop))
    await asyncio.sleep(0.05)
    box.settings.meminfo_path.write_text("MemTotal: 1 kB\n")
    with pytest.raises(ReadingError, match="MemAvailable"):
        await asyncio.wait_for(running, timeout=5)


async def test_the_agent_stops_cleanly_when_asked(tmp_path: Path) -> None:
    stop = asyncio.Event()
    running = asyncio.create_task(run_agent(Box(tmp_path).settings, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(running, timeout=5)


def test_the_command_fails_at_once_when_proc_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-stat"
    # The first reading happens before the port is opened, so nothing is ever bound here.
    assert main(["--stat-path", str(missing), "--host", "127.0.0.1"], {}) == 1
    assert "no-such-stat" in capsys.readouterr().err


def test_settings_come_from_the_environment_and_flags_override_them() -> None:
    environment = {
        "LEANPOOL_AGENT_PORT": "19000",
        "LEANPOOL_AGENT_MEMORY_FLOOR_MIB": "512",
        "LEANPOOL_AGENT_USE_LOAD_AVERAGE": "true",
        "LEANPOOL_AGENT_SAMPLE_INTERVAL_SECONDS": "2.5",
    }
    settings = parse_settings([], environment)
    assert settings.port == 19000
    assert settings.memory_floor_bytes == 512 * 1024**2
    assert settings.use_load_average is True
    assert settings.sample_interval_seconds == 2.5

    overridden = parse_settings(["--port", "19001", "--no-use-load-average"], environment)
    assert overridden.port == 19001
    assert overridden.use_load_average is False


def test_default_settings_read_proc_and_drain_below_two_gibibytes() -> None:
    settings = parse_settings([], {})
    assert settings == AgentSettings()
    assert settings.stat_path == Path("/proc/stat")
    assert settings.memory_floor_bytes == 2 * GIBIBYTE
    assert settings.port == 18200


@pytest.mark.parametrize("arguments", [["--memory-floor-mib", "0"], ["--sample-interval", "0"]])
def test_unusable_settings_are_usage_errors(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_information:
        parse_settings(arguments, {})
    assert exit_information.value.code == 2


VERSION = "0123456789abcdef0123456789abcdef"


def test_the_installers_version_comes_from_the_environment_or_a_flag() -> None:
    assert parse_settings([], {}).installer_version is None
    # A compose file that names no version hands the agent an empty variable: no version.
    assert parse_settings([], {"LEANPOOL_AGENT_VERSION": ""}).installer_version is None
    assert parse_settings([], {"LEANPOOL_AGENT_VERSION": VERSION}).installer_version == VERSION
    assert parse_settings(["--installer-version", "ab12"], {}).installer_version == "ab12"


@pytest.mark.parametrize(
    "version", ["0123456789ABCDEF", "12 34", "12#34", "g1", "a" * 65, "version", "12\n"]
)
def test_an_installers_version_that_is_not_hex_is_refused_at_the_start(
    version: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for arguments, environment in (
        (["--installer-version", version], {}),
        ([], {"LEANPOOL_AGENT_VERSION": version}),
    ):
        with pytest.raises(SystemExit) as exit_information:
            parse_settings(arguments, environment)
        assert exit_information.value.code == 2
        assert "is not an installer version" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reply", ["drain\n", "drain 0%\n", "ready up\n", "ready up 40%\n", "ready up 40% maxconn:5\n"]
)
def test_with_a_version_every_reply_ends_with_it_and_is_otherwise_the_same(reply: str) -> None:
    assert with_installer_version(reply, VERSION) == f"{reply[:-1]} # version {VERSION}\n"
    assert with_installer_version(reply, None) == reply


def test_a_box_whose_agent_was_told_the_version_states_it_in_every_answer(tmp_path: Path) -> None:
    box = Box(tmp_path)
    box.settings = dataclasses.replace(box.settings, installer_version=VERSION)
    assert sampled_reply(box, busy=0, idle=100) == f"ready up 100% # version {VERSION}\n"
    box.set_memory(FLOOR - 4096)
    assert sampled_reply(box, busy=0, idle=100) == f"drain # version {VERSION}\n"


@pytest.mark.parametrize("module", ["leanpool.agent.cli", "leanpool.haproxy.cli"])
def test_the_agent_and_the_haproxy_tooling_need_only_the_standard_library(module: str) -> None:
    """Import each with every third-party package made unimportable."""
    program = (
        "import sys\n"
        "sys.path[:] = [path for path in sys.path if 'site-packages' not in path]\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
        f"import {module}\n"
        "assert 'aiohttp' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# --- open slots ----------------------------------------------------------------------------------

SLOTS_LINE = re.compile(
    r"(drain 0%|ready up( (?P<percent>\d{1,3})%)? maxconn:(?P<slots>[1-9]\d*))\n"
)
CPU_STAT = """\
usage_usec 123456789
user_usec 100000000
system_usec 23456789
nr_periods 0
nr_throttled 0
throttled_usec 0
"""


class SharedBox:
    """A 16-CPU box that runs a 13-worker Lean server beside other work, reserve 1 core.

    ``run`` lets time pass with so many CPUs busy with other work and so many with the Lean
    server's own, and writes what ``/proc/stat`` and the server's ``cpu.stat`` would then say.
    One thread a core unless ``threads_of_each_core`` says otherwise (the box then has as many
    CPUs as those threads together).
    """

    TICKS_PER_SECOND = 100

    def __init__(
        self,
        directory: Path,
        workers: int | None = 13,
        reserve: float = 1.0,
        threads_of_each_core: tuple[int, ...] | None = None,
        beside_other_work: int | None = None,
    ) -> None:
        self.CORES = 16 if threads_of_each_core is None else sum(threads_of_each_core)
        topology = directory / "cpu-topology"
        if threads_of_each_core is not None:
            write_topology(topology, threads_of_each_core)
        self.cpu_stat_path = directory / "cpu.stat"
        self.settings = AgentSettings(
            host="127.0.0.1",
            port=0,
            memory_floor_bytes=FLOOR,
            stat_path=directory / "stat",
            meminfo_path=directory / "meminfo",
            loadavg_path=directory / "loadavg",
            workers=workers,
            cpu_reserve=reserve,
            lean_cpu_stat_path=self.cpu_stat_path,
            cpu_topology_path=topology,
            slots_beside_other_work=beside_other_work,
            clock_ticks_per_second=self.TICKS_PER_SECOND,
        )
        self._busy_ticks = self._idle_ticks = self.lean_microseconds = 0
        # What the server's cpu.stat holds instead of its counter: None for the counter, "" for
        # no file at all, anything else as the file's whole text.
        self.cpu_stat_instead: str | None = None
        self.settings.meminfo_path.write_text(meminfo_text(PLENTY))
        self.settings.loadavg_path.write_text("0.00 0.00 0.00 1/1 1\n")
        self.sampler = UsageSampler(self.settings)
        self._write()
        self.sampler.sample()

    def run(self, others: float, lean: float = 0.0, seconds: float = 5.0) -> str:
        """Let ``seconds`` pass, take a reading and return the reply it gives."""
        ticks = self.CORES * self.TICKS_PER_SECOND * seconds
        busy = round((others + lean) * self.TICKS_PER_SECOND * seconds)
        self._busy_ticks += busy
        self._idle_ticks += round(ticks) - busy
        self.lean_microseconds += round(lean * seconds * 1_000_000)
        self._write()
        self.sampler.sample()
        return self.sampler.reply()

    def _write(self) -> None:
        self.settings.stat_path.write_text(
            stat_text(self._busy_ticks, self._idle_ticks, cores=self.CORES)
        )
        if self.cpu_stat_instead == "":
            self.cpu_stat_path.unlink(missing_ok=True)
        elif self.cpu_stat_instead is not None:
            self.cpu_stat_path.write_text(self.cpu_stat_instead)
        else:
            self.cpu_stat_path.write_text(f"usage_usec {self.lean_microseconds}\nuser_usec 0\n")


def write_topology(directory: Path, threads_of_each_core: tuple[int, ...]) -> None:
    """What the kernel keeps under /sys/devices/system/cpu: each CPU's list of its core's CPUs."""
    cpu = 0
    for threads in threads_of_each_core:
        siblings = str(cpu) if threads == 1 else f"{cpu}-{cpu + threads - 1}"
        for _ in range(threads):
            (directory / f"cpu{cpu}" / "topology").mkdir(parents=True)
            (directory / f"cpu{cpu}" / "topology" / "core_cpus_list").write_text(siblings + "\n")
            cpu += 1
    (directory / "cpufreq").mkdir()  # the kernel keeps other things beside the CPUs


def slots_of(reply: str) -> int:
    match = SLOTS_LINE.fullmatch(reply)
    assert match is not None, f"not a reply that states slots: {reply!r}"
    return 0 if match["slots"] is None else int(match["slots"])


def test_a_control_groups_cpu_use_is_read_in_microseconds() -> None:
    assert parse_cpu_usage_microseconds(CPU_STAT) == 123456789
    for text in ("", "user_usec 5\n", "usage_usec\n", "usage_usec -4\n", "usage_usec 1.5\n"):
        with pytest.raises(ReadingError, match="usage_usec"):
            parse_cpu_usage_microseconds(text)


def test_the_boxs_busy_cpus_and_the_time_between_two_readings_come_from_the_same_counters() -> None:
    earlier = CpuTimes(idle_ticks=1_000, total_ticks=4_000, cores=16)
    later = CpuTimes(idle_ticks=4_200, total_ticks=12_000, cores=16)
    # 8,000 ticks on 16 CPUs at 100 a second are 5 seconds, 3,200 of them idle: 9.6 CPUs busy.
    assert busy_cpus(earlier, later) == pytest.approx(9.6)
    assert elapsed_seconds(earlier, later, ticks_per_second=100) == 5.0
    assert busy_cpus(later, later) is None
    assert elapsed_seconds(later, earlier, ticks_per_second=100) is None


@pytest.mark.parametrize(
    ("others", "lean", "slots"),
    [(0.0, 0.0, 13), (9.6, 0.0, 5), (14.2, 0.0, 0), (6.0, 9.0, 9), (0.0, 13.0, 13), (15.9, 0.1, 0)],
)
def test_what_fits_is_the_boxs_cpus_less_the_reserve_less_everything_but_the_lean_server(
    others: float, lean: float, slots: int
) -> None:
    assert fitting_slots(16, 1.0, others + lean, lean, 13) == slots


def test_what_fits_is_never_below_zero_above_the_workers_or_lost_to_rounding() -> None:
    assert fitting_slots(4, 1.0, 9.0, 0.0, 8) == 0  # more work than CPUs
    assert fitting_slots(64, 1.0, 0.0, 0.0, 8) == 8
    assert fitting_slots(16, 1.0, 10.0 + 1e-12, 0.0, 13) == 5  # 4.999999999999 CPUs of room is 5
    assert fitting_slots(16, 0.0, 9.6, 0.0, 13) == 6
    assert fitting_slots(16, 2.5, 9.6, 0.0, 13) == 3
    # The Lean server cannot have used more than the box had busy; a reading that says so is
    # two files read a moment apart, and counts as no other work.
    assert fitting_slots(16, 1.0, 8.0, 8.4, 13) == 13


def test_slots_go_down_at_once_and_up_by_one() -> None:
    assert next_slots(None, 7) == 7
    assert next_slots(13, 5) == 5
    assert next_slots(5, 13) == 6
    assert next_slots(5, 5) == 5
    assert next_slots(0, 1) == 1


def test_a_box_with_nothing_else_on_it_states_its_worker_count(tmp_path: Path) -> None:
    box = SharedBox(tmp_path)
    assert box.run(others=0.0) == "ready up 100% maxconn:13\n"
    # Busy with nothing but checks: the Lean server's own use lowers the weight, never the slots.
    assert box.run(others=0.0, lean=13.0) == "ready up 19% maxconn:13\n"


def test_other_work_takes_slots_and_the_lean_servers_own_work_does_not(tmp_path: Path) -> None:
    assert SharedBox(tmp_path).run(others=9.6) == "ready up 40% maxconn:5\n"
    (tmp_path / "second").mkdir()
    # 15 CPUs busy, 9 of them the Lean server's: other work holds 6, and 9 slots are open.
    assert SharedBox(tmp_path / "second").run(others=6.0, lean=9.0) == "ready up 6% maxconn:9\n"


def test_no_room_for_a_check_answers_drain_and_never_a_limit_of_zero(tmp_path: Path) -> None:
    box = SharedBox(tmp_path)
    assert box.run(others=14.2) == "drain 0%\n"
    assert box.run(others=16.0) == "drain 0%\n"
    # "ready" in the next reply is what cancels the drain on HAProxy's side.
    assert box.run(others=13.0) == "ready up 19% maxconn:1\n"


def test_slots_fall_in_one_answer_and_climb_back_one_per_answer(tmp_path: Path) -> None:
    box = SharedBox(tmp_path)
    assert slots_of(box.run(others=0.0)) == 13
    assert slots_of(box.run(others=9.6)) == 5
    climb = [slots_of(box.run(others=0.0)) for _ in range(10)]
    assert climb == [6, 7, 8, 9, 10, 11, 12, 13, 13, 13]
    # A second burst while climbing is met at once, from wherever the climb had reached.
    assert slots_of(box.run(others=12.0)) == 3
    assert slots_of(box.run(others=11.0)) == 4
    assert slots_of(box.run(others=11.0)) == 4


@pytest.mark.parametrize("others", [0.0, 3.3, 9.6, 14.0, 14.2, 15.0, 16.0])
@pytest.mark.parametrize("lean", [0.0, 0.5, 4.0])
@pytest.mark.parametrize("reserve", [0.0, 1.0, 2.5])
def test_every_reply_that_states_slots_is_a_valid_line_and_never_says_zero(
    tmp_path: Path, others: float, lean: float, reserve: float
) -> None:
    box = SharedBox(tmp_path, reserve=reserve)
    reply = box.run(others=min(others, 16.0 - lean), lean=lean)
    assert SLOTS_LINE.fullmatch(reply) is not None, reply
    assert "maxconn:0" not in reply


def test_memory_below_the_floor_drains_whatever_the_cpu_says(tmp_path: Path) -> None:
    box = SharedBox(tmp_path)
    assert box.run(others=0.0) == "ready up 100% maxconn:13\n"
    box.settings.meminfo_path.write_text(meminfo_text(FLOOR - 4096))
    assert box.run(others=0.0) == "drain 0%\n"
    box.settings.meminfo_path.write_text(meminfo_text(PLENTY))
    assert box.run(others=0.0) == "ready up 100% maxconn:13\n"


@pytest.mark.parametrize(
    "told",
    [{"workers": None}, {"lean_cpu_stat_path": None}],
    ids=["no-workers", "no-control-group"],
)
def test_an_agent_not_told_how_to_measure_slots_answers_as_before_to_the_character(
    tmp_path: Path, told: dict[str, Any]
) -> None:
    """Both settings are needed. With one missing nothing of this item shows in a reply."""
    shared = SharedBox(tmp_path)
    untold = UsageSampler(dataclasses.replace(shared.settings, **told))
    before = UsageSampler(
        AgentSettings(
            memory_floor_bytes=FLOOR,
            stat_path=shared.settings.stat_path,
            meminfo_path=shared.settings.meminfo_path,
        )
    )
    for sampler in (untold, before):
        sampler.sample()
    assert untold.reply() == before.reply() == "ready up\n"
    for others, memory in ((9.6, PLENTY), (16.0, PLENTY), (0.0, FLOOR - 1), (3.0, 3 * GIBIBYTE)):
        shared.settings.meminfo_path.write_text(meminfo_text(memory))
        shared.run(others=others)
        for sampler in (untold, before):
            sampler.sample()
        assert untold.reply() == before.reply()
        assert "maxconn" not in untold.reply()
        assert untold.reply() != "drain 0%\n"


def test_a_control_group_that_cannot_be_read_states_the_whole_worker_count(tmp_path: Path) -> None:
    """HAProxy keeps the last limit it was told. An agent that measured 5 and then fell silent
    would leave 5 standing on a server nobody measures, so it says the worker count instead."""
    box = SharedBox(tmp_path)
    assert slots_of(box.run(others=9.6)) == 5
    box.cpu_stat_instead = ""
    assert box.run(others=9.6) == "ready up 40% maxconn:13\n"
    box.cpu_stat_instead = "no such line\n"
    assert box.run(others=12.0) == "ready up 25% maxconn:13\n"


def test_a_control_group_that_comes_back_is_measured_from_its_second_reading(
    tmp_path: Path,
) -> None:
    box = SharedBox(tmp_path)
    assert slots_of(box.run(others=9.6)) == 5
    box.cpu_stat_instead = ""
    assert slots_of(box.run(others=9.6)) == 13
    # Back, with other work on 9.6 CPUs all along. One reading is no interval: 13 stands.
    box.cpu_stat_instead = None
    assert slots_of(box.run(others=9.6)) == 13
    assert slots_of(box.run(others=9.6)) == 5
    # A reading with no time since the last one has no percentage, and keeps the slots.
    box.sampler.sample()
    assert box.sampler.reply() == "ready up maxconn:5\n"


def test_a_control_group_made_again_keeps_the_slots_until_a_whole_interval(tmp_path: Path) -> None:
    """A Lean server that was made again starts its counter from zero."""
    box = SharedBox(tmp_path)
    box.run(others=9.6, lean=4.0)
    assert slots_of(box.run(others=9.6, lean=4.0)) == 5
    box.lean_microseconds = 0
    assert slots_of(box.run(others=11.6, lean=0.5)) == 5  # the counter went backwards
    assert slots_of(box.run(others=11.6, lean=0.5)) == 3


def test_the_slots_settings_come_from_the_environment_and_blank_ones_are_unset() -> None:
    settings = parse_settings(
        [],
        {
            "LEANPOOL_AGENT_WORKERS": "13",
            "LEANPOOL_AGENT_LEAN_CPU_STAT_PATH": "/sys/fs/cgroup/leanpool.slice/cpu.stat",
            "LEANPOOL_AGENT_CPU_RESERVE": "2.5",
        },
    )
    assert settings.workers == 13
    assert settings.lean_cpu_stat_path == Path("/sys/fs/cgroup/leanpool.slice/cpu.stat")
    assert settings.cpu_reserve == 2.5
    assert settings.states_slots is True
    # What a compose file passes for variables an installer left blank.
    blank = parse_settings(
        [],
        {
            "LEANPOOL_AGENT_WORKERS": "",
            "LEANPOOL_AGENT_LEAN_CPU_STAT_PATH": "",
            "LEANPOOL_AGENT_CPU_RESERVE": "",
        },
    )
    assert blank == AgentSettings()
    assert blank.states_slots is False
    assert parse_settings(["--cpu-reserve", "0"], {}).cpu_reserve == 0.0
    assert blank.cpu_topology_path == Path("/sys/devices/system/cpu")
    elsewhere = parse_settings([], {"LEANPOOL_AGENT_CPU_TOPOLOGY_PATH": "/host/sys/cpu"})
    assert elsewhere.cpu_topology_path == Path("/host/sys/cpu")
    assert blank.slots_beside_other_work is None
    assert parse_settings([], {"LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK": ""}) == AgentSettings()
    limited = parse_settings([], {"LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK": "1"})
    assert limited.slots_beside_other_work == 1
    assert parse_settings(["--slots-beside-other-work", "0"], {}).slots_beside_other_work == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["--workers", "0"],
        ["--cpu-reserve", "-1"],
        ["--cpu-reserve", "nan"],
        ["--slots-beside-other-work", "-1"],
        ["--slots-beside-other-work", "1.5"],
    ],
)
def test_unusable_slots_settings_are_usage_errors(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_information:
        parse_settings(arguments, {})
    assert exit_information.value.code == 2


async def test_the_agent_serves_the_slots_it_measured(tmp_path: Path) -> None:
    box = SharedBox(tmp_path)
    agent = UsageAgent(box.settings)
    await agent.start()
    try:
        assert await read_reply(agent.port) == b"ready up\n"
        box.sampler = agent.sampler
        assert box.run(others=9.6) == "ready up 40% maxconn:5\n"
        assert await read_reply(agent.port) == b"ready up 40% maxconn:5\n"
    finally:
        await agent.close()


# --- slots are counted in cores, not hyperthreads -----------------------------------------------

# A desktop of 20 threads on 12 cores: eight cores of two threads and four of one.
TWENTY_THREADS_ON_TWELVE_CORES = (2,) * 8 + (1,) * 4
SIXTEEN_CORES_OF_TWO_THREADS = (2,) * 16


def test_the_cores_are_the_distinct_lists_of_cpus_that_share_one() -> None:
    assert threads_of_each_core(["4\n", "0-1\n", "0-1\n", "2-3\n", "2-3\n"]) == (2, 2, 1)
    older_spelling = ["0,12\n", "1,13\n", "0,12\n", "1,13\n"]
    assert threads_of_each_core(older_spelling) == (2, 2)
    assert threads_of_each_core(["0\n", "1\n", "2\n"]) == (1, 1, 1)
    assert threads_of_each_core(["0-3\n"] * 4) == (4,)
    assert threads_of_each_core([]) == ()
    assert threads_of_each_core(["", "\n"]) == ()


def test_the_topology_is_read_from_what_the_kernel_keeps_beside_each_cpu(tmp_path: Path) -> None:
    write_topology(tmp_path / "cpu", TWENTY_THREADS_ON_TWELVE_CORES)
    described = cpu_topology_of(tmp_path / "cpu")
    assert described == CpuTopology(TWENTY_THREADS_ON_TWELVE_CORES)
    assert described is not None
    assert (described.threads, described.cores) == (20, 12)
    # A CPU that is offline has no topology and is not counted.
    (tmp_path / "cpu" / "cpu20").mkdir()
    assert cpu_topology_of(tmp_path / "cpu") == described
    # An older kernel names the file otherwise.
    older = tmp_path / "older"
    (older / "cpu0" / "topology").mkdir(parents=True)
    (older / "cpu1" / "topology").mkdir(parents=True)
    (older / "cpu0" / "topology" / "thread_siblings_list").write_text("0-1\n")
    (older / "cpu1" / "topology" / "thread_siblings_list").write_text("0-1\n")
    assert cpu_topology_of(older) == CpuTopology((2,))
    assert cpu_topology_of(tmp_path / "nothing-here") is None
    (tmp_path / "empty").mkdir()
    assert cpu_topology_of(tmp_path / "empty") is None


@pytest.mark.parametrize(
    ("cores", "others", "workers", "slots"),
    [
        # The desktop: the other work and the reserve take the two-thread cores first.
        (TWENTY_THREADS_ON_TWELVE_CORES, 8.4, 13, 2),  # 9.4 cores taken: 2.6 one-thread cores left
        (TWENTY_THREADS_ON_TWELVE_CORES, 0.0, 13, 13),  # nothing else there: every worker
        (TWENTY_THREADS_ON_TWELVE_CORES, 3.0, 13, 12),  # 4 two-thread cores and 4 of one left
        (TWENTY_THREADS_ON_TWELVE_CORES, 6.5, 13, 5),  # half a two-thread core and 4 of one
        (TWENTY_THREADS_ON_TWELVE_CORES, 11.0, 13, 0),  # no core left
        # Every core alike: the cores left, times their threads.
        (SIXTEEN_CORES_OF_TWO_THREADS, 7.0, 12, 12),  # 8 cores left, 16 threads: every worker
        (SIXTEEN_CORES_OF_TWO_THREADS, 12.0, 12, 6),  # 3 cores left, two threads each
        ((1,) * 16, 9.6, 13, 5),  # one thread a core: what it always was
        ((1, 2, 1, 2), 1.0, 13, 2),  # in whatever order the cores are named
    ],
)
def test_a_check_may_use_a_thread_only_of_a_core_the_other_work_leaves(
    cores: tuple[int, ...], others: float, workers: int, slots: int
) -> None:
    threads = sum(cores)
    assert fitting_slots(threads, 1.0, others, 0.0, workers, cores) == slots
    # The Lean server's own work is no other work, however many threads it keeps busy.
    assert fitting_slots(threads, 1.0, others + 5.0, 5.0, workers, cores) == slots


def test_cores_that_are_not_of_the_cpus_counted_are_taken_as_one_thread_a_core() -> None:
    for wrong in (None, (), (2,) * 4, (2,) * 16, (0,) * 16, (17, -1)):
        assert fitting_slots(16, 1.0, 9.6, 0.0, 13, wrong) == 5


def test_on_a_box_with_two_threads_a_core_other_work_takes_cores_not_threads(
    tmp_path: Path,
) -> None:
    """A box measured live: 8 single-threaded tasks on 20 threads. Counted in threads
    10 checks fitted, and the tasks ran a third slower while the checks ran. Counted in cores, 2:
    the tasks hold the eight fast cores, and of the four slow ones one is the reserve's."""
    box = SharedBox(tmp_path, threads_of_each_core=TWENTY_THREADS_ON_TWELVE_CORES)
    assert box.run(others=8.4) == "ready up 58% maxconn:2\n"
    assert slots_of(box.run(others=0.0)) == 3  # back one a reading
    (tmp_path / "second").mkdir()
    free = SharedBox(tmp_path / "second", threads_of_each_core=TWENTY_THREADS_ON_TWELVE_CORES)
    assert free.run(others=0.5) == "ready up 98% maxconn:13\n"
    (tmp_path / "third").mkdir()
    big = SharedBox(
        tmp_path / "third", workers=12, threads_of_each_core=SIXTEEN_CORES_OF_TWO_THREADS
    )
    assert slots_of(big.run(others=7.0, lean=12.0)) == 12
    assert slots_of(big.run(others=12.0, lean=6.0)) == 6


def test_a_topology_that_is_not_of_the_cpus_being_counted_is_left_out(tmp_path: Path) -> None:
    """Where /sys is not the host's, it describes other CPUs than /proc/stat counts."""
    box = SharedBox(tmp_path)  # 16 CPUs in /proc/stat
    write_topology(tmp_path / "cpu-topology", (2,) * 4)  # and a topology of 8 threads on 4 cores
    sampler = UsageSampler(box.settings)
    sampler.sample()
    box.sampler = sampler
    assert box.run(others=9.6) == "ready up 40% maxconn:5\n"


# --- a measured limit on checks beside other work -----------------------------------------------


@pytest.mark.parametrize(
    ("others", "beside", "slots"),
    [
        (4.4, None, 9),  # the count alone: what the measured box stated beside four tasks
        (4.4, 1, 1),  # and what its measured limit makes of it
        (4.4, 3, 3),
        (4.4, 0, 0),  # a box that may take nothing beside other work
        (4.4, 12, 9),  # a limit never raises what the cores give
        (11.0, 1, 0),  # nor gives a slot where no core is left
        (1.0, 1, 1),  # one task's worth of other work is other work
        (0.9, 1, 13),  # under it the box is the Lean server's own
        (0.0, 0, 13),
    ],
)
def test_beside_other_work_a_box_states_no_more_than_its_measured_limit(
    others: float, beside: int | None, slots: int
) -> None:
    cores = TWENTY_THREADS_ON_TWELVE_CORES
    assert fitting_slots(20, 1.0, others, 0.0, 13, cores, beside) == slots
    # The Lean server's own work is no other work: thirteen busy checks do not bring the limit on.
    assert fitting_slots(20, 1.0, others + 13.0, 13.0, 13, cores, beside) == slots


def test_the_limit_comes_on_at_once_and_the_slots_come_back_one_a_reading(tmp_path: Path) -> None:
    """Other work comes to a box that is full of checks, stays, and leaves."""
    box = SharedBox(
        tmp_path, threads_of_each_core=TWENTY_THREADS_ON_TWELVE_CORES, beside_other_work=1
    )
    assert box.run(others=0.3, lean=12.0) == "ready up 38% maxconn:13\n"
    assert slots_of(box.run(others=4.4, lean=12.0)) == 1  # four tasks arrive: down at once
    assert slots_of(box.run(others=4.4, lean=1.0)) == 1
    assert slots_of(box.run(others=8.4, lean=1.0)) == 1  # more of them: the cores still give 2
    assert box.run(others=11.2, lean=0.0) == "drain 0%\n"  # the box is full: no core is left
    assert slots_of(box.run(others=4.4, lean=0.0)) == 1
    assert [slots_of(box.run(others=0.2, lean=float(n))) for n in (1, 2, 3)] == [2, 3, 4]


def test_a_limit_of_no_checks_beside_other_work_drains_and_is_never_maxconn_zero(
    tmp_path: Path,
) -> None:
    box = SharedBox(tmp_path, beside_other_work=0)
    assert box.run(others=0.2) == "ready up 99% maxconn:13\n"
    assert box.run(others=3.0) == "drain 0%\n"
    assert box.run(others=0.2) == "ready up 99% maxconn:1\n"


def test_a_box_given_no_limit_states_what_it_stated_before(tmp_path: Path) -> None:
    with_none = SharedBox(tmp_path, threads_of_each_core=TWENTY_THREADS_ON_TWELVE_CORES)
    assert with_none.run(others=4.4, lean=9.0) == "ready up 33% maxconn:9\n"
