"""The demo's services as plain processes on loopback, to run its walkthrough without Docker.

``NativeDemo`` starts what ``examples/demo/compose.yaml`` starts: the two stand-in Lean servers,
a usage agent for each, HAProxy on the configuration rendered for them, and the cache. The
programs are the ones the compose file runs; only the addresses differ (loopback ports instead
of compose service names).

Run as a program it is the walkthrough's stand-in for ``docker compose``:

    python native_demo.py DIRECTORY stop NAME...
    python native_demo.py DIRECTORY start NAME...
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).parent.parent
DEMO = PROJECT / "examples" / "demo"
API_KEY = "demo-key"
_SERVICES_FILE = "services.json"
_STOP_SECONDS = 10.0


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port: int = listener.getsockname()[1]
    return port


def _accepts_connections(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        return True
    return False


@dataclass
class NativeDemo:
    """The demo's six services, described in ``DIRECTORY/services.json``."""

    directory: Path
    public_port: int
    _started: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def url(self) -> str:
        """The pool's address, as the walkthrough is given it."""
        return f"http://127.0.0.1:{self.public_port}"

    @classmethod
    def create(cls, directory: Path, haproxy: str, seconds: float = 0.4) -> NativeDemo:
        """Write the pool's configuration and the description of every service."""
        # Imported here: as the walkthrough's `docker compose` this file needs none of it.
        from live_pool import on_loopback

        from leanpool.haproxy import PoolSettings, parse_server_list, render_haproxy_config

        demo_servers = parse_server_list((DEMO / "servers").read_text())
        names = [server.name for server in demo_servers]
        workers = {server.name: server.workers for server in demo_servers}
        lean_ports = {name: _free_port() for name in names}
        agent_ports = {name: _free_port() for name in names}
        settings = PoolSettings(
            public_port=_free_port(),
            checkers_port=_free_port(),
            cache_address=f"127.0.0.1:{_free_port()}",
            stats_port=_free_port(),
        )
        server_list = "".join(
            f"{name} 127.0.0.1:{lean_ports[name]} {workers[name]} {agent_ports[name]}\n"
            for name in names
        )
        config = directory / "haproxy.cfg"
        config.write_text(
            on_loopback(render_haproxy_config(parse_server_list(server_list), settings))
        )

        cache_port = int(settings.cache_address.rpartition(":")[2])
        services: dict[str, dict[str, Any]] = {
            "haproxy": {
                "argv": [haproxy, "-db", "-f", str(config)],
                "environment": {},
                "port": settings.public_port,
            },
            "cache": {
                "argv": [sys.executable, "-m", "leanpool.cache"],
                "environment": {
                    "LEANPOOL_CACHE_PIN": "demo-stand-in",
                    "LEANPOOL_CACHE_API_KEY": API_KEY,
                    "LEANPOOL_CACHE_DATABASE": str(directory / "cache.sqlite3"),
                    "LEANPOOL_CACHE_PORT": str(cache_port),
                    "LEANPOOL_CACHE_UPSTREAM_URL": f"http://127.0.0.1:{settings.checkers_port}",
                },
                "port": cache_port,
            },
        }
        for name in names:
            suffix = name.removeprefix("lean-")
            services[name] = {
                "argv": [sys.executable, str(DEMO / "standin_lean_server.py")],
                "environment": {
                    "STANDIN_NAME": name,
                    "STANDIN_HOST": "127.0.0.1",
                    "STANDIN_PORT": str(lean_ports[name]),
                    "STANDIN_WORKERS": str(workers[name]),
                    "STANDIN_SECONDS": str(seconds),
                    "STANDIN_API_KEY": API_KEY,
                },
                "port": lean_ports[name],
            }
            services[f"agent-{suffix}"] = {
                "argv": [
                    sys.executable,
                    "-m",
                    "leanpool.agent",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(agent_ports[name]),
                ],
                "environment": {},
                "port": agent_ports[name],
            }
        (directory / _SERVICES_FILE).write_text(json.dumps(services, indent=2))
        return cls(directory=directory, public_port=settings.public_port)

    def start_all(self) -> None:
        """Start every service, the Lean servers before the proxy."""
        names = sorted(_services(self.directory), key=lambda name: name in ("haproxy", "cache"))
        for name in names:
            self._started.append(start(self.directory, name))

    def stop_all(self) -> None:
        """Stop every service that is running, and reap the ones this object started."""
        for name in _services(self.directory):
            stop(self.directory, name)
        for process in self._started:
            process.wait(timeout=_STOP_SECONDS)

    def log(self, name: str) -> str:
        """Everything one service has written so far."""
        path = self.directory / f"{name}.log"
        return path.read_text(errors="replace") if path.exists() else ""


def _services(directory: Path) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = json.loads((directory / _SERVICES_FILE).read_text())
    return services


def start(directory: Path, name: str) -> subprocess.Popen[bytes]:
    """Start one service and wait until its port accepts connections."""
    service = _services(directory)[name]
    environment = {**os.environ, **service["environment"]}
    with (directory / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(
            service["argv"],
            env=environment,
            cwd=PROJECT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    (directory / f"{name}.pid").write_text(str(process.pid))
    deadline = time.monotonic() + 30
    while not _accepts_connections(service["port"]):
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"{name} did not start; see {directory / (name + '.log')}")
        time.sleep(0.05)
    return process


def stop(directory: Path, name: str) -> None:
    """Stop one service and wait until its port is closed."""
    pid_file = directory / f"{name}.pid"
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text())
    pid_file.unlink()
    port = _services(directory)[name]["port"]
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_SECONDS
    while _accepts_connections(port):
        if time.monotonic() > deadline:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            break
        time.sleep(0.05)


def main(arguments: list[str]) -> int:
    """``DIRECTORY stop|start NAME...``: the two ``docker compose`` verbs the walkthrough uses."""
    if len(arguments) < 3 or arguments[1] not in ("stop", "start"):
        print("usage: native_demo.py DIRECTORY stop|start NAME...", file=sys.stderr)
        return 2
    directory, verb = Path(arguments[0]), arguments[1]
    for name in arguments[2:]:
        (stop if verb == "stop" else start)(directory, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
