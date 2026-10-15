"""The memory ceiling of a box's Lean server.

The REAL installer and the REAL `kimina-ctl` run here on the fake machine of `stand_ins.py` (read
its docstring first). What a real box adds, and no test here shows: that Docker holds the container to the
ceiling it was made with, and that `free` on that box gives the memory the kernel has.
"""

import os
import subprocess

import pytest
from stand_ins import Machine, rewritten_installers, tools_run_from_source

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

INSTANCE = "/srv/kimina-demo"
LEAN = "acme-kimina-demo"
CTL = "/usr/local/bin/kimina-ctl"
INSTALL = ("install.sh", "--instance", "demo", "--no-door")
GIB = 1024**3
START = "--- memory ceiling:"


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture
def machine(tmp_path, deploy):
    """A box with the `demo` Lean server as the installer leaves it: a ceiling of 36g."""
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    assert (
        settings(machine)["KIMINA_MEM_LIMIT"] == "36g" and machine.memory_ceiling(LEAN) == 36 * GIB
    )
    return machine


def installed(machine: Machine, *options: str) -> subprocess.CompletedProcess[str]:
    result = machine.bash(*INSTALL, *options)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def settings(machine: Machine) -> dict[str, str]:
    lines = machine.path(f"{INSTANCE}/kimina.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def control(machine: Machine, *words: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return machine.bash(machine.path(CTL), "@demo", *words, **environment)


def ceiling_line(machine: Machine, **environment: str) -> str:
    result = control(machine, "status", **environment)
    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith(START)]
    return line


# --- the command --------------------------------------------------------------------------------------------------


def test_set_mem_writes_the_ceiling_and_makes_the_lean_server_again_and_nothing_else(machine):
    had, said = machine.recreated(), settings(machine)
    result = control(machine, "set-mem", "28g")
    assert result.returncode == 0, result.stderr
    assert "memory ceiling set to 28g" in result.stdout
    # The one setting, and every other as it was: the worker count, what one worker may take, the CPU ceiling.
    assert settings(machine) == {**said, "KIMINA_MEM_LIMIT": "28g"}
    assert machine.recreated() == [*had, LEAN]  # not its usage agent
    assert machine.memory_ceiling(LEAN) == 28 * GIB and machine.running(LEAN)
    # Megabytes, and either case of the letter; the setting is kept in one form.
    assert control(machine, "set-mem", "512M").returncode == 0
    assert (
        settings(machine)["KIMINA_MEM_LIMIT"] == "512m"
        and machine.memory_ceiling(LEAN) == 512 * 1024**2
    )


def test_set_mem_goes_through_the_door(machine):
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo set-mem 28g")
    assert result.returncode == 0, result.stderr
    assert (
        settings(machine)["KIMINA_MEM_LIMIT"] == "28g" and machine.memory_ceiling(LEAN) == 28 * GIB
    )


def test_a_later_run_of_the_installer_keeps_the_ceiling(machine):
    assert control(machine, "set-mem", "28g").returncode == 0
    had = machine.recreated()
    installed(machine)  # the way a refresh runs it: no --mem-limit
    assert settings(machine)["KIMINA_MEM_LIMIT"] == "28g" and machine.recreated() == had
    assert machine.memory_ceiling(LEAN) == 28 * GIB
    # Changing the worker count leaves it too.
    assert control(machine, "set-repls", "6").returncode == 0
    assert (
        settings(machine)["KIMINA_MEM_LIMIT"] == "28g" and machine.memory_ceiling(LEAN) == 28 * GIB
    )


