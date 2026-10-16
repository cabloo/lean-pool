"""The screened door, installed on a fake machine (stand_ins.py; docs/cache.md).

The machine is the tests' site with `demo`'s pool, installed as it is today: the door closed. It is then opened as
an existing pool opens it, `install.sh --proxy demo --screened-door --screened-key-copy DIR`, with DIR where a
program on the host hands the key out (a directory under one that program's user owns). What is shown: the door
is closed unless asked for; opened, it is published on the docker bridge's address alone, with a key of its own
that only the cache and the copy hold, and limits the cache is given; the key is kept, renewed by `lean-pool-ctl
NAME screened-key new`, written without following a link in or above the copy's directory, and never printed.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from stand_ins import (
    ADMIT_CASES,
    Machine,
    installed_instances,
    rewritten_installers,
    tools_run_from_source,
)

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

POOL = "/srv/lean-pool-demo"
INSTANCE = "/srv/kimina-demo"
COPY = "/srv/hand-out/screened-key"
OVERLAY = "compose.screened-door.yaml"
CACHE = "lean-pool-demo-cache-1"
PROXY = "lean-pool-demo-haproxy-1"
LEAN_SERVERS = (
    "lean-server-kimina-1",
    "lean-server-demo-kimina-1",
    "lean-server-demo-front-1",
    "lean-server-demo-agent-1",
)


def failed(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout[-3000:] + result.stderr[-3000:]


def settings(machine: Machine) -> dict[str, str]:
    lines = machine.path(f"{POOL}/pool.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def resolved(machine: Machine) -> dict[str, Any]:
    """The pool's project as Compose last made it: every file merged, every setting read."""
    project: dict[str, Any] = json.loads(
        (machine.stub / "state" / "resolved.lean-pool-demo").read_text()
    )
    return project


