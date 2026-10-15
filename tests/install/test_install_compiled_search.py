"""Linarith's search step as compiled code, as the INSTALLER and `kimina-ctl` handle it.

The REAL installer, the REAL `make.sh` it runs and the REAL `kimina-ctl` run here on the fake machine of
`stand_ins.py` (read its docstring first). Its Docker plays the build and the check: what they really do, against
a real Lean, is tests/test_compiled_search.py's. What a real box adds, and no test here shows: that its Docker
mounts the directory where the launcher expects it and lets a script on it be started.
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
NATIVE = f"{INSTANCE}/native"
LEAN = "lean-server-demo-kimina-1"
AGENT = "lean-server-demo-agent-1"
CTL = "/usr/local/bin/kimina-ctl"
INSTALL = ("install.sh", "--instance", "demo", "--no-door")
LAUNCHER = "/opt/lean-native/repl-native"
PLAIN = "/repl/.lake/build/bin/repl"  # the image's own, which its LEAN_SERVER_REPL_PATH names
OVERLAY = "compose.compiled-search.yaml"
START = "--- compiled search step:"
MEASURED = (
    "linarith's search took 11.4 s as the image runs it and 1.4 s compiled (8.4 times less); the same answers; "
    "a program a tactic starts does not inherit the library"
)
# What the library is made from, beside the image: the files of the lean-pool project's compiled-search/.
MADE_FROM = ("build.sh", "check.py", "inside.sh", "make.sh", "modules", "repl-native")
BUILT = {"repl-native", "repl-path", "linarith-search.so", "sources", "built-from", "state"}
LAUNCHER_ONLY = {"repl-native", "repl-path", "state"}


def compiled_search(installers):
    """The compiled-search/ directory of the project the installers in INSTALLERS are part of."""
    return installers.parent / "lean-server" / "compiled-search"


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture
def fresh(tmp_path, deploy):
    """A box with Docker and nothing of ours on it yet."""
    return Machine(tmp_path / "host", deploy)


@pytest.fixture
def machine(fresh):
    """A box with the `demo` Lean server as the installer leaves it: the compiled search step on."""
    result = installed(fresh)
    assert f"COMPILED SEARCH STEP: on - {MEASURED}" in result.stdout
    return fresh


def installed(
    machine: Machine, *options: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    result = machine.bash(*INSTALL, *options, **environment)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def settings(machine: Machine, directory: str = INSTANCE) -> dict[str, str]:
    lines = machine.path(f"{directory}/kimina.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def overlaid(machine: Machine) -> bool:
    """Whether the instance runs with the compiled search step's overlay: what turns the step on."""
    return OVERLAY in settings(machine)["COMPOSE_FILE"].split(":")


def control(machine: Machine, *words: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return machine.bash(machine.path(CTL), "@demo", *words, **environment)


def status_line(machine: Machine) -> str:
    result = control(machine, "status")
    assert result.returncode == 0, result.stderr
    assert "the-key-a-status-line-must-never-show" not in result.stdout + result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith(START)]
    return line


def held(machine: Machine) -> set[str]:
    return {path.name for path in machine.path(NATIVE).iterdir()}


# --- an install ---------------------------------------------------------------------------------------------------


