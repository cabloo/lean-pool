"""The long-running commands as real processes: they start, serve and stop on SIGTERM."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import pytest
from live_pool import free_port

Value = TypeVar("Value")

STARTUP_SECONDS = 60.0
COMMANDS = {
    "leanpool-cache",
    "leanpool-agent",
    "leanpool-haproxy-config",
    "leanpool-admit",
    "leanpool-pki",
    "leanpool-join",
}


def eventually(attempt: Callable[[], Value | None], process: subprocess.Popen[str]) -> Value:
    """Repeat ``attempt`` until it returns something, while the process is still running."""
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"the command exited early:\n{process.communicate()[1]}")
        value = attempt()
        if value is not None:
            return value
        time.sleep(0.05)
    raise AssertionError("the command did not become ready in time")


@contextmanager
def running(
    module: str, arguments: list[str], environment: dict[str, str]
) -> Iterator[subprocess.Popen[str]]:
    """Run ``python -m module`` and make sure it is gone afterwards."""
    process = subprocess.Popen(
        [sys.executable, "-m", module, *arguments],
        env={"PATH": os.environ["PATH"], **environment},
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def stop(process: subprocess.Popen[str]) -> int:
    process.send_signal(signal.SIGTERM)
    return process.wait(timeout=STARTUP_SECONDS)


def test_every_command_is_installed() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    ours = {script.name: script for script in scripts if script.name.startswith("leanpool-")}
    assert set(ours) == COMMANDS
    for script in ours.values():
        assert callable(script.load())


def test_the_cache_command_serves_until_it_is_told_to_stop(tmp_path: Path) -> None:
    port = free_port()
    environment = {
        "LEANPOOL_CACHE_PIN": "lean-4.27.0",
        "LEANPOOL_CACHE_API_KEY": "pool-key",
        "LEANPOOL_CACHE_DATABASE": str(tmp_path / "cache.sqlite3"),
        "LEANPOOL_CACHE_PORT": str(port),
    }

    def health() -> int | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as reply:
                status: int = reply.status
                return status
        except (urllib.error.URLError, ConnectionError):
            return None

    with running("leanpool.cache", [], environment) as process:
        assert eventually(health, process) == 200
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/status", headers={"Authorization": "Bearer pool-key"}
        )
        with urllib.request.urlopen(request, timeout=5) as reply:
            assert json.load(reply)["stored_entries"] == 0
        assert stop(process) == 0
    assert (tmp_path / "cache.sqlite3").exists()


@pytest.mark.skipif(not Path("/proc/stat").exists(), reason="the agent reads Linux's /proc")
def test_the_agent_command_reports_this_box_until_it_is_told_to_stop() -> None:
    port = free_port()
    arguments = ["--host", "127.0.0.1", "--port", str(port), "--sample-interval", "0.2"]
    # A floor of 1 MiB: a small test machine must not answer "drain".
    arguments += ["--memory-floor-mib", "1"]

    def percentage() -> int | None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
                reply = connection.makefile().read()
        except OSError:
            return None
        match = re.fullmatch(r"ready up (\d{1,3})%\n", reply)
        assert match or reply == "ready up\n", f"not an agent-check line: {reply!r}"
        return int(match[1]) if match else None

    with running("leanpool.agent", arguments, {}) as process:
        assert 1 <= eventually(percentage, process) <= 100
        assert stop(process) == 0