def control(
    machine: Machine, *arguments: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    return control_in(None, machine, *arguments, **environment)


def control_in(
    directory: Path | None, machine: Machine, *arguments: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    return machine.bash_in(
        directory, machine.path("/usr/local/sbin/lean-pool-ctl"), "demo", *arguments, **environment
    )


def containers(machine: Machine) -> dict[str, str]:
    return {name: machine.container_id(name) for name in (*LEAN_SERVERS, PROXY, CACHE)}


def holding(machine: Machine, secret: str) -> list[str]:
    """Every file on the machine that holds SECRET: under its root, and in the stand-ins' own records (the calls,
    the owners' book, what Compose made, the containers' and volumes' files)."""
    found = []
    for top in (machine.root, machine.stub):
        for path in sorted(top.rglob("*")):
            if path.is_file() and not path.is_symlink() and secret.encode() in path.read_bytes():
                found.append(str(path.relative_to(machine.directory)))
    return found


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture(scope="module")
def plain(tmp_path_factory, deploy):
    """`demo`'s pool as an installer without a screened door leaves it: `install.sh --proxy demo`."""
    machine = installed_instances(tmp_path_factory.mktemp("plain") / "host", deploy)
    (machine.root / COPY.lstrip("/")).parent.mkdir(parents=True)
    result = machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, failed(result)
    return SimpleNamespace(machine=machine, result=result)


@pytest.fixture(scope="module")
def opened(tmp_path_factory, plain):
    """That pool, with its door opened."""
    machine = plain.machine.copy_to(tmp_path_factory.mktemp("opened") / "host")
    before, recreated, mark = containers(machine), machine.recreated(), machine.mark()
    result = machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        "--screened-door",
        "--screened-key-copy",
        str(machine.path(COPY)),
    )
    return SimpleNamespace(
        machine=machine,
        result=result,
        before=before,
        recreated=machine.recreated()[len(recreated) :],
        calls=machine.calls_since(mark),
    )


@pytest.fixture
def pool(tmp_path, opened):
    """A pool with its door open, of the test's own, to change."""
    assert opened.result.returncode == 0, failed(opened.result)
    machine = opened.machine.copy_to(tmp_path / "host")
    # The pool's settings name the copy's directory by its full path, which is the first machine's.
    env = machine.path(f"{POOL}/pool.env")
    env.write_text(env.read_text().replace(str(opened.machine.path(COPY)), str(machine.path(COPY))))
    return machine


def key_of(machine: Machine) -> str:
    return machine.path(f"{POOL}/screened-api-key").read_text().strip()


# --- closed unless asked for ------------------------------------------------------------------------------------------


def test_without_the_option_the_door_is_closed_and_has_no_key(plain):
    machine = plain.machine
    assert settings(machine)["LEANPOOL_SCREENED_DOOR"] == "off"
    assert OVERLAY not in settings(machine)["COMPOSE_FILE"]
    assert not machine.path(f"{POOL}/screened-api-key").exists()
    assert "screened_door" not in machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert "screened_api_key" not in json.dumps(resolved(machine))
    assert not machine.path("/srv/lean-pool-firewall/door-demo.conf").exists()
    assert "  screened " not in plain.result.stdout
    assert "--- screened door: off" in control(machine, "status").stdout


# --- opened -----------------------------------------------------------------------------------------------------------


def test_opening_the_door_ends_verified_and_makes_again_the_proxy_and_the_cache_alone(opened):
    assert opened.result.returncode == 0, failed(opened.result)
    assert "POOL INSTALLED AND VERIFIED" in opened.result.stdout
    assert (
        "screened door: on, at 172.17.0.1:18120 (the docker bridges only)" in opened.result.stdout
    )
    assert re.search(
        r"^  screened    https://172\.17\.0\.1:18120 \(the docker bridges only\); its key and the authority's "
        r"certificate are copied to \S+/srv/hand-out/screened-key$",
        opened.result.stdout,
        re.MULTILINE,
    ), opened.result.stdout
    assert sorted(opened.recreated) == sorted([PROXY, CACHE])
    assert {name: opened.machine.container_id(name) for name in LEAN_SERVERS} == {
        name: opened.before[name] for name in LEAN_SERVERS
    }


def test_the_door_is_published_on_the_bridges_address_alone_and_leads_to_the_cache(opened):
    machine = opened.machine
    pool = settings(machine)
    assert pool["LEANPOOL_SCREENED_DOOR"] == "on"
    assert pool["LEANPOOL_SCREENED_DOOR_ADDRESS"] == "172.17.0.1"
    assert pool["LEANPOOL_SCREENED_DOOR_PORT"] == "18120"
    assert pool["LEANPOOL_SCREENED_KEY_COPY"] == str(machine.path(COPY))
    # The overlay comes last, and was copied beside the others.
    assert pool["COMPOSE_FILE"].split(":")[-1] == OVERLAY
    assert machine.path(f"{POOL}/{OVERLAY}").is_file()
    project = resolved(machine)
    door = [port for port in project["services"]["haproxy"]["ports"] if "18120" in str(port)]
    assert door == ["172.17.0.1:18120:18120"]
    cache = project["services"]["cache"]
    assert (
        cache["environment"]["LEANPOOL_CACHE_SCREENED_API_KEY_FILE"]
        == "/run/secrets/screened_api_key"
    )
    assert "screened_api_key" in cache["secrets"]
    assert project["secrets"]["screened_api_key"] == {"file": "./screened-api-key"}
    # The cache's limits for the door's senders: the pool's workers, and its Lean timeout.
    config = machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    (workers,) = re.findall(r"^# Pool: \d+ Lean servers, (\d+) workers\.$", config, re.MULTILINE)
    assert pool["LEANPOOL_CACHE_SCREENED_IN_FLIGHT"] == workers
    assert cache["environment"]["LEANPOOL_CACHE_SCREENED_IN_FLIGHT"] == workers
    assert (
        cache["environment"]["LEANPOOL_CACHE_SCREENED_MAX_TIMEOUT_SECONDS"]
        == pool["LEANPOOL_CACHE_SCREENED_MAX_TIMEOUT_SECONDS"]
        == pool["LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS"]
    )
    assert f"maxconn {workers}" in config
    assert "screened_api_key" not in json.dumps(project["services"]["haproxy"])
    config = machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert "frontend screened_door" in config and "bind :18120 ssl crt" in config


def test_the_doors_key_is_its_own_and_the_cache_alone_reads_it(opened):
    machine = opened.machine
    key = key_of(machine)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key != machine.path(f"{POOL}/api-key").read_text().strip()
    # As the pool's key is: root's, readable by the pool image's group (the cache's), nobody else.
    assert machine.owner(f"{POOL}/screened-api-key") == ("root", "10001", 0o640)
    assert "made the screened door's key" in opened.result.stdout


def test_the_copy_holds_the_key_and_the_authority_owned_by_its_directorys_owner(opened):
    machine = opened.machine
    copy = machine.path(COPY)
    assert (copy / "api-key").read_text().strip() == key_of(machine)
    assert (copy / "ca.crt").read_bytes() == machine.path(f"{POOL}/certs/ca.crt").read_bytes()
    assert copy.stat().st_mode & 0o777 == 0o700
    assert (copy / "api-key").stat().st_mode & 0o777 == 0o600
    assert (copy / "ca.crt").stat().st_mode & 0o777 == 0o644
    owner = copy.parent.stat()
    for path in (copy, copy / "api-key", copy / "ca.crt"):
        assert (path.stat().st_uid, path.stat().st_gid) == (owner.st_uid, owner.st_gid)
    assert sorted(path.name for path in copy.iterdir()) == ["api-key", "ca.crt"]


def test_the_key_is_never_printed_and_is_held_by_its_file_and_the_copy_alone(opened):
    machine = opened.machine
    key = key_of(machine)
    assert key not in opened.result.stdout and key not in opened.result.stderr
    status = control(machine, "status")
    assert status.returncode == 0 and key not in status.stdout + status.stderr
    # No Lean server's settings, no join bundle, no record of a call: the cache's file and the copy alone.
    assert holding(machine, key) == sorted([f"root{POOL}/screened-api-key", f"root{COPY}/api-key"])


def test_status_says_where_the_door_is_and_where_its_key_is_copied(opened):
    status = control(opened.machine, "status")
    assert (
        f"--- screened door: on, https://172.17.0.1:18120; key copy {opened.machine.path(COPY)}"
        in status.stdout
    ), status.stdout


def test_the_door_is_firewalled_to_the_docker_bridges_with_no_lan_subnet(opened):
    machine = opened.machine
    assert (
        machine.path("/srv/lean-pool-firewall/door-demo.conf").read_text()
        == "LEANPOOL_FW_PORT=18120\nLEANPOOL_FW_SUBNET=\n"
    )
    assert "systemctl enable lean-pool-firewall@door-demo.service" in opened.calls
    mark = machine.mark()
    applied = machine.bash(machine.path("/usr/local/sbin/lean-pool-firewall"), "apply", "door-demo")
    assert applied.returncode == 0, failed(applied)
    rules = [
        call
        for call in machine.calls_since(mark)
        if call.startswith("iptables -A LEANPOOL-DOOR-DEMO")
    ]
    # The bridges by their interfaces: a LAN numbered inside Docker's own range is still turned away.
    assert rules == [
        "iptables -A LEANPOOL-DOOR-DEMO -i docker0 -j RETURN",
        "iptables -A LEANPOOL-DOOR-DEMO -i br-+ -j RETURN",
        "iptables -A LEANPOOL-DOOR-DEMO -s 127.0.0.0/8 -j RETURN",
        "iptables -A LEANPOOL-DOOR-DEMO -j DROP",
    ]
    assert (
        "iptables -I DOCKER-USER 1 -p tcp -m conntrack --ctorigdstport 18120 --ctdir ORIGINAL -j LEANPOOL-DOOR-DEMO"
        in machine.calls_since(mark)
    )


COMPOSE_NETWORK = "0123456789ab" + "c" * 52
NAMED_NETWORK = "fedcba987654" + "3" * 52


def door_rules(
    machine: Machine, **environment: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    mark = machine.mark()
    applied = machine.bash(
        machine.path("/usr/local/sbin/lean-pool-firewall"), "apply", "door-demo", **environment
    )
    rules = [
        call.removeprefix("iptables -A LEANPOOL-DOOR-DEMO ")
        for call in machine.calls_since(mark)
        if call.startswith("iptables -A LEANPOOL-DOOR-DEMO")
    ]
    return applied, rules


def chains_of(machine: Machine, rule: str, verb: str = "apply") -> tuple[int, set[str]]:
    """Apply (or remove) one firewall rule; its exit status and the chains its iptables calls name."""
    mark = machine.mark()
    result = machine.bash(machine.path("/usr/local/sbin/lean-pool-firewall"), verb, rule)
    named = set()
    for call in machine.calls_since(mark):
        words = call.split()
        if words[0] == "iptables" and words[1] in ("-N", "-A", "-F", "-X"):
            named.add(words[2])
        if "-j" in words and words[words.index("-j") + 1].startswith("LEANPOOL-"):
            named.add(words[words.index("-j") + 1])
    return result.returncode, named


@pytest.mark.parametrize("kind", ["proxy", "join", "door", "agent"])
def test_every_rule_of_a_sixteen_character_pool_has_a_chain_iptables_takes(pool, kind):
    # iptables takes a chain's name of at most 28 characters (the stand-in refuses a longer one, as it does).
    rule = f"{kind}-{'a' * 16}"
    other = f"{kind}-{'a' * 15}b"
    for name in (rule, other):
        pool.path(f"/srv/lean-pool-firewall/{name}.conf").write_text(
            "LEANPOOL_FW_PORT=18200\nLEANPOOL_FW_SUBNET=192.168.1.0/24\n"
        )
    status, chains = chains_of(pool, rule)
    assert status == 0
    (chain,) = chains
    assert len(chain) == 28 and chain.startswith(f"LEANPOOL-{rule[:12].upper()}-"), chain
    assert chains_of(pool, rule, "remove") == (0, {chain})
    # Two pools alike in their first twelve characters keep apart.
    other_status, other_chains = chains_of(pool, other)
    assert other_status == 0 and other_chains.isdisjoint(chains)


def test_a_rule_whose_chain_already_fits_keeps_its_chain(pool):
    # A pool installed before this change keeps the chain it has: its rules are found and removed as they were.
    rule = f"proxy-{'a' * 13}"
    pool.path(f"/srv/lean-pool-firewall/{rule}.conf").write_text(
        "LEANPOOL_FW_PORT=18200\nLEANPOOL_FW_SUBNET=192.168.1.0/24\n"
    )
    assert chains_of(pool, rule) == (0, {f"LEANPOOL-{rule.upper()}"})
    assert chains_of(pool, "door-demo") == (0, {"LEANPOOL-DOOR-DEMO"})


def test_a_bridge_named_br_that_is_not_dockers_is_turned_away_by_name(pool):
    # Docker's: a Compose network's br-<id>, and a network that names its own bridge. Not Docker's: a LAN's br-lan.
    for interface in ("eth0", "docker0", "br-0123456789ab", "br-named", "br-lan"):
        pool.path(f"/sys/class/net/{interface}").mkdir()
    applied, rules = door_rules(pool, STUB_NETWORKS=f"{COMPOSE_NETWORK}= {NAMED_NETWORK}=br-named")
    assert applied.returncode == 0, failed(applied)
    assert rules == [
        "-i docker0 -j RETURN",
        "-i br-lan -j DROP",
        "-i br-+ -j RETURN",
        "-s 127.0.0.0/8 -j RETURN",
        "-j DROP",
    ]


def test_without_dockers_networks_no_bridge_named_br_is_let_in(pool):
    pool.path("/sys/class/net/br-0123456789ab").mkdir()
    applied, rules = door_rules(pool, STUB_NETWORKS_FAIL="1")
    assert applied.returncode == 0, failed(applied)
    assert "docker0 alone is let in" in applied.stderr
    assert rules == ["-i docker0 -j RETURN", "-s 127.0.0.0/8 -j RETURN", "-j DROP"]


# --- kept, closed, renewed ---------------------------------------------------------------------------------------------


def test_a_re_run_without_the_options_keeps_the_door_its_key_and_its_copy(pool):
    key, before, recreated = key_of(pool), containers(pool), pool.recreated()
    (pool.path(COPY) / "api-key").unlink()
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, failed(result)
    assert key_of(pool) == key and settings(pool)["LEANPOOL_SCREENED_DOOR"] == "on"
    assert (pool.path(COPY) / "api-key").read_text().strip() == key
    assert pool.recreated() == recreated and containers(pool) == before


def test_no_screened_key_copy_stops_the_copy_and_leaves_its_files(pool):
    key, copy = key_of(pool), pool.path(COPY)
    result = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, "--no-screened-key-copy"
    )
    assert result.returncode == 0, failed(result)
    assert settings(pool)["LEANPOOL_SCREENED_KEY_COPY"] == ""
    assert (
        f"the copy in {copy} is no longer written, and its files are left there: remove them by hand"
        in result.stdout
    )
    assert (copy / "api-key").read_text().strip() == key
    # A later run writes nothing there, and the door stays open.
    (copy / "api-key").unlink()
    again = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert again.returncode == 0, failed(again)
    assert not (copy / "api-key").exists()
    assert settings(pool)["LEANPOOL_SCREENED_DOOR"] == "on"
    assert "key copy not written (no directory named)" in control(pool, "status").stdout


def test_the_copy_is_refused_in_a_directory_that_holds_the_pools_own_key(pool):
    copy = pool.path(COPY)
    pools_key = pool.path(f"{POOL}/api-key").read_text()
    (copy / "api-key").write_text(pools_key)
    result = control(pool, "screened-key", "copy")
    assert result.returncode != 0 and "holds the other key's copy" in result.stderr
    assert (copy / "api-key").read_text() == pools_key
    assert pools_key.strip() not in result.stdout + result.stderr


def install(machine: Machine, *options: str) -> subprocess.CompletedProcess[str]:
    return machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, *options)


