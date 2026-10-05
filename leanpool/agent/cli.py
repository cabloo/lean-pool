"""``leanpool-agent``: run the usage agent on a Lean server box.

Exit status: 0 after a requested stop, 1 when a measurement could not be read, 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from leanpool.agent.readings import ReadingError
from leanpool.agent.server import AgentSettings, run_agent
from leanpool.environment import (
    SettingsParser,
    parse_port,
    parse_positive_integer,
    parse_positive_number,
)

_BYTES_PER_MEBIBYTE = 1024**2
_MEASUREMENT_FAILED = 1


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run the agent until it is stopped and return the process exit status."""
    settings = parse_settings(arguments, os.environ if environment is None else environment)
    try:
        asyncio.run(_run_until_signalled(settings))
    except (ReadingError, OSError) as error:
        print(f"leanpool-agent: {error}", file=sys.stderr)
        return _MEASUREMENT_FAILED
    return 0


def parse_settings(
    arguments: Sequence[str] | None, environment: Mapping[str, str]
) -> AgentSettings:
    """Read the agent's settings from flags and environment variables."""
    defaults = AgentSettings()
    parser = argparse.ArgumentParser(
        prog="leanpool-agent",
        description="Report this box's CPU and memory headroom to HAProxy (agent-check).",
        allow_abbrev=False,
    )
    settings = SettingsParser(parser, environment)
    settings.add("--host", "LEANPOOL_AGENT_HOST", str, defaults.host, "the address to listen on")
    settings.add(
        "--port", "LEANPOOL_AGENT_PORT", parse_port, defaults.port, "the port to listen on"
    )
    settings.add(
        "--sample-interval",
        "LEANPOOL_AGENT_SAMPLE_INTERVAL_SECONDS",
        parse_positive_number,
        defaults.sample_interval_seconds,
        "seconds between two readings; the CPU share is measured over this interval",
    )
    settings.add(
        "--memory-floor-mib",
        "LEANPOOL_AGENT_MEMORY_FLOOR_MIB",
        parse_positive_integer,
        defaults.memory_floor_bytes // _BYTES_PER_MEBIBYTE,
        "available memory, in MiB, below which the agent answers 'drain'; the reported "
        "percentage starts to fall at twice this",
    )
    settings.add_switch(
        "--use-load-average",
        "LEANPOOL_AGENT_USE_LOAD_AVERAGE",
        defaults.use_load_average,
        "also limit the percentage by the 1-minute load average per core",
    )
    settings.add(
        "--stat-path",
        "LEANPOOL_AGENT_STAT_PATH",
        Path,
        defaults.stat_path,
        "where to read CPU times",
    )
    settings.add(
        "--meminfo-path",
        "LEANPOOL_AGENT_MEMINFO_PATH",
        Path,
        defaults.meminfo_path,
        "where to read available memory",
    )
    settings.add(
        "--loadavg-path",
        "LEANPOOL_AGENT_LOADAVG_PATH",
        Path,
        defaults.loadavg_path,
        "where to read the load average",
    )
    options = parser.parse_args(arguments)
    return AgentSettings(
        host=options.host,
        port=options.port,
        sample_interval_seconds=options.sample_interval,
        memory_floor_bytes=options.memory_floor_mib * _BYTES_PER_MEBIBYTE,
        use_load_average=options.use_load_average,
        stat_path=options.stat_path,
        meminfo_path=options.meminfo_path,
        loadavg_path=options.loadavg_path,
    )


async def _run_until_signalled(settings: AgentSettings) -> None:
    """Run the agent until SIGINT or SIGTERM.

    The handlers are installed explicitly because the agent is usually process 1 of a container,
    and the kernel does not apply a signal's default action to process 1: without a handler,
    stopping the container would wait for its kill timeout.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stop.set)
    await run_agent(settings, stop)
