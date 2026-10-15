"""The Lean server's root filesystem read-only, as the INSTALLERS and `kimina-ctl` handle it.

The REAL installers and the REAL `kimina-ctl` run here on the fake machine of `stand_ins.py` (read its
docstring first). Its Docker remembers whether a container was made read-only, plays `docker diff` of a Lean
server that has served checks, and fails a check against a read-only server when a test says so. What a real box
adds, and no test here shows: that the real server writes nothing outside /tmp, and that Docker refuses what it
would. The installer's own reading of each box (`docker diff`) is what answers the first, live.
"""

import os
import subprocess

import pytest
import yaml
from stand_ins import (
    ADMIT_CASES,
    INSTALLERS,
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
START = "--- read-only root:"
ELSEWHERE = "C /lean-project\nC /lean-project/.lake\nA /lean-project/.lake/lakefile.olean.lock"


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture
def fresh(tmp_path, deploy):
    """A box with Docker and nothing of ours on it yet."""
    return Machine(tmp_path / "host", deploy)


@pytest.fixture
def machine(fresh):
    """A box with the `demo` Lean server as the installer leaves it: read-only."""
    installed(fresh)
    assert fresh.read_only(LEAN)
    return fresh


@pytest.fixture
def writable(machine):
    """A box whose `demo` Lean server was installed before read-only roots: no setting, no record, a writable root."""
    before_read_only_roots(machine)
    return machine


def installed(
    machine: Machine, *options: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    result = machine.bash(*INSTALL, *options, **environment)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def before_read_only_roots(machine: Machine) -> None:
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(
        "".join(
            line
            for line in env.read_text().splitlines(keepends=True)
            if not line.startswith(("LEAN_SERVER_ROOT_READ_ONLY=", "LEAN_SERVER_READ_ONLY="))
        )
    )
    machine.path(f"{INSTANCE}/read-only.state").unlink(missing_ok=True)
    assert control(machine, "restart").returncode == 0
    assert not machine.read_only(LEAN)  # the compose file's default


def settings(machine: Machine) -> dict[str, str]:
    lines = machine.path(f"{INSTANCE}/kimina.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def control(machine: Machine, *words: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return machine.bash(machine.path(CTL), "@demo", *words, **environment)


def status_line(machine: Machine) -> str:
    result = control(machine, "status")
    assert result.returncode == 0, result.stderr
    assert "the-key-a-status-line-must-never-show" not in result.stdout + result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith(START)]
    return line


def state(machine: Machine) -> str:
    return machine.path(f"{INSTANCE}/read-only.state").read_text()


def said(result: subprocess.CompletedProcess[str], words: str) -> int:
    return (result.stdout + result.stderr).count(words)


# --- the compose file ---------------------------------------------------------------------------------------------


def test_the_compose_file_makes_the_root_read_only_only_when_the_setting_says_so_and_puts_tmp_in_memory():
    text = (INSTALLERS.parent / "lean-server" / "compose.yaml").read_text()
    kimina = yaml.safe_load(text)["services"]["kimina"]
    assert kimina["read_only"] == "${LEAN_SERVER_ROOT_READ_ONLY:-false}"
    assert kimina["tmpfs"] == ["/tmp:size=512m,mode=1777,noexec,nosuid,nodev"]
    assert text.count("read_only:") == 1 and text.count("tmpfs:") == 1


# --- the reading, and the switch ----------------------------------------------------------------------------------


def test_a_first_install_is_read_only_with_nothing_to_read_first(fresh):
    mark = fresh.mark()
    result = installed(fresh)
    assert fresh.diffs() == [] and fresh.read_only(LEAN)
    assert settings(fresh)[
        "LEAN_SERVER_ROOT_READ_ONLY"
    ] == "true" and "LEAN_SERVER_READ_ONLY" not in settings(fresh)
    assert (
        state(fresh) == "on\n"
        and fresh.path(f"{INSTANCE}/read-only.state").stat().st_mode & 0o777 == 0o644
    )
    line = "READ-ONLY ROOT: on - a new server, with nothing to read first; a real check is sent to it once it is made"
    assert said(result, line) == 2 and f"  read-only   {line}" in result.stdout
    # The check after the switch went to this server, by itself.
    assert any(
        "exec -i lean-server-demo-kimina-1 python -" in call for call in fresh.calls_since(mark)
    )
    assert status_line(fresh) == (
        f"{START} on (a check can change neither Lean, Mathlib nor the REPL for the checks "
        "after it; /tmp is in memory)"
    )


def test_an_install_over_a_server_that_wrote_only_tmp_and_byte_code_switches_it(writable):
    had = writable.recreated()
    result = installed(writable)
    assert (
        writable.diffs() == [LEAN]
        and writable.read_only(LEAN)
        and writable.recreated() == [*had, LEAN]
    )
    listing = writable.path(f"{INSTANCE}/read-only.diff").read_text()
    assert (
        "A /tmp/tmpq8x1v2c4" in listing
        and "__pycache__" in listing
        and "A /opt/lean-native" in listing
    )
    assert "/lean-project/.lake/packages/mathlib/.git/index" in listing  # git's own cache, allowed
    assert "A /usr/sbin/docker-init" in listing  # Docker's own init, allowed
    assert (
        "docker diff: 18 lines, 0 outside /tmp, the mount points (Docker's own too), Python's byte-code cache and "
        "git's index of each Lake package"
    ) in result.stdout
    line = "READ-ONLY ROOT: on - the running server wrote nothing outside /tmp; a real check is sent to it once it is made again"
    assert said(result, line) == 2
    assert state(writable) == "on\n" and settings(writable)["LEAN_SERVER_ROOT_READ_ONLY"] == "true"


def test_a_server_that_wrote_elsewhere_stays_writable_and_the_next_run_reads_it_again(writable):
    had = writable.recreated()
    result = installed(writable, STUB_DIFF_EXTRA=ELSEWHERE)
    assert not writable.read_only(LEAN) and writable.recreated() == had
    reason = "the running server wrote outside /tmp: /lean-project/.lake/lakefile.olean.lock"
    assert (
        said(result, f"READ-ONLY ROOT: OFF - {reason}. The Lean server stays writable, as it was")
        == 2
    )
    assert "docker diff: 21 lines, 1 outside /tmp" in result.stdout
    assert (
        state(writable) == f"off {reason}\n"
        and settings(writable)["LEAN_SERVER_ROOT_READ_ONLY"] == "false"
    )
    assert (
        status_line(writable)
        == f"{START} OFF ({reason}; the server is writable, as it was, with the same answers)"
    )
    # Every path the reading refused, one a line under it, through the door as well (the reason holds a few).
    assert (
        "\n      wrote: /lean-project/.lake/lakefile.olean.lock\n"
        in control(writable, "status").stdout
    )
    # Not a setting: the next run reads the server again, and switches when it is clean.
    installed(writable)
    assert (
        writable.diffs() == [LEAN, LEAN] and writable.read_only(LEAN) and state(writable) == "on\n"
    )
    assert not writable.path(f"{INSTANCE}/read-only.outside").exists()


def test_status_lists_every_refused_path_and_counts_past_forty(writable):
    many = "\n".join(f"A /var/cache/lean/{number:03d}" for number in range(45))
    installed(writable, STUB_DIFF_EXTRA=many)
    output = control(writable, "status").stdout
    assert output.count("      wrote: /var/cache/lean/") == 40
    assert f"      wrote: ... and 5 more (in {writable.path(INSTANCE)}/read-only.outside)" in output
    assert writable.path(f"{INSTANCE}/read-only.outside").read_text().count("\n") == 45


def test_a_reading_that_fails_leaves_the_server_writable(writable):
    result = installed(writable, STUB_DIFF_FAILS="the layer is gone")
    assert not writable.read_only(LEAN)
    assert (
        said(
            result,
            "READ-ONLY ROOT: OFF - could not read what the running server writes (docker diff: Error response from daemon: the layer is gone)",
        )
        == 2
    )


def test_a_check_that_fails_with_the_root_read_only_makes_the_server_writable_again(fresh):
    result = installed(fresh, STUB_READ_ONLY_CHECK_FAILS=LEAN)
    assert not fresh.read_only(LEAN) and fresh.running(LEAN)
    reason = "a real check failed against the server with its root read-only"
    assert said(result, f"READ-ONLY ROOT: OFF - {reason}, so it was made writable again") == 2
    assert (
        state(fresh) == f"off {reason}\n"
        and settings(fresh)["LEAN_SERVER_ROOT_READ_ONLY"] == "false"
    )
    assert "LEAN_SERVER_READ_ONLY" not in settings(fresh)
    # The next run tries again: it reads the writable server, and keeps the switch when the check passes.
    installed(fresh)
    assert fresh.diffs() == [LEAN] and fresh.read_only(LEAN) and state(fresh) == "on\n"


def test_a_server_that_fails_writable_too_stays_writable_and_the_cause_is_said_to_be_elsewhere(
    fresh,
):
    """A server that answers no check at all fails read-only AND again writable: the read-only root was not the
    cause, and the installer says so rather than blame it. The install fails, and its root stays writable."""
    result = fresh.bash(*INSTALL, STUB_CHECK_FAILS_FIRST="1000")
    assert result.returncode == 1 and "INSTALLED, BUT THE CHECK FAILED" in result.stdout
    assert not fresh.read_only(LEAN) and fresh.running(LEAN)
    assert (
        said(
            result,
            "READ-ONLY ROOT: OFF - a real check failed against the server with its root read-only AND "
            "again writable, so the cause is not the read-only root",
        )
        == 2
    )
    assert state(fresh) == (
        "off a real check failed against the server read-only and again writable, "
        "so the cause is not the read-only root\n"
    )
    # The next run, with the server answering, reads it and switches it.
    installed(fresh)
    assert fresh.diffs() == [LEAN] and fresh.read_only(LEAN) and state(fresh) == "on\n"


def test_a_second_run_reads_nothing_and_makes_nothing_again(machine):
    had = machine.recreated()
    result = installed(machine)
    assert said(result, "READ-ONLY ROOT: on - the server already runs with it; not read again") == 2
    assert machine.diffs() == [] and machine.recreated() == had and machine.read_only(LEAN)


# --- the switch by hand -------------------------------------------------------------------------------------------


def test_off_makes_the_lean_server_alone_writable_and_on_goes_back(machine):
    had = machine.recreated()
    result = control(machine, "read-only", "off")
    assert result.returncode == 0, result.stderr
    assert "read-only root off: the Lean server is writable, as it was" in result.stdout
    assert (
        settings(machine)["LEAN_SERVER_READ_ONLY"] == "off"
        and settings(machine)["LEAN_SERVER_ROOT_READ_ONLY"] == "false"
    )
    assert not machine.read_only(LEAN) and machine.recreated() == [
        *had,
        LEAN,
    ]  # not its usage agent
    assert (
        status_line(machine)
        == f"{START} OFF (turned off on this box: sudo kimina-ctl @demo read-only on)"
    )
    result = control(machine, "read-only", "on")
    assert result.returncode == 0, result.stderr
    assert (
        "read-only root on: the Lean server was made again with it (to check it: sudo kimina-ctl @demo check)"
        in result.stdout
    )
    assert (
        "LEAN_SERVER_READ_ONLY" not in settings(machine)
        and settings(machine)["LEAN_SERVER_ROOT_READ_ONLY"] == "true"
    )
    assert machine.read_only(LEAN) and machine.recreated() == [*had, LEAN, LEAN]


def test_a_later_run_of_the_installer_keeps_an_off_and_reads_nothing(machine):
    assert control(machine, "read-only", "off").returncode == 0
    had = machine.recreated()
    result = installed(machine)
    assert (
        "READ-ONLY ROOT: off by this box's own setting (LEAN_SERVER_READ_ONLY=off" in result.stdout
    )
    assert "'sudo kimina-ctl @demo read-only on' turns it on" in result.stdout
    assert machine.diffs() == [] and machine.recreated() == had and not machine.read_only(LEAN)


def test_on_refuses_where_no_reading_of_the_server_passed_and_clears_the_setting(writable):
    installed(writable, STUB_DIFF_EXTRA=ELSEWHERE)
    assert control(writable, "read-only", "off").returncode == 0
    had = writable.recreated()
    result = control(writable, "read-only", "on")
    assert (
        result.returncode == 1
        and "the installer has not found this server writing nothing outside /tmp" in result.stderr
    )
    assert "the join command with --refresh" in result.stderr
    assert (
        "LEAN_SERVER_READ_ONLY" not in settings(writable)
        and settings(writable)["LEAN_SERVER_ROOT_READ_ONLY"] == "false"
    )
    assert not writable.read_only(LEAN) and writable.recreated() == had


def test_the_switch_goes_through_the_door(machine):
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo read-only off")
    assert result.returncode == 0, result.stderr
    assert not machine.read_only(LEAN)
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo read-only on")
    assert result.returncode == 0, result.stderr
    assert machine.read_only(LEAN)


@pytest.mark.parametrize(
    "words",
    [
        ("@demo", "read-only"),
        ("@demo", "read-only", "maybe"),
        ("@demo", "read-only", "ON"),
        ("@demo", "read-only", "on", "now"),
        ("@demo", "read-only", "true"),
    ],
)
def test_the_switch_refuses_anything_but_on_or_off(machine, words):
    env = machine.path(f"{INSTANCE}/kimina.env").read_bytes()
    had = machine.recreated()
    result = machine.bash(machine.path(CTL), *words)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refused" in result.stderr and "read-only on|off" in result.stderr
    assert machine.path(f"{INSTANCE}/kimina.env").read_bytes() == env and machine.recreated() == had


# --- status: how the RUNNING container was made -------------------------------------------------------------------


def test_status_says_when_the_settings_and_the_running_container_disagree(writable):
    env = writable.path(f"{INSTANCE}/kimina.env")
    env.write_text(env.read_text() + "LEAN_SERVER_ROOT_READ_ONLY=true\n")
    assert status_line(writable) == (
        f"{START} on in the settings, NOT in effect (the running container was made "
        "before it: sudo kimina-ctl @demo restart)"
    )
    assert control(writable, "restart").returncode == 0
    assert writable.read_only(LEAN) and status_line(writable).startswith(f"{START} on (")


def test_status_of_a_server_installed_before_read_only_roots(writable):
    assert status_line(writable) == (
        f"{START} OFF (this server was installed before it had one: the installer, or a "
        "refresh, reads it and turns it on)"
    )


# --- instance 1, and the pool's own host --------------------------------------------------------------------------


def test_instance_one_is_read_only_too(fresh):
    result = fresh.bash("install.sh", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert fresh.read_only("lean-server-kimina-1") and "READ-ONLY ROOT: on" in result.stdout
    status = fresh.bash(fresh.path(CTL), "status")
    assert status.returncode == 0 and f"{START} on (" in status.stdout


def test_the_pools_own_host_switches_its_server_and_checks_it_by_itself(writable):
    mark = writable.mark()
    result = writable.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout and writable.read_only(LEAN)
    assert (
        "Verifying this box's own Lean server by itself, with its root read-only" in result.stdout
    )
    assert (
        "  read-only   READ-ONLY ROOT: on - the running server wrote nothing outside /tmp"
        in result.stdout
    )
    assert any(
        "exec -i lean-server-demo-kimina-1 python -" in call for call in writable.calls_since(mark)
    )


def test_the_pools_own_host_makes_its_server_writable_again_when_its_check_fails(writable):
    result = writable.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        STUB_READ_ONLY_CHECK_FAILS=LEAN,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout and not writable.read_only(LEAN)
    assert (
        "  read-only   READ-ONLY ROOT: OFF - a real check failed against the server with its root read-only"
        in result.stdout
    )


def test_the_pools_own_host_keeps_a_failed_check_through_the_pool_as_its_verdict(writable):
    """When the check through the pool fails, this box's own server is still checked by itself (a pool whose only
    server the read-only root broke is put back), and the pool's failure stays the verdict."""
    result = writable.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_CHECK_FAILS_FIRST="1000"
    )
    assert (
        result.returncode == 1
        and "POOL INSTALLED, BUT THE CHECK THROUGH IT FAILED" in result.stdout
    )
    assert "OWN LEAN SERVER FAILED" not in result.stdout
    assert (
        "Verifying this box's own Lean server by itself, with its root read-only" in result.stdout
    )
    assert not writable.read_only(LEAN) and "so the cause is not the read-only root" in state(
        writable
    )