def test_screened_imports_are_kept_replaced_and_cleared_and_reach_the_cache(pool):
    def cache_imports() -> str:
        environment = resolved(pool)["services"]["cache"]["environment"]
        return str(environment["LEANPOOL_CACHE_SCREENED_IMPORTS"])

    assert cache_imports() == ""
    given = install(
        pool, "--screened-import", "FormalConjectures", "--screened-import", "Batteries"
    )
    assert given.returncode == 0, failed(given)
    assert settings(pool)["LEANPOOL_CACHE_SCREENED_IMPORTS"] == "FormalConjectures,Batteries"
    assert cache_imports() == "FormalConjectures,Batteries"
    assert "its checks may also import FormalConjectures, Batteries" in given.stdout
    assert (
        "imports beyond Mathlib and Aesop: FormalConjectures, Batteries"
        in control(pool, "status").stdout
    )
    # Kept by a run that gives none, replaced by one that gives any, cleared by --no-screened-import.
    kept = install(pool)
    assert kept.returncode == 0, failed(kept)
    assert cache_imports() == "FormalConjectures,Batteries"
    replaced = install(pool, "--screened-import", "FormalConjectures")
    assert replaced.returncode == 0, failed(replaced)
    assert cache_imports() == "FormalConjectures"
    cleared = install(pool, "--no-screened-import")
    assert cleared.returncode == 0, failed(cleared)
    assert settings(pool)["LEANPOOL_CACHE_SCREENED_IMPORTS"] == ""
    assert cache_imports() == ""
    assert "may also import" not in cleared.stdout
    assert "imports beyond" not in control(pool, "status").stdout


