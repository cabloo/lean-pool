"""``leanpool-join`` as a real process: HTTPS only, the pin a joining box checks, and its log.

The service listens on loopback with a throwaway front-door certificate made by the project's
own certificate code. The pin tests use the real ``curl`` and are skipped where it is not
installed.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from live_pool import free_port
from tls_support import ThrowawayAuthority, strict_client_context

from leanpool.pki import public_key_pin, read_certificate

CURL = shutil.which("curl")
TOKEN = "4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4c"
API_KEY = "pool-key-not-to-be-logged"
SCRIPT = "#!/bin/sh\necho 'joining the pool'\n"
CURL_PINNED_KEY_MISMATCH = 90
STARTUP_SECONDS = 60.0


@dataclass
class RunningJoin:
    """A join service process on loopback, and what a box needs to talk to it."""

    process: subprocess.Popen[str]
    port: int
    spool: Path
    authority_file: Path
    front_door: Path

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/j/{TOKEN}"

    @property
    def pin(self) -> str:
        return public_key_pin(read_certificate(self.front_door))

    def get(self, what: str = "") -> tuple[int, bytes]:
        """One HTTPS request from a client that trusts the pool's authority."""
        context = strict_client_context(self.authority_file)
        try:
            with urllib.request.urlopen(f"{self.url}{what}", context=context, timeout=10) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def stop(self) -> tuple[int, str]:
        """Ask the service to stop; return its exit status and everything it logged."""
        self.process.send_signal(signal.SIGTERM)
        _output, log = self.process.communicate(timeout=STARTUP_SECONDS)
        return self.process.returncode, log


def wait_until_listening(process: subprocess.Popen[str], port: int) -> None:
    """Wait for the command to accept connections, while it is still running."""
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"the command exited early:\n{process.communicate()[1]}")
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=1),
        ):
            return
        time.sleep(0.05)
    raise AssertionError("the command did not start listening in time")


@pytest.fixture
def running_join(tmp_path: Path) -> Iterator[RunningJoin]:
    authority = ThrowawayAuthority.create(tmp_path / "tls")
    front_door = authority.server("pool.test", addresses=("127.0.0.1",))
    (tmp_path / "join.sh").write_text(SCRIPT)
    port = free_port()
    environment = {
        "PATH": os.environ["PATH"],
        "LEANPOOL_JOIN_TOKEN": TOKEN,
        "LEANPOOL_JOIN_API_KEY": API_KEY,
        "LEANPOOL_JOIN_SCRIPT": str(tmp_path / "join.sh"),
        "LEANPOOL_JOIN_TLS_PEM": str(front_door),
        "LEANPOOL_JOIN_SPOOL": str(tmp_path / "spool"),
        "LEANPOOL_JOIN_HOST": "127.0.0.1",
        "LEANPOOL_JOIN_PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "leanpool.join"], env=environment, stderr=subprocess.PIPE, text=True
    )
    try:
        wait_until_listening(process, port)
        yield RunningJoin(process, port, tmp_path / "spool", authority.certificate_path, front_door)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def curl(*arguments: str) -> subprocess.CompletedProcess[str]:
    assert CURL is not None
    return subprocess.run(
        [CURL, *arguments], capture_output=True, text=True, check=False, timeout=30
    )


def test_the_command_serves_a_window_over_https_until_it_is_told_to_stop(
    running_join: RunningJoin,
) -> None:
    assert running_join.get() == (200, SCRIPT.encode())
    assert running_join.get("/certificate") == (204, b"")
    assert running_join.get("/nothing-here")[0] == 404
    assert (running_join.spool / "requests").is_dir()
    status, _log = running_join.stop()
    assert status == 0


def test_the_command_answers_nothing_in_plain_http(running_join: RunningJoin) -> None:
    plain_url = running_join.url.replace("https://", "http://")
    # A reset, or a TLS alert where an HTTP status line should be: no answer either way.
    refusals = (urllib.error.URLError, ConnectionError, http.client.HTTPException)
    with pytest.raises(refusals):
        urllib.request.urlopen(plain_url, timeout=10)


def test_the_command_refuses_a_handshake_below_tls_1_3(running_join: RunningJoin) -> None:
    context = strict_client_context(running_join.authority_file)
    assert handshake_version(running_join.port, context) == "TLSv1.3"
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ssl.SSLError, ConnectionError)):
        handshake_version(running_join.port, context)


def handshake_version(port: int, context: ssl.SSLContext) -> str | None:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10) as connection,
        context.wrap_socket(connection, server_hostname="pool.test") as secured,
    ):
        return secured.version()


@pytest.mark.skipif(CURL is None, reason="curl is not installed")
def test_curl_fetches_the_script_when_the_pin_is_the_front_doors(
    running_join: RunningJoin,
) -> None:
    """The command a joining box runs. ``-k`` only skips the chain check, which a box that
    does not have the pool's authority yet cannot do; the pin is still enforced.
    """
    fetched = curl("-fsSk", "--pinnedpubkey", running_join.pin, running_join.url)
    assert (fetched.returncode, fetched.stdout) == (0, SCRIPT)


