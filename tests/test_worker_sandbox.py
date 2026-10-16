"""The worker sandbox (`deploy/lean-server/worker-sandbox/`), on this machine's own kernel.

Each wall is shown twice: the act a worker could do without the sandbox succeeds unsandboxed (so
the test can see a missing wall), and is refused under it. Skipped, with the reason, on a kernel
without Landlock. A wall of a later Landlock version than the kernel's is shown absent, as the
documentation says it is there.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

HERE = Path(__file__).resolve().parent
WORKER_SANDBOX = HERE.parent / "deploy" / "lean-server" / "worker-sandbox"
SANDBOX = WORKER_SANDBOX / "sandbox.py"


def _probe() -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(SANDBOX), "--probe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    said = result.stdout.strip()
    if result.returncode != 0:
        return 0, said
    return int(said.split("Landlock ")[1].split(":")[0]), said


VERSION, PROBE_SAID = _probe()
pytestmark = pytest.mark.skipif(VERSION == 0, reason=f"no Landlock here: {PROBE_SAID}")


def sandboxed(*program: str, **environment: str) -> subprocess.CompletedProcess[str]:
    """Run ``program`` under sandbox.py, as lean-worker does."""
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(SANDBOX), "--", *program],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **environment},
    )


def python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", code)


def unsandboxed(*program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(program, capture_output=True, text=True, timeout=30)


@pytest.fixture
def victim() -> Iterator[subprocess.Popen[bytes]]:
    """Another worker: a process outside the sandbox, waiting on a pipe."""
    process = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE)
    yield process
    process.kill()
    process.wait()


@pytest.fixture
def sandboxed_victim() -> Iterator[subprocess.Popen[bytes]]:
    """Another worker in a sandbox of its own: a sibling domain."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", str(SANDBOX), "--", "/bin/sleep", "60"],
        stdin=subprocess.PIPE,
    )
    yield process
    process.kill()
    process.wait()


def opens(path: str) -> str:
    return f"open({path!r}, 'rb').close(); print('opened')"


def test_the_probe_says_the_version_and_what_it_covers() -> None:
    assert PROBE_SAID.startswith(f"WORKER SANDBOX PROBE: ok - Landlock {VERSION}: /proc, files")