def test_an_install_builds_the_step_checks_it_and_only_then_starts_the_workers_with_it(fresh):
    mark = fresh.mark()
    result = installed(fresh)
    # Built and checked once by the project's make.sh, in the instance's image, hardened as the server is, with
    # no network and nothing pulled, in a container named for the instance.
    assert fresh.native_builds() == ["acme/kimina:lean-demo-1"]
    calls = fresh.calls_since(mark)
    (build,) = [call for call in calls if "/lean-native-src/inside.sh" in call]
    source = compiled_search(fresh.deploy)
    assert build == (
        "docker run --pull never --name lean-server-demo-native --network none --cap-drop ALL "
        f"--security-opt no-new-privileges:true -v {source}:/lean-native-src:ro "
        "--entrypoint bash acme/kimina:lean-demo-1 /lean-native-src/inside.sh /opt/lean-native"
    )
    # ... copied out of the container into a directory of the run's own, never written into the box by it; the
    # container and that directory are gone when the run ends.
    copy = calls[calls.index(build) + 1]
    assert copy.startswith(
        f"docker cp lean-server-demo-native:/opt/lean-native/. {fresh.path(INSTANCE)}/native.work."
    )
    assert calls[calls.index(build) + 2] == "docker rm -f lean-server-demo-native"
    assert not list(fresh.path(INSTANCE).glob("native.work.*"))
    # ... before the server's container was made.
    assert calls.index(build) < min(
        index
        for index, call in enumerate(calls)
        if " up -d" in call and "--project-name lean-server-demo " in call
    )
    # What the box holds: the launcher (to be started) and the image's REPL, the library and what ties it to the
    # image, the record.
    assert held(fresh) == BUILT
    assert fresh.owner(f"{NATIVE}/repl-native") == ("root", "root", 0o755)
    assert fresh.owner(f"{NATIVE}/linarith-search.so") == ("root", "root", 0o644)
    assert fresh.owner(NATIVE) == ("root", "root", 0o755)
    assert fresh.path(f"{NATIVE}/repl-native").read_bytes() == (source / "repl-native").read_bytes()
    assert fresh.path(f"{NATIVE}/repl-path").read_text() == f"{PLAIN}\n"
    assert fresh.path(f"{NATIVE}/state").read_text() == f"on {MEASURED}\n"
    assert (
        fresh.path(f"{INSTANCE}/native.built").exists()
        and fresh.path(f"{INSTANCE}/native.log").exists()
    )
    # The server mounts that directory read-only through the step's overlay, and its workers are the launcher.
    compose = fresh.path(f"{INSTANCE}/{OVERLAY}").read_text()
    assert (
        "        source: ./native\n        target: /opt/lean-native\n        read_only: true\n"
        in compose
    )
    assert f"      LEAN_SERVER_REPL_PATH: {LAUNCHER}\n" in compose
    assert settings(fresh)["COMPOSE_FILE"].endswith(f":{OVERLAY}")
    assert overlaid(fresh) and fresh.worker_program(LEAN) == LAUNCHER
    assert "LEAN_SERVER_COMPILED_SEARCH" not in settings(fresh)
    # Said where it was made and again in the installer's last lines.
    assert result.stdout.count(f"COMPILED SEARCH STEP: on - {MEASURED}") == 2
    assert f"  search      COMPILED SEARCH STEP: on - {MEASURED}" in result.stdout
    assert status_line(fresh) == f"{START} on ({MEASURED})"


def test_a_second_run_makes_nothing_again(machine):
    had = (
        machine.recreated(),
        machine.native_builds(),
        machine.path(f"{NATIVE}/linarith-search.so").stat().st_mtime_ns,
    )
    result = installed(machine)
    assert (
        "COMPILED SEARCH STEP: on - already made from this image and checked" in result.stdout
        and "not made again" in result.stdout
    )
    assert (
        machine.recreated(),
        machine.native_builds(),
        machine.path(f"{NATIVE}/linarith-search.so").stat().st_mtime_ns,
    ) == had
    assert machine.worker_program(LEAN) == LAUNCHER


@pytest.mark.parametrize("changed", MADE_FROM)
def test_a_changed_file_of_compiled_search_makes_the_library_again_and_not_the_server(
    tmp_path, machine, changed
):
    """The library is a function of the image and of these files. The server's definition did not change, so
    its container stays: a worker loads the new library when it next starts. The usage agent is made again,
    once: the installer's version it states is of these files too."""
    newer = rewritten_installers(tmp_path / "newer")
    with open(compiled_search(newer) / changed, "a") as handle:
        handle.write("\n# one more line\n")
    had = (machine.recreated(), len(machine.native_builds()))
    machine.deploy = newer
    result = installed(machine)
    assert f"COMPILED SEARCH STEP: on - {MEASURED}" in result.stdout
    assert len(machine.native_builds()) == had[1] + 1 and machine.recreated() == [*had[0], AGENT]
    assert (
        machine.path(f"{NATIVE}/repl-native").read_bytes()
        == (compiled_search(newer) / "repl-native").read_bytes()
    )
    assert (
        installed(machine).stdout.count("not made again") == 2
        and len(machine.native_builds()) == had[1] + 1
    )


def test_a_library_that_is_gone_is_made_again(machine):
    machine.path(f"{NATIVE}/linarith-search.so").unlink()
    left = machine.path(
        f"{INSTANCE}/native.work.cutshort/native"
    )  # what a run that was cut short leaves
    left.mkdir(parents=True)
    had = len(machine.native_builds())
    installed(machine)
    assert (
        len(machine.native_builds()) == had + 1
        and machine.path(f"{NATIVE}/linarith-search.so").exists()
    )
    assert not list(machine.path(INSTANCE).glob("native.work.*"))


# --- a build or a check that fails does not fail the install ------------------------------------------------------


