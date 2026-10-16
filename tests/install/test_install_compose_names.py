"""A box an older installer set up, moved to deploy/'s compose files and Compose's names by today's.

The older installers ran each instance as up to three compose projects of their own files (the server, its usage
agent, its TLS front) and the pool as one, all named after the site's prefix, with settings under names of
their own. Today's run each as ONE project of the lean-pool project's files (deploy/lean-server, deploy/pool),
under Compose's own names, with each setting under the name of what reads it. The first run of today's
installer on such a box moves the whole box before it changes anything else (`move_to_compose_names`,
pool-lib.sh).

The older box is made here by the older installers themselves: this repository at OLDER, the last commit before
the move, run on today's fake machine (stand_ins.py, which still plays their single compose files and
their build of the compiled search step). A checkout without that history cannot make it; CI fetches all of it.
"""

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from stand_ins import (
    ADMIT_CASES,
    ROOT,
    Machine,
    copy_rewritten,
    rewritten_installers,
    snapshot,
    tools_run_from_source,
)

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
    pytest.mark.skipif(
        not (ROOT / ".git").exists(), reason="not a git checkout: no older installer"
    ),
]

OLDER = "8740fd4"  # the installers as they were before the move
INSTANCE_ONE = "/srv/kimina"
INSTANCE = "/srv/kimina-demo"
POOL = "/srv/lean-pool-demo"
MOVED = ".before-compose-names"
# The older names on the test site (prefix `acme`), and today's.
OLD_CONTAINERS = [
    "acme-kimina",
    "acme-kimina-demo",
    "acme-agent-demo",
    "acme-front-demo",
    "acme-proxy-demo",
    "acme-cache-demo",
]
NEW_CONTAINERS = [
    "lean-server-kimina-1",
    "lean-server-demo-kimina-1",
    "lean-server-demo-agent-1",
    "lean-server-demo-front-1",
    "lean-pool-demo-haproxy-1",
    "lean-pool-demo-cache-1",
]
# What the move renames (pool-lib.sh's MOVED_INSTANCE_SETTINGS and MOVED_POOL_SETTINGS), written out again.
INSTANCE_NAMES = {
    "KIMINA_API_KEY": "LEAN_SERVER_API_KEY",
    "KIMINA_MAX_REPLS": "LEAN_SERVER_MAX_REPLS",
    "KIMINA_MAX_REPL_USES": "LEAN_SERVER_MAX_REPL_USES",
    "KIMINA_MAX_REPL_MEM": "LEAN_SERVER_MAX_REPL_MEM",
    "KIMINA_MAX_WAIT": "LEAN_SERVER_MAX_WAIT",
    "KIMINA_LOG_LEVEL": "LEAN_SERVER_LOG_LEVEL",
    "KIMINA_PORT": "LEAN_SERVER_PUBLISHED_PORT",
    "KIMINA_AGENT_PORT": "LEAN_SERVER_AGENT_PORT",
    "KIMINA_SUBNET": "LEAN_SERVER_SUBNET",
    "KIMINA_CPUS": "LEAN_SERVER_CPUS",
    "KIMINA_MEM_LIMIT": "LEAN_SERVER_MEM_LIMIT",
    "KIMINA_CGROUP_PARENT": "LEAN_SERVER_CGROUP_PARENT",
    "KIMINA_CPU_PRIORITY": "LEAN_SERVER_CPU_PRIORITY",
    "KIMINA_ROOT_READ_ONLY": "LEAN_SERVER_ROOT_READ_ONLY",
    "KIMINA_READ_ONLY": "LEAN_SERVER_READ_ONLY",
    "KIMINA_COMPILED_SEARCH": "LEAN_SERVER_COMPILED_SEARCH",
    "KIMINA_SERVER_NAME": "LEAN_SERVER_NAME",
    "KIMINA_AGENT_IMAGE": "LEAN_SERVER_AGENT_IMAGE",
    "KIMINA_FRONT_IMAGE": "LEAN_SERVER_FRONT_IMAGE",
    "KIMINA_FRONT_ADDRESS": "LEAN_SERVER_FRONT_ADDRESS",
    "KIMINA_AGENT_BESIDE_OTHER_WORK": "LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK",
    "KIMINA_AGENT_LEAN_CPU_STAT": "LEANPOOL_AGENT_LEAN_CPU_STAT_PATH",
    "KIMINA_AGENT_CPU_RESERVE": "LEANPOOL_AGENT_CPU_RESERVE",
    "KIMINA_FRONT_LEAN_TIMEOUT": "LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS",
    "KIMINA_FRONT_SERVER_WAIT": "LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS",
    "KIMINA_INSTALLER_VERSION": "LEANPOOL_AGENT_VERSION",
}
INSTANCE_DROPPED = {
    "KIMINA_REPL_PATH",
    "KIMINA_CONTAINER",
    "KIMINA_PROJECT",
    "KIMINA_NETWORK",
    "KIMINA_AGENT_CONTAINER",
    "KIMINA_FRONT_CONTAINER",
}
POOL_NAMES = {
    "POOL_PIN": "LEANPOOL_CACHE_PIN",
    "POOL_CACHE_MAX_BYTES": "LEANPOOL_CACHE_MAX_BYTES",
    "POOL_CACHE_UPSTREAM_TIMEOUT": "LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS",
    "POOL_MAX_REQUEST_BYTES": "LEANPOOL_CACHE_MAX_REQUEST_BYTES",
    "POOL_PUBLIC_PORT": "LEANPOOL_HAPROXY_PUBLIC_PORT",
    "POOL_LEAN_TIMEOUT": "LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS",
    "POOL_SERVER_WAIT": "LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS",
    "POOL_JOIN_PORT": "LEANPOOL_JOIN_PUBLIC_PORT",
    "POOL_JOIN_HOST": "LEANPOOL_JOIN_PUBLIC_HOST",
    "POOL_IMAGE": "LEANPOOL_IMAGE",
    "POOL_HAPROXY_IMAGE": "LEANPOOL_HAPROXY_IMAGE",
    "POOL_IMAGE_GID": "LEANPOOL_IMAGE_GID",
    "POOL_SUBNET": "LEANPOOL_SUBNET",
    "POOL_DNS": "LEANPOOL_DNS",
    "POOL_FRONT_NAMES": "LEANPOOL_FRONT_NAMES",
    "POOL_INSTALLER_VERSION": "LEANPOOL_HAPROXY_INSTALLER_VERSION",
}
# The installer's version, which the move keeps under its new name and the install then writes anew: today's.
VERSIONS = {"LEANPOOL_AGENT_VERSION", "LEANPOOL_HAPROXY_INSTALLER_VERSION"}
# A line no installer wrote (a site's own), and two settings an older box may lack: the move keeps the first
# and writes out the default the older compose files used for the other two.
SITE_OWN = "SITE_OWN_SETTING=kept as it is"
CACHED = "a cached answer"