@pytest.mark.skipif(CURL is None, reason="curl is not installed")
def test_curl_fetches_nothing_and_exits_90_when_the_pin_is_wrong(
    running_join: RunningJoin, tmp_path: Path
) -> None:
    another_key = ThrowawayAuthority.create(tmp_path / "another").authority.certificate
    wrong_pins = [
        public_key_pin(another_key),
        public_key_pin(read_certificate(running_join.authority_file)),  # the authority's own
        "sha256//AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    ]
    for wrong_pin in wrong_pins:
        fetched = curl("-fsSk", "--pinnedpubkey", wrong_pin, running_join.url)
        assert fetched.returncode == CURL_PINNED_KEY_MISMATCH
        assert fetched.stdout == ""
    _status, log = running_join.stop()
    # curl gave up before it sent a request: the service never saw one.
    assert "the join script was fetched" not in log


@pytest.mark.skipif(CURL is None, reason="curl is not installed")
def test_a_box_joins_with_curl_alone(running_join: RunningJoin, tmp_path: Path) -> None:
    """Every step of a join, as the join script makes them: pinned, and with JSON bodies."""
    pinned = ["-sSk", "--pinnedpubkey", running_join.pin, "-w", "\n%{http_code}"]
    request = {
        "name": "lean-a",
        "lean_port": 8000,
        "agent_port": 18200,
        "workers": 6,
        "csr": "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n-----END CERTIFICATE REQUEST-----\n",
    }
    body_file = tmp_path / "request.json"
    body_file.write_text(json.dumps(request))
    json_body = ["-H", "Content-Type: application/json", "--data-binary", f"@{body_file}"]

    def step(*arguments: str) -> tuple[str, str]:
        body, _, status = curl(*pinned, *arguments).stdout.rpartition("\n")
        return status, body

    assert step(*json_body, f"{running_join.url}/csr") == ("202", '{"status": "received"}')
    assert step(f"{running_join.url}/certificate") == ("204", "")
    answer = {"certificate": "CERTIFICATE", "ca": "AUTHORITY"}
    (running_join.spool / "responses").mkdir()
    (running_join.spool / "responses" / "certificate.json").write_text(json.dumps(answer))
    status, body = step(f"{running_join.url}/certificate")
    assert (status, json.loads(body)) == ("200", {**answer, "api_key": API_KEY})
    assert step("-X", "POST", f"{running_join.url}/ready")[0] == "202"
    verdict = {"admitted": True, "detail": "admitted"}
    (running_join.spool / "responses" / "verdict.json").write_text(json.dumps(verdict))
    status, body = step(f"{running_join.url}/verdict")
    assert (status, json.loads(body)) == ("200", verdict)

    written = json.loads((running_join.spool / "requests" / "csr.json").read_text())
    assert written == {**request, "address": "127.0.0.1"}


def test_the_commands_log_holds_neither_the_token_nor_the_api_key(
    running_join: RunningJoin,
) -> None:
    assert running_join.get()[0] == 200
    assert running_join.get("/certificate")[0] == 204
    assert running_join.get("/no-such-thing")[0] == 404
    context = strict_client_context(running_join.authority_file)
    wrong = urllib.request.Request(f"https://127.0.0.1:{running_join.port}/j/{'0' * 32}")
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(wrong, context=context, timeout=10)
    # A request that is not HTTP at all, inside TLS.
    with (
        socket.create_connection(("127.0.0.1", running_join.port), timeout=10) as connection,
        context.wrap_socket(connection, server_hostname="pool.test") as secured,
    ):
        secured.sendall(f"NONSENSE /j/{TOKEN} \x01\r\n\r\n".encode())
        secured.recv(4096)

    status, log = running_join.stop()

    assert status == 0
    assert "lean-pool join service on https://127.0.0.1:" in log
    assert "the join script was fetched from 127.0.0.1" in log
    assert "a request without this window's token" in log
    assert TOKEN not in log
    assert API_KEY not in log
    assert "/j/" not in log


def start_and_fail(tmp_path: Path, **changes: str) -> tuple[int, str]:
    """Run the command with settings it cannot start on; return its status and its error."""
    authority = ThrowawayAuthority.create(tmp_path / "tls")
    (tmp_path / "join.sh").write_text(SCRIPT)
    environment = {
        "PATH": os.environ["PATH"],
        "LEANPOOL_JOIN_TOKEN": TOKEN,
        "LEANPOOL_JOIN_API_KEY": API_KEY,
        "LEANPOOL_JOIN_SCRIPT": str(tmp_path / "join.sh"),
        "LEANPOOL_JOIN_TLS_PEM": str(authority.server("pool.test")),
        "LEANPOOL_JOIN_SPOOL": str(tmp_path / "spool"),
        "LEANPOOL_JOIN_HOST": "127.0.0.1",
        "LEANPOOL_JOIN_PORT": str(free_port()),
        **changes,
    }
    result = subprocess.run(
        [sys.executable, "-m", "leanpool.join"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=STARTUP_SECONDS,
    )
    return result.returncode, result.stderr


def test_the_command_does_not_start_without_a_usable_certificate(tmp_path: Path) -> None:
    (tmp_path / "not-a-certificate.pem").write_text("nothing useful\n")
    status, error = start_and_fail(
        tmp_path, LEANPOOL_JOIN_TLS_PEM=str(tmp_path / "not-a-certificate.pem")
    )
    assert status == 1
    assert "does not hold a certificate followed by its private key" in error
    assert TOKEN not in error


def test_the_command_does_not_start_on_a_spool_it_cannot_use(tmp_path: Path) -> None:
    (tmp_path / "spool").write_text("a file where the spool should be")
    status, error = start_and_fail(tmp_path)
    assert status == 1
    assert "leanpool-join: cannot create" in error


def test_the_command_does_not_start_on_a_port_that_is_taken(tmp_path: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        status, error = start_and_fail(tmp_path, LEANPOOL_JOIN_PORT=str(taken.getsockname()[1]))
    assert status == 1
    assert "leanpool-join: " in error
    assert "address already in use" in error.lower()


def test_a_usage_error_exits_with_status_two(tmp_path: Path) -> None:
    status, error = start_and_fail(tmp_path, LEANPOOL_JOIN_TOKEN="too-short")
    assert status == 2
    assert "the token must be 22 to 128 characters" in error
    assert "too-short" not in error