def test_an_image_without_the_c_files_installs_with_the_plain_repl_and_says_so(fresh):
    result = installed(fresh, STUB_NATIVE="no-c-files")
    said = [line for line in result.stderr.splitlines() if "COMPILED SEARCH STEP: OFF" in line]
    assert len(said) == 1 and said[0].startswith(
        "WARNING: COMPILED SEARCH STEP: OFF - nothing was built: build.sh: "
    )
    assert (
        "this image holds no C file for Mathlib/Tactic/Linarith/Oracle/SimplexAlgorithm of mathlib"
        in said[0]
    )
    assert (
        "every answer is the same, linarith's search is slower" in said[0]
        and f"Full log: {fresh.path(INSTANCE)}/native.log" in said[0]
    )
    assert "  search      COMPILED SEARCH STEP: OFF - nothing was built" in result.stdout
    # Nothing a worker could load, and the server runs the program it always ran.
    assert held(fresh) == LAUNCHER_ONLY and not fresh.path(f"{INSTANCE}/native.built").exists()
    assert fresh.path(f"{NATIVE}/state").read_text().startswith("off nothing was built: ")
    assert not overlaid(fresh) and fresh.worker_program(LEAN) == PLAIN and fresh.running(LEAN)
    assert status_line(fresh).startswith(f"{START} OFF (nothing was built: ")
    assert status_line(fresh).endswith("; the workers run the plain REPL, with the same answers)")
    # The next run tries again, and a build that passes turns the step on: the server is made again, once.
    had = fresh.recreated()
    installed(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER and fresh.recreated() == [*had, LEAN]
    assert status_line(fresh) == f"{START} on ({MEASURED})"


def test_a_library_that_does_not_pass_its_check_is_never_given_to_the_server(fresh):
    result = installed(fresh, STUB_NATIVE="check-fails")
    reason = "the check 'starts a program' got a different answer with the library than without"
    assert f"WARNING: COMPILED SEARCH STEP: OFF - {reason}." in result.stderr
    assert held(fresh) == LAUNCHER_ONLY  # the library the build wrote stayed in its container
    assert fresh.path(f"{NATIVE}/state").read_text() == f"off {reason}\n"
    assert not overlaid(fresh) and fresh.worker_program(LEAN) == PLAIN
    assert (
        status_line(fresh)
        == f"{START} OFF ({reason}; the workers run the plain REPL, with the same answers)"
    )


def test_a_box_that_had_the_step_loses_it_when_a_newer_build_does_not_pass(tmp_path, machine):
    newer = rewritten_installers(tmp_path / "newer")
    with open(compiled_search(newer) / "modules", "a") as handle:
        handle.write("mathlib Mathlib/Tactic/Linarith/Preprocessing\n")
    machine.deploy = newer
    had = machine.recreated()
    result = installed(machine, STUB_NATIVE="check-fails")
    assert "COMPILED SEARCH STEP: OFF" in result.stderr
    assert held(machine) == LAUNCHER_ONLY and not machine.path(f"{INSTANCE}/native.built").exists()
    assert not overlaid(machine)
    assert machine.worker_program(LEAN) == PLAIN and machine.recreated() == [*had, LEAN, AGENT]


# --- the switch ---------------------------------------------------------------------------------------------------


def test_off_starts_the_plain_repl_and_on_goes_back_with_the_library_the_box_kept(machine):
    had = machine.recreated()
    result = control(machine, "compiled-search", "off")
    assert result.returncode == 0, result.stderr
    assert "compiled search step off: the workers run the plain REPL" in result.stdout
    assert not overlaid(machine) and settings(machine)["LEAN_SERVER_COMPILED_SEARCH"] == "off"
    assert machine.worker_program(LEAN) == PLAIN and machine.recreated() == [
        *had,
        LEAN,
    ]  # not its usage agent
    assert (
        status_line(machine)
        == f"{START} OFF (turned off on this box; the workers run the plain REPL: sudo kimina-ctl @demo compiled-search on)"
    )
    assert "linarith-search.so" in held(machine)
    # Off twice makes nothing again.
    assert control(machine, "compiled-search", "off").returncode == 0 and machine.recreated() == [
        *had,
        LEAN,
    ]
    result = control(machine, "compiled-search", "on")
    assert result.returncode == 0, result.stderr
    assert f"compiled search step on: {MEASURED}" in result.stdout
    assert overlaid(machine) and "LEAN_SERVER_COMPILED_SEARCH" not in settings(machine)
    assert machine.worker_program(LEAN) == LAUNCHER and machine.recreated() == [*had, LEAN, LEAN]
    assert status_line(machine) == f"{START} on ({MEASURED})"


def test_a_later_run_of_the_installer_keeps_an_off_and_builds_nothing(machine):
    assert control(machine, "compiled-search", "off").returncode == 0
    had = (machine.recreated(), machine.native_builds())
    result = installed(machine)  # the way a refresh runs it
    assert (
        "COMPILED SEARCH STEP: off by this box's own setting (LEAN_SERVER_COMPILED_SEARCH=off"
        in result.stdout
    )
    assert "'sudo kimina-ctl @demo compiled-search on' turns it on" in result.stdout
    assert (machine.recreated(), machine.native_builds()) == had
    assert not overlaid(machine) and machine.worker_program(LEAN) == PLAIN


def test_on_refuses_where_the_box_has_no_library_that_passed_and_clears_the_setting(fresh):
    installed(fresh, STUB_NATIVE="check-fails")
    assert control(fresh, "compiled-search", "off").returncode == 0
    had = fresh.recreated()
    result = control(fresh, "compiled-search", "on")
    assert (
        result.returncode == 1
        and "holds no compiled search step that passed its check" in result.stderr
    )
    assert "the join command with --refresh" in result.stderr
    assert not overlaid(fresh) and "LEAN_SERVER_COMPILED_SEARCH" not in settings(fresh)
    assert fresh.worker_program(LEAN) == PLAIN and fresh.recreated() == had
    # The installer's next run makes one, now that nothing says off.
    installed(fresh)
    assert fresh.worker_program(LEAN) == LAUNCHER


def test_the_switch_goes_through_the_door(machine):
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo compiled-search off")
    assert result.returncode == 0, result.stderr
    assert machine.worker_program(LEAN) == PLAIN
    result = machine.bash(machine.path(CTL), SSH_ORIGINAL_COMMAND="@demo compiled-search on")
    assert result.returncode == 0, result.stderr
    assert machine.worker_program(LEAN) == LAUNCHER


@pytest.mark.parametrize(
    "words",
    [
        ("@demo", "compiled-search"),
        ("@demo", "compiled-search", "maybe"),
        ("@demo", "compiled-search", "ON"),
        ("@demo", "compiled-search", "on", "now"),
        ("compiled-search", "on"),
        ("compiled-search", "off"),
    ],
)
def test_the_switch_refuses_anything_but_on_or_off_of_a_named_instance(machine, words):
    had = (machine.recreated(), settings(machine))
    result = machine.bash(machine.path(CTL), *words)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refused" in result.stderr and "compiled-search on|off" in result.stderr
    assert (machine.recreated(), settings(machine)) == had


# --- status: what the RUNNING container was started with ----------------------------------------------------------


def test_status_says_when_the_settings_and_the_running_container_disagree(machine):
    assert control(machine, "compiled-search", "off").returncode == 0
    env = machine.path(f"{INSTANCE}/kimina.env")
    files = settings(machine)["COMPOSE_FILE"]
    env.write_text(
        env.read_text()
        .replace(f"COMPOSE_FILE={files}\n", f"COMPOSE_FILE={files}:{OVERLAY}\n")
        .replace("LEAN_SERVER_COMPILED_SEARCH=off\n", "")
    )
    assert status_line(machine) == (
        f"{START} on in the settings, NOT in effect (the running container was made before it: "
        "sudo kimina-ctl @demo restart)"
    )
    assert control(machine, "restart").returncode == 0
    assert status_line(machine) == f"{START} on ({MEASURED})"


def test_status_says_when_the_launcher_has_nothing_to_load(machine):
    machine.path(f"{NATIVE}/built-from").unlink()
    assert status_line(machine).startswith(
        f"{START} OFF in effect (the workers are started through the launcher, but this box holds no library"
    )


def test_status_of_a_server_from_before_the_step(machine):
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(env.read_text().replace(f":{OVERLAY}\n", "\n"))
    machine.path(f"{NATIVE}/state").unlink()
    assert control(machine, "restart").returncode == 0
    assert machine.worker_program(LEAN) == PLAIN  # the image's own
    assert (
        status_line(machine)
        == f"{START} OFF (this server was installed before it had one: the installer, or a refresh, makes and checks it)"
    )


# --- instance 1, and the pool's own host --------------------------------------------------------------------------


def test_instance_one_gets_no_step_and_the_program_it_always_ran(fresh):
    result = fresh.bash("install.sh", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        "COMPILED SEARCH STEP" not in result.stdout + result.stderr and fresh.native_builds() == []
    )
    assert not fresh.path("/srv/kimina/native").exists()
    assert settings(fresh, "/srv/kimina")["COMPOSE_FILE"] == "compose.yaml"
    assert fresh.worker_program("lean-server-kimina-1") == PLAIN
    status = fresh.bash(fresh.path(CTL), "status")
    assert status.returncode == 0 and START not in status.stdout


def test_the_pools_own_host_keeps_what_its_instance_made_and_says_so_last(machine):
    had = machine.native_builds()
    result = machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    assert (
        "  search      COMPILED SEARCH STEP: on - already made from this image and checked"
        in result.stdout
    )
    assert machine.native_builds() == had and machine.worker_program(LEAN) == LAUNCHER
    # Behind its TLS front now, with the step's overlay last.
    assert settings(machine)["COMPOSE_FILE"] == (
        f"compose.yaml:compose.agent.yaml:compose.tls.yaml:{OVERLAY}"
    )