def older_installers(directory: Path) -> Path:
    """This repository at OLDER, its installers rewritten for the fake machine as today's are, with the site the
    tests used then. Returns the installers' directory."""
    archive = directory / "older.tar"
    made = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "-o", str(archive), OLDER],
        capture_output=True,
        text=True,
    )
    assert made.returncode == 0, f"{made.stderr}(a shallow checkout lacks {OLDER}: fetch all of it)"
    tree = directory / "tree"
    with tarfile.open(archive) as handle:
        handle.extractall(tree, filter="tar")
    project = directory / "project"
    shutil.copytree(tree, project, ignore=shutil.ignore_patterns("tests", "docs", ".github"))
    shutil.rmtree(project / "deploy" / "install")
    installers = project / "deploy" / "install"
    copy_rewritten(tree / "deploy" / "install", installers)
    shutil.copytree(tree / "tests" / "install" / "site", installers / "site")
    return installers


def run(machine: Machine, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
    result = machine.bash("install.sh", *arguments, **environment)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return result


def settings(machine: Machine, path: str) -> dict[str, str]:
    lines = machine.path(path).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def state(machine: Machine) -> Path:
    return machine.stub / "state"


def volume(machine: Machine, name: str) -> Path:
    return state(machine) / "volumes" / name


def compose_files_named(call: str) -> list[str]:
    return re.findall(r"(?:^| )-f (\S+)", call)


@pytest.fixture(scope="module")
def today(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Today's installers."""
    return rewritten_installers(tmp_path_factory.mktemp("today"))


@pytest.fixture(scope="module")
def older(tmp_path_factory: pytest.TempPathFactory) -> Machine:
    """A pool host as the older installers left it: instance 1, `demo` behind its TLS front with the compiled
    search step on, and the pool; one settings line of the site's own, two settings left to the older files'
    defaults, and an answer in the cache's volume."""
    directory = tmp_path_factory.mktemp("older")
    machine = Machine(directory / "host", older_installers(directory))
    run(machine, "--no-door")
    run(machine, "--instance", "demo", "--no-door")
    run(machine, "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    env = machine.path(f"{INSTANCE}/kimina.env")
    env.write_text(
        "".join(
            line + "\n"
            for line in env.read_text().splitlines()
            if not line.startswith(("KIMINA_MAX_REPL_MEM=", "KIMINA_LOG_LEVEL="))
        )
        + SITE_OWN
        + "\n"
    )
    pool_env = machine.path(f"{POOL}/pool.env")
    pool_env.write_text(
        "".join(
            line + "\n"
            for line in pool_env.read_text().splitlines()
            if not line.startswith("POOL_MAX_REQUEST_BYTES=")
        )
    )
    (volume(machine, "acme-cache-demo") / "answers").mkdir()
    (volume(machine, "acme-cache-demo") / "answers" / "one").write_text(CACHED)
    return machine


def moved_copy(older: Machine, today: Path, directory: Path) -> SimpleNamespace:
    """A copy of the older box, with what it held before, given today's installers."""
    machine = older.copy_to(directory / "host")
    before = SimpleNamespace(
        files={path: snapshot(machine.path(path)) for path in (INSTANCE_ONE, INSTANCE, POOL)},
        settings={
            path: settings(machine, path)
            for path in (f"{INSTANCE_ONE}/kimina.env", f"{INSTANCE}/kimina.env", f"{POOL}/pool.env")
        },
        recreated=len(machine.recreated()),
    )
    machine.deploy = today
    return SimpleNamespace(machine=machine, before=before, mark=machine.mark())


@pytest.fixture(scope="module")
def moved(older: Machine, today: Path, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """The pool host's install line, `install.sh --proxy demo`, on a copy of the older box."""
    box = moved_copy(older, today, tmp_path_factory.mktemp("moved"))
    box.result = box.machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    box.calls = box.machine.calls_since(box.mark)
    return box


def test_the_pool_hosts_install_moves_the_box_and_the_pool_is_installed_and_verified(
    moved: SimpleNamespace,
) -> None:
    assert moved.result.returncode == 0, moved.result.stdout[-3000:] + moved.result.stderr[-3000:]
    assert "MOVED TO COMPOSE'S NAMES" in moved.result.stdout
    assert "POOL INSTALLED AND VERIFIED" in moved.result.stdout


def test_every_value_is_kept_under_its_new_name_and_lines_the_move_does_not_know_stay(
    moved: SimpleNamespace,
) -> None:
    machine = moved.machine
    for path, renamed in (
        (f"{INSTANCE_ONE}/kimina.env", INSTANCE_NAMES),
        (f"{INSTANCE}/kimina.env", INSTANCE_NAMES),
        (f"{POOL}/pool.env", POOL_NAMES),
    ):
        old, new = moved.before.settings[path], settings(machine, path)
        for key, value in old.items():
            if renamed.get(key) in VERSIONS:
                assert (
                    re.fullmatch(r"[0-9a-f]{32}", new[renamed[key]]) and new[renamed[key]] != value
                )
            elif key in renamed:
                assert new[renamed[key]] == value, (path, key)
            elif key == "KIMINA_IMAGE_TAG":
                assert new["LEAN_SERVER_IMAGE"] == f"acme/kimina:{value}"
            elif key not in INSTANCE_DROPPED | {"POOL_NAME"}:
                assert new[key] == value, (path, key)  # a line the move does not know
        assert not [key for key in new if key.startswith(("KIMINA_", "POOL_"))], path
        # What it had is kept beside it, private.
        kept = machine.path(path + MOVED)
        assert settings(machine, path + MOVED) == old
        assert kept.stat().st_mode & 0o777 == 0o600
    assert settings(machine, f"{INSTANCE}/kimina.env")["SITE_OWN_SETTING"] == "kept as it is"


def test_what_the_older_files_used_by_default_is_written_out(moved: SimpleNamespace) -> None:
    """The older box ran with 8G and WARNING where it had no setting: so it does, though the instance's own
    values and the compose files' defaults are others; and the pool's 256 KB requests likewise."""
    demo = settings(moved.machine, f"{INSTANCE}/kimina.env")
    assert demo["LEAN_SERVER_MAX_REPL_MEM"] == "8G" and demo["LEAN_SERVER_LOG_LEVEL"] == "WARNING"
    assert (
        settings(moved.machine, f"{POOL}/pool.env")["LEANPOOL_CACHE_MAX_REQUEST_BYTES"] == "262144"
    )


def test_every_container_runs_under_its_new_name_and_no_older_project_is_left(
    moved: SimpleNamespace,
) -> None:
    machine = moved.machine
    for name in NEW_CONTAINERS:
        assert machine.running(name), name
    for name in OLD_CONTAINERS:
        assert not machine.running(name) and machine.container_id(name) == "", name
    assert not list(state(machine).glob("project.acme-*"))
    assert not list(state(machine).glob("network.acme-*"))
    # Each instance and the pool is one project, of the files its env file names.
    assert settings(machine, f"{INSTANCE}/kimina.env")["COMPOSE_FILE"] == (
        "compose.yaml:compose.agent.yaml:compose.tls.yaml:compose.compiled-search.yaml"
        ":compose.worker-sandbox.yaml"
    )
    assert settings(machine, f"{INSTANCE_ONE}/kimina.env")["COMPOSE_FILE"] == "compose.yaml"
    for name in ("kimina", "agent", "front"):
        label = state(machine) / f"label.lean-server-demo-{name}-1"
        assert label.read_text() == "lean-server-demo"


def test_the_caches_data_is_in_the_new_volume_and_the_older_volume_is_gone(
    moved: SimpleNamespace,
) -> None:
    machine = moved.machine
    assert (volume(machine, "lean-pool-demo_cache-data") / "answers" / "one").read_text() == CACHED
    assert not (state(machine) / "volume.acme-cache-demo").exists()
    # Copied while nothing of the pool ran, by the pool's own image, with no network.
    (copy,) = [call for call in moved.calls if "--entrypoint cp" in call]
    assert copy.startswith("docker run --rm --network none --user 0:0 -v acme-cache-demo:/from:ro ")
    assert moved.calls.index(copy) < min(
        index
        for index, call in enumerate(moved.calls)
        if "--project-name lean-pool-demo " in call and call.endswith(" up -d")
    )


def test_every_compose_call_of_a_new_project_runs_the_lean_pool_projects_own_files(
    moved: SimpleNamespace,
) -> None:
    projects = {
        "lean-server": ROOT / "deploy" / "lean-server",
        "lean-pool": ROOT / "deploy" / "pool",
    }
    seen = 0
    for call in moved.calls:
        project = re.search(r"^docker compose --project-name (\S+) ", call)
        if not project or not project.group(1).startswith(tuple(projects)):
            continue
        source = projects[
            "lean-pool" if project.group(1).startswith("lean-pool") else "lean-server"
        ]
        for path in compose_files_named(call):
            seen += 1
            assert Path(path).read_bytes() == (source / Path(path).name).read_bytes(), call
    assert seen


def test_each_lean_server_is_made_again_once_and_the_front_keeps_its_address(
    moved: SimpleNamespace,
) -> None:
    machine = moved.machine
    made = machine.recreated()[moved.before.recreated :]
    # Instance 1 once, by the move. The pool's own server twice: by the move, as it was (a move that fails its
    # check is taken back, so it adds nothing), then to start its workers in the sandbox, which the older
    # installers did not have.
    assert made.count("lean-server-kimina-1") == 1, made
    assert made.count("lean-server-demo-kimina-1") == 2, made
    demo = settings(machine, f"{INSTANCE}/kimina.env")
    assert (
        demo["LEAN_SERVER_FRONT_ADDRESS"]
        == moved.before.settings[f"{INSTANCE}/kimina.env"]["KIMINA_FRONT_ADDRESS"]
    )
    # The front's config was rendered for the services' names, and the older one is kept beside it.
    front = machine.path(f"{INSTANCE}/front/haproxy.cfg").read_text()
    assert "server lean kimina:8000" in front and "server agent agent:18200" in front
    assert (
        "acme-kimina-demo:8000" in machine.path(f"{INSTANCE}/front/haproxy.cfg{MOVED}").read_text()
    )


def test_the_older_compose_files_are_kept_beside_the_new_ones(moved: SimpleNamespace) -> None:
    machine = moved.machine
    for path in (
        f"{INSTANCE_ONE}/docker-compose.yml",
        f"{INSTANCE}/docker-compose.yml",
        f"{INSTANCE}/docker-compose.agent.yml",
        f"{INSTANCE}/docker-compose.front.yml",
        f"{POOL}/docker-compose.pool.yml",
    ):
        assert not machine.path(path).exists() and machine.path(path + MOVED).exists(), path


def test_a_second_run_has_nothing_to_move(moved: SimpleNamespace, tmp_path: Path) -> None:
    assert moved.result.returncode == 0
    machine = moved.machine.copy_to(tmp_path / "host")
    result = run(machine, "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert "Moving" not in result.stdout and "MOVED TO COMPOSE'S NAMES" not in result.stdout


def test_a_box_without_the_sites_names_file_moves_its_pool_too(
    older: Machine, today: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The pool's check runs today's lean-pool-ctl, which reads /etc/lean-pool/site.env: the move writes it
    first, for a box set up before the installers wrote it."""
    box = moved_copy(older, today, tmp_path_factory.mktemp("no-site-file"))
    site_file = box.machine.path("/etc/lean-pool/site.env")
    site_file.unlink()
    result = box.machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "MOVED TO COMPOSE'S NAMES" in result.stdout
    assert "SITE_PREFIX=acme\n" in site_file.read_text()


def test_a_pool_without_certificates_is_refused_before_anything_changes(
    older: Machine, today: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """deploy/pool always runs TLS, so a pool with no certificates (one set up before TLS) cannot be moved:
    the pre-flight refuses it, and no instance or pool of the box has moved."""
    box = moved_copy(older, today, tmp_path_factory.mktemp("no-tls"))
    shutil.rmtree(box.machine.path(f"{POOL}/tls"))
    result = box.machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode != 0
    assert "has no certificates" in result.stderr and "Nothing was changed" in result.stderr
    assert "Moving " not in result.stdout and len(box.machine.recreated()) == box.before.recreated
    for path in (INSTANCE_ONE, INSTANCE):
        assert snapshot(box.machine.path(path)) == box.before.files[path], path


def test_the_documented_way_back_brings_the_older_projects_back(
    moved: SimpleNamespace, older: Machine, tmp_path: Path
) -> None:
    """docs/deployment.md, "A box set up by an older installer": take each new project down, move every
    `*.before-compose-names` file back over the file of its name, and run the older installers. The cache
    starts empty: its results were moved into the new volume."""
    machine = moved.machine.copy_to(tmp_path / "host")
    kimina_ctl = machine.path("/usr/local/bin/kimina-ctl")
    for instance in ([], ["@demo"]):
        down = machine.bash(kimina_ctl, *instance, "down")
        assert down.returncode == 0, down.stdout + down.stderr
    run(machine, "--proxy", "demo", "--uninstall")
    for directory in (INSTANCE_ONE, INSTANCE, POOL):
        for kept in machine.path(directory).rglob(f"*{MOVED}"):
            kept.rename(kept.with_name(kept.name.removesuffix(MOVED)))
    machine.deploy = older.deploy
    run(machine, "--no-door")
    run(machine, "--instance", "demo", "--no-door")
    run(machine, "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    for name in OLD_CONTAINERS:
        assert machine.running(name), name
    for name in NEW_CONTAINERS:
        assert not machine.running(name), name
    assert not (volume(machine, "acme-cache-demo") / "answers").exists()


# --- an instance's own install moves the whole box ------------------------------------------------------------------


def test_an_instance_install_moves_the_whole_box_past_the_ports_and_the_network_its_older_names_hold(
    older: Machine, today: Path, tmp_path: Path
) -> None:
    """The ports and the subnet are taken, by the box's own older containers and network: the pre-flight
    knows them, and the box moves, its pool included (kimina-ctl is one program for the whole box)."""
    box = moved_copy(older, today, tmp_path)
    result = run(
        box.machine,
        "--instance",
        "demo",
        "--no-door",
        STUB_BUSY_PORT="18001",
        STUB_EXTRA_ROUTE="172.29.54.0/24",
    )
    assert "MOVED TO COMPOSE'S NAMES" in result.stdout and "INSTALLED AND VERIFIED" in result.stdout
    for name in NEW_CONTAINERS:
        assert box.machine.running(name), name
    assert (
        volume(box.machine, "lean-pool-demo_cache-data") / "answers" / "one"
    ).read_text() == CACHED


def test_a_joined_box_refreshed_as_its_join_command_does_is_moved(
    older: Machine, today: Path, tmp_path: Path
) -> None:
    """A box that joined holds `demo` behind its TLS front and no pool. Its refresh runs the bundle's installer
    as `install.sh --instance demo --no-door --keep-image` (join-box.sh)."""
    box = moved_copy(older, today, tmp_path)
    box.machine.deploy = older.deploy
    run(box.machine, "--proxy", "demo", "--uninstall", "--purge")
    box.machine.deploy = today
    result = run(box.machine, "--instance", "demo", "--no-door", "--keep-image")
    assert "MOVED TO COMPOSE'S NAMES" in result.stdout and "INSTALLED AND VERIFIED" in result.stdout
    for name in NEW_CONTAINERS[:4]:
        assert box.machine.running(name), name
    assert "compose.tls.yaml" in settings(box.machine, f"{INSTANCE}/kimina.env")["COMPOSE_FILE"]


def test_a_joined_box_busy_with_other_work_is_moved_by_its_refresh_and_keeps_its_protections(
    older: Machine, today: Path, tmp_path: Path
) -> None:
    """The same refresh on a box whose CPUs other work keeps busy (STUB_BOX_BUSY): at the lowest weight the moved
    server's first check runs past its time limit, and the move would be put back. The move's check and the
    install's run at the box's ordinary weight (pool-lib.sh, lift_cpu_priority); the lowest comes back after."""
    box = moved_copy(older, today, tmp_path)
    box.machine.deploy = older.deploy
    run(box.machine, "--proxy", "demo", "--uninstall", "--purge")
    box.machine.deploy = today
    result = run(box.machine, "--instance", "demo", "--no-door", "--keep-image", STUB_BOX_BUSY="1")
    assert "MOVED TO COMPOSE'S NAMES" in result.stdout and "INSTALLED AND VERIFIED" in result.stdout
    assert not (box.machine.stub / "starved-checks").exists()
    said = settings(box.machine, f"{INSTANCE}/kimina.env")
    assert said["LEAN_SERVER_CGROUP_PARENT"] == "leanpool-demo.slice"
    assert said["LEAN_SERVER_ROOT_READ_ONLY"] == "true"
    for off in ("READ-ONLY ROOT: OFF", "WORKER SANDBOX: OFF"):
        assert off not in result.stdout + result.stderr
    assert box.machine.path("/sys/fs/cgroup/leanpool.slice/cpu.weight").read_text().strip() == "1"


# --- a move that fails --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def failed(
    older: Machine, today: Path, tmp_path_factory: pytest.TempPathFactory
) -> SimpleNamespace:
    """The same install on a copy of the older box whose `demo` server, made under its new name, never becomes healthy."""
    box = moved_copy(older, today, tmp_path_factory.mktemp("failed"))
    box.result = box.machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        STUB_UNHEALTHY="lean-server-demo-kimina-1",
    )
    return box


def test_a_project_that_does_not_come_up_is_taken_down_and_the_older_one_comes_back(
    failed: SimpleNamespace,
) -> None:
    machine = failed.machine
    assert failed.result.returncode != 0
    assert (
        "could not move demo to Compose's names: its Lean server did not become healthy"
        in failed.result.stderr
    )
    # `demo` is as it was, file for file, and runs under its older names; its new project is gone.
    assert snapshot(machine.path(INSTANCE)) == failed.before.files[INSTANCE]
    for name in ("acme-kimina-demo", "acme-agent-demo", "acme-front-demo"):
        assert machine.running(name), name
    for name in (
        "lean-server-demo-kimina-1",
        "lean-server-demo-agent-1",
        "lean-server-demo-front-1",
    ):
        assert not machine.running(name), name
    # The pool was not reached: as it was, running, with its cache.
    assert snapshot(machine.path(POOL)) == failed.before.files[POOL]
    assert machine.running("acme-proxy-demo") and machine.running("acme-cache-demo")
    # Instance 1 moved before it, and stays moved.
    assert machine.running("lean-server-kimina-1") and not machine.running("acme-kimina")


def test_the_control_program_refuses_the_instance_that_did_not_move_and_says_how_to_move_it(
    failed: SimpleNamespace,
) -> None:
    result = failed.machine.bash(
        failed.machine.path("/usr/local/bin/kimina-ctl"), "@demo", "status"
    )
    assert result.returncode != 0
    assert "sudo bash install.sh --instance demo" in result.stderr


def test_a_second_run_then_moves_the_box(failed: SimpleNamespace, tmp_path: Path) -> None:
    machine = failed.machine.copy_to(tmp_path / "host")
    result = run(machine, "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    for name in NEW_CONTAINERS:
        assert machine.running(name), name
    assert (volume(machine, "lean-pool-demo_cache-data") / "answers" / "one").read_text() == CACHED


# --- removing what an older installer set up -------------------------------------------------------------------------


def test_todays_uninstall_takes_down_an_older_instance_and_an_older_pool(
    older: Machine, today: Path, tmp_path: Path
) -> None:
    box = moved_copy(older, today, tmp_path)
    run(box.machine, "--proxy", "demo", "--uninstall", "--purge")
    run(box.machine, "--instance", "demo", "--uninstall")
    for name in OLD_CONTAINERS[1:]:
        assert not box.machine.running(name), name
    assert box.machine.running("acme-kimina")  # instance 1 was not asked about
    assert not (state(box.machine) / "volume.acme-cache-demo").exists()
