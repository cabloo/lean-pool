"""The usage agent's measurements, its formula and its replies."""

from __future__ import annotations

import asyncio
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
    headroom_percent,
    idle_share,
    memory_share,
    parse_cpu_times,
)
from leanpool.agent.cli import main, parse_settings
from leanpool.agent.readings import (
    ReadingError,
    parse_available_memory_bytes,
    parse_one_minute_load,
)
from leanpool.agent.server import run_agent

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