def test_a_screened_import_that_is_not_a_root_or_says_both_is_refused(untouched):
    machine = untouched.machine
    before = machine.path(f"{POOL}/pool.env").read_bytes()
    for options in (
        ("--screened-import", "formal"),
        ("--screened-import", "Formal.Conjectures"),
        ("--screened-import", "A B"),
        ("--screened-import", ""),
        ("--screened-import", "FormalConjectures", "--no-screened-import"),
    ):
        result = install(machine, "--screened-door", *options)
        assert result.returncode != 0, options
        assert "--screened-import" in result.stderr, options
    assert machine.path(f"{POOL}/pool.env").read_bytes() == before


def test_closing_the_door_takes_its_port_and_its_rule_away_and_keeps_its_key(pool):
    key = key_of(pool)
    mark = pool.mark()
    result = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, "--no-screened-door"
    )
    assert result.returncode == 0, failed(result)
    assert settings(pool)["LEANPOOL_SCREENED_DOOR"] == "off"
    assert OVERLAY not in settings(pool)["COMPOSE_FILE"]
    assert "screened_door" not in pool.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert not any("18120" in str(port) for port in resolved(pool)["services"]["haproxy"]["ports"])
    assert "screened_api_key" not in json.dumps(resolved(pool)["services"]["cache"])
    assert not pool.path("/srv/lean-pool-firewall/door-demo.conf").exists()
    assert "systemctl disable --now lean-pool-firewall@door-demo.service" in pool.calls_since(mark)
    assert key_of(pool) == key
    refused = control(pool, "screened-key", "copy")
    assert refused.returncode != 0 and "screened door is closed" in refused.stderr
    # A copy's directory alone does not open it.
    refused = pool.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        "--screened-key-copy",
        str(pool.path(COPY)),
    )
    assert refused.returncode != 0 and "which pool demo has closed" in refused.stderr
    # Opened again, it takes the key it had and writes the copy where it did.
    result = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, "--screened-door"
    )
    assert result.returncode == 0, failed(result)
    assert key_of(pool) == key and (pool.path(COPY) / "api-key").read_text().strip() == key