@pytest.mark.parametrize(
    "words",
    [
        ("set-mem",),
        ("set-mem", "28"),
        ("set-mem", "28gb"),
        ("set-mem", "1.5g"),
        ("set-mem", "0g"),
        ("set-mem", "028g"),
        ("set-mem", "28t"),
        ("set-mem", "g"),
        ("set-mem", "10000g"),
        ("set-mem", "28g", "2"),
    ],
)
def test_set_mem_refuses_anything_but_one_size_before_anything_is_touched(machine, words):
    had = (machine.recreated(), settings(machine), machine.memory_ceiling(LEAN))
    result = control(machine, *words)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refused" in result.stderr and "set-mem SIZE" in result.stderr
    assert (machine.recreated(), settings(machine), machine.memory_ceiling(LEAN)) == had


def test_a_ceiling_docker_will_not_make_a_container_with_is_taken_back(machine):
    had = (machine.recreated(), machine.path(f"{INSTANCE}/kimina.env").read_bytes())
    result = control(machine, "set-mem", "1m")  # under Docker's floor of 6 MB
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Minimum memory limit allowed is 6MB" in result.stdout  # Docker's own words
    assert (
        "could not be made with a memory ceiling of 1m" in result.stderr
        and "the settings are as they were" in result.stderr
    )
    assert (machine.recreated(), machine.path(f"{INSTANCE}/kimina.env").read_bytes()) == had
    assert machine.running(LEAN) and machine.memory_ceiling(LEAN) == 36 * GIB
    # A settings file that had no ceiling of its own (the compose file's default) has none again.
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(
        "".join(
            line
            for line in env.read_text().splitlines(keepends=True)
            if not line.startswith("KIMINA_MEM_LIMIT=")
        )
    )
    assert control(machine, "set-mem", "5m").returncode == 1
    assert "KIMINA_MEM_LIMIT" not in settings(machine) and machine.running(LEAN)
    assert control(machine, "set-mem", "6m").returncode == 0  # the floor itself is taken
    assert settings(machine)["KIMINA_MEM_LIMIT"] == "6m"


# --- status -------------------------------------------------------------------------------------------------------


def test_status_gives_the_ceiling_beside_the_boxs_memory(machine):
    assert ceiling_line(machine) == f"{START} 36.0 GiB of this box's 64.0 GiB of memory"
    assert control(machine, "set-mem", "28g").returncode == 0
    assert (
        ceiling_line(machine, STUB_BOX_MEMORY=str(33_500_000_000))
        == f"{START} 28.0 GiB of this box's 31.2 GiB of memory"
    )
    # A ceiling the box's memory exactly covers is not above it.
    assert (
        ceiling_line(machine, STUB_BOX_MEMORY=str(28 * GIB))
        == f"{START} 28.0 GiB of this box's 28.0 GiB of memory"
    )


def test_status_marks_a_ceiling_above_the_boxs_memory(machine):
    """The box that lost memory: a ceiling sized for what it had protects nothing."""
    line = ceiling_line(machine, STUB_BOX_MEMORY=str(33_500_000_000))
    assert line == (
        f"{START} 36.0 GiB, ABOVE this box's 31.2 GiB of memory: it protects nothing "
        "(lower it: sudo kimina-ctl @demo set-mem SIZE)"
    )
    assert "ABOVE" in ceiling_line(machine, STUB_BOX_MEMORY=str(36 * GIB - 1))


def test_status_gives_the_ceiling_alone_where_the_boxs_memory_cannot_be_read(machine):
    assert ceiling_line(machine, STUB_FREE_FAILS="1") == f"{START} 36.0 GiB"
    assert ceiling_line(machine, STUB_BOX_MEMORY="unknown") == f"{START} 36.0 GiB"
    assert ceiling_line(machine, STUB_BOX_MEMORY="0") == f"{START} 36.0 GiB"


def test_status_of_instance_one_names_its_own_command(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    assert machine.bash("install.sh", "--no-door").returncode == 0
    result = machine.bash(machine.path(CTL), "status", STUB_BOX_MEMORY=str(8 * GIB))
    assert result.returncode == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith(START)]
    assert line.endswith("(lower it: sudo kimina-ctl set-mem SIZE)")
