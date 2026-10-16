"""Each Lean worker in a sandbox of its own, as the INSTALLERS and `kimina-ctl` handle it.

The REAL installers and the REAL `kimina-ctl` run here on the fake machine of `stand_ins.py` (read its
docstring first). Its Docker plays the probe make.sh runs in a container of the box's image, remembers the
program a Lean server's workers are started as when it was made, lists a running server's processes with their
environment (`docker top`), and fails a check against a server whose workers start in the sandbox when a test
says so. What the sandbox itself does is tests/test_worker_sandbox.py's, on a real kernel. What a real box adds,
and no test here shows: that its kernel and its Docker let the probe make the domain, and that Kimina's workers
answer from inside it. The installer's own real check of each server answers the second, live.
"""

import os
import subprocess

import pytest
from stand_ins import (
    ADMIT_CASES,
    Machine,
    rewritten_installers,
    tools_run_from_source,
)

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

INSTANCE = "/srv/kimina-demo"
LEAN = "lean-server-demo-kimina-1"
CTL = "/usr/local/bin/kimina-ctl"
INSTALL = ("install.sh", "--instance", "demo", "--no-door")
START = "--- worker sandbox:"
LAUNCHER = "/opt/lean-sandbox/lean-worker"
NATIVE = "/opt/lean-native/repl-native"
IMAGE_REPL = "/repl/.lake/build/bin/repl"
OVERLAY = "compose.worker-sandbox.yaml"
FOUND = "Landlock 6: /proc, files, TCP, signals"
CHECK_FAILED = "a real check failed against the server with each worker in its sandbox"


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture
def fresh(tmp_path, deploy):
    """A box with Docker and nothing of ours on it yet."""
    return Machine(tmp_path / "host", deploy)