def test_a_new_key_replaces_both_copies_and_makes_again_the_cache_alone(pool):
    old, pools_key = key_of(pool), pool.path(f"{POOL}/api-key").read_bytes()
    before, recreated = containers(pool), pool.recreated()
    result = control(pool, "screened-key", "new")
    assert result.returncode == 0, failed(result)
    new = key_of(pool)
    assert re.fullmatch(r"[0-9a-f]{64}", new) and new != old
    assert (pool.path(COPY) / "api-key").read_text().strip() == new
    assert pool.owner(f"{POOL}/screened-api-key") == ("root", "10001", 0o640)
    assert pool.path(f"{POOL}/api-key").read_bytes() == pools_key
    assert pool.recreated() == [*recreated, CACHE]
    assert {name: pool.container_id(name) for name in (*LEAN_SERVERS, PROXY)} == {
        name: before[name] for name in (*LEAN_SERVERS, PROXY)
    }
    for key in (old, new):
        assert key not in result.stdout + result.stderr
    assert holding(pool, old) == []
    assert not list(pool.path(POOL).glob(".screened-api-key.*"))


def test_a_new_key_with_the_door_closed_retires_the_old_one_and_makes_no_container(pool):
    closed = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, "--no-screened-door"
    )
    assert closed.returncode == 0, failed(closed)
    old, before, recreated = key_of(pool), containers(pool), pool.recreated()
    result = control(pool, "screened-key", "new")
    assert result.returncode == 0, failed(result)
    assert "the new key is the one its cache takes when it opens" in result.stdout
    new = key_of(pool)
    assert new != old and (pool.path(COPY) / "api-key").read_text().strip() == new
    assert holding(pool, old) == []
    assert pool.recreated() == recreated and containers(pool) == before


