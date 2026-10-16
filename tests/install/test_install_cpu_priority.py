"""The Lean server's control group, the lowest CPU priority, and what the usage agent is given to state
open slots.

The REAL installer and the REAL `kimina-ctl` run here on the fake machine of `stand_ins.py` (read
its docstring first). What a real box adds, and no test here shows: that systemd gives `leanpool.slice` the
weight its unit names, that Docker starts the container under it, and that the kernel then gives the box's
other work a contended CPU first.
"""

import fcntl
import os
import shutil
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
AGENT = "lean-server-demo-agent-1"
IMAGE = "acme/kimina:lean-demo-1"
UNIT = "/etc/systemd/system/leanpool.slice"
INSTALL = ("install.sh", "--instance", "demo", "--no-door")
GROUP_TEST = (
    f"docker run --rm --cgroup-parent leanpool-demo.slice --init --cap-drop ALL "
    f"--security-opt no-new-privileges:true --network none --entrypoint true {IMAGE}"
)


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


def installed(
    machine: Machine, *options: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    result = machine.bash(*INSTALL, *options, **environment)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def settings(machine: Machine) -> dict[str, str]:
    lines = machine.path(f"{INSTANCE}/kimina.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def status(machine: Machine, **environment: str) -> str:
    result = machine.bash(
        machine.path("/usr/local/bin/kimina-ctl"), "@demo", "status", **environment
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def line_of(output: str, start: str) -> str:
    (line,) = [line for line in output.splitlines() if line.startswith(start)]
    return line


# --- a box that can -----------------------------------------------------------------------------------------------


def test_the_lean_server_is_started_in_its_own_control_group_at_the_lowest_cpu_weight(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    result = installed(machine)
    # The top-level group's unit: the lowest CPU weight there is, root's, for every Lean server of the box.
    unit = machine.path(UNIT).read_text()
    assert "\n[Slice]\nCPUWeight=1\n" in unit
    assert machine.owner(UNIT) == ("root", "root", 0o644)
    # The box was asked first, and in this order: how Docker keeps control groups, the unit loaded and the
    # group made, a container that does nothing started in the server's own group. Only then the server.
    calls = machine.calls
    asked = calls.index("docker info -f {{.CgroupDriver}} {{.CgroupVersion}}")
    loaded, made = (
        calls.index("systemctl daemon-reload"),
        calls.index("systemctl start leanpool.slice"),
    )
    tried = calls.index(GROUP_TEST)
    started = next(
        index
        for index, call in enumerate(calls)
        if "--project-name lean-server-demo " in call and call.endswith(" up -d")
    )
    assert asked < loaded < made < tried < started
    # The settings the compose files read, and the container as it was made.
    said = settings(machine)
    assert said["LEAN_SERVER_CPU_PRIORITY"] == "lowest"
    assert said["LEAN_SERVER_CGROUP_PARENT"] == "leanpool-demo.slice"
    assert (
        said["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"]
        == "/host-cgroup/leanpool.slice/leanpool-demo.slice/cpu.stat"
    )
    assert machine.control_group(LEAN) == "leanpool-demo.slice"
    assert (
        "CPU priority: lowest. The Lean server runs in the control group leanpool-demo.slice"
        in result.stdout
    )


def test_the_agent_is_given_the_worker_count_that_group_and_the_boxs_control_groups_read_only(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    # The agent's own overlay, which the instance runs.
    assert "compose.agent.yaml" in settings(machine)["COMPOSE_FILE"].split(":")
    compose = machine.path(f"{INSTANCE}/compose.agent.yaml").read_text()
    # From the settings, so a later `set-repls` reaches the agent; an empty value is "not set" to the agent.
    assert '      LEANPOOL_AGENT_WORKERS: "${LEAN_SERVER_MAX_REPLS:-}"\n' in compose
    assert (
        '      LEANPOOL_AGENT_LEAN_CPU_STAT_PATH: "${LEANPOOL_AGENT_LEAN_CPU_STAT_PATH:-}"\n'
        in compose
    )
    assert '      LEANPOOL_AGENT_CPU_RESERVE: "${LEANPOOL_AGENT_CPU_RESERVE:-}"\n' in compose
    assert (
        '      LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK: "${LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK:-}"\n'
        in compose
    )
    # One host mount, read-only, and where the settings' path points into.
    assert "    volumes:\n" in compose and "      - /sys/fs/cgroup:/host-cgroup:ro\n" in compose
    assert settings(machine)["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"].startswith("/host-cgroup/")
    assert settings(machine)["LEAN_SERVER_MAX_REPLS"] == "8"
    # The agent of a pool member (behind a TLS front) is given exactly the same: the TLS overlay takes away
    # its published port and nothing else.
    tls = machine.path(f"{INSTANCE}/compose.tls.yaml").read_text()
    agent = tls[tls.index("\n  agent:\n") : tls.index("\n  front:\n")]
    assert agent.split() == ["agent:", "ports:", "!reset", "[]"]


def test_status_says_the_cpu_priority_and_what_the_agent_says(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    output = status(machine)
    assert line_of(output, "--- cpu priority:").startswith(
        "--- cpu priority: lowest (control group leanpool-demo.slice under leanpool.slice, CPU weight 1"
    )
    assert line_of(output, "--- usage agent says:").startswith(
        "--- usage agent says: ready up 97% maxconn:8 (maxconn = the open slots"
    )
    assert "of 8 workers)" in line_of(output, "--- usage agent says:")
    # Asked inside the agent's container: behind a TLS front its port is not published.
    assert any(call.startswith(f"docker exec -i {AGENT} python -") for call in machine.calls)
    drained = status(machine, STUB_AGENT_SAYS="drain 0%")
    assert "no check fits beside this box's other work, or memory is below the floor" in line_of(
        drained, "--- usage agent says:"
    )
    silent = status(machine, STUB_AGENT_SAYS="")
    assert line_of(silent, "--- usage agent says:") == "--- usage agent says: ? (it did not answer)"


def test_a_second_run_changes_nothing_that_runs_and_writes_no_unit_again(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    had = (machine.recreated(), settings(machine), machine.path(UNIT).read_bytes())
    unit_written = machine.path(UNIT).stat().st_mtime_ns
    installed(machine)
    assert (machine.recreated(), settings(machine), machine.path(UNIT).read_bytes()) == had
    assert (
        machine.path(UNIT).stat().st_mtime_ns == unit_written
    )  # the same unit is not written again
    assert machine.control_group(LEAN) == "leanpool-demo.slice"


def test_setting_the_workers_tells_the_agent_which_states_no_more_slots_than_that(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    had = machine.recreated()
    result = machine.bash(machine.path("/usr/local/bin/kimina-ctl"), "@demo", "set-repls", "5")
    assert result.returncode == 0, result.stderr
    assert settings(machine)["LEAN_SERVER_MAX_REPLS"] == "5"
    # The Lean server with its new worker count, then its agent with the same number as the most it states.
    assert machine.recreated() == [*had, LEAN, AGENT]
    assert "of 5 workers)" in line_of(status(machine), "--- usage agent says:")


# --- the box's own limit beside other work ------------------------------------------------------------------------

CTL = "/usr/local/bin/kimina-ctl"


def test_a_box_has_a_limit_of_one_check_beside_other_work_until_it_is_measured(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == "1"
    compose = machine.path(f"{INSTANCE}/compose.agent.yaml").read_text()
    assert (
        '      LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK: "${LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK:-}"\n'
        in compose
    )
    assert line_of(status(machine), "--- beside other work:").startswith(
        "--- beside other work: at most 1 check(s) (while anything else keeps a CPU busy here"
    )


def test_set_beside_changes_the_boxs_limit_and_makes_only_the_agent_again(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    had = machine.recreated()
    result = machine.bash(machine.path(CTL), "@demo", "set-beside", "3")
    assert result.returncode == 0, result.stderr
    assert "checks beside other work: 3" in result.stdout
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == "3"
    assert machine.recreated() == [
        *had,
        AGENT,
    ]  # the Lean server and the checks it holds are not touched
    assert "at most 3 check(s)" in line_of(status(machine), "--- beside other work:")
    # `none`: no limit of its own, kept as an EMPTY setting, which a later run of the installer leaves alone.
    result = machine.bash(machine.path(CTL), "@demo", "set-beside", "none")
    assert result.returncode == 0, result.stderr
    assert "checks beside other work: no limit of its own" in result.stdout
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == ""
    assert "no limit of its own (the free cores alone decide" in line_of(
        status(machine), "--- beside other work:"
    )
    installed(machine)
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == ""
    # 0: nothing beside other work. Also kept.
    assert machine.bash(machine.path(CTL), "@demo", "set-beside", "0").returncode == 0
    assert "no check (while anything else keeps a CPU busy here" in line_of(
        status(machine), "--- beside other work:"
    )
    installed(machine)
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == "0"


@pytest.mark.parametrize(
    "words",
    [
        ("set-beside",),
        ("set-beside", "33"),
        ("set-beside", "-1"),
        ("set-beside", "1.5"),
        ("set-beside", "01"),
        ("set-beside", "all"),
        ("set-beside", "1", "2"),
    ],
)
def test_set_beside_refuses_anything_but_a_number_up_to_32_or_none(tmp_path, deploy, words):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    had = (machine.recreated(), settings(machine))
    result = machine.bash(machine.path(CTL), "@demo", *words)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refused" in result.stderr
    assert (machine.recreated(), settings(machine)) == had


def test_a_box_installed_before_the_limit_says_so_and_gets_one_from_the_installer(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    env = machine.path(f"{INSTANCE}/kimina.env")
    kept = [
        line
        for line in env.read_text().splitlines(keepends=True)
        if not line.startswith("LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK=")
    ]
    env.write_text("".join(kept))
    assert "this box was installed before it had one" in line_of(
        status(machine), "--- beside other work:"
    )
    installed(machine)
    assert settings(machine)["LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK"] == "1"


# --- a box that cannot --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "reason", "tried"),
    [
        (
            {"STUB_CGROUPS": "cgroupfs 2"},
            "keeps its containers' control groups as 'cgroupfs 2'",
            False,
        ),
        (
            {"STUB_CGROUPS": "systemd 1"},
            "keeps its containers' control groups as 'systemd 1'",
            False,
        ),
        (
            {"STUB_NO_CGROUP_PARENT": "1"},
            "Docker did not start a container in the control group leanpool-demo.slice",
            True,
        ),
        ({"STUB_SLICE_WITHOUT_WEIGHT": "1"}, "does not carry the lowest CPU weight", True),
    ],
    ids=["dockers-own-groups", "version-1", "docker-refuses-the-group", "no-weight"],
)
def test_a_box_that_cannot_keep_the_group_gets_everything_else_and_is_told(
    tmp_path, deploy, fault, reason, tried
):
    machine = Machine(tmp_path / "host", deploy)
    result = installed(machine, **fault)  # the install ends verified all the same
    warning = line_of(result.stderr, "WARNING: CPU priority 'lowest' is NOT available on this box")
    assert reason in warning and "Everything else is installed" in warning
    assert "its usage agent states no open slots" in warning
    # Empty settings: the container starts where Docker puts every container, and the agent is told nothing.
    said = settings(machine)
    assert (
        said["LEAN_SERVER_CPU_PRIORITY"],
        said["LEAN_SERVER_CGROUP_PARENT"],
        said["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"],
    ) == ("lowest", "", "")
    assert machine.control_group(LEAN) == ""
    assert machine.running(LEAN) and machine.running(AGENT)
    assert (GROUP_TEST in machine.calls) is tried
    if not tried:  # a box that was not even a candidate: nothing written
        assert (
            not machine.path(UNIT).exists()
            and "systemctl start leanpool.slice" not in machine.calls
        )
    output = status(machine, STUB_AGENT_SAYS="ready up 97%", **fault)
    assert line_of(output, "--- cpu priority:").startswith(
        "--- cpu priority: lowest NOT available here"
    )
    assert "open slots are NOT stated" in line_of(output, "--- usage agent says:")


def test_a_host_without_systemd_installs_as_it_always_did_and_calls_no_systemd(tmp_path, deploy):
    """Docker inside WSL often has no systemd. Such a host installs with `--no-firewall`, and nothing of it
    asks systemd for anything."""
    machine = Machine(tmp_path / "host", deploy)
    shutil.rmtree(machine.path("/run/systemd"))
    result = installed(machine, "--no-firewall")
    warning = line_of(result.stderr, "WARNING: CPU priority 'lowest' is NOT available on this box")
    assert "this box does not run systemd" in warning
    assert not any(call.startswith("systemctl") for call in machine.calls)
    assert GROUP_TEST not in machine.calls and not machine.path(UNIT).exists()
    assert (
        machine.control_group(LEAN) == ""
        and settings(machine)["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"] == ""
    )


def test_a_box_that_could_and_no_longer_can_goes_back_to_where_it_ran(tmp_path, deploy):
    """The settings are decided again at every run: a group Docker no longer starts a container in is not
    left in them, where it would keep the server from starting."""
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    assert machine.control_group(LEAN) == "leanpool-demo.slice"
    installed(machine, STUB_NO_CGROUP_PARENT="1")
    assert (
        machine.control_group(LEAN) == "" and settings(machine)["LEAN_SERVER_CGROUP_PARENT"] == ""
    )
    assert machine.running(LEAN)


# --- the ordinary priority ----------------------------------------------------------------------------------------


def test_cpu_priority_normal_is_a_group_of_ordinary_weight_and_is_kept_by_a_later_run(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    result = installed(machine, "--cpu-priority", "normal")
    said = settings(machine)
    assert said["LEAN_SERVER_CPU_PRIORITY"] == "normal"
    assert said["LEAN_SERVER_CGROUP_PARENT"] == "leanpoolnormal-demo.slice"
    # The agent still reads the server's own use, so it still states open slots.
    assert (
        said["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"]
        == "/host-cgroup/leanpoolnormal.slice/leanpoolnormal-demo.slice/cpu.stat"
    )
    assert machine.control_group(LEAN) == "leanpoolnormal-demo.slice"
    assert not machine.path(UNIT).exists()  # no weight is asked for: no unit
    assert "CPU priority: normal." in result.stdout
    assert line_of(status(machine), "--- cpu priority:").startswith(
        "--- cpu priority: normal (control group leanpoolnormal-demo.slice"
    )
    # A later run that says nothing (a refresh is one) keeps the choice, and changes nothing that runs.
    had = machine.recreated()
    installed(machine)
    assert settings(machine)["LEAN_SERVER_CPU_PRIORITY"] == "normal" and machine.recreated() == had
    assert machine.control_group(LEAN) == "leanpoolnormal-demo.slice"
    # Asked for, the lowest: the server is made again once, in the other group.
    installed(machine, "--cpu-priority", "lowest")
    assert machine.control_group(LEAN) == "leanpool-demo.slice" and machine.recreated()[
        len(had) :
    ] == [LEAN, AGENT]
    assert "CPUWeight=1" in machine.path(UNIT).read_text()


def test_another_word_for_the_priority_is_refused_before_anything_is_touched(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(*INSTALL, "--cpu-priority", "idle")
    assert result.returncode == 1 and "--cpu-priority is lowest or normal" in result.stderr
    assert machine.calls == [] and not machine.path(INSTANCE).exists()


# --- a box busy with other work -----------------------------------------------------------------------------------
# At the lowest weight, a Lean server just made on a busy box gets almost no CPU, and its first check runs past
# its time limit (STUB_BOX_BUSY). An installer runs its own checks at the box's ordinary weight and puts the
# lowest back after, on every way out (pool-lib.sh, lift_cpu_priority).

WEIGHT = "/sys/fs/cgroup/leanpool.slice/cpu.weight"
LIFTED = "systemctl set-property --runtime leanpool.slice CPUWeight=100"
LOWEST = "systemctl set-property --runtime leanpool.slice CPUWeight=1"
LOCK = "/run/lean-pool/cpu-priority.lock"
PROTECTIONS_OFF = ("READ-ONLY ROOT: OFF", "WORKER SANDBOX: OFF")


def weight(machine: Machine) -> str:
    return machine.path(WEIGHT).read_text().strip()


def starved_checks(machine: Machine) -> list[str]:
    said = machine.stub / "starved-checks"
    return said.read_text().splitlines() if said.exists() else []


def keeps_its_protections(machine: Machine, result: subprocess.CompletedProcess[str]) -> bool:
    said = settings(machine)
    return (
        said["LEAN_SERVER_ROOT_READ_ONLY"] == "true"
        and "compose.worker-sandbox.yaml" in said["COMPOSE_FILE"].split(":")
        and not any(off in result.stdout + result.stderr for off in PROTECTIONS_OFF)
    )


def lifted_as_a_killed_installer_left_it(machine: Machine) -> None:
    """What an installer killed outright while its checks ran leaves: the group at the ordinary weight (systemd
    keeps a runtime property under /run until it is set again) and the lock file, which nobody holds."""
    runtime = machine.path("/run/systemd/system.control/leanpool.slice.d/50-CPUWeight.conf")
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("[Slice]\nCPUWeight=100\n")
    machine.path(WEIGHT).write_text("100\n")
    machine.path(LOCK).parent.mkdir(parents=True, exist_ok=True)
    machine.path(LOCK).touch()


def test_a_busy_box_installs_and_verifies_its_lean_server_with_its_protections_and_the_lowest_weight_after(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    result = installed(machine, STUB_BOX_BUSY="1")
    # Its checks ran at the box's ordinary weight, from before the server was made until they were done.
    calls = machine.calls
    lifted, lowest = calls.index(LIFTED), calls.index(LOWEST)
    started = next(
        index
        for index, call in enumerate(calls)
        if "--project-name lean-server-demo " in call and call.endswith(" up -d")
    )
    checked = [
        index for index, call in enumerate(calls) if call == f"docker exec -i {LEAN} python -"
    ]
    assert checked and lifted < started < checked[0] and checked[-1] < lowest
    assert starved_checks(machine) == []
    # With both protections, which a starved check would have taken away, and the lowest weight again.
    assert keeps_its_protections(machine, result)
    assert weight(machine) == "1"
    assert line_of(status(machine), "--- cpu priority:").startswith("--- cpu priority: lowest (")


def test_an_install_that_stops_puts_the_lowest_weight_back(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(*INSTALL, STUB_UNHEALTHY=LEAN)
    assert result.returncode == 1 and "did not become healthy in 5 minutes" in result.stderr
    calls = machine.calls
    assert calls.index(LIFTED) < calls.index(LOWEST) and weight(machine) == "1"


def test_a_lift_that_a_killed_installer_left_is_said_and_the_next_install_puts_the_lowest_back(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    lifted_as_a_killed_installer_left_it(machine)
    assert "lowest NOT available here" in line_of(status(machine), "--- cpu priority:")
    result = installed(machine)
    assert "is NOT available on this box" not in result.stderr
    assert (
        weight(machine) == "1"
        and settings(machine)["LEAN_SERVER_CGROUP_PARENT"] == "leanpool-demo.slice"
    )
    assert line_of(status(machine), "--- cpu priority:").startswith("--- cpu priority: lowest (")


def test_an_install_beside_one_that_is_checking_leaves_the_group_lifted_until_that_one_ends(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    lifted_as_a_killed_installer_left_it(machine)
    with open(machine.path(LOCK)) as lock:  # another installer here, still checking its Lean server
        fcntl.flock(lock, fcntl.LOCK_SH)
        assert line_of(status(machine), "--- cpu priority:").startswith(
            "--- cpu priority: lowest, at the box's ordinary weight (100) for now"
        )
        mark = machine.mark()
        result = installed(machine, STUB_BOX_BUSY="1")
        # The other's lift is not a group without a weight: the lowest stays this server's priority.
        assert "is NOT available on this box" not in result.stderr
        assert settings(machine)["LEAN_SERVER_CGROUP_PARENT"] == "leanpool-demo.slice"
        assert keeps_its_protections(machine, result) and starved_checks(machine) == []
        # Its checks ran lifted, and it leaves the group lifted for the other one, which puts the lowest back.
        assert LIFTED in machine.calls_since(mark) and LOWEST not in machine.calls_since(mark)
        assert weight(machine) == "100"


def test_a_check_sent_by_hand_on_a_busy_box_is_starved_at_the_lowest_weight(tmp_path, deploy):
    """What the tests above stand on: the fake machine starves a check of a server at the lowest weight, through
    the same `kimina-ctl check` the installers send. A check sent by hand is not lifted."""
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    result = machine.bash(
        machine.path("/usr/local/bin/kimina-ctl"), "@demo", "check", STUB_BOX_BUSY="1"
    )
    assert result.returncode == 1
    assert "Lean REPL header command timed out in 300.0 seconds" in result.stdout
    assert starved_checks(machine) == [LEAN] and weight(machine) == "1"


def as_before_cpu_priority(machine: Machine) -> None:
    """The box as an installer older than the control group left it: no unit, no group, none in the settings."""
    machine.path(UNIT).unlink()
    for group in ("/run/systemd/system.control/leanpool.slice.d", "/sys/fs/cgroup/leanpool.slice"):
        shutil.rmtree(machine.path(group))
    env = machine.path(f"{INSTANCE}/kimina.env")
    kept = [
        line
        for line in env.read_text().splitlines()
        if not line.startswith(
            (
                "LEAN_SERVER_CPU_PRIORITY=",
                "LEAN_SERVER_CGROUP_PARENT=",
                "LEANPOOL_AGENT_LEAN_CPU_STAT_PATH=",
            )
        )
    ]
    env.write_text("".join(line + "\n" for line in kept))


@pytest.mark.parametrize(
    "older", [False, True], ids=["after-its-instance", "unit-written-by-the-pool"]
)
def test_a_busy_pool_host_installs_and_verifies_its_pool_and_its_own_lean_server(
    tmp_path, deploy, older
):
    """Also where the pool's own install is the first to give its server the control group (an instance an
    older installer set up): the unit is written inside the TLS front's install, and the lift comes after it."""
    machine = Machine(tmp_path / "host", deploy)
    installed(machine)
    if older:
        as_before_cpu_priority(machine)
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_BOX_BUSY="1"
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    assert starved_checks(machine) == []
    assert keeps_its_protections(machine, result)
    assert machine.control_group(LEAN) == "leanpool-demo.slice" and weight(machine) == "1"


# --- instance 1, and leaving --------------------------------------------------------------------------------------


def test_instance_one_gets_a_group_of_its_own_under_the_same_top_level_group(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash("install.sh", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert machine.control_group("lean-server-kimina-1") == "leanpool-main.slice"
    installed(machine)
    assert machine.control_group(LEAN) == "leanpool-demo.slice"
    assert machine.path(UNIT).read_text().count("CPUWeight=1") == 1


def test_the_unit_goes_with_the_boxs_last_lean_server(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    assert machine.bash("install.sh", "--no-door").returncode == 0
    installed(machine)
    assert (
        machine.bash("install.sh", "--instance", "demo", "--uninstall", "--purge").returncode == 0
    )
    assert machine.path(UNIT).exists()  # instance 1 still runs under it
    # Like the door: it goes when no other instance is left on the box (a kept settings file is one), and with
    # it the weight the installers' checks set at runtime.
    runtime = machine.path("/run/systemd/system.control/leanpool.slice.d")
    assert runtime.exists()
    assert machine.bash("install.sh", "--uninstall").returncode == 0
    assert not machine.path(UNIT).exists() and not runtime.exists()