def test_the_worker_starts_without_any_secret_and_with_the_mark() -> None:
    result = sandboxed(
        "/usr/bin/env",
        LEAN_SERVER_API_KEY="pool-key",
        LEANPOOL_CACHE_API_KEY_FILE="/run/secrets/pool_api_key",
        GITHUB_TOKEN="t",
        db_password="p",
        AWS_SECRET_ACCESS_KEY="s",
        LEAN_PATH="/lean/lib",
    )
    assert result.returncode == 0, result.stderr
    environment = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert environment["LEANPOOL_WORKER_SANDBOX"] == f"landlock-{VERSION}"
    assert environment["LEAN_PATH"] == "/lean/lib"
    for name in (
        "LEAN_SERVER_API_KEY",
        "LEANPOOL_CACHE_API_KEY_FILE",
        "GITHUB_TOKEN",
        "db_password",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert name not in environment
    assert "pool-key" not in result.stdout


@pytest.mark.parametrize("target", ["fd/0", "environ"])
def test_another_workers_pipes_and_environment_are_refused(
    victim: subprocess.Popen[bytes], target: str
) -> None:
    """``mem`` is refused by the same check, but asks for the stronger ptrace right, which Yama
    (``ptrace_scope`` 1, Ubuntu's default) refuses between siblings without the sandbox too: no
    positive control can be had for it here."""
    path = f"/proc/{victim.pid}/{target}"
    assert unsandboxed(*python(opens(path))).stdout == "opened\n"
    result = sandboxed(*python(opens(path)))
    assert result.returncode != 0
    assert "PermissionError" in result.stderr


def test_a_sibling_workers_pipe_is_refused(sandboxed_victim: subprocess.Popen[bytes]) -> None:
    path = f"/proc/{sandboxed_victim.pid}/fd/0"
    assert unsandboxed(*python(opens(path))).stdout == "opened\n"
    result = sandboxed(*python(opens(path)))
    assert "PermissionError" in result.stderr


def test_the_parents_environment_is_refused() -> None:
    """Kimina's place: the process that started the worker, holding the server's key."""
    path = f"/proc/{os.getpid()}/environ"
    result = sandboxed(*python(opens(path)))
    assert "PermissionError" in result.stderr


def test_files_are_written_in_tmp_alone_and_dev_null_takes_writes() -> None:
    outside = Path(tempfile.mkdtemp(dir="/var/tmp")) if Path("/var/tmp").is_dir() else None
    if outside is None:
        pytest.skip("no /var/tmp here to write outside /tmp")
    inside = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        create = "open({!r}, 'w').write('x'); print('written')"
        assert unsandboxed(*python(create.format(str(outside / "a")))).stdout == "written\n"
        refused = sandboxed(*python(create.format(str(outside / "b"))))
        assert "PermissionError" in refused.stderr
        assert not (outside / "b").exists()
        assert sandboxed(*python(create.format(str(inside / "c")))).stdout == "written\n"
        null = sandboxed("/bin/sh", "-c", "echo x > /dev/null && echo ok")
        assert null.stdout == "ok\n"
        changed = sandboxed(*python(create.format(str(outside / "a"))))
        assert "PermissionError" in changed.stderr
    finally:
        shutil.rmtree(outside)
        shutil.rmtree(inside)


def test_reading_is_not_limited() -> None:
    result = sandboxed(*python(f"print(len(open({str(SANDBOX)!r}).read()) > 0)"))
    assert result.stdout == "True\n", result.stderr


def test_tcp_is_refused_both_ways_from_landlock_4() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        connect = f"import socket; socket.create_connection(('127.0.0.1', {port})); print('in')"
        assert unsandboxed(*python(connect)).stdout == "in\n"
        connected = sandboxed(*python(connect))
    bind = "import socket; socket.socket().bind(('127.0.0.1', 0)); print('bound')"
    assert unsandboxed(*python(bind)).stdout == "bound\n"
    bound = sandboxed(*python(bind))
    if VERSION >= 4:
        assert "PermissionError" in connected.stderr
        assert "PermissionError" in bound.stderr
    else:
        assert (connected.stdout, bound.stdout) == ("in\n", "bound\n")
        assert "TCP" not in PROBE_SAID


def test_a_signal_to_another_worker_is_refused_from_landlock_6(
    victim: subprocess.Popen[bytes],
) -> None:
    result = sandboxed(*python(f"import os; os.kill({victim.pid}, {int(signal.SIGTERM)})"))
    if VERSION >= 6:
        assert "PermissionError" in result.stderr
        assert victim.poll() is None
    else:
        assert result.returncode == 0, result.stderr
        assert victim.wait(timeout=10) == -signal.SIGTERM
        assert "signals" not in PROBE_SAID


def load_sandbox_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("worker_sandbox", SANDBOX)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_a_worker_that_cannot_be_sandboxed_is_not_started(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_sandbox_module()

    def no_landlock() -> int:
        raise module.SandboxError("this kernel has no Landlock (Linux 5.13 or later)")

    def must_not_start(*_arguments: object) -> None:
        raise AssertionError("an unsandboxed worker was started")

    monkeypatch.setattr(module, "landlock_version", no_landlock)
    monkeypatch.setattr(module.os, "execve", must_not_start)
    assert module.main(["/bin/true"]) == 126
    error = capsys.readouterr().err
    assert "cannot be sandboxed, so it is not started: this kernel has no Landlock" in error
    assert module.main(["--probe"]) == 1
    assert "WORKER SANDBOX PROBE: FAILED - this kernel has no Landlock" in capsys.readouterr().out


@pytest.fixture
def box(tmp_path: Path) -> Path:
    """A sandbox/ directory as make.sh leaves it, with stand-in programs to start."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    shutil.copy(WORKER_SANDBOX / "lean-worker", sandbox / "lean-worker")
    shutil.copy(SANDBOX, sandbox / "sandbox.py")
    for name in ("repl", "repl-native"):
        program = tmp_path / name
        program.write_text(f'#!/bin/sh\necho "{name} $* $LEANPOOL_WORKER_SANDBOX"\n')
        program.chmod(0o755)
    (sandbox / "repl-path").write_text(f"{tmp_path / 'repl'}\n")
    return tmp_path


def lean_worker(box: Path, launcher: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(box / "sandbox" / "lean-worker"), "--arg"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LEAN_SANDBOX_NATIVE_LAUNCHER": str(launcher)},
    )


def test_lean_worker_starts_the_compiled_launcher_where_it_can_and_the_repl_otherwise(
    box: Path,
) -> None:
    native = lean_worker(box, box / "repl-native")
    assert native.stdout == f"repl-native --arg landlock-{VERSION}\n", native.stderr
    plain = lean_worker(box, box / "absent")
    assert plain.stdout == f"repl --arg landlock-{VERSION}\n", plain.stderr


def test_lean_worker_without_a_repl_path_says_so(box: Path) -> None:
    (box / "sandbox" / "repl-path").unlink()
    result = lean_worker(box, box / "absent")
    assert result.returncode == 127
    assert "repl-path is missing (run worker-sandbox/make.sh)" in result.stderr