def test_a_new_key_the_cache_was_not_made_again_with_is_a_failure(pool):
    # The cache cannot be made again (its other key's file is gone): the old container, holding the old key, is
    # still the one running, and the verb says so instead of claiming the old key refused.
    pool.path(f"{POOL}/api-key").unlink()
    result = control(pool, "screened-key", "new")
    assert result.returncode != 0
    assert "the cache was not made again with the new key" in result.stderr


def test_the_copy_is_never_written_through_a_link_planted_in_its_directory(pool, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("left alone\n")
    victim.chmod(0o640)
    copy = pool.path(COPY)
    (copy / "api-key").unlink()
    (copy / "api-key").symlink_to(victim)
    result = control(pool, "screened-key", "copy")
    assert result.returncode == 0, failed(result)
    assert victim.read_text() == "left alone\n" and victim.stat().st_mode & 0o777 == 0o640
    assert not (copy / "api-key").is_symlink()
    assert (copy / "api-key").read_text().strip() == key_of(pool)


def test_a_copy_directory_that_is_a_link_is_refused(pool, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    copy = pool.path(COPY)
    for path in copy.iterdir():
        path.unlink()
    copy.rmdir()
    copy.symlink_to(elsewhere)
    result = control(pool, "screened-key", "copy")
    assert result.returncode != 0 and "could not be written" in result.stderr
    assert "a link or a file is in its path" in result.stderr
    assert list(elsewhere.iterdir()) == []


def test_a_link_above_the_copy_directory_is_never_followed(pool, tmp_path):
    # The owner of the directory above swaps it for a link to a directory holding one named like the copy's.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "lean-pool").mkdir(parents=True)
    above = pool.path(COPY).parent
    above.rename(tmp_path / "moved")
    above.symlink_to(elsewhere)
    result = control(pool, "screened-key", "copy")
    assert result.returncode != 0 and "a link or a file is in its path" in result.stderr
    assert list((elsewhere / "lean-pool").iterdir()) == []


def test_a_copy_that_cannot_be_moved_into_place_leaves_no_file_behind(pool):
    copy = pool.path(COPY)
    (copy / "api-key").unlink()
    (copy / "api-key").mkdir()
    result = control(pool, "screened-key", "copy")
    assert result.returncode != 0 and "could not be written" in result.stderr
    assert sorted(path.name for path in copy.iterdir()) == ["api-key", "ca.crt"]
    assert holding(pool, key_of(pool)) == [f"root{POOL}/screened-api-key"]


def test_python_run_as_root_never_imports_from_the_directory_it_was_started_in(pool, tmp_path):
    planted = tmp_path / "planted"
    planted.mkdir()
    marker = tmp_path / "ran"
    (planted / "secrets.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    for arguments in (("screened-key", "copy"), ("screened-key", "new")):
        result = control_in(planted, pool, *arguments)
        assert result.returncode == 0, failed(result)
    assert not marker.exists()


# --- refused before anything is installed ----------------------------------------------------------------------------


@pytest.fixture
def untouched(tmp_path, plain):
    machine = plain.machine.copy_to(tmp_path / "host")
    return SimpleNamespace(machine=machine, settings=machine.path(f"{POOL}/pool.env").read_bytes())


def test_a_copy_directory_that_is_not_a_plain_absolute_path_is_refused(untouched):
    for directory in ("relative/dir", "/srv/../etc", "/srv/a dir", "/srv/.", "/srv/x/", "/srv//x"):
        result = untouched.machine.bash(
            "install.sh",
            "--proxy",
            "demo",
            "--admit-cases",
            ADMIT_CASES,
            "--screened-door",
            "--screened-key-copy",
            directory,
        )
        assert (
            result.returncode != 0 and "--screened-key-copy must be an absolute" in result.stderr
        ), directory
    assert untouched.machine.path(f"{POOL}/pool.env").read_bytes() == untouched.settings


def test_a_copy_directory_with_the_door_closed_is_refused(untouched):
    result = untouched.machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        "--no-screened-door",
        "--screened-key-copy",
        str(untouched.machine.path(COPY)),
    )
    assert result.returncode != 0 and "which --no-screened-door closes" in result.stderr
    assert untouched.machine.path(f"{POOL}/pool.env").read_bytes() == untouched.settings


def test_a_host_whose_bridge_address_cannot_be_read_opens_no_door(untouched):
    machine = untouched.machine
    result = machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        "--screened-door",
        STUB_BRIDGE="",
    )
    assert result.returncode != 0 and "nothing was installed" in result.stderr
    assert machine.path(f"{POOL}/pool.env").read_bytes() == untouched.settings
    assert not machine.path(f"{POOL}/screened-api-key").exists()


def test_the_verb_takes_new_or_copy_alone(pool):
    for arguments in ((), ("old",), ("new", "copy")):
        result = control(pool, "screened-key", *arguments)
        assert result.returncode == 2 and "screened-key new" in result.stderr, arguments
    assert Path(pool.path(f"{POOL}/screened-api-key")).exists()