@pytest.fixture
def machine(fresh):
    """A box with the `demo` Lean server as the installer leaves it: each worker in its sandbox."""
    installed(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER
    return fresh


def installed(
    machine: Machine, *options: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    result = machine.bash(*INSTALL, *options, **environment)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def settings(machine: Machine, instance: str = INSTANCE) -> dict[str, str]:
    lines = machine.path(f"{instance}/kimina.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def overlays(machine: Machine, instance: str = INSTANCE) -> list[str]:
    return settings(machine, instance)["COMPOSE_FILE"].split(":")


def mounts(machine: Machine, name: str = LEAN) -> list[str]:
    """Where the container's volumes were mounted inside it when it was last made."""
    return (machine.stub / "state" / f"mounts.{name}").read_text().split()


def control(machine: Machine, *words: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return machine.bash(machine.path(CTL), "@demo", *words, **environment)


def status_lines(machine: Machine, *words: str, **environment: str) -> list[str]:
    result = machine.bash(machine.path(CTL), *words, "status", **environment)
    assert result.returncode == 0, result.stderr
    assert "the-key-a-status-line-must-never-show" not in result.stdout + result.stderr
    return result.stdout.splitlines()


def status_line(machine: Machine, **environment: str) -> str:
    (line,) = [
        line for line in status_lines(machine, "@demo", **environment) if line.startswith(START)
    ]
    return line


def state(machine: Machine, instance: str = INSTANCE) -> str:
    return machine.path(f"{instance}/sandbox/state").read_text()


def said(result: subprocess.CompletedProcess[str], words: str) -> int:
    return (result.stdout + result.stderr).count(words)


def source(machine: Machine):
    """The worker-sandbox/ directory of the project the installers are part of."""
    return machine.deploy.parent / "lean-server" / "worker-sandbox"


# --- an install ---------------------------------------------------------------------------------------------------


def test_a_probe_that_passes_puts_the_sandbox_in_place_before_the_server_is_made(fresh):
    mark = fresh.mark()
    result = installed(fresh)
    # Asked once, in the instance's image, made as the server is: nothing pulled, no network, no capability,
    # no new privileges, a read-only root, a /tmp in memory, the sandbox's source read-only and nothing else.
    assert fresh.sandbox_probes() == ["acme/kimina:lean-demo-1"]
    calls = fresh.calls_since(mark)
    (probe,) = [call for call in calls if "sandbox.py --probe" in call]
    assert probe.startswith(
        "docker run --rm --pull never --name lean-server-demo-sandbox --network none --cap-drop ALL "
        "--security-opt no-new-privileges:true --read-only "
        "--tmpfs /tmp:size=16m,mode=1777,noexec,nosuid,nodev "
        f"-v {source(fresh)}:/opt/lean-sandbox:ro --entrypoint sh acme/kimina:lean-demo-1 -c "
    )
    assert "docker rm -f lean-server-demo-sandbox" in calls[calls.index(probe) + 1 :]
    # The sandbox in the instance's directory, and the overlay last, before the server was first made.
    held = fresh.path(f"{INSTANCE}/sandbox")
    assert {path.name for path in held.iterdir()} == {
        "lean-worker",
        "sandbox.py",
        "repl-path",
        "state",
    }
    assert (held / "lean-worker").read_bytes() == (source(fresh) / "lean-worker").read_bytes()
    assert (held / "sandbox.py").read_bytes() == (source(fresh) / "sandbox.py").read_bytes()
    assert (held / "lean-worker").stat().st_mode & 0o777 == 0o755
    assert (held / "repl-path").read_text() == f"{IMAGE_REPL}\n" and state(fresh) == f"on {FOUND}\n"
    assert fresh.path(f"{INSTANCE}/sandbox.log").exists()
    assert overlays(fresh)[-1] == OVERLAY and "LEAN_SERVER_WORKER_SANDBOX" not in settings(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER and fresh.recreated().count(LEAN) == 1
    assert {"/opt/lean-sandbox", "/opt/lean-native"} <= set(mounts(fresh))
    assert said(result, f"WORKER SANDBOX: on - {FOUND}") == 2
    assert (
        f"  sandbox     WORKER SANDBOX: on - {FOUND}; a real check is sent to the server once it is made"
        in result.stdout
    )
    assert "| read-only on|off | sandbox on|off" in result.stdout
    # The installer's real check went to this server, by itself, with its workers in the sandbox.
    assert any("exec -i lean-server-demo-kimina-1 python -" in call for call in calls)
    assert status_line(fresh) == f"{START} on ({FOUND}); workers sandboxed: 4 of 4"


def test_the_compiled_search_step_is_on_inside_the_sandbox(machine):
    (line,) = [
        line
        for line in status_lines(machine, "@demo")
        if line.startswith("--- compiled search step:")
    ]
    assert line.startswith("--- compiled search step: on (")


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("no-landlock", "this kernel has no Landlock (Linux 5.13 or later)"),
        ("no-python", "the probe did not run: sh: 1: exec: python3: not found"),
        (
            "relative-repl",
            "this image does not name its REPL by an absolute path in LEAN_SERVER_REPL_PATH ('repl')",
        ),
    ],
)
def test_a_probe_that_fails_leaves_the_workers_as_before_says_why_and_the_next_run_tries_again(
    fresh, outcome, reason
):
    result = installed(fresh, STUB_SANDBOX=outcome)
    assert fresh.worker_program(LEAN) == NATIVE and OVERLAY not in overlays(fresh)
    assert "/opt/lean-sandbox" not in mounts(fresh)
    assert state(fresh) == f"off {reason}\n"
    assert not fresh.path(f"{INSTANCE}/sandbox/lean-worker").exists()
    # Where it was found, and again in the last lines.
    assert (
        said(
            result,
            f"WORKER SANDBOX: OFF - {reason}. The Lean server starts its workers as it did before, "
            "without it",
        )
        == 2
    )
    assert f"  sandbox     WORKER SANDBOX: OFF - {reason}." in result.stdout
    assert status_line(fresh) == (
        f"{START} OFF ({reason}; the workers start without it, with the same answers)"
    )
    # Not a setting: the next run asks again, and turns it on where the probe passes.
    installed(fresh)
    assert fresh.sandbox_probes() == ["acme/kimina:lean-demo-1"] * 2
    assert fresh.worker_program(LEAN) == LAUNCHER and state(fresh) == f"on {FOUND}\n"


def test_a_check_that_fails_with_the_sandbox_takes_it_away_and_the_install_is_still_verified(fresh):
    result = installed(fresh, STUB_SANDBOX_CHECK_FAILS=LEAN)
    assert fresh.worker_program(LEAN) == NATIVE and OVERLAY not in overlays(fresh)
    assert fresh.read_only(LEAN) and fresh.running(LEAN)  # only the one protection went
    assert state(fresh) == f"off {CHECK_FAILED}\n"
    assert (
        said(result, f"WORKER SANDBOX: OFF - {CHECK_FAILED}, so it was made again without it") == 2
    )
    assert "LEAN_SERVER_WORKER_SANDBOX" not in settings(fresh)
    # The next run tries again, and keeps it when the check passes.
    installed(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER and state(fresh) == f"on {FOUND}\n"


def test_a_check_that_fails_without_the_sandbox_too_puts_it_back_and_tries_the_read_only_root(
    fresh,
):
    """A server that fails only while its root is read-only fails with the sandbox and again without it: the
    sandbox was not the cause, so it comes back, and the read-only root is the one protection taken away."""
    result = installed(fresh, STUB_READ_ONLY_CHECK_FAILS=LEAN)
    assert fresh.worker_program(LEAN) == LAUNCHER and not fresh.read_only(LEAN)
    assert state(fresh) == f"on {FOUND}\n" and overlays(fresh)[-1] == OVERLAY
    assert (
        said(
            result,
            f"WORKER SANDBOX: on - {FOUND}; a real check failed with it AND without it, so it was not "
            "the cause",
        )
        == 2
    )
    assert (
        said(
            result,
            "READ-ONLY ROOT: OFF - a real check failed against the server with its root read-only",
        )
        == 2
    )
    assert status_line(fresh) == f"{START} on ({FOUND}); workers sandboxed: 4 of 4"


def test_a_check_that_passes_only_with_both_protections_off_takes_both_away(fresh):
    result = installed(fresh, STUB_SANDBOX_CHECK_FAILS=LEAN, STUB_READ_ONLY_CHECK_FAILS=LEAN)
    assert fresh.worker_program(LEAN) == NATIVE and not fresh.read_only(LEAN)
    both = "a real check passed only with the sandbox and the read-only root both off"
    assert state(fresh) == f"off {both}\n"
    assert fresh.path(f"{INSTANCE}/read-only.state").read_text() == f"off {both}\n"
    assert said(result, f"WORKER SANDBOX: OFF - {both}") == 2
    assert said(result, f"READ-ONLY ROOT: OFF - {both}") == 2


def test_a_server_that_answers_no_check_is_left_as_it_was_before_both_and_the_install_fails(fresh):
    result = fresh.bash(*INSTALL, STUB_CHECK_FAILS_FIRST="1000")
    assert result.returncode == 1 and "INSTALLED, BUT THE CHECK FAILED" in result.stdout
    assert fresh.worker_program(LEAN) == NATIVE and not fresh.read_only(LEAN)
    assert fresh.running(LEAN)
    assert state(fresh) == (
        f"off {CHECK_FAILED} and again without it, so the cause is not the sandbox\n"
    )
    assert said(result, f"WORKER SANDBOX: OFF - {CHECK_FAILED} AND again without it") == 2
    assert (
        "so the cause is not the read-only root"
        in fresh.path(f"{INSTANCE}/read-only.state").read_text()
    )
    # The next run, with the server answering, turns both on again.
    installed(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER and fresh.read_only(LEAN)


def test_a_second_run_with_nothing_changed_makes_nothing_again(machine):
    had = machine.recreated()
    result = installed(machine)
    assert machine.recreated() == had and machine.worker_program(LEAN) == LAUNCHER
    assert said(result, f"WORKER SANDBOX: on - {FOUND}") == 2
    assert state(machine) == f"on {FOUND}\n"


# --- the switch by hand -------------------------------------------------------------------------------------------


def test_off_makes_the_lean_server_alone_again_and_on_goes_back(machine):
    had = machine.recreated()
    result = control(machine, "sandbox", "off")
    assert result.returncode == 0, result.stderr
    assert "worker sandbox off: the workers start without it" in result.stdout
    assert settings(machine)["LEAN_SERVER_WORKER_SANDBOX"] == "off"
    assert OVERLAY not in overlays(machine) and machine.worker_program(LEAN) == NATIVE
    assert machine.recreated() == [*had, LEAN]
    assert (
        status_line(machine)
        == f"{START} OFF (turned off on this box: sudo kimina-ctl @demo sandbox on)"
    )
    result = control(machine, "sandbox", "on")
    assert result.returncode == 0, result.stderr
    assert f"worker sandbox on: {FOUND} (to check it: sudo kimina-ctl @demo check)" in result.stdout
    assert "LEAN_SERVER_WORKER_SANDBOX" not in settings(machine)
    assert overlays(machine)[-1] == OVERLAY and machine.worker_program(LEAN) == LAUNCHER
    assert machine.recreated() == [*had, LEAN, LEAN]


def test_a_later_run_of_the_installer_keeps_an_off_and_asks_nothing(machine):
    assert control(machine, "sandbox", "off").returncode == 0
    had = machine.recreated()
    result = installed(machine)
    assert (
        "WORKER SANDBOX: off by this box's own setting (LEAN_SERVER_WORKER_SANDBOX=off"
        in result.stdout
    )
    assert "'sudo kimina-ctl @demo sandbox on' turns it on" in result.stdout
    assert machine.sandbox_probes() == ["acme/kimina:lean-demo-1"]
    assert machine.recreated() == had and machine.worker_program(LEAN) == NATIVE


def test_on_refuses_where_the_probe_did_not_pass_and_clears_the_setting(fresh):
    installed(fresh, STUB_SANDBOX="no-landlock")
    assert control(fresh, "sandbox", "off").returncode == 0
    had = fresh.recreated()
    result = control(fresh, "sandbox", "on")
    assert result.returncode == 1
    assert "the installer has not seen this box make the sandbox" in result.stderr
    assert "the join command with --refresh" in result.stderr
    assert "LEAN_SERVER_WORKER_SANDBOX" not in settings(fresh)
    assert OVERLAY not in overlays(fresh) and fresh.worker_program(LEAN) == NATIVE
    assert fresh.recreated() == had


def test_the_switch_goes_through_the_door(machine):
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo sandbox off")
    assert result.returncode == 0, result.stderr
    assert machine.worker_program(LEAN) == NATIVE
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo sandbox on")
    assert result.returncode == 0, result.stderr
    assert machine.worker_program(LEAN) == LAUNCHER


@pytest.mark.parametrize(
    "words",
    [
        ("@demo", "sandbox"),
        ("@demo", "sandbox", "maybe"),
        ("@demo", "sandbox", "ON"),
        ("@demo", "sandbox", "on", "now"),
        ("@demo", "sandbox", "true"),
    ],
)
def test_the_switch_refuses_anything_but_on_or_off(machine, words):
    env = machine.path(f"{INSTANCE}/kimina.env").read_bytes()
    had = machine.recreated()
    result = machine.bash(machine.path(CTL), *words)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refused" in result.stderr and "sandbox on|off" in result.stderr
    assert machine.path(f"{INSTANCE}/kimina.env").read_bytes() == env
    assert machine.recreated() == had


# --- status: what the RUNNING container starts its workers as, and the workers themselves ---------------------------


def test_status_counts_the_workers_that_carry_the_mark_and_never_shows_their_environment(machine):
    mark = machine.mark()
    assert status_line(machine, STUB_WORKERS_UNMARKED="1") == (
        f"{START} on ({FOUND}); workers sandboxed: 3 of 4"
    )
    assert "docker top lean-server-demo-kimina-1 -o pid,args eww" in machine.calls_since(mark)
    assert status_line(machine, STUB_WORKERS_RUNNING="0") == (
        f"{START} on ({FOUND}); workers sandboxed: no worker running"
    )
    lines = status_lines(machine, "@demo")
    assert not any("LEANPOOL_WORKER_SANDBOX" in line or "HOME=" in line for line in lines)


def test_status_says_when_the_running_container_starts_its_workers_without_a_probe_that_passed(
    machine,
):
    machine.path(f"{INSTANCE}/sandbox/state").write_text("off the kernel changed\n")
    assert status_line(machine) == (
        f"{START} on in the running container, but this box's probe did not pass ('off the kernel "
        "changed'): a worker that cannot be sandboxed does not start (re-run the installer, or sudo "
        "kimina-ctl @demo sandbox off); workers sandboxed: 4 of 4"
    )


def test_status_says_when_the_settings_and_the_running_container_disagree(machine):
    assert control(machine, "sandbox", "off").returncode == 0
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(
        env.read_text()
        .replace("LEAN_SERVER_WORKER_SANDBOX=off\n", "")
        .replace(
            f"COMPOSE_FILE={settings(machine)['COMPOSE_FILE']}",
            f"COMPOSE_FILE={settings(machine)['COMPOSE_FILE']}:{OVERLAY}",
        )
    )
    assert status_line(machine) == (
        f"{START} on in the settings, NOT in effect (the running container was made before it: "
        "sudo kimina-ctl @demo restart)"
    )
    assert control(machine, "restart").returncode == 0
    assert machine.worker_program(LEAN) == LAUNCHER
    assert status_line(machine).startswith(f"{START} on (")


def test_status_of_a_server_installed_before_the_sandbox(machine):
    assert control(machine, "sandbox", "off").returncode == 0
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(env.read_text().replace("LEAN_SERVER_WORKER_SANDBOX=off\n", ""))
    machine.path(f"{INSTANCE}/sandbox/state").unlink()
    assert status_line(machine) == (
        f"{START} OFF (this server was installed before it had one: the installer, or a refresh, asks "
        "its image and turns it on)"
    )


# --- the other protections switch under it ------------------------------------------------------------------------


def test_the_compiled_search_step_and_the_read_only_root_switch_without_losing_it(machine):
    for words, check in (
        (("compiled-search", "off"), lambda: "/opt/lean-native" not in mounts(machine)),
        (("compiled-search", "on"), lambda: "/opt/lean-native" in mounts(machine)),
        (("read-only", "off"), lambda: not machine.read_only(LEAN)),
        (("read-only", "on"), lambda: machine.read_only(LEAN)),
    ):
        result = control(machine, *words)
        assert result.returncode == 0, result.stderr
        assert check(), words
        assert machine.worker_program(LEAN) == LAUNCHER and "/opt/lean-sandbox" in mounts(machine)
        assert status_line(machine) == f"{START} on ({FOUND}); workers sandboxed: 4 of 4"
    assert overlays(machine)[-1] == OVERLAY


# --- instance 1, and the pool's own host --------------------------------------------------------------------------


def test_instance_one_is_sandboxed_and_switched_too(fresh):
    result = fresh.bash("install.sh", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert fresh.worker_program("lean-server-kimina-1") == LAUNCHER
    assert "WORKER SANDBOX: on" in result.stdout
    assert state(fresh, "/srv/kimina") == f"on {FOUND}\n"
    (line,) = [line for line in status_lines(fresh) if line.startswith(START)]
    assert line == f"{START} on ({FOUND}); workers sandboxed: 4 of 4"
    assert fresh.bash(fresh.path(CTL), "sandbox", "off").returncode == 0
    assert fresh.worker_program("lean-server-kimina-1") == IMAGE_REPL
    assert fresh.bash(fresh.path(CTL), "sandbox", "on").returncode == 0
    assert fresh.worker_program("lean-server-kimina-1") == LAUNCHER


def test_the_pools_own_host_sandboxes_its_server_and_checks_it_by_itself(machine):
    mark = machine.mark()
    result = machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    assert machine.worker_program(LEAN) == LAUNCHER
    assert "Verifying this box's own Lean server by itself, with its protections" in result.stdout
    assert f"  sandbox     WORKER SANDBOX: on - {FOUND}" in result.stdout
    assert any(
        "exec -i lean-server-demo-kimina-1 python -" in call for call in machine.calls_since(mark)
    )


def test_the_pools_own_host_takes_the_sandbox_away_when_its_check_fails(machine):
    result = machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        STUB_SANDBOX_CHECK_FAILS=LEAN,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    assert machine.worker_program(LEAN) == NATIVE and machine.read_only(LEAN)
    assert f"  sandbox     WORKER SANDBOX: OFF - {CHECK_FAILED}" in result.stdout
