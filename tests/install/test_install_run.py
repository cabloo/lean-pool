"""The installers, run on a fake machine.

`install.sh --proxy demo` and `lean-pool-ctl` need root, Docker and systemd, so they cannot run here for
real. They run here against the stand-ins of `stand_ins.py` instead (read its docstring first): the
REAL scripts, the REAL certificate and config tools from the lean-pool project's source, a fake root, and a
`docker` that enforces file permissions the way an unprivileged container would meet them.

The machine starts with instance 1 (the instance with no name, which the pool must never touch) and the
plain `demo` instance, both put there by the installer. The upgrade is then `install.sh --proxy demo`, which
puts the TLS pool in front of `demo`.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from stand_ins import (
    ADMIT_CASES,
    Machine,
    copy_deploy,
    installed_instances,
    rewritten_installers,
    snapshot,
    tools_run_from_source,
)

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID  # noqa: E402

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

POOL = "/srv/lean-pool-demo"
INSTANCE = "/srv/kimina-demo"


def certificate(path: Path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def names_of(cert) -> tuple[set[str], set[str]]:
    alternative = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(alternative.get_values_for_type(x509.DNSName)), {
        str(a) for a in alternative.get_values_for_type(x509.IPAddress)
    }


def common_name(cert) -> str:
    return str(cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value)


def usages(cert) -> list[object]:
    return list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)


def pin_of(machine: Machine) -> str:
    result = subprocess.run(
        [
            str(machine.stub / "bin" / "leanpool-pki"),
            "pin",
            str(machine.path(f"{POOL}/tls/front.pem")),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def changing(calls: list[str]) -> list[str]:
    """The calls that change something that runs: a container made, removed or signalled, a firewall re-applied."""
    return [
        call
        for call in calls
        if re.search(
            r"^docker (rm|kill|stop|start) | up -d| down$|^systemctl restart |^docker exec \S+ sh -c kill -HUP 1$",
            call,
        )
    ]


def compose_files_of(machine: Machine, directory: str = INSTANCE) -> list[str]:
    """The compose files an instance or a pool runs, as its env file names them (COMPOSE_FILE)."""
    env = "pool.env" if directory.startswith("/srv/lean-pool-") else "kimina.env"
    for line in machine.path(f"{directory}/{env}").read_text().splitlines():
        if line.startswith("COMPOSE_FILE="):
            return line.removeprefix("COMPOSE_FILE=").split(":")
    return []


def started(machine: Machine, project: str, directory: str) -> str:
    """How `compose up -d` of a project shows in the record of calls: its name, its env file, and each of its
    files passed by itself."""
    env = "pool.env" if project.startswith("lean-pool-") else "kimina.env"
    files = " ".join(
        f"-f {machine.path(directory)}/{name}" for name in compose_files_of(machine, directory)
    )
    return f"docker compose --project-name {project} --env-file {machine.path(directory)}/{env} {files} up -d"


def reload_of(container: str) -> str:
    """How a reload shows in the record of calls: sent from INSIDE the container, never with `docker kill`."""
    return f"docker exec {container} sh -c kill -HUP 1"


EVERY_CONTAINER = (
    "lean-server-kimina-1",
    "lean-server-demo-kimina-1",
    "lean-server-demo-front-1",
    "lean-server-demo-agent-1",
    "lean-pool-demo-haproxy-1",
    "lean-pool-demo-cache-1",
)


# --- the machine ----------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deploy(tmp_path_factory):
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture(scope="module")
def today(tmp_path_factory, deploy):
    """The host before the pool: instance 1 and the `demo` instance with its own published port and agent,
    both from the installer. From here on, `install.sh --proxy demo` puts the pool in front of `demo`."""
    return installed_instances(tmp_path_factory.mktemp("today") / "host", deploy)


def listed(machine: Machine) -> str:
    """The pool host's own server as the server list names it (an old install kept the host name's capitals)."""
    return machine.path(f"{POOL}/servers").read_text().splitlines()[-1].split()[0]


@pytest.fixture(scope="module")
def upgraded(tmp_path_factory, today):
    """One run of `install.sh --proxy demo` on a copy of that machine, with what it was before."""
    machine = today.copy_to(tmp_path_factory.mktemp("upgraded") / "host")
    before = SimpleNamespace(
        instance_one=snapshot(machine.path("/srv/kimina")),
        instance_one_container=machine.container_id("lean-server-kimina-1"),
        demo_container=machine.container_id("lean-server-demo-kimina-1"),
        demo_files=compose_files_of(machine),
        proxy=machine.container_id("lean-pool-demo-haproxy-1"),
        cache=machine.container_id("lean-pool-demo-cache-1"),
        firewall_script=machine.path("/usr/local/sbin/kimina-firewall").read_text(),
    )
    mark = machine.mark()
    result = machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    return SimpleNamespace(
        machine=machine, result=result, calls=machine.calls_since(mark), before=before
    )


@pytest.fixture
def pool(tmp_path, upgraded):
    """A TLS pool of the test's own, to change."""
    assert upgraded.result.returncode == 0, (
        upgraded.result.stdout[-3000:] + upgraded.result.stderr[-3000:]
    )
    return upgraded.machine.copy_to(tmp_path / "host")


# --- the upgrade of the pool that exists --------------------------------------------------------------------------------


def test_the_upgrade_ends_verified_over_https(upgraded):
    assert upgraded.result.returncode == 0, (
        upgraded.result.stdout[-3000:] + upgraded.result.stderr[-3000:]
    )
    assert "POOL INSTALLED AND VERIFIED" in upgraded.result.stdout
    assert "https://workstation.lan:18100" in upgraded.result.stdout
    assert (
        "sudo lean-pool-ctl demo join-open" in upgraded.result.stdout
        and "add-lean-server" not in upgraded.result.stdout
    )


def setting_of(machine: Machine, path: str, key: str) -> str:
    """One KEY=VALUE of a settings file the installers wrote (empty when absent)."""
    return next(
        (
            line.split("=", 1)[1]
            for line in machine.path(path).read_text().splitlines()
            if line.startswith(f"{key}=")
        ),
        "",
    )


def test_the_upgrade_records_one_installer_version_for_the_pool_and_its_own_server(upgraded):
    """The version is written for the usage agent of the pool's own server and for the pool's
    /health, the same for both; `kimina-ctl status` shows its first 12 digits."""
    machine = upgraded.machine
    version = setting_of(machine, f"{INSTANCE}/kimina.env", "LEANPOOL_AGENT_VERSION")
    assert re.fullmatch(r"[0-9a-f]{32}", version), version
    assert setting_of(machine, f"{POOL}/pool.env", "LEANPOOL_HAPROXY_INSTALLER_VERSION") == version
    assert f',"version":"{version}"}}\'' in machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    # The agent behind the front is handed it by its compose overlay.
    assert "compose.agent.yaml" in compose_files_of(machine)
    assert (
        'LEANPOOL_AGENT_VERSION: "${LEANPOOL_AGENT_VERSION:-}"'
        in machine.path(f"{INSTANCE}/compose.agent.yaml").read_text()
    )
    output = status(machine, "@demo").stdout
    (health,) = [line for line in output.splitlines() if line.startswith("--- health: ")]
    assert health.endswith(f" (installer version {version[:12]})"), health


def test_a_plain_instance_with_an_agent_records_the_installers_version(today):
    """No pool yet: the agent beside `demo` publishes its own port, and is handed the version too."""
    version = setting_of(today, f"{INSTANCE}/kimina.env", "LEANPOOL_AGENT_VERSION")
    assert re.fullmatch(r"[0-9a-f]{32}", version), version
    assert "compose.agent.yaml" in compose_files_of(today)
    assert (
        'LEANPOOL_AGENT_VERSION: "${LEANPOOL_AGENT_VERSION:-}"'
        in today.path(f"{INSTANCE}/compose.agent.yaml").read_text()
    )
    # Instance 1 runs no agent, and its settings are as they were: no version, and its status says so.
    assert "LEANPOOL_AGENT_VERSION" not in today.path("/srv/kimina/kimina.env").read_text()
    assert " (installer version ?)" in status(today).stdout


def test_a_drain_that_states_the_version_is_read_as_a_drain(pool):
    version = "0123456789abcdef0123456789abcdef"
    output = status(pool, "@demo", STUB_AGENT_SAYS=f"drain 0% # version {version}").stdout
    assert (
        f"--- usage agent says: drain 0% # version {version} (no check fits beside this box's other work"
        in output
    )


def test_the_upgrade_gives_the_pools_own_lean_server_the_compiled_search_step(upgraded):
    """What `install.sh --proxy` does for the compiled search step on the pool's host: the pool's own Lean
    server runs its workers through it."""
    machine = upgraded.machine
    assert upgraded.result.returncode == 0, (
        upgraded.result.stdout[-3000:] + upgraded.result.stderr[-3000:]
    )
    assert "  search      COMPILED SEARCH STEP: on - " in upgraded.result.stdout
    assert machine.worker_program("lean-server-demo-kimina-1") == "/opt/lean-native/repl-native"
    assert compose_files_of(machine)[-1] == "compose.compiled-search.yaml"
    assert {path.name for path in machine.path(f"{INSTANCE}/native").iterdir()} == {
        "repl-native",
        "repl-path",
        "linarith-search.so",
        "sources",
        "built-from",
        "state",
    }
    status = machine.bash(machine.path("/usr/local/bin/kimina-ctl"), "@demo", "status")
    assert status.returncode == 0 and "--- compiled search step: on (" in status.stdout
    # Instance 1 is not the pool's: the refresh left its container and its settings as they were.
    assert machine.container_id("lean-server-kimina-1") == upgraded.before.instance_one_container
    assert snapshot(machine.path("/srv/kimina")) == upgraded.before.instance_one


def test_every_file_a_container_reads_has_exactly_the_owner_group_and_mode_it_needs(upgraded):
    """Every file a container reads. HAProxy's group (99 here) and the pool image's (10001) were read from the
    images by the installer; the stand-in docker refused any read these modes would not allow."""
    owner = upgraded.machine.owner
    # The authority: root only, and mounted in no running container.
    assert owner(f"{POOL}/pki") == ("root", "root", 0o700)
    assert owner(f"{POOL}/pki/ca/ca.key")[2] == 0o600 and owner(f"{POOL}/pki/ca")[2] == 0o700
    # The proxy container (HAProxy's group): the front door's key and the proxy's client key.
    assert owner(f"{POOL}/tls") == ("root", "99", 0o750)
    assert owner(f"{POOL}/tls/front.pem") == ("root", "99", 0o640)
    assert owner(f"{POOL}/tls/proxy-client.pem") == ("root", "99", 0o640)
    assert owner(f"{POOL}/tls/ca.crt") == ("root", "root", 0o644)
    assert owner(f"{POOL}/haproxy/haproxy.cfg") == ("root", "root", 0o644)
    # The cache (the pool image's group): the pool's key, and the authority's public certificate.
    assert owner(f"{POOL}/api-key") == ("root", "10001", 0o640)
    assert owner(f"{POOL}/certs/ca.crt") == ("root", "root", 0o644)
    # This box's TLS front (HAProxy's group): this box's key.
    assert owner(f"{INSTANCE}/tls") == ("root", "99", 0o750)
    assert owner(f"{INSTANCE}/tls/box.pem") == ("root", "99", 0o640)
    assert owner(f"{INSTANCE}/tls/ca.crt") == ("root", "root", 0o644)
    assert owner(f"{INSTANCE}/front/haproxy.cfg") == ("root", "root", 0o644)
    assert owner(f"{POOL}/pool.env") == ("root", "root", 0o600)


def test_no_private_key_is_readable_by_everyone_or_lies_where_it_is_public(upgraded):
    machine = upgraded.machine
    keys = []
    for path in machine.root.rglob("*"):
        if path.is_file() and b"PRIVATE KEY" in path.read_bytes():
            keys.append(str(path.relative_to(machine.root)))
            _, group, mode = machine.owner(path.relative_to(machine.root))
            assert mode & 0o007 == 0 and group in ("root", "99"), path
            assert (
                "/certs/" not in str(path)
                and "/join/" not in str(path)
                and "/front/" not in str(path)
            ), path
    assert sorted(keys) == [
        "srv/kimina-demo/tls/box.pem",
        "srv/lean-pool-demo/pki/box/box.key",
        "srv/lean-pool-demo/pki/box/box.pem",
        "srv/lean-pool-demo/pki/ca/ca.key",
        "srv/lean-pool-demo/pki/proxy/front.key",
        "srv/lean-pool-demo/pki/proxy/front.pem",
        "srv/lean-pool-demo/pki/proxy/proxy-client.key",
        "srv/lean-pool-demo/pki/proxy/proxy-client.pem",
        "srv/lean-pool-demo/tls/front.pem",
        "srv/lean-pool-demo/tls/proxy-client.pem",
    ]


def test_the_certificates_are_for_the_names_the_settings_give(upgraded):
    machine = upgraded.machine
    authority = certificate(machine.path(f"{POOL}/certs/ca.crt"))
    assert authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    front = certificate(machine.path(f"{POOL}/certs/front.crt"))
    # The names clients use; loopback (lean-pool-ctl checks through it) and docker0 (a container's way in).
    assert names_of(front) == ({"workstation", "workstation.lan"}, {"127.0.0.1", "172.17.0.1"})
    assert usages(front) == [ExtendedKeyUsageOID.SERVER_AUTH]
    client = certificate(machine.path(f"{POOL}/certs/proxy-client.crt"))
    assert common_name(client) == "lean-pool-proxy" and usages(client) == [
        ExtendedKeyUsageOID.CLIENT_AUTH
    ]
    # This box's own server: a certificate for its NAME in the server list, whatever address the proxy dials.
    # The old installer listed the host name as it was ("Workstation" on this fake machine); the proxy
    # checks the name in lower case, so that is what the certificate carries. The list itself is not edited.
    box = certificate(machine.path(f"{INSTANCE}/tls/box.pem"))
    assert names_of(box) == ({"workstation"}, set()) and usages(box) == [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    assert (
        machine.path(f"{POOL}/servers").read_text().splitlines()[-1].lower()
        == "workstation 172.29.55.1:18001 8 18201"
    )
    for issued in (front, client, box):
        assert issued.issuer == authority.subject
    assert (
        machine.path(f"{POOL}/tls/front.pem").read_bytes()
        == machine.path(f"{POOL}/pki/proxy/front.pem").read_bytes()
    )


def test_the_pools_config_is_tls_on_every_listener_that_leaves_the_box(upgraded):
    config = upgraded.machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert "UNENCRYPTED" not in config
    assert "bind :18100 ssl crt /etc/leanpool/tls/front.pem ssl-min-ver TLSv1.3" in config
    binds = [line.strip() for line in config.splitlines() if line.strip().startswith("bind ")]
    assert binds and all(" ssl " in bind or bind.startswith("bind 127.0.0.1:") for bind in binds), (
        binds
    )
    server = next(
        line
        for line in config.splitlines()
        if line.strip().startswith(f"server {listed(upgraded.machine)} ")
    )
    assert (
        "172.29.55.1:18001 ssl verify required ca-file /etc/leanpool/tls/ca.crt crt /etc/leanpool/tls/proxy-client.pem"
        in server
    )
    assert (
        "sni str(workstation) verifyhost workstation" in server and "agent-addr 127.0.0.1" in server
    )


def test_this_boxs_server_went_behind_a_front_that_lets_in_the_proxy_alone(upgraded):
    machine = upgraded.machine
    front = machine.path(f"{INSTANCE}/front/haproxy.cfg").read_text()
    # Inside the project; the host's 18001 and 18201 are published to them.
    for port in ("8000", "18200"):
        assert (
            f"bind :{port} ssl crt /etc/leanpool/tls/box.pem ssl-min-ver TLSv1.3 ca-file /etc/leanpool/tls/ca.crt verify required"
            in front
        )
    assert (
        front.count("tcp-request session reject unless { ssl_c_s_dn(cn) -m str lean-pool-proxy }")
        == 2
    )
    assert "server lean kimina:8000" in front and "server agent agent:18200" in front
    assert (
        "timeout client 780s" in front
    )  # the pool's own limit (300 s Lean timeout, 120 s wait) and a margin
    # The Lean server and the agent publish nothing any more; the front does (the TLS overlay).
    assert upgraded.before.demo_files == [
        "compose.yaml",
        "compose.agent.yaml",
        "compose.compiled-search.yaml",
    ]
    assert compose_files_of(machine) == [
        "compose.yaml",
        "compose.agent.yaml",
        "compose.tls.yaml",
        "compose.compiled-search.yaml",
    ]
    settings = machine.path(f"{INSTANCE}/kimina.env").read_text()
    assert (
        "LEAN_SERVER_FRONT_ADDRESS=172.29.54.250" in settings
        and "LEAN_SERVER_NAME=workstation" in settings
    )
    for service in ("kimina", "agent", "front"):  # one project
        assert (
            machine.stub / "state" / f"label.lean-server-demo-{service}-1"
        ).read_text() == "lean-server-demo"
    # The firewall learned the front's address, and only the demo unit was re-applied.
    assert (
        "KIMINA_FW_FRONT_ADDRESS=172.29.54.250"
        in machine.path(f"{INSTANCE}/firewall.conf").read_text()
    )
    assert "systemctl restart kimina-firewall-demo.service" in upgraded.calls
    # The Lean server was recreated exactly once, to drop its published port.
    assert machine.container_id("lean-server-demo-kimina-1") != upgraded.before.demo_container


def test_haproxy_judged_both_configs_before_anything_that_runs_was_touched(upgraded):
    calls = upgraded.calls
    validations = [
        index for index, call in enumerate(calls) if "haproxy -c -f /cfg/haproxy.cfg" in call
    ]
    first_change = calls.index(changing(calls)[0])
    assert len(validations) == 3 and validations[0] < first_change and validations[1] < first_change
    assert (
        "/srv/lean-pool-demo/tls:/etc/leanpool/tls:ro" in calls[validations[0]]
    )  # the pool's config
    assert "/srv/kimina-demo/tls:/etc/leanpool/tls:ro" in calls[validations[1]]  # this box's front
    # The first change: the firewall learns the front's address, before the project starts with the front.
    assert changing(calls)[:2] == [
        "systemctl restart kimina-firewall-demo.service",
        started(upgraded.machine, "lean-server-demo", INSTANCE),
    ]


def test_instance_one_is_untouched(upgraded):
    """Its directory is byte for byte what it was, its container is the same container, and no
    call named its project, its container or its firewall unit."""
    machine = upgraded.machine
    assert snapshot(machine.path("/srv/kimina")) == upgraded.before.instance_one
    assert (
        machine.container_id("lean-server-kimina-1") == upgraded.before.instance_one_container != ""
    )
    for call in upgraded.calls:
        assert not re.search(
            r"lean-server-kimina-1( |$)|/srv/kimina/|kimina-firewall\.service", call
        ), call
    assert (
        not machine.path("/srv/kimina/tls").exists()
        and not machine.path("/srv/kimina/front").exists()
    )


def test_the_join_port_is_firewalled_and_no_join_service_runs_outside_a_window(upgraded):
    machine = upgraded.machine
    assert (
        machine.path("/srv/lean-pool-firewall/join-demo.conf").read_text()
        == "LEANPOOL_FW_PORT=18110\nLEANPOOL_FW_SUBNET=192.168.50.0/24\n"
    )
    assert "systemctl enable lean-pool-firewall@join-demo.service" in upgraded.calls
    assert not any("leanpool-join" in call for call in upgraded.calls)
    settings = machine.path(f"{POOL}/pool.env").read_text()
    assert (
        "LEANPOOL_JOIN_PUBLIC_HOST=workstation.lan" in settings
        and "LEANPOOL_JOIN_PUBLIC_PORT=18110" in settings
        and "LEANPOOL_IMAGE_GID=10001" in settings
    )


# --- a re-run -----------------------------------------------------------------------------------------------------------


def test_a_refresh_of_the_pool_host_makes_its_writable_server_read_only_once(pool):
    """A pool host whose own Lean server runs writable (installed before its root could be read-only, or
    left writable by an earlier reading). One run of `install.sh --proxy` reads it, makes it again
    read-only ONCE, checks it by itself, and touches no other container."""
    env = pool.path(f"{INSTANCE}/kimina.env")
    env.write_text(
        "".join(
            line
            for line in env.read_text().splitlines(keepends=True)
            if not line.startswith(("LEAN_SERVER_ROOT_READ_ONLY=", "LEAN_SERVER_READ_ONLY="))
        )
    )
    pool.path(f"{INSTANCE}/read-only.state").unlink(missing_ok=True)
    assert pool.bash(pool.path("/usr/local/bin/kimina-ctl"), "@demo", "restart").returncode == 0
    assert not pool.read_only(LEAN)
    recreated = pool.recreated()
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "POOL INSTALLED AND VERIFIED" in result.stdout and pool.read_only(LEAN)
    assert pool.recreated() == [*recreated, LEAN]


def test_a_re_run_keeps_the_authority_and_the_pin_and_recreates_nothing(pool):
    authority = pool.path(f"{POOL}/pki/ca/ca.crt").read_bytes()
    key = pool.path(f"{POOL}/pki/ca/ca.key").read_bytes()
    pin, recreated, mark = pin_of(pool), pool.recreated(), pool.mark()
    containers = {
        name: pool.container_id(name)
        for name in (
            "lean-server-kimina-1",
            "lean-server-demo-kimina-1",
            "lean-server-demo-front-1",
            "lean-server-demo-agent-1",
            "lean-pool-demo-haproxy-1",
            "lean-pool-demo-cache-1",
        )
    }
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert (
        pool.path(f"{POOL}/pki/ca/ca.crt").read_bytes() == authority
        and pool.path(f"{POOL}/pki/ca/ca.key").read_bytes() == key
    )
    assert pin_of(pool) == pin
    for kept in (
        "keeping the pool's certificate authority",
        "keeping the front door's certificate",
        "keeping the proxy's client certificate",
        "keeping this box's server certificate",
    ):
        assert kept in result.stdout
    calls = pool.calls_since(mark)
    assert not any("leanpool-pki init" in call or "leanpool-pki issue" in call for call in calls)
    # Every container is the container it was. The two HAProxys reload (HUP), which drops no check; the
    # firewall rules are applied again.
    assert pool.recreated() == recreated
    assert {name: pool.container_id(name) for name in containers} == containers
    assert changing(calls) == [
        "systemctl restart kimina-firewall-demo.service",
        started(pool, "lean-server-demo", INSTANCE),
        reload_of("lean-server-demo-front-1"),
        "systemctl restart lean-pool-firewall@agent-demo.service",
        started(pool, "lean-pool-demo", POOL),
        reload_of("lean-pool-demo-haproxy-1"),
        "systemctl restart lean-pool-firewall@proxy-demo.service",
        "systemctl restart lean-pool-firewall@join-demo.service",
    ]


def test_a_new_front_name_makes_a_new_front_door_certificate_and_only_that(pool):
    authority, pin = pool.path(f"{POOL}/pki/ca/ca.crt").read_bytes(), pin_of(pool)
    client = pool.path(f"{POOL}/pki/proxy/proxy-client.pem").read_bytes()
    result = pool.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        "--front-name",
        "pool.example",
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "the front door's names changed" in result.stdout
    assert names_of(certificate(pool.path(f"{POOL}/tls/front.pem")))[0] == {
        "workstation",
        "workstation.lan",
        "pool.example",
    }
    assert pin_of(pool) != pin  # a new key: a join command shows the new pin
    assert (
        pool.path(f"{POOL}/pki/ca/ca.crt").read_bytes() == authority
    )  # clients keep trusting what they trusted
    assert pool.path(f"{POOL}/pki/proxy/proxy-client.pem").read_bytes() == client
    # The name is remembered: a later re-run without the option asks for the same certificate.
    pin = pin_of(pool)
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert (
        result.returncode == 0
        and "keeping the front door's certificate" in result.stdout
        and pin_of(pool) == pin
    )
    assert (
        pool.bash(
            "install.sh",
            "--proxy",
            "demo",
            "--admit-cases",
            ADMIT_CASES,
            "--front-name",
            "Pool_Example",
        ).returncode
        == 1
    )


def test_re_running_either_instance_installer_after_the_upgrade_recreates_only_a_server_whose_image_or_compose_file_changed(
    pool,
):
    one = pool.container_id("lean-server-kimina-1")
    before, recreated, mark = snapshot(pool.path("/srv/kimina")), pool.recreated(), pool.mark()
    result = pool.bash("install.sh", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    # Instance 1 builds on every run: every run calls `docker build`, and no stamp is kept.
    assert any(
        call.startswith("docker build") and "acme/kimina:lean-default-1" in call
        for call in pool.calls_since(mark)
    )
    assert not pool.path("/srv/kimina/image.built").exists()
    after = snapshot(pool.path("/srv/kimina"))
    assert set(after) == set(before)
    assert (
        after == before
        and pool.container_id("lean-server-kimina-1") == one
        and pool.recreated() == recreated
    )
    # From then on a run of it changes nothing: the same container, the directory byte for byte.
    one, before, recreated = pool.container_id("lean-server-kimina-1"), after, pool.recreated()
    assert pool.bash("install.sh", "--no-door").returncode == 0
    assert pool.container_id("lean-server-kimina-1") == one and pool.recreated() == recreated
    assert snapshot(pool.path("/srv/kimina")) == before
    # Instance 1 stays published, plain.
    assert compose_files_of(pool, "/srv/kimina") == ["compose.yaml"]
    # The pool member stays behind its front, with the certificate and the settings it has.
    box = pool.path(f"{INSTANCE}/tls/box.pem").read_bytes()
    result = pool.bash("install.sh", "--instance", "demo", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "behind a TLS front as 'workstation': keeping it" in result.stdout
    assert "https://Workstation:18001, mutual TLS as 'workstation'" in result.stdout
    # Nothing of the instance changed since the upgrade, so the re-run makes nothing again.
    assert pool.recreated() == recreated
    assert pool.path(f"{INSTANCE}/tls/box.pem").read_bytes() == box
    assert "compose.tls.yaml" in compose_files_of(pool)
    assert (
        "KIMINA_FW_FRONT_ADDRESS=172.29.54.250"
        in pool.path(f"{INSTANCE}/firewall.conf").read_text()
    )
    # With nothing changed since, a re-run recreates nothing, and does not even call `docker build`.
    recreated, built = pool.recreated(), len(lean_builds(pool))
    result = pool.bash("install.sh", "--instance", "demo", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        "is already built from these build files: not building it again" in result.stdout
        and len(lean_builds(pool)) == built
    )
    assert (
        pool.recreated() == recreated and pool.path(f"{INSTANCE}/tls/box.pem").read_bytes() == box
    )


def test_a_host_whose_docker0_address_cannot_be_read_is_still_upgraded_with_a_warning(
    tmp_path, today
):
    machine = today.copy_to(tmp_path / "host")
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_BRIDGE=""
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "could not read the docker0 bridge address" in result.stderr
    assert names_of(certificate(machine.path(f"{POOL}/certs/front.crt"))) == (
        {"workstation", "workstation.lan"},
        {"127.0.0.1"},
    )


# --- a config HAProxy refuses changes nothing -----------------------------------------------------------------------------


def test_a_config_haproxy_rejects_stops_the_upgrade_before_anything_that_runs_is_touched(
    tmp_path, today
):
    machine = today.copy_to(tmp_path / "host")
    mark = machine.mark()
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_HAPROXY_REJECTS="1"
    )
    assert result.returncode == 1 and "nothing that runs was changed" in result.stderr
    assert changing(machine.calls_since(mark)) == []
    assert "compose.tls.yaml" not in compose_files_of(machine)  # still the plain server
    assert not machine.path(f"{POOL}/haproxy/haproxy.cfg").exists()


def test_the_stand_in_refuses_what_stops_a_real_install(tmp_path, today, deploy):
    """The harness must be able to fail the way a real host does: take the `chmod 0755 "$work"` out of
    lean-pool-ctl and HAProxy's user can no longer enter the `mktemp -d` directory its candidate is in."""
    broken = copy_deploy(deploy, tmp_path / "deploy")
    control = broken / "lean-pool-ctl"
    assert control.read_text().count('  chmod 0755 "$work"\n') == 1
    control.write_text(control.read_text().replace('  chmod 0755 "$work"\n', ""))
    machine = today.copy_to(tmp_path / "host")
    machine.deploy = broken
    result = machine.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 1 and "Permission denied" in result.stderr
    assert changing(machine.calls) == changing(today.calls)  # and nothing was touched


# --- lean-pool-ctl on the TLS pool ------------------------------------------------------------------------------------------


def control(
    machine: Machine, *arguments: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    return machine.bash(
        machine.path("/usr/local/sbin/lean-pool-ctl"), "demo", *arguments, **environment
    )


def test_ca_prints_the_authoritys_certificate_and_nothing_private(pool):
    result = control(pool, "ca")
    assert (
        result.returncode == 0 and result.stdout == pool.path(f"{POOL}/pki/ca/ca.crt").read_text()
    )
    assert "PRIVATE KEY" not in result.stdout


def test_status_shows_the_days_left_on_every_certificate(pool):
    result = control(pool, "status")
    assert result.returncode == 0
    for name in ("ca.crt", "front.crt", "proxy-client.crt", "servers/workstation.crt"):
        assert re.search(rf"--- certificate {re.escape(name)}\s+\d+ days left", result.stdout), (
            result.stdout
        )
    assert re.search(r"ca\.crt\s+36\d\d days left", result.stdout) and re.search(
        r"front\.crt\s+18\d\d days left", result.stdout
    )


def test_add_backend_admits_through_mutual_tls_held_to_the_servers_name(pool):
    result = control(pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201")
    assert result.returncode == 0, result.stderr
    admission = shlex.split((pool.stub / "admissions").read_text().splitlines()[-1])
    assert admission[admission.index("--server") + 1] == "https://192.168.50.44:18001"
    assert admission[admission.index("--tls-server-name") + 1] == "laptop"
    assert admission[admission.index("--tls-ca-file") + 1] == "/run/pool/tls/ca.crt"
    assert admission[admission.index("--tls-client-pem") + 1] == "/run/pool/tls/proxy-client.pem"
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 6 18201"
    )
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert (
        "server laptop 192.168.50.44:18001 ssl verify required" in config
        and "verifyhost laptop" in config
    )
    assert pool.calls[-1] == reload_of(
        "lean-pool-demo-haproxy-1"
    )  # a reload: checks in flight are not dropped


@pytest.mark.parametrize(
    ("outcome", "status", "said"),
    [
        (
            "tls",
            4,
            "admission FAILED for 192.168.50.44:18001: TLS with https://192.168.50.44:18001: the server's certificate is "
            "for another name than 'laptop'; the pool is unchanged",
        ),
        (
            "misbehaved",
            4,
            "admission FAILED for 192.168.50.44:18001: 1 of 12 admission cases misbehaved (status 1): "
            "verify/sum_comm.lean expected accepted, observed undecided (Lean timeout); the pool is unchanged",
        ),
    ],
)
def test_a_failed_admission_says_what_kind_and_leaves_the_pool_byte_for_byte(
    pool, outcome, status, said
):
    servers = pool.path(f"{POOL}/servers").read_bytes()
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes()
    mark = pool.mark()
    result = control(
        pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201", STUB_ADMIT=outcome
    )
    assert result.returncode == status and said in result.stderr, result.stderr
    assert (
        pool.path(f"{POOL}/servers").read_bytes() == servers
        and pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    )
    assert changing(pool.calls_since(mark)) == []
    assert not list(pool.path(POOL).glob(".work.*"))


def test_a_name_or_address_the_list_already_has_is_refused_with_the_lists_reason(pool):
    servers = pool.path(f"{POOL}/servers").read_bytes()
    own = listed(pool)
    # The pool host's own name, in lower case as a name must now be given: it is in the list already (or, where
    # an older list wrote it with capitals, it differs from a listed name only in letter case and would share
    # that server's certificate). Refused either way, and the pool is unchanged.
    result = control(pool, "add-backend", own.lower(), "192.168.50.44:18001", "6")
    assert result.returncode == 5 and "the pool is unchanged" in result.stderr
    assert ("already in the list" in result.stderr) or ("different letter case" in result.stderr)
    taken = control(pool, "add-backend", "laptop", "172.29.55.1:18001", "6")
    assert taken.returncode == 5 and f"already in the list as '{own}'" in taken.stderr
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    # A name with a dot, a capital or an underscore is not a name a server can be ADDED under at all.
    for name in ("laptop.lan", "Laptop", "lap_top"):
        assert control(pool, "add-backend", name, "192.168.50.44:18001", "6").returncode == 2
    assert pool.path(f"{POOL}/servers").read_bytes() == servers


def test_admit_holds_a_server_to_its_listed_name_and_asks_for_one_it_does_not_know(pool):
    assert control(pool, "admit", "172.29.55.1:18001", "8").returncode == 0
    assert (
        "--tls-server-name workstation" in (pool.stub / "admissions").read_text().splitlines()[-1]
    )
    unknown = control(pool, "admit", "10.0.0.9:18001", "4")
    assert unknown.returncode == 2 and "its name is not known" in unknown.stderr
    assert control(pool, "admit", "10.0.0.9:18001", "4", "desk").returncode == 0
    assert "--tls-server-name desk" in (pool.stub / "admissions").read_text().splitlines()[-1]
    failed = control(pool, "admit", "10.0.0.9:18001", "4", "desk", STUB_ADMIT="tls")
    assert (
        failed.returncode == 3
        and "admission FAILED for 10.0.0.9:18001: TLS with https://10.0.0.9:18001" in failed.stderr
    )


def test_remove_backend_takes_a_server_and_its_certificate_copy_out(pool):
    assert (
        control(pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201").returncode == 0
    )
    pool.path(f"{POOL}/certs/servers/laptop.crt").write_text("a certificate")
    result = control(pool, "remove-backend", "laptop")
    assert result.returncode == 0 and "laptop is out of pool demo" in result.stdout
    assert (
        "laptop" not in pool.path(f"{POOL}/servers").read_text()
        and "laptop" not in pool.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    )
    assert not pool.path(f"{POOL}/certs/servers/laptop.crt").exists()
    last = control(pool, "remove-backend", listed(pool))  # a pool keeps at least one server
    assert (
        last.returncode == 5
        and f"{listed(pool)} 172.29.55.1:18001" in pool.path(f"{POOL}/servers").read_text()
    )
    assert pool.path(f"{POOL}/certs/servers/workstation.crt").exists()


# --- a paused server stays listed and is sent nothing ----------------------------------------------------------


def checker_line(machine: Machine, name: str) -> str:
    """The line of backend `checkers` that declares the server NAME in the installed config."""
    config = machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    (line,) = [
        line.strip() for line in config.splitlines() if line.strip().startswith(f"server {name} ")
    ]
    return line


def admissions(machine: Machine) -> list[str]:
    record = machine.stub / "admissions"
    return record.read_text().splitlines() if record.exists() else []


@pytest.fixture
def with_laptop(pool):
    """The pool with a second server, `laptop`, and the pool's copy of its certificate."""
    pool.own = listed(pool)
    assert (
        control(pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201").returncode == 0
    )
    pool.path(f"{POOL}/certs/servers/laptop.crt").write_text("a certificate")
    return pool


def test_pause_keeps_the_server_listed_with_its_certificate_and_declares_it_disabled(with_laptop):
    pool = with_laptop
    own_line, laptop_line, asked, mark = (
        checker_line(pool, pool.own),
        checker_line(pool, "laptop"),
        admissions(pool),
        pool.mark(),
    )
    result = control(pool, "pause", "laptop")
    assert result.returncode == 0 and "laptop is paused in pool demo" in result.stdout, (
        result.stderr
    )
    assert "sudo lean-pool-ctl demo resume laptop" in result.stdout  # how to bring it back
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 6 18201 paused"
    )
    assert pool.path(f"{POOL}/certs/servers/laptop.crt").read_text() == "a certificate"
    # Its line is the line it had with `disabled` where the agent check stood: an agent's `ready` would end
    # the maintenance. Its agent tunnel is gone; the other server's line and tunnel did not move.
    assert (
        checker_line(pool, "laptop")
        == re.sub(r"agent-check .*$", "disabled", laptop_line)
        != laptop_line
    )
    assert checker_line(pool, pool.own) == own_line
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    assert (
        "listen agent_tunnel_laptop" not in config and f"listen agent_tunnel_{pool.own}" in config
    )
    assert changing(pool.calls_since(mark)) == [
        reload_of("lean-pool-demo-haproxy-1")
    ]  # a reload: no check in flight is dropped
    assert admissions(pool) == asked  # pausing asks the server nothing
    assert not list(pool.path(POOL).glob(".work.*"))
    status = control(pool, "status")
    assert (
        "--- paused (listed, sent nothing; sudo lean-pool-ctl demo resume SERVER): laptop\n"
        in status.stdout
    )
    assert re.search(
        r"--- certificate servers/laptop\.crt", status.stdout
    )  # still reported with the others


def test_status_names_no_paused_server_when_none_is(with_laptop):
    assert "paused" not in control(with_laptop, "status").stdout


def test_resume_runs_the_admission_test_again_and_gives_the_pool_it_was_before_the_pause(
    with_laptop,
):
    pool = with_laptop
    servers = pool.path(f"{POOL}/servers").read_bytes()
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes()
    assert control(pool, "pause", "laptop").returncode == 0
    asked = admissions(pool)
    result = control(pool, "resume", "laptop")
    assert result.returncode == 0, result.stderr
    assert (
        "laptop (192.168.50.44:18001, 6 workers) takes checks again in pool demo" in result.stdout
    )
    assert len(admissions(pool)) == len(asked) + 1  # that server alone, once
    admission = shlex.split(admissions(pool)[-1])
    assert admission[admission.index("--server") + 1] == "https://192.168.50.44:18001"
    assert admission[admission.index("--tls-server-name") + 1] == "laptop"
    assert admission[admission.index("--concurrency") + 1] == "6"
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    assert pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    assert pool.calls[-1] == reload_of("lean-pool-demo-haproxy-1")
    assert not list(pool.path(POOL).glob(".work.*"))


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        (
            "tls",
            "admission FAILED for 192.168.50.44:18001: TLS with https://192.168.50.44:18001: the server's certificate is "
            "for another name than 'laptop'; laptop stays paused and the pool is unchanged",
        ),
        (
            "misbehaved",
            "admission FAILED for 192.168.50.44:18001: 1 of 12 admission cases misbehaved (status 1): "
            "verify/sum_comm.lean expected accepted, observed undecided (Lean timeout); "
            "laptop stays paused and the pool is unchanged",
        ),
    ],
)
def test_a_server_that_fails_admission_stays_paused_and_the_pool_is_byte_for_byte(
    with_laptop, outcome, said
):
    pool = with_laptop
    assert control(pool, "pause", "laptop").returncode == 0
    servers = pool.path(f"{POOL}/servers").read_bytes()
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes()
    mark = pool.mark()
    result = control(pool, "resume", "laptop", STUB_ADMIT=outcome)
    assert result.returncode == 4 and said in result.stderr, result.stderr
    assert (
        pool.path(f"{POOL}/servers").read_bytes() == servers
        and pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    )
    assert changing(pool.calls_since(mark)) == []
    assert not list(pool.path(POOL).glob(".work.*"))


def test_a_pause_or_resume_the_list_refuses_changes_nothing_and_asks_no_server(with_laptop):
    pool = with_laptop
    assert control(pool, "pause", "laptop").returncode == 0
    servers = pool.path(f"{POOL}/servers").read_bytes()
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes()
    asked, mark = admissions(pool), pool.mark()
    for arguments, reason in [
        (("pause", pool.own), "would leave no server taking checks: a pool keeps at least one"),
        (("pause", "laptop"), "'laptop' is already paused"),
        (("pause", "desk"), "no server is named 'desk'"),
        (("resume", "desk"), "no server is named 'desk'"),
        (
            ("resume", pool.own),
            f"'{pool.own}' is not paused",
        ),  # and no admission test is run for it
    ]:
        result = control(pool, *arguments)
        assert (
            result.returncode == 5
            and reason in result.stderr
            and "the pool is unchanged" in result.stderr
        ), result.stderr
    # The last server taking checks cannot be removed either while the rest are paused: the list would hold
    # paused servers only, which is refused as an empty list is.
    assert control(pool, "remove-backend", pool.own).returncode == 5
    assert (
        pool.path(f"{POOL}/servers").read_bytes() == servers
        and pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    )
    assert pool.path(f"{POOL}/certs/servers/workstation.crt").exists()
    assert changing(pool.calls_since(mark)) == [] and admissions(pool) == asked
    assert not list(pool.path(POOL).glob(".work.*"))


def test_a_paused_server_is_removed_with_the_same_command_as_any_other(with_laptop):
    pool = with_laptop
    assert control(pool, "pause", "laptop").returncode == 0
    assert control(pool, "remove-backend", "laptop").returncode == 0
    assert (
        "laptop" not in pool.path(f"{POOL}/servers").read_text()
        and "laptop" not in pool.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    )
    assert not pool.path(f"{POOL}/certs/servers/laptop.crt").exists()
    assert "paused" not in control(pool, "status").stdout


def test_a_pause_outlives_a_later_install_and_a_reboot(with_laptop):
    pool = with_laptop
    assert control(pool, "pause", "laptop").returncode == 0
    servers, line = pool.path(f"{POOL}/servers").read_bytes(), checker_line(pool, "laptop")
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert control(pool, "render").returncode == 0
    pool.restart_daemon()
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    assert checker_line(pool, "laptop") == line and line.endswith(" disabled")
    assert pool.path(f"{POOL}/certs/servers/laptop.crt").read_text() == "a certificate"


# --- what the pool tells a Prometheus scraper ------------------------------------------------------------------

FACTS = f"{POOL}/haproxy/pool.metrics"
ASKED_FOR_THE_EXPORTER = "docker run --rm --network none haproxy:3.0 haproxy -vv"


def facts(machine: Machine) -> dict[str, int]:
    """The pool's installed facts for a scraper: every sample of the file beside haproxy.cfg, by name and labels."""
    lines = [
        line
        for line in machine.path(FACTS).read_text().splitlines()
        if line and not line.startswith("#")
    ]
    return {sample: int(value) for sample, _, value in (line.rpartition(" ") for line in lines)}


def last_moment(path: Path) -> int:
    """The end of a certificate's validity as `cryptography` reads it, in seconds since 1970."""
    return int(certificate(path).not_valid_after_utc.timestamp())


def test_the_upgrade_asked_the_haproxy_image_for_its_exporter_before_it_built_or_wrote_anything(
    upgraded,
):
    calls = upgraded.calls
    pulled, asked = calls.index("docker pull -q haproxy:3.0"), calls.index(ASKED_FOR_THE_EXPORTER)
    built = next(index for index, call in enumerate(calls) if call.startswith("docker build "))
    assert pulled < asked < built and asked < calls.index(changing(calls)[0])
    assert calls.count(ASKED_FOR_THE_EXPORTER) == 1


def test_the_upgrade_wrote_the_pools_facts_beside_the_config_and_the_config_answers_a_scraper_with_them(
    upgraded,
):
    machine = upgraded.machine
    config = machine.path(f"{POOL}/haproxy/haproxy.cfg").read_text()
    # The three paths, GET only and without the key: HAProxy's own exporter, the cache's counters from the
    # cache server alone, and the file beside the config (found there wherever the two are mounted).
    assert "    http-request use-service prometheus-exporter if METH_GET is_metrics\n" in config
    assert "    use_backend cache_metrics if METH_GET is_cache_metrics\n" in config
    assert (
        "\nbackend cache_metrics\n" in config
        and "    server cache 127.0.0.1:18102 track cache/cache\n" in config
    )
    assert (
        " file pool.metrics if METH_GET is_pool_metrics\n" in config
        and "    default-path config\n" in config
    )
    # The file: public, as the config beside it is, in the directory the proxy mounts.
    assert (
        machine.owner(FACTS)
        == ("root", "root", 0o644)
        == machine.owner(f"{POOL}/haproxy/haproxy.cfg")
    )
    assert "./haproxy:/usr/local/etc/haproxy:ro" in machine.path(f"{POOL}/compose.yaml").read_text()
    # What it says: the one server of the list, as listed, and the last moment of each of the pool's four
    # public certificates, under the names `status` gives them.
    own, workers = machine.path(f"{POOL}/servers").read_text().splitlines()[-1].split()[0:3:2]
    names = ("ca.crt", "front.crt", "proxy-client.crt", "servers/workstation.crt")
    assert facts(machine) == {
        f'leanpool_server_listed_workers{{server="{own}"}}': int(workers),
        f'leanpool_server_paused{{server="{own}"}}': 0,
        **{
            f'leanpool_certificate_not_after_seconds{{certificate="{name}"}}': last_moment(
                machine.path(f"{POOL}/certs/{name}")
            )
            for name in names
        },
    }
    # No key, no address and no path of this host is in it.
    text = machine.path(FACTS).read_text()
    for hidden in (
        machine.path(f"{POOL}/api-key").read_text().strip(),
        "172.29.55.1",
        "18001",
        "/srv",
        "PRIVATE",
    ):
        assert hidden not in text
    # HAProxy judged the candidate with the facts beside it, and nothing is left of the candidate.
    assert not list(machine.path(POOL).glob(".work.*"))


def built_images(machine: Machine) -> list[str]:
    """The images the stand-in holds, but HAProxy's: the installer pulls that one before it asks it anything."""
    state = machine.stub / "state"
    return sorted(
        path.read_text() for path in state.glob("built.*") if path.name != "built.haproxy_3.0"
    )


def test_an_haproxy_image_without_the_exporter_stops_the_installer_with_nothing_changed(
    pool, today, tmp_path
):
    """The pool's config answers GET /metrics with HAProxy's own exporter, and an HAProxy built
    without it refuses that config. So the image is asked first: on the TLS pool and on the plain one alike,
    the installer stops before it builds, writes or touches anything."""
    for machine in (pool, today.copy_to(tmp_path / "plain")):
        had_a_pool = machine.path(POOL).exists()
        before = snapshot(machine.path("/srv"))
        scripts = snapshot(machine.path("/usr/local"))
        containers = {name: machine.container_id(name) for name in EVERY_CONTAINER}
        images = built_images(machine)
        mark = machine.mark()
        result = machine.bash(
            "install.sh",
            "--proxy",
            "demo",
            "--admit-cases",
            ADMIT_CASES,
            STUB_HAPROXY_NO_EXPORTER="1",
        )
        assert result.returncode == 1, result.stdout[-2000:]
        assert (
            "the HAProxy in haproxy:3.0 was built without its Prometheus exporter" in result.stderr
        )
        assert (
            "Nothing was changed: no image was built, no file was written, and no container or control script was touched."
            in result.stderr
        )
        calls = machine.calls_since(mark)
        assert calls[-1] == ASKED_FOR_THE_EXPORTER  # the last thing it did was ask
        assert changing(calls) == [] and not any(call.startswith("docker build") for call in calls)
        assert not any("leanpool-" in call or " compose " in call for call in calls)
        assert (
            snapshot(machine.path("/srv")) == before
            and snapshot(machine.path("/usr/local")) == scripts
        )
        assert {name: machine.container_id(name) for name in EVERY_CONTAINER} == containers
        assert built_images(machine) == images
        if had_a_pool:
            assert (
                control(machine, "check").returncode == 0
            )  # the pool that was there still answers


def test_a_pause_and_a_resume_leave_the_facts_agreeing_with_the_list(with_laptop):
    """The server list is the single source: the facts are written from it by the render that writes the config,
    and installed with it. A file that is no certificate has no sample, and the render says so and goes on."""
    pool = with_laptop
    laptop, own = (
        'leanpool_server_paused{server="laptop"}',
        f'leanpool_server_paused{{server="{pool.own}"}}',
    )
    assert (
        facts(pool)[laptop] == 0
        and facts(pool)['leanpool_server_listed_workers{server="laptop"}'] == 6
    )
    certificates = {
        sample: value
        for sample, value in facts(pool).items()
        if sample.startswith("leanpool_certificate_")
    }
    assert len(certificates) == 4
    paused = control(pool, "pause", "laptop")
    assert paused.returncode == 0, paused.stderr
    assert (facts(pool)[laptop], facts(pool)[own]) == (1, 0)
    assert " paused" in pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
    # `with_laptop` put a file that is no certificate where the laptop's would be.
    assert (
        "warning: certificate servers/laptop.crt has no sample in /metrics/pool: it holds no PEM certificate"
        in paused.stderr
    )
    assert {
        sample: value
        for sample, value in facts(pool).items()
        if sample.startswith("leanpool_certificate_")
    } == certificates
    resumed = control(pool, "resume", "laptop")
    assert resumed.returncode == 0, resumed.stderr
    assert (facts(pool)[laptop], facts(pool)[own]) == (0, 0)
    assert " paused" not in pool.path(f"{POOL}/servers").read_text()
    # A reload each time, after both files were in place: the facts first, then the config that names them.
    assert pool.calls[-1] == reload_of("lean-pool-demo-haproxy-1") and not list(
        pool.path(POOL).glob(".work.*")
    )


@pytest.mark.parametrize(
    ("arguments", "environment", "status"),
    [
        (("add-backend", "desk", "192.168.50.45:18001", "4"), {"STUB_ADMIT": "tls"}, 4),
        (("add-backend", "desk", "192.168.50.45:18001", "4"), {"STUB_HAPROXY_REJECTS": "1"}, 5),
        (("pause", "laptop"), {"STUB_HAPROXY_REJECTS": "1"}, 5),
        (("remove-backend", "laptop"), {"STUB_HAPROXY_REJECTS": "1"}, 5),
        (("render",), {"STUB_HAPROXY_REJECTS": "1"}, 5),
    ],
)
def test_a_change_that_is_refused_leaves_the_facts_byte_for_byte(
    with_laptop, arguments, environment, status
):
    pool = with_laptop
    before, mark = snapshot(pool.path(POOL)), pool.mark()
    result = control(pool, *arguments, **environment)
    assert result.returncode == status, result.stderr
    assert snapshot(pool.path(POOL)) == before and changing(pool.calls_since(mark)) == []


@pytest.mark.parametrize(
    ("fault", "said"),
    [
        (
            '  chmod 0600 "$work/$POOL_METRICS"\n',
            "Permission denied",
        ),  # a file HAProxy's user cannot read
        (
            '  rm -f "$work/$POOL_METRICS"\n',
            "No such file or directory",
        ),  # a config that names a file that is not there
    ],
)
def test_the_stand_in_refuses_a_config_whose_facts_haproxy_could_not_read(
    pool, deploy, tmp_path, fault, said
):
    """HAProxy reads the file an answer is made of when it reads the config, and refuses the config without it.
    The harness must be able to fail that way: here the control script spoils the candidate's file just before
    HAProxy judges it."""
    broken = copy_deploy(deploy, tmp_path / "deploy")
    script = broken / "lean-pool-ctl"
    judged = '  docker run --rm --network none -v "$work":/cfg:ro'
    assert script.read_text().count(judged) == 1
    script.write_text(script.read_text().replace(judged, fault + judged))
    before, mark = snapshot(pool.path(POOL)), pool.mark()
    result = pool.bash(script, "demo", "render")
    assert (
        result.returncode == 5 and said in result.stderr and "nothing changed" in result.stderr
    ), result.stderr
    assert snapshot(pool.path(POOL)) == before and changing(pool.calls_since(mark)) == []


# --- the worker count a server is listed with ------------------------------------------------------------------


def without_weight(line: str) -> str:
    return re.sub(r" weight \d+", "", line)


def test_set_workers_changes_the_listed_count_with_one_reload_and_no_admission_test(with_laptop):
    pool = with_laptop
    own_line, laptop_line, asked, mark = (
        checker_line(pool, pool.own),
        checker_line(pool, "laptop"),
        admissions(pool),
        pool.mark(),
    )
    assert " maxconn 6 weight 192 " in laptop_line and " maxconn 8 weight 256 " in own_line
    told = facts(pool)
    assert told['leanpool_server_listed_workers{server="laptop"}'] == 6
    result = control(pool, "set-workers", "laptop", "4")
    assert (
        result.returncode == 0 and "laptop is listed with 4 workers in pool demo" in result.stdout
    ), result.stderr
    assert "Its box was not touched" in result.stdout
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 4 18201"
    )
    # Its line is the line it had, with the limit and the weight of 4 workers; the other server's did not move.
    assert checker_line(pool, "laptop") == laptop_line.replace(
        " maxconn 6 weight 192 ", " maxconn 4 weight 128 "
    )
    assert checker_line(pool, pool.own) == own_line
    assert changing(pool.calls_since(mark)) == [
        reload_of("lean-pool-demo-haproxy-1")
    ]  # one reload: no check is dropped
    assert admissions(pool) == asked  # the server is asked nothing
    assert pool.path(f"{POOL}/certs/servers/laptop.crt").read_text() == "a certificate"
    assert not list(pool.path(POOL).glob(".work.*"))
    # The pool's facts for a scraper are written with the same render: 4 for that server, and
    # every other sample as it was.
    assert facts(pool) == {**told, 'leanpool_server_listed_workers{server="laptop"}': 4}
    # Above the largest server: weights are scaled by the largest, so the OTHER server's weight moves, and
    # nothing else of its line does.
    assert control(pool, "set-workers", "laptop", "16").returncode == 0
    assert " maxconn 16 weight 256 " in checker_line(pool, "laptop")
    assert " maxconn 8 weight 128 " in checker_line(pool, pool.own)
    assert without_weight(checker_line(pool, pool.own)) == without_weight(own_line)


def test_set_workers_leaves_a_paused_server_paused(with_laptop):
    pool = with_laptop
    assert control(pool, "pause", "laptop").returncode == 0
    line, asked = checker_line(pool, "laptop"), admissions(pool)
    assert control(pool, "set-workers", "laptop", "4").returncode == 0
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 4 18201 paused"
    )
    assert checker_line(pool, "laptop") == line.replace(
        " maxconn 6 weight 192 ", " maxconn 4 weight 128 "
    )
    assert checker_line(pool, "laptop").endswith(" disabled") and admissions(pool) == asked
    # Its resume is then tested at the count it now has.
    assert control(pool, "resume", "laptop").returncode == 0
    admission = shlex.split(admissions(pool)[-1])
    assert admission[admission.index("--concurrency") + 1] == "4"


def test_a_set_workers_the_list_refuses_changes_nothing_and_reloads_nothing(with_laptop):
    pool = with_laptop
    servers = pool.path(f"{POOL}/servers").read_bytes()
    config = pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes()
    told = pool.path(FACTS).read_bytes()
    asked, mark = admissions(pool), pool.mark()
    for arguments, reason in [
        (("desk", "4"), "no server is named 'desk'"),
        (("laptop", "6"), "'laptop' is already listed with 6 workers"),
    ]:
        result = control(pool, "set-workers", *arguments)
        assert (
            result.returncode == 5
            and reason in result.stderr
            and "the pool is unchanged" in result.stderr
        ), result.stderr
    for malformed in [
        ("laptop", "0"),
        ("laptop", "257"),
        ("laptop",),
        ("laptop", "4", "5"),
        ("laptop", "four"),
    ]:
        assert (
            control(pool, "set-workers", *malformed).returncode == 2
        )  # refused before anything is read
    assert (
        pool.path(f"{POOL}/servers").read_bytes() == servers
        and pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    )
    assert pool.path(FACTS).read_bytes() == told
    assert changing(pool.calls_since(mark)) == [] and admissions(pool) == asked
    assert not list(pool.path(POOL).glob(".work.*"))


def test_the_door_hands_set_workers_over_and_status_reads_the_new_count(with_laptop):
    pool = with_laptop
    door = pool.path("/usr/local/bin/kimina-ctl")
    result = pool.bash(door, SSH_ORIGINAL_COMMAND="@proxy-demo set-workers laptop 4")
    assert (
        result.returncode == 0 and "laptop is listed with 4 workers in pool demo" in result.stdout
    ), result.stderr
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 4 18201"
    )
    refused = pool.bash(door, SSH_ORIGINAL_COMMAND="@proxy-demo set-workers laptop four")
    assert refused.returncode == 2 and "refused" in refused.stderr
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1]
        == "laptop 192.168.50.44:18001 4 18201"
    )


def test_the_instances_own_status_shows_its_front_and_instance_one_has_none(pool):
    """kimina-ctl is one script for every instance: instance 1's status must read as it did."""
    door = pool.path("/usr/local/bin/kimina-ctl")
    member = pool.bash(door, "@demo", "status")
    assert member.returncode == 0, member.stderr
    assert re.search(
        r"--- tls front: running as 'workstation' on ports 18001 and 18201; its certificate has 18\d\d days left",
        member.stdout,
    )
    first = pool.bash(door, "status")
    assert (
        first.returncode == 0
        and "tls front" not in first.stdout
        and "--- settings: port=18000" in first.stdout
    )


# --- the Lean server has an init process, and `status` shows its zombies -------------------------------------

LEAN = "lean-server-demo-kimina-1"
AGENT = "lean-server-demo-agent-1"


def status(
    machine: Machine, *instance: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    """`kimina-ctl [@NAME] status`, with the control script that is installed on the machine."""
    return machine.bash(
        machine.path("/usr/local/bin/kimina-ctl"), *instance, "status", **environment
    )


def usage_line(result: subprocess.CompletedProcess[str]) -> str:
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("--- usage:")]
    return line


def before_the_init_process(deploy: Path, directory: Path) -> Path:
    """The installers as they were before the init process: the Lean server's compose file without
    `init: true`."""
    directory = copy_deploy(deploy, directory)
    compose = directory.parent / "lean-server" / "compose.yaml"
    assert compose.read_text().count("\n    init: true\n") == 1
    compose.write_text(compose.read_text().replace("\n    init: true\n", "\n"))
    return directory


def this_process() -> dict[str, str]:
    """For Docker's stand-in: the container's first process is this test process, and the container's ID is the
    control group this process really is in. `status` looks that process up in /proc itself, and agrees."""
    if sys.platform != "linux":
        pytest.skip("status reads the host's /proc and its process table, as Linux has them")
    return {
        "STUB_FIRST_PID": str(os.getpid()),
        "STUB_CONTAINER_ID": Path("/proc/self/cgroup").read_text().splitlines()[-1].strip(),
    }


def test_status_counts_the_containers_zombies_at_the_end_of_its_usage_line(pool):
    mark = pool.mark()
    result = status(pool, "@demo", STUB_ZOMBIES="7", STUB_PIDS="437", **this_process())
    assert result.returncode == 0, result.stderr
    # The line is what it was, with the count added at its END: what reads `pids N` from it still does.
    assert usage_line(result) == "--- usage: cpu 412.50%  mem 5.120GiB / 24GiB  pids 437  zombies 7"
    assert (pids := re.search(r"pids ([0-9]+)", usage_line(result))) and pids.group(1) == "437"
    # Seven: the container's own, written `Z`, `Zs` and `Z+`. Not its live processes, and not the two zombies
    # in the host's table that belong to another PID namespace.
    calls = pool.calls_since(mark)
    assert f"docker inspect -f {{{{.State.Pid}}}} {{{{.Id}}}} {LEAN}" in calls
    # Asked of the host. Nothing is started in the Lean server's container (it could not be, at its process
    # limit), and `docker top` is not used (it shows no zombie: tests/install/test_install_reaping.py).
    # The one `docker exec` of a status is in the usage agent's own container, to hear what it says.
    assert not any(
        call.startswith("docker top")
        or (call.startswith("docker exec") and f" {LEAN} " in f"{call} ")
        for call in calls
    )
    assert [call for call in calls if call.startswith("docker exec")] == [
        "docker exec -i lean-server-demo-agent-1 python -"
    ]
    # No zombie is a number too.
    none = status(pool, "@demo", STUB_ZOMBIES="0", **this_process())
    assert usage_line(none) == "--- usage: cpu 412.50%  mem 5.120GiB / 24GiB  pids 83  zombies 0"


@pytest.mark.parametrize(
    "fault",
    [
        {"STUB_PS_FAILS": "1"},  # the host's process table cannot be read
        {"STUB_FIRST_PID": ""},  # Docker does not say where the container's first process is
        {"STUB_FIRST_PID": "0"},  # what Docker says of a container that is not running
        {"STUB_CONTAINER_ID": ""},  # nor which container it is
        # Docker runs under another kernel than this shell (Docker Desktop). The PID it names is somebody else's
        # here: a process whose control group is not the container's. Its namespace's zombies are not counted.
        {"STUB_CONTAINER_ID": "0f" * 32},
        {"STUB_FIRST_PID": "999999999"},  # ... or nobody's
    ],
)
def test_a_zombie_count_that_cannot_be_read_is_a_question_mark_and_status_goes_on(pool, fault):
    result = status(pool, "@demo", STUB_ZOMBIES="7", **{**this_process(), **fault})
    assert result.returncode == 0, result.stderr
    assert usage_line(result) == "--- usage: cpu 412.50%  mem 5.120GiB / 24GiB  pids 83  zombies ?"
    # Every line after it is still there.
    assert (
        "--- settings: port=18001" in result.stdout
        and "--- tls front: running as 'workstation'" in result.stdout
    )


def test_a_container_that_is_not_there_gets_dockers_own_words_and_no_count(pool):
    assert (
        subprocess.run(
            [str(pool.stub / "bin" / "docker"), "rm", "-f", LEAN], env=pool.environment()
        ).returncode
        == 0
    )
    result = status(pool, "@demo", STUB_ZOMBIES="7", **this_process())
    assert result.returncode == 0
    assert f"Error response from daemon: No such container: {LEAN}" in result.stdout
    assert "zombies" not in result.stdout and "--- init process: ?" in result.stdout
    assert "--- settings: port=18001" in result.stdout


def test_the_zombie_count_is_read_from_the_real_process_table(pool):
    """The table above is what `ps` is believed to print. Here it IS ps, on real zombies: the "container" is the
    PID namespace and the control group this test runs in (Docker's stand-in names this very process as its
    first, and that group as its ID), so the count is every zombie of that namespace this user may see, the
    four made here among them."""
    here = this_process()
    own = subprocess.run(
        ["ps", "-o", "pidns=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    if own.returncode != 0 or not own.stdout.strip().isdigit():
        pytest.skip("this machine's ps does not show PID namespaces")
    children = [subprocess.Popen(["true"]) for _ in range(4)]
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and any(
            Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
            for child in children
        ):
            time.sleep(0.01)
        result = status(pool, "@demo", **here)
        assert result.returncode == 0, result.stderr
        counted = re.fullmatch(r"--- usage: .*  pids 83  zombies ([0-9]+)", usage_line(result))
        assert counted and int(counted.group(1)) >= 4, usage_line(result)
        # A process that is not in the control group Docker names is not taken for the container's first.
        other = status(pool, "@demo", **{**here, "STUB_CONTAINER_ID": "0f" * 32})
        assert usage_line(other).endswith("  pids 83  zombies ?")
    finally:
        for child in children:
            child.wait()


def test_status_says_whether_the_container_has_an_init_process(pool):
    member = status(pool, "@demo")
    assert (
        member.returncode == 0 and "--- init process: yes" in member.stdout
    )  # made by the upgrade, from today's file
    assert "--- init process: yes" in status(pool).stdout


def test_a_pool_refresh_makes_this_hosts_lean_server_again_once_when_its_compose_file_changed(
    pool, deploy, tmp_path
):
    """A refresh (`install.sh --proxy`) installs the Lean server's
    compose file and brings the service up: a server made from another file is made again, once, and that is
    how the pool's own host gets the init process. It builds no image and makes no other container again but
    the usage agent, once, whose settings now carry the newer installer's version."""
    # A host installed before the init process: its Lean server made from a file without the line.
    pool.deploy = before_the_init_process(deploy, tmp_path / "before")
    assert pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
    assert "--- init process: NO" in status(pool, "@demo").stdout
    lean, recreated, builds, mark = (
        pool.container_id(LEAN),
        pool.recreated(),
        len(lean_builds(pool)),
        pool.mark(),
    )
    others = {
        name: pool.container_id(name) for name in EVERY_CONTAINER if name not in (LEAN, AGENT)
    }
    # Today's files.
    pool.deploy = deploy
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0 and "POOL INSTALLED AND VERIFIED" in result.stdout, (
        result.stdout[-3000:] + result.stderr[-3000:]
    )
    assert pool.recreated() == [*recreated, LEAN, AGENT] and pool.container_id(LEAN) not in (
        "",
        lean,
    )
    assert {name: pool.container_id(name) for name in others} == others
    assert len(lean_builds(pool)) == builds  # a refresh never builds the Lean image
    compose = pool.path(f"{INSTANCE}/compose.yaml").read_text()
    assert "\n    init: true\n" in compose and "compose.tls.yaml" in compose_files_of(pool)
    assert "--- init process: yes" in status(pool, "@demo").stdout
    # Whether this Docker can start such a container was asked first, with a container that does nothing and
    # the server's own hardening, before anything that runs was touched.
    calls = pool.calls_since(mark)
    asked = next(
        index for index, call in enumerate(calls) if call.startswith("docker run --rm --init ")
    )
    assert calls[asked] == (
        "docker run --rm --init --cap-drop ALL --security-opt no-new-privileges:true --network none "
        "--entrypoint true acme/kimina:lean-demo-1"
    )
    assert asked < calls.index(changing(calls)[0])
    # And once is once: the same refresh again makes nothing again.
    recreated = pool.recreated()
    assert (
        pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
        and pool.recreated() == recreated
    )


def test_a_docker_that_cannot_give_a_container_an_init_process_stops_both_installers_before_anything_changes(
    pool, deploy, tmp_path
):
    """Docker puts its own docker-init into a container with `init: true`; a Docker that has none refuses to
    START it. Without this check the running server would be removed and its replacement would not start."""
    pool.deploy = before_the_init_process(deploy, tmp_path / "before")
    assert pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
    pool.deploy = deploy
    lean, mark = pool.container_id(LEAN), pool.mark()
    compose = pool.path(f"{INSTANCE}/compose.yaml").read_bytes()
    for arguments in (
        ("--proxy", "demo", "--admit-cases", ADMIT_CASES),
        ("--instance", "demo", "--no-door"),
    ):
        result = pool.bash("install.sh", *arguments, STUB_NO_INIT="1")
        assert result.returncode == 1, result.stdout[-2000:]
        assert (
            "this host's Docker cannot start acme/kimina:lean-demo-1 with an init process"
            in result.stderr
        )
        assert (
            'exec: "docker-init": executable file not found in $PATH' in result.stderr
        )  # Docker's own words
        assert "Nothing was changed: a running server is as it was." in result.stderr
    assert (
        changing(pool.calls_since(mark)) == []
        and pool.container_id(LEAN) == lean
        and pool.running(LEAN)
    )
    assert pool.path(f"{INSTANCE}/compose.yaml").read_bytes() == compose


# --- a join, over real HTTPS ------------------------------------------------------------------------------------------------

JOIN_COMMAND = re.compile(
    r"^  curl -fsSk --pinnedpubkey '(sha256//[A-Za-z0-9+/]{43}=)' https://([A-Za-z0-9.-]+):(\d+)/j/([A-Za-z0-9_-]{22}) \| sudo bash$"
)
# What the pool host's resolver and the proxy's both know: `box-a.lan` is the machine at 127.0.0.1.
THE_LAN_KNOWS_BOX_A = {
    "STUB_HOST_LOOKUP": "box-a.lan 127.0.0.1",
    "STUB_PROXY_LOOKUP": "box-a.lan 127.0.0.1",
}


class Window:
    """A join window on the pool's machine: `lean-pool-ctl INSTANCE join-open`, waiting in the foreground.

    INSTANCE is the pool's (`demo` here); a site's own tests open a window on theirs."""

    def __init__(
        self,
        machine: Machine,
        minutes: str = "5",
        join_host: str = "127.0.0.1",
        *,
        instance: str = "demo",
        **environment: str,
    ) -> None:
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            self.port = free.getsockname()[1]
        self.instance, self.pool = instance, f"/srv/lean-pool-{instance}"
        settings = machine.path(f"{self.pool}/pool.env")
        settings.write_text(
            re.sub(
                r"(?m)^LEANPOOL_JOIN_PUBLIC_HOST=.*$",
                f"LEANPOOL_JOIN_PUBLIC_HOST={join_host}",
                re.sub(
                    r"(?m)^LEANPOOL_JOIN_PUBLIC_PORT=.*$",
                    f"LEANPOOL_JOIN_PUBLIC_PORT={self.port}",
                    settings.read_text(),
                ),
            )
        )
        self.machine = machine
        self.process = machine.start(
            str(machine.path("/usr/local/sbin/lean-pool-ctl")),
            instance,
            "join-open",
            "--minutes",
            minutes,
            **environment,
        )
        self.lines: list[str] = []
        self.pin = self.url = self.token = ""

    def wait_for_the_command(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))
            found = JOIN_COMMAND.match(self.lines[-1])
            if found:
                self.pin, self.host, port, self.token = found.groups()
                self.url = f"https://127.0.0.1:{port}/j/{self.token}"  # the service listens here, whatever it is called
            if "Waiting here for the box" in line:
                break
        assert self.url, "\n".join(self.lines)

    def finish(self, timeout: int = 120) -> int:
        rest, _ = self.process.communicate(timeout=timeout)
        self.lines += rest.splitlines()
        return self.process.returncode

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def service_runs(self) -> bool:
        holder = self.machine.stub / "state" / f"pid.lean-pool-{self.instance}-join"
        if not holder.exists():
            return False
        try:
            os.kill(int(holder.read_text()), 0)
            return True
        except OSError:
            return False

    def left_behind(self) -> list[str]:
        """What a window leaves in the pool's directory once it has ended: only its lock file."""
        return sorted(
            path.name
            for path in self.machine.path(self.pool).iterdir()
            if path.name.startswith(".join") or path.name.startswith(".work")
        )


def fetch(pin: str, url: str) -> subprocess.CompletedProcess[str]:
    """The first half of the printed command, with the real curl."""
    return subprocess.run(
        ["curl", "-fsSk", "--pinnedpubkey", pin, url], capture_output=True, text=True, timeout=30
    )


def join(
    box: Machine, window: Window, *options: str, **environment: str
) -> subprocess.CompletedProcess[str]:
    """The printed command on the new box: the real curl, the real script on bash's standard input."""
    script = fetch(window.pin, window.url)
    assert script.returncode == 0 and script.stdout.splitlines()[-1] == 'main "$@"', script.stderr
    return subprocess.run(
        ["bash", "-s", "--", *options],
        input=script.stdout,
        capture_output=True,
        text=True,
        timeout=180,
        env=box.environment(**environment),
        cwd=box.directory,
    )


def ask(window: Window, path: str, *options: str) -> tuple[str, str]:
    """One pinned request to the window's service, with the real curl: (HTTP status, body)."""
    result = subprocess.run(
        [
            "curl",
            "-sSk",
            "--pinnedpubkey",
            window.pin,
            "-w",
            "\n%{http_code}",
            *options,
            window.url + path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    body, _, status = result.stdout.rpartition("\n")
    return status, body


def ask_to_join(
    window: Window, directory: Path, name: str, csr: str | None = None
) -> tuple[str, str]:
    """What a box sends, without the join script: a signing request under `name`. Returns the pool's answer to
    it, (status, body) of `GET /certificate` once it is no longer 204."""
    if csr is None:
        made = subprocess.run(
            [
                str(window.machine.stub / "bin" / "leanpool-pki"),
                "csr",
                "--out-dir",
                str(directory / "key"),
                "--name",
                name,
                "--file-stem",
                "box",
            ],
            capture_output=True,
            text=True,
        )
        assert made.returncode == 0, made.stderr
        csr = (directory / "key" / "box.csr").read_text()
    body = directory / "csr.json"
    body.write_text(
        json.dumps(
            {"name": name, "lean_port": 18001, "agent_port": 18201, "workers": 2, "csr": csr}
        )
    )
    status, reply = ask(
        window, "/csr", "-H", "Content-Type: application/json", "--data-binary", f"@{body}"
    )
    assert status == "202", reply
    for _ in range(400):
        status, reply = ask(window, "/certificate")
        if status != "204":
            break
        time.sleep(0.05)
    return status, reply


@pytest.fixture
def joining(tmp_path, pool, deploy):
    """A pool with its join window open, and a new box that has nothing but Docker."""
    pytest.importorskip("aiohttp")
    if shutil.which("curl") is None:
        pytest.skip("curl is needed for a join over real HTTPS")
    windows: list[Window] = []

    def open_window(join_host: str = "127.0.0.1", **environment: str) -> Window:
        windows.append(Window(pool, join_host=join_host, **{**THE_LAN_KNOWS_BOX_A, **environment}))
        windows[-1].wait_for_the_command()
        return windows[-1]

    yield SimpleNamespace(pool=pool, box=Machine(tmp_path / "box", deploy), open=open_window)
    for window in windows:
        if window.process.poll() is None:
            window.process.send_signal(signal.SIGINT)
            window.process.wait(timeout=30)


def test_a_box_joins_over_https_with_no_ssh_and_serves_under_its_own_certificate(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-a", "--workers", "2")
    code = window.finish()
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:] + window.output
    assert code == 0, window.output
    # The box: told it joined, and how to leave.
    assert (
        "JOINED: box-a (box-a.lan:18001, 2 workers) is serving checks in pool demo: all 12 admission cases behaved"
        in result.stdout
    )
    assert "sudo lean-pool-ctl demo remove-backend box-a" in result.stdout
    # The pool: the request, a certificate for that name only, the address by name, admission, then the list.
    assert (
        "127.0.0.1 asks to join as 'box-a': 2 workers, Lean port 18001, usage port 18201"
        in window.output
    )
    assert (
        "'box-a' is ready at box-a.lan:18001 (box-a.lan resolves to 127.0.0.1, here and for the proxy)"
        in window.output
    )
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )
    admission = (pool.stub / "admissions").read_text().splitlines()[-1]
    assert (
        "--server https://box-a.lan:18001" in admission and "--tls-server-name box-a" in admission
    )
    # What the box holds: its own key (made there), a certificate the pool's authority signed for its name
    # and nothing else, the pool's key, and a front that only the proxy passes.
    pem = box.path(f"{INSTANCE}/tls/box.pem").read_bytes()
    issued = x509.load_pem_x509_certificate(pem)
    key = serialization.load_pem_private_key(
        pem[pem.index(b"-----BEGIN PRIVATE KEY-----") :], password=None
    )
    der = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert issued.public_key().public_bytes(*der) == key.public_key().public_bytes(*der)
    assert names_of(issued) == ({"box-a"}, set()) and usages(issued) == [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    assert issued.issuer == certificate(pool.path(f"{POOL}/certs/ca.crt")).subject
    assert box.owner(f"{INSTANCE}/tls/box.pem") == ("root", "99", 0o640) and box.owner(
        f"{INSTANCE}/tls"
    ) == ("root", "99", 0o750)
    assert (
        box.path(f"{INSTANCE}/tls/ca.crt").read_bytes()
        == pool.path(f"{POOL}/certs/ca.crt").read_bytes()
    )
    key_line = next(
        line
        for line in pool.path(f"{INSTANCE}/kimina.env").read_text().splitlines()
        if line.startswith("LEAN_SERVER_API_KEY=")
    )
    assert (
        box.path(f"{INSTANCE}/kimina.env").read_text().splitlines()[0] == key_line
    )  # the pool's key, once
    assert box.path(f"{INSTANCE}/kimina.env").read_text().count("LEAN_SERVER_API_KEY=") == 1
    assert box.owner(f"{INSTANCE}/kimina.env") == ("root", "root", 0o600)
    settings = box.path(f"{INSTANCE}/kimina.env").read_text()
    assert (
        "LEAN_SERVER_NAME=box-a" in settings
        and "LEAN_SERVER_MAX_REPLS=2" in settings
        and "LEAN_SERVER_MEM_LIMIT=12g" in settings
    )
    # The version the box computed from the bundle it unpacked is the one the pool's host computed from its
    # checkout: one installer, one version.
    assert re.search(r"^LEANPOOL_AGENT_VERSION=[0-9a-f]{32}$", settings, re.MULTILINE)
    assert setting_of(box, f"{INSTANCE}/kimina.env", "LEANPOOL_AGENT_VERSION") == setting_of(
        pool, f"{POOL}/pool.env", "LEANPOOL_HAPROXY_INSTALLER_VERSION"
    )
    assert "compose.tls.yaml" in compose_files_of(box)
    assert (
        "reject unless { ssl_c_s_dn(cn) -m str lean-pool-proxy }"
        in box.path(f"{INSTANCE}/front/haproxy.cfg").read_text()
    )
    assert (
        "KIMINA_FW_FRONT_ADDRESS=172.29.54.250" in box.path(f"{INSTANCE}/firewall.conf").read_text()
    )
    assert box.path(f"{INSTANCE}/installer/install.sh").exists()  # removal there is one command
    assert (
        pool.path(f"{POOL}/certs/servers/box-a.crt").read_bytes()
        == box.path(f"{INSTANCE}/tls/box.crt").read_bytes()
    )


def test_a_join_leaves_no_secret_behind_on_either_side_and_none_on_a_command_line(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    api_key = pool.path(f"{POOL}/api-key").read_text().strip()
    # The token: in the printed command, once; never an argument of anything on either machine.
    assert window.output.count(window.token) == 1
    for call in pool.calls + box.calls:
        assert window.token not in call and api_key not in call, call
    assert api_key not in window.output and api_key not in result.stdout + result.stderr
    assert "PRIVATE KEY" not in window.output + result.stdout + result.stderr
    # The window is gone: the service, the token, the spool and the copy of the front door's key.
    assert not window.service_runs() and window.left_behind() == [".join.lock"]
    assert "docker rm -f -v lean-pool-demo-join" in pool.calls
    # The box's temporary directory (with the key copy and the pool's answer) is gone too.
    work = next(call.split(" -v ")[1].split(":/work")[0] for call in box.calls if ":/work " in call)
    assert work.startswith("/tmp") and not Path(work).exists()
    for path in box.path(f"{INSTANCE}/installer").rglob("*"):
        assert path.name not in ("box.key", "box.pem", "api-key")
    keys = sorted(
        str(path.relative_to(box.root))
        for path in box.root.rglob("*")
        if path.is_file() and b"PRIVATE KEY" in path.read_bytes()
    )
    assert keys == ["srv/kimina-demo/tls/box.pem"]  # the box's one key, in one place


def test_a_box_whose_name_does_not_resolve_is_listed_by_its_address_with_a_warning(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-b", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        "'box-b' is ready at 127.0.0.1:18001 (WARNING: neither box-b.lan nor box-b resolves to 127.0.0.1"
        in window.output
    )
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-b 127.0.0.1:18001 2 18201"
    )
    assert (
        "--server https://127.0.0.1:18001"
        in (pool.stub / "admissions").read_text().splitlines()[-1]
    )
    assert (
        "--tls-server-name box-b" in (pool.stub / "admissions").read_text().splitlines()[-1]
    )  # held to its NAME all the same


def test_the_wrong_pin_fetches_nothing_and_so_does_the_wrong_token(joining):
    """A join with the wrong pin fetches nothing. The real curl, against the real service."""
    window = joining.open()
    other_key = "sha256//" + "A" * 43 + "="
    refused = fetch(other_key, window.url)
    assert refused.returncode == 90 and refused.stdout == ""
    assert (
        fetch(window.pin, window.url[:-1] + ("A" if window.url[-1] != "A" else "B")).returncode
        == 22
    )  # 404: no such window
    assert fetch(window.pin, window.url).stdout.startswith("#!/usr/bin/env bash")
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130


def test_a_name_the_pool_already_has_is_refused_before_anything_is_signed_or_started(joining):
    box, pool = joining.box, joining.pool
    assert (
        control(pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201").returncode == 0
    )
    window = joining.open()
    servers = pool.path(f"{POOL}/servers").read_bytes()
    mark = pool.mark()
    result = join(box, window, "--name", "laptop", "--workers", "2")
    assert window.finish() == 6, window.output
    assert (
        result.returncode == 1
        and "the pool refused this box: a server named 'laptop' is already in the pool"
        in result.stderr
    )
    assert (
        "REFUSED: a server named 'laptop' is already in the pool" in window.output
        and "The pool is unchanged" in window.output
    )
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    assert not any(
        "sign-csr" in call or "leanpool-admit" in call for call in pool.calls_since(mark)
    )
    # The box had its image by then (a refusal is learnt after the image is there, and the image is
    # not lost). What stays is named, with the command that removes it; nothing that needs a key, nothing running.
    assert sorted(path.name for path in box.path(INSTANCE).iterdir()) == [
        "installer"
    ]  # the image itself came from the pool
    assert (
        "On this box: the Lean server's image and" in result.stderr
        and "--uninstall --purge" in result.stderr
    )
    assert not any(re.search(r"^docker compose .* up", call) for call in box.calls)
    assert not [
        path
        for path in box.root.rglob("*")
        if path.is_file() and b"PRIVATE KEY" in path.read_bytes()
    ]
    assert not window.service_runs() and window.left_behind() == [".join.lock"]


def test_a_box_that_fails_admission_is_told_which_kind_and_the_pool_is_unchanged(joining):
    window, box, pool = joining.open(STUB_ADMIT="tls"), joining.box, joining.pool
    servers = pool.path(f"{POOL}/servers").read_bytes()
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 4, window.output
    said = "admission FAILED for box-a.lan:18001: TLS with https://box-a.lan:18001: the server's certificate is for another name than 'box-a'"
    assert f"NOT ADMITTED: {said}" in window.output
    assert (
        result.returncode == 1 and f"NOT ADMITTED: {said}; the pool is unchanged" in result.stderr
    )
    assert (
        "--uninstall --purge" in result.stderr
    )  # the Lean server stays on the box; how to remove it
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    assert not pool.path(f"{POOL}/certs/servers/box-a.crt").exists()
    assert not window.service_runs() and window.left_behind() == [".join.lock"]


def test_ctrl_c_closes_the_window_and_cleans_up(joining):
    window, pool = joining.open(), joining.pool
    assert window.service_runs()
    # The service: the pool's image as its own unprivileged user, read-only, without capabilities, one port.
    started = next(
        call for call in pool.calls if call.startswith("docker run -d --name lean-pool-demo-join ")
    )
    assert (
        "--read-only --cap-drop ALL --security-opt no-new-privileges:true" in started
        and "--user" not in started
    )
    assert (
        f"-p 0.0.0.0:{window.port}:18110" in started and "/pki" not in started
    )  # it never sees the authority
    assert (
        started.count(" -v ") == 4
        and "/serve:/run/join:ro" in started
        and "/api-key:/run/pool/api-key:ro" in started
    )
    assert "/join/image:/run/join-image:ro" in started  # the exported image, read-only
    second = control(pool, "join-open")  # one window at a time
    assert (
        second.returncode == 1
        and "a join window is already open" in second.stderr
        and window.service_runs()
    )
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130
    assert not window.service_runs() and window.left_behind() == [".join.lock"]
    assert fetch(window.pin, window.url).returncode == 7  # nothing listens any more


def test_a_window_nobody_uses_closes_at_its_time_limit(pool):
    pytest.importorskip("aiohttp")
    window = Window(pool, minutes="1", STUB_CLOCK_STEP="20")
    window.wait_for_the_command()
    assert window.finish() == 3
    assert "the window closed: no box asked to join. The pool is unchanged." in window.output
    assert not window.service_runs() and window.left_behind() == [".join.lock"]


def test_a_window_serves_one_box_and_is_closed_once_that_box_has_its_answer(joining):
    """One box per window. Whatever the first box's outcome (here: refused), the window ends with it, and the
    same command run again reaches nobody. (That a second box is turned away WHILE a window is open is the
    join service's own rule, tested with the join service.)"""
    window, box = joining.open(), joining.box
    script = fetch(window.pin, window.url).stdout
    first = subprocess.run(
        ["bash", "-s", "--", "--name", "workstation"],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
        env=box.environment(),
        cwd=box.directory,
    )
    assert first.returncode == 1 and "is a name of the pool itself" in first.stderr
    assert window.finish() == 6 and not window.service_runs()
    late = subprocess.run(
        ["bash", "-s", "--", "--name", "other-box", "--workers", "2"],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
        env=box.environment(),
        cwd=box.directory,
    )
    # A saved script, run after its window closed, learns that at step 7 now, after the build.
    # The build is not lost, and nothing that needs a key is on the box.
    assert late.returncode == 1 and "step 7 failed" in late.stderr
    assert "the image is here and is neither downloaded nor built again" in late.stderr
    assert sorted(path.name for path in box.path(INSTANCE).iterdir()) == [
        "Dockerfile.demo",
        "build.log",
        "image.built",
        "installer",
        "setup-demo.sh",
    ]


def test_a_joined_box_is_removed_with_the_one_command_it_was_told(joining):
    window, box = joining.open(), joining.box
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    command = next(
        line.split("remove here", 1)[1].split()
        for line in result.stdout.splitlines()
        if "remove here" in line
    )
    assert command[:2] == ["sudo", "bash"] and command[3:] == [
        "--instance",
        "demo",
        "--uninstall",
        "--purge",
    ]
    mark = box.mark()
    removed = box.bash(Path(command[2]), *command[3:])
    assert removed.returncode == 0, removed.stdout + removed.stderr
    downs = [
        call.split("--project-name ")[1].split()[0]
        for call in box.calls_since(mark)
        if call.endswith(" down")
    ]
    # The server, its agent and its front are one project, taken down at once.
    assert downs == ["lean-server-demo"]
    assert not box.path(INSTANCE).exists()  # the box's key went with it
    assert not [
        path
        for path in box.root.rglob("*")
        if path.is_file() and b"PRIVATE KEY" in path.read_bytes()
    ]


def test_a_joined_boxs_certificate_is_in_the_pools_facts_from_the_reload_that_brings_it_in_and_leaves_with_it(
    joining,
):
    """The box's certificate is signed in the window and installed among the pool's public
    certificates only once the box is in. The render that puts the box in the list reads it from the window, so
    the proxy never serves a list with the box and facts without its certificate."""
    window, box, pool = joining.open(), joining.box, joining.pool
    mark = pool.mark()
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    found = facts(pool)
    assert (
        found['leanpool_certificate_not_after_seconds{certificate="servers/box-a.crt"}']
        == last_moment(pool.path(f"{POOL}/certs/servers/box-a.crt"))
        == last_moment(box.path(f"{INSTANCE}/tls/box.crt"))
    )
    assert (
        found['leanpool_server_listed_workers{server="box-a"}'] == 2
        and found['leanpool_server_paused{server="box-a"}'] == 0
    )
    assert (
        pool.calls_since(mark).count(reload_of("lean-pool-demo-haproxy-1")) == 1
    )  # one reload brought in both
    removed = control(pool, "remove-backend", "box-a")
    assert removed.returncode == 0, removed.stderr
    assert not [sample for sample in facts(pool) if "box-a" in sample]
    assert not pool.path(f"{POOL}/certs/servers/box-a.crt").exists()


def test_a_signing_request_for_another_name_than_the_one_it_claims_is_not_signed(joining, tmp_path):
    """A box says it is `laptop` and sends a request for a certificate that would also
    pass as `workstation` (another server of the pool). The pool signs for the validated name alone, so
    this request is refused, nothing is signed, and the window ends."""
    window, pool = joining.open(), joining.pool
    request = subprocess.run(
        [
            str(pool.stub / "bin" / "leanpool-pki"),
            "csr",
            "--out-dir",
            str(tmp_path / "box"),
            "--name",
            "laptop",
            "--dns",
            "workstation",
            "--file-stem",
            "box",
        ],
        capture_output=True,
        text=True,
    )
    assert request.returncode == 0, request.stderr
    body = tmp_path / "csr.json"
    body.write_text(
        json.dumps(
            {
                "name": "laptop",
                "lean_port": 18001,
                "agent_port": 18201,
                "workers": 2,
                "csr": (tmp_path / "box" / "box.csr").read_text(),
            }
        )
    )
    sent = subprocess.run(
        [
            "curl",
            "-sSk",
            "--pinnedpubkey",
            window.pin,
            "-w",
            "%{http_code}",
            "-o",
            "/dev/null",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            f"@{body}",
            window.url + "/csr",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert sent.stdout == "202", sent.stderr
    assert window.finish() == 6, window.output
    assert "asks to join as 'laptop'" in window.output
    assert (
        "REFUSED: the signing request was not signed" in window.output
        and "The pool is unchanged" in window.output
    )
    assert not list(pool.path(f"{POOL}/certs/servers").glob("laptop*"))
    assert "laptop" not in pool.path(f"{POOL}/servers").read_text()
    assert not any("leanpool-admit" in call for call in pool.calls)
    assert not window.service_runs() and window.left_behind() == [".join.lock"]


def test_a_box_renews_its_certificate_by_leaving_and_joining_again(joining):
    """Renewal is joining again. The same box, a second window: while it is still in the
    list it is refused; once removed it joins with a NEW key and certificate, and nothing on it is recreated."""
    box, pool = joining.box, joining.pool
    first = joining.open()
    assert (
        join(box, first, "--name", "box-a", "--workers", "2").returncode == 0
        and first.finish() == 0
    )
    old = box.path(f"{INSTANCE}/tls/box.pem").read_bytes()
    containers, recreated = box.container_id("lean-server-demo-kimina-1"), box.recreated()

    still_listed = joining.open()
    again = join(box, still_listed, "--name", "box-a", "--workers", "2")
    assert (
        still_listed.finish() == 6
        and again.returncode == 1
        and "already in the pool; remove it first" in again.stderr
    )
    assert pool.stopped_by_hand("lean-pool-demo-haproxy-1") is False
    assert (
        box.path(f"{INSTANCE}/tls/box.pem").read_bytes() == old
    )  # a refused join changes nothing on the box

    assert control(pool, "remove-backend", "box-a").returncode == 0
    second = joining.open()
    mark = box.mark()
    renewed = join(box, second, "--name", "box-a", "--workers", "2")
    assert second.finish() == 0 and renewed.returncode == 0, renewed.stderr + second.output
    new = box.path(f"{INSTANCE}/tls/box.pem").read_bytes()
    assert new != old and names_of(x509.load_pem_x509_certificate(new)) == ({"box-a"}, set())
    assert (
        pool.path(f"{POOL}/certs/servers/box-a.crt").read_bytes()
        == box.path(f"{INSTANCE}/tls/box.crt").read_bytes()
    )
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )
    # The Lean server kept running; its front loaded the new certificate with a reload.
    assert (
        box.container_id("lean-server-demo-kimina-1") == containers and box.recreated() == recreated
    )
    assert reload_of("lean-server-demo-front-1") in box.calls_since(mark)
    # ... and that reload did not cost the front its place at the next boot.
    box.restart_daemon()
    pool.restart_daemon()
    assert (
        box.running("lean-server-demo-front-1")
        and box.running("lean-server-demo-kimina-1")
        and box.running("lean-server-demo-agent-1")
    )
    assert pool.running("lean-pool-demo-haproxy-1") and pool.running("lean-server-demo-front-1")


# --- a box that is already in the pool takes a newer installer with `--refresh` ------------------------------


def steps(result: subprocess.CompletedProcess[str]) -> list[str]:
    """The numbered steps a join script printed: `1/10` ... or, for a refresh, `1/3` ..."""
    return [
        line.split()[1] for line in result.stdout.splitlines() if re.match(r"==> \d+/\d+ ", line)
    ]


def close(window: Window) -> None:
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130


@pytest.fixture
def joined(joining, deploy, tmp_path):
    """The pool and one box of it as they are before the init process reaches them: the pool's host still serves the
    older files, `box-a` joined with them, and its Lean server was made without an init process. Then the
    pool's host is refreshed with today's files, which is what every later window serves."""
    pool, box = joining.pool, joining.box
    pool.deploy = before_the_init_process(deploy, tmp_path / "before")
    assert pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
    window = joining.open()
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert "--- init process: NO" in status(box, "@demo").stdout
    assert (
        "\n    init: true\n"
        not in box.path(
            f"{INSTANCE}/installer/lean_pool/deploy/lean-server/compose.yaml"
        ).read_text()
    )
    pool.deploy = deploy
    assert pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
    return joining


def but_the_installers_version(settings: bytes) -> list[bytes]:
    """An instance's settings, every line but the installer's version."""
    return [
        line for line in settings.splitlines() if not line.startswith(b"LEANPOOL_AGENT_VERSION=")
    ]


def test_a_box_in_the_pool_takes_the_new_installer_and_only_its_lean_server_and_agent_are_made_again(
    joined, deploy, tmp_path
):
    pool, box = joined.pool, joined.box
    window = joined.open()
    assert 'with "sudo bash -s -- --refresh"' in window.output  # the window says how
    had = SimpleNamespace(
        certificate=box.path(f"{INSTANCE}/tls/box.pem").read_bytes(),
        settings=box.path(f"{INSTANCE}/kimina.env").read_bytes(),
        version=setting_of(box, f"{INSTANCE}/kimina.env", "LEANPOOL_AGENT_VERSION"),
        stamp=box.path(f"{INSTANCE}/image.built").read_bytes(),
        lean=box.container_id(LEAN),
        recreated=box.recreated(),
        others={name: box.container_id(name) for name in ("lean-server-demo-front-1",)},
        builds=len(lean_builds(box)),
        servers=pool.path(f"{POOL}/servers").read_bytes(),
        config=pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes(),
    )
    box_mark, pool_mark = box.mark(), pool.mark()
    result = join(box, window, "--refresh")
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert steps(result) == ["1/3", "2/3", "3/3"] and "REFRESHED: 'box-a'" in result.stdout
    assert "INSTALLED AND VERIFIED" in result.stdout  # the installer's own check ran
    # The box: today's compose file, in a pool member's form, and its Lean server made again, once, with an init;
    # its usage agent made again, once, for the newer installer's version.
    compose = box.path(f"{INSTANCE}/compose.yaml").read_text()
    assert "\n    init: true\n" in compose and "compose.tls.yaml" in compose_files_of(box)
    assert box.recreated() == [*had.recreated, LEAN, AGENT] and box.container_id(LEAN) not in (
        "",
        had.lean,
    )
    version = setting_of(box, f"{INSTANCE}/kimina.env", "LEANPOOL_AGENT_VERSION")
    assert version != had.version and version == setting_of(
        pool, f"{POOL}/pool.env", "LEANPOOL_HAPROXY_INSTALLER_VERSION"
    )
    assert "--- init process: yes" in status(box, "@demo").stdout
    # What it had, it has: the image (not built, not loaded) and its record, the key and every setting but the
    # installer's version, the certificate and the front.
    calls = box.calls_since(box_mark)
    assert len(lean_builds(box)) == had.builds and not any(
        call.startswith("docker load") for call in calls
    )
    assert box.path(f"{INSTANCE}/image.built").read_bytes() == had.stamp
    assert but_the_installers_version(
        box.path(f"{INSTANCE}/kimina.env").read_bytes()
    ) == but_the_installers_version(had.settings)
    assert box.path(f"{INSTANCE}/tls/box.pem").read_bytes() == had.certificate
    assert {name: box.container_id(name) for name in had.others} == had.others
    assert not any("leanpool-pki csr" in call for call in calls)  # no key was made
    # The installer copy on the box is today's.
    assert (
        "\n    init: true\n"
        in box.path(f"{INSTANCE}/installer/lean_pool/deploy/lean-server/compose.yaml").read_text()
    )
    # The pool: asked for nothing. Nothing reached its spool, nothing was signed or tested, its list and its
    # configuration are byte for byte, and the window is as open as it was.
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*"))
    assert not any(
        "sign-csr" in call or "leanpool-admit" in call for call in pool.calls_since(pool_mark)
    )
    assert pool.path(f"{POOL}/servers").read_bytes() == had.servers
    assert pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == had.config
    assert window.process.poll() is None and window.service_runs()
    # The same command again, in the same window: nothing is made again.
    recreated = box.recreated()
    again = join(box, window, "--refresh")
    assert again.returncode == 0 and box.recreated() == recreated, again.stderr
    # And the window is still good for a box that JOINS.
    other = Machine(tmp_path / "other", deploy)
    new = join(other, window, "--name", "box-b", "--workers", "2")
    assert window.finish() == 0 and new.returncode == 0, new.stderr + window.output
    assert pool.path(f"{POOL}/servers").read_text().splitlines()[-2:] == [
        "box-a box-a.lan:18001 2 18201",
        "box-b 127.0.0.1:18001 2 18201",
    ]


def test_a_box_that_built_its_own_image_is_refreshed_without_building_it_again(joining):
    """A join builds with the box's own job count, and the record of a built image holds that count: the
    installer run again without it would build. `--keep-image` is what a refresh passes."""
    box = joining.box
    window = joining.open()
    result = join(
        box, window, "--name", "box-a", "--workers", "2", STUB_MACHINE="riscv64"
    )  # another machine type: it builds
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    stamp, lean, recreated = (
        box.path(f"{INSTANCE}/image.built").read_text(),
        box.container_id(LEAN),
        box.recreated(),
    )
    assert stamp.split()[0] != "pool" and len(lean_builds(box)) == 1
    window = joining.open()
    refreshed = join(box, window, "--refresh", STUB_MACHINE="riscv64")
    assert refreshed.returncode == 0, refreshed.stdout[-3000:] + refreshed.stderr[-3000:]
    assert (
        "Keeping the image acme/kimina:lean-demo-1 exactly as it is (--keep-image)"
        in refreshed.stdout
    )
    assert len(lean_builds(box)) == 1 and box.path(f"{INSTANCE}/image.built").read_text() == stamp
    # The window carried the very files the box joined with: nothing is made again.
    assert box.container_id(LEAN) == lean and box.recreated() == recreated
    close(window)
    # What the option is for: the installer copy, run the same way without it, builds.
    plain = box.bash(
        box.path(f"{INSTANCE}/installer/install.sh"), "--instance", "demo", "--no-door"
    )
    assert plain.returncode == 0 and len(lean_builds(box)) == 2


def test_refresh_is_refused_where_there_is_no_joined_server_and_nothing_changes(joining):
    pool, box = joining.pool, joining.box
    window = joining.open()
    # A box that never joined.
    fresh = join(box, window, "--refresh")
    assert fresh.returncode == 1 and "this box holds no server of pool demo" in fresh.stderr
    assert (
        "run the same command without --refresh" in fresh.stderr
        and "Nothing was changed" in fresh.stderr
    )
    assert not box.path(INSTANCE).exists() and not any(
        re.search(r"^docker (build|run|compose)", call) for call in box.calls
    )
    # The pool's own host: its server is refreshed with the pool, and `--no-door` here would take the door away.
    mark, door = pool.mark(), snapshot(pool.path("/usr/local/bin"))
    own = join(pool, window, "--refresh")
    assert (
        own.returncode == 1
        and "this is the pool's own host" in own.stderr
        and "install.sh --proxy demo" in own.stderr
    )
    assert changing(pool.calls_since(mark)) == [] and snapshot(pool.path("/usr/local/bin")) == door
    # Neither used the window.
    assert (
        not any(pool.path(POOL).glob(".join.*/spool/requests/*"))
        and window.process.poll() is None
        and window.service_runs()
    )
    close(window)


def test_a_box_whose_docker_cannot_give_an_init_process_is_not_refreshed_and_its_server_runs_on(
    joined,
):
    box = joined.box
    window = joined.open()
    lean, mark = box.container_id(LEAN), box.mark()
    compose, installer = (
        box.path(f"{INSTANCE}/compose.yaml").read_bytes(),
        snapshot(box.path(f"{INSTANCE}/installer")),
    )
    failed = join(box, window, "--refresh", STUB_NO_INIT="1")
    assert failed.returncode == 1 and "step 3 failed" in failed.stderr, failed.stdout[-2000:]
    assert (
        "this host's Docker cannot start acme/kimina:lean-demo-1 with an init process"
        in failed.stderr
    )
    assert (
        "The pool's list is unchanged" in failed.stderr
        and "run the same command again" in failed.stderr
    )
    assert (
        changing(box.calls_since(mark)) == []
        and box.container_id(LEAN) == lean
        and box.running(LEAN)
    )
    assert box.path(f"{INSTANCE}/compose.yaml").read_bytes() == compose
    assert (
        snapshot(box.path(f"{INSTANCE}/installer")) == installer
    )  # still the installer of its join
    # The cause put right, the same command in the same window.
    assert window.service_runs()
    assert (
        join(box, window, "--refresh").returncode == 0
        and "--- init process: yes" in status(box, "@demo").stdout
    )
    close(window)


def test_keep_image_is_refused_where_it_cannot_hold(tmp_path, deploy):
    machine = Machine(tmp_path / "box", deploy)
    options = ("install.sh", "--instance", "demo", "--no-door")
    absent = machine.bash(*options, "--keep-image")
    assert (
        absent.returncode == 1
        and "--keep-image: there is no image acme/kimina:lean-demo-1 on this host" in absent.stderr
    )
    assert (
        "Nothing was changed" in absent.stderr
        and list(machine.path("/srv").iterdir()) == []
        and lean_builds(machine) == []
    )
    assert (
        machine.bash("install.sh", "--no-door", "--keep-image").returncode == 1
    )  # instance 1 builds its own
    assert machine.bash(*options, "--keep-image", "--build-only").returncode == 1
    assert machine.bash(*options, "--keep-image", "--image-from-pool", "a" * 64).returncode == 1
    assert list(machine.path("/srv").iterdir()) == [] and lean_builds(machine) == []


# ====================================================================================================================
# Seven ways an install went wrong, each shown on the fake machines.
# ====================================================================================================================

# --- A. a reload must not cost a container its place at the next boot -----------------------------------------------


def test_the_stand_in_keeps_dockers_book_a_container_docker_signalled_does_not_come_back(pool):
    """The harness must be able to show the defect. `docker kill -s HUP` reloads HAProxy and ALSO marks the
    container as stopped by hand (moby: daemon/kill.go, for any signal); with `restart: unless-stopped` the
    daemon then leaves it stopped when it next starts."""
    docker = str(pool.stub / "bin" / "docker")
    assert (
        subprocess.run(
            [docker, "kill", "-s", "HUP", "lean-pool-demo-haproxy-1"], env=pool.environment()
        ).returncode
        == 0
    )
    assert pool.running("lean-pool-demo-haproxy-1") and pool.stopped_by_hand(
        "lean-pool-demo-haproxy-1"
    )
    pool.restart_daemon()
    assert not pool.running("lean-pool-demo-haproxy-1") and pool.running("lean-pool-demo-cache-1")


def test_a_reboot_brings_every_container_back_after_a_second_install_a_reload_and_an_added_server(
    pool,
):
    mark = pool.mark()
    assert (
        pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES).returncode == 0
    )  # the second run: both HAProxys reload
    assert pool.reloads() == ["lean-server-demo-front-1", "lean-pool-demo-haproxy-1"]
    assert control(pool, "reload").returncode == 0
    assert (
        control(pool, "add-backend", "laptop", "192.168.50.44:18001", "6", "18201").returncode == 0
    )
    assert control(pool, "render").returncode == 0
    assert control(pool, "remove-backend", "laptop").returncode == 0
    assert pool.reloads()[2:] == ["lean-pool-demo-haproxy-1"] * 4
    assert not any(re.match(r"docker (kill|stop) ", call) for call in pool.calls_since(mark))
    assert not any(pool.stopped_by_hand(name) for name in EVERY_CONTAINER)
    pool.restart_daemon()
    assert [name for name in EVERY_CONTAINER if not pool.running(name)] == []


def test_a_reboot_after_a_join_brings_the_proxy_and_the_new_boxs_front_back(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert pool.reloads() == ["lean-pool-demo-haproxy-1"]  # the new server was loaded with a reload
    assert not any(call.startswith("docker kill") for call in pool.calls + box.calls)
    pool.restart_daemon()
    box.restart_daemon()
    assert [name for name in EVERY_CONTAINER if not pool.running(name)] == []
    assert (
        box.running("lean-server-demo-front-1")
        and box.running("lean-server-demo-agent-1")
        and box.running("lean-server-demo-kimina-1")
    )


def test_a_reload_of_a_proxy_that_is_not_running_says_so(pool):
    assert (
        subprocess.run(
            [str(pool.stub / "bin" / "docker"), "stop", "lean-pool-demo-haproxy-1"],
            env=pool.environment(),
        ).returncode
        == 0
    )
    result = control(pool, "reload")
    assert result.returncode == 1 and "could not be told to reload" in result.stderr


# --- B. no box is signed under one of the pool's own names -----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "join_host", "reason"),
    [
        (
            "workstation",
            "127.0.0.1",
            "a name of the pool itself",
        ),  # on the front door's certificate
        (
            "lean-pool-proxy",
            "127.0.0.1",
            "a name of the pool itself",
        ),  # on the proxy's client certificate
        ("localhost", "127.0.0.1", "a name of the pool itself"),
        (
            "pool-gate",
            "Pool-Gate",
            "a name of the pool itself",
        ),  # the join host, written in other letter case
        (
            "workstation.lan",
            "127.0.0.1",
            "ONE DNS label",
        ),  # a request for the front door's own name
        ("box.example", "127.0.0.1", "ONE DNS label"),
    ],
)
def test_a_request_for_one_of_the_pools_names_or_for_a_dotted_name_is_refused_and_nothing_is_signed(
    joining, tmp_path, name, join_host, reason
):
    """A join request for `workstation.lan` was signed as a server certificate, which then
    passed for the pool's front door to every client that trusts the pool's authority; so was `lean-pool-proxy`.
    Sent here as a box would send them (a real signing request, the real join service, the real curl)."""
    pool = joining.pool
    servers, config = (
        pool.path(f"{POOL}/servers").read_bytes(),
        pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes(),
    )
    window = joining.open(join_host=join_host)
    assert window.host == join_host  # the printed command names the join host
    mark = pool.mark()
    status, reply = ask_to_join(window, tmp_path, name)
    assert status == "409" and reason in json.loads(reply)["reason"], (
        reply
    )  # the reply is {"reason": ...}
    assert window.finish() == 6
    assert (
        "REFUSED: " in window.output
        and reason in window.output
        and "The pool is unchanged" in window.output
    )
    assert not any(
        "sign-csr" in call or "leanpool-admit" in call for call in pool.calls_since(mark)
    )  # nothing was signed
    assert (
        pool.path(f"{POOL}/servers").read_bytes() == servers
        and pool.path(f"{POOL}/haproxy/haproxy.cfg").read_bytes() == config
    )
    assert sorted(path.name for path in pool.path(f"{POOL}/certs/servers").iterdir()) == [
        "workstation.crt"
    ]
    assert not window.service_runs() and window.left_behind() == [".join.lock"]


def test_the_pools_names_are_read_from_the_certificate_that_is_installed(joining, tmp_path):
    """Not from a list in the script: a name added to the front door's certificate afterwards is reserved from
    the next window on, and an ordinary name still joins nowhere near it."""
    pool = joining.pool
    assert (
        pool.bash(
            "install.sh",
            "--proxy",
            "demo",
            "--admit-cases",
            ADMIT_CASES,
            "--front-name",
            "pool-alias",
        ).returncode
        == 0
    )
    mark = pool.mark()
    window = joining.open()
    status, reply = ask_to_join(window, tmp_path, "pool-alias")
    assert status == "409" and "a name of the pool itself" in json.loads(reply)["reason"]
    assert window.finish() == 6
    names = next(call for call in pool.calls_since(mark) if "certificate-names" in call)
    assert (
        "/run/pool/tls/front.pem /run/pool/tls/proxy-client.pem" in names
        and "--network none" in names
        and "--cap-drop ALL" in names
    )
    assert "/tls:/run/pool/tls:ro" in names


def test_no_window_opens_when_the_pools_own_names_cannot_be_read(pool):
    pytest.importorskip("aiohttp")
    pool.path(f"{POOL}/tls/front.pem").write_text("not a certificate\n")
    result = control(pool, "join-open")
    assert (
        result.returncode == 1
        and "could not read the names on the pool's own certificates" in result.stderr
    )
    assert not any("leanpool-join" in call for call in pool.calls)
    assert sorted(
        path.name for path in pool.path(POOL).iterdir() if path.name.startswith(".join")
    ) == [".join.lock"]


# --- C. a front that keeps restarting is not a front that runs --------------------------------------------------------


def test_a_front_that_keeps_restarting_stops_the_upgrade_and_is_named_with_its_last_lines(
    tmp_path, today
):
    """`.State.Running` is true for a container Docker restarts in a loop, so a front that
    could not read its certificate passed for a running one."""
    machine = today.copy_to(tmp_path / "host")
    mark = machine.mark()
    result = machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        STUB_CRASH_LOOP="lean-server-demo-front-1",
    )
    # "running", as Docker reports a restart loop.
    assert machine.running("lean-server-demo-front-1")
    assert result.returncode == 1 and "POOL INSTALLED" not in result.stdout
    assert (
        "the TLS front lean-server-demo-front-1 of 'workstation' does not stay up" in result.stderr
    )  # the front, by name
    assert (
        "unable to stat SSL certificate from file '/etc/leanpool/tls/box.pem' : Permission denied"
        in result.stderr
    )
    assert "run this again" in result.stderr
    # The pool's proxy was not switched to a config whose only server has no front.
    assert not any(
        "--project-name lean-pool-demo " in call and call.endswith("up -d")
        for call in machine.calls_since(mark)
    )


def test_a_new_boxs_front_that_keeps_restarting_is_never_reported_ready(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    servers = pool.path(f"{POOL}/servers").read_bytes()
    result = join(
        box, window, "--name", "box-a", "--workers", "2", STUB_CRASH_LOOP="lean-server-demo-front-1"
    )
    assert result.returncode == 1 and "step 8 failed" in result.stderr
    assert "the TLS front lean-server-demo-front-1 of 'box-a' does not stay up" in result.stderr
    # The pool has signed this box's certificate by then: the box is told its window is used up.
    assert (
        "this join window is used up" in result.stderr
        and "run the NEW command here" in result.stderr
    )
    assert pool.path(f"{POOL}/servers").read_bytes() == servers
    assert window.process.poll() is None  # the pool still waits: no `ready` was sent
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/ready.json"))


# --- D. a box that cannot take the installer learns it before it has used the window -----------------------------------


@pytest.mark.parametrize(
    ("fault", "message", "step"),
    [
        (
            {"STUB_DISK_FREE": "10", "STUB_MACHINE": "riscv64"},
            "need at least 60 GB free",
            "step 4 failed",
        ),  # a box that builds
        ({"STUB_BUSY_PORT": "18001"}, "port 18001 is already in use", "step 4 failed"),
        ({"STUB_BUSY_PORT": "18201"}, "port 18201 (the usage agent's", "step 4 failed"),
        (
            {"STUB_EXTRA_ROUTE": "172.29.54.0/24"},
            "the host already routes 172.29.54.0/24",
            "step 4 failed",
        ),
        (
            {"STUB_DOCKER_ROOT": "/var/snap/docker/common/var-lib-docker"},
            "the snap package, which is not supported",
            "step 1 failed",
        ),
        ({"STUB_MOUNTS_EMPTY": "1"}, "a container on this box cannot see", "step 3 failed"),
    ],
)
def test_a_box_that_cannot_take_the_installer_stops_before_it_asks_the_pool_and_the_window_stays_open(
    joining, fault, message, step
):
    """The certificate and the pool's key were handed over at step 5 and the installer's
    pre-flight ran at step 6, so such a box had used up the window (a second signing request answers 409)
    while the pool kept waiting. Now the installer's own pre-flight runs first, with its own message."""
    window, box, pool = joining.open(), joining.box, joining.pool
    failed = join(box, window, "--name", "box-a", "--workers", "2", **fault)
    assert failed.returncode == 1 and message in failed.stderr and step in failed.stderr, (
        failed.stderr
    )
    assert "Nothing was installed" in failed.stderr and not box.path(INSTANCE).exists()
    # The pool was asked for nothing: no signing request reached the spool, nothing was signed, the window is open.
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*")) and not any(
        "sign-csr" in call for call in pool.calls
    )
    assert window.process.poll() is None and window.service_runs()
    # The fault put right, the SAME command joins in the SAME window.
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )


def test_the_pre_flight_alone_checks_and_installs_nothing(tmp_path, deploy):
    machine = Machine(tmp_path / "box", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        "--tls-dir",
        "/not/there/yet",
        "--server-name",
        "box-a",
        "--preflight-only",
    )
    assert (
        result.returncode == 0
        and "pre-flight passed: this host can take the install. Nothing was installed."
        in result.stdout
    )
    assert (
        list(machine.path("/srv").iterdir()) == []
        and list(machine.path("/usr/local/sbin").iterdir()) == []
    )
    assert not any(
        re.search(r"^docker (build|run|pull|compose .* up)|^systemctl", call)
        for call in machine.calls
    )
    # ... and a full install still wants the certificate it was told about.
    full = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        "--tls-dir",
        "/not/there/yet",
        "--server-name",
        "box-a",
    )
    assert (
        full.returncode == 1
        and "--tls-dir: /not/there/yet/box.pem is missing or empty" in full.stderr
    )


# --- E. the just-recreated Lean server gets the five minutes install.sh gives it ----------------------------------------


def test_a_lean_server_that_answers_only_after_twenty_tries_still_ends_verified(tmp_path, today):
    """The pool's verify loop gave up after 12 tries (about 2 minutes) on a server it had just
    recreated, for which install.sh allows 5."""
    machine = today.copy_to(tmp_path / "host")
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_CHECK_FAILS_FIRST="20"
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        "POOL INSTALLED AND VERIFIED" in result.stdout
        and result.stdout.count('"check": "FAILED"') == 20
    )


def test_a_lean_server_that_never_answers_is_reported_after_thirty_tries(tmp_path, today):
    machine = today.copy_to(tmp_path / "host")
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_CHECK_FAILS_FIRST="1000"
    )
    assert (
        result.returncode == 1
        and "POOL INSTALLED, BUT THE CHECK THROUGH IT FAILED" in result.stdout
    )
    # Thirty through the pool; then this box's own server by itself, read-only and again writable.
    assert result.stdout.count('"check": "FAILED"') == 30 + 2


# --- F. nothing a box sends reaches the operator's terminal unprintable ---------------------------------------------------


def is_plain(text: str) -> bool:
    return all(character == "\n" or " " <= character <= "~" for character in text)


def test_a_request_that_carries_an_escape_sequence_reaches_neither_the_terminal_nor_the_reply(
    joining, tmp_path
):
    """The certificate tool quotes the names a request asks for, and its words were shown and
    sent back with only their line breaks removed. A request whose subject holds an escape sequence and
    letters of another alphabet, sent as a box would send it."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    hostile = "\x1b]0;owned\x07\x1b[2J\x1b[31m\u0430dmin\u202e"
    subject = x509.Name(
        [
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "box-f"),
            x509.NameAttribute(x509.NameOID.COMMON_NAME, hostile),
        ]
    )
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256())
    )
    window, pool = joining.open(), joining.pool
    status, reply = ask_to_join(
        window, tmp_path, "box-f", csr=request.public_bytes(serialization.Encoding.PEM).decode()
    )
    assert status == "409"
    reason = json.loads(reply)["reason"]
    assert window.finish() == 6
    refused = next(line for line in window.output.splitlines() if line.startswith("==> REFUSED"))
    assert (
        "the signing request was not signed" in refused
        and "which is outside the allowed names" in refused
    )
    assert "]0;owned [2j [31m dmin" in refused  # its words are there; no escape, no other alphabet
    assert is_plain(window.output) and is_plain(reason) and "\\u" not in reply
    assert len(reason) <= 400 and "box-f" not in pool.path(f"{POOL}/servers").read_text()
    # The tool really did print the raw bytes: without the filter they would have been shown.
    (tmp_path / "hostile.csr").write_bytes(request.public_bytes(serialization.Encoding.PEM))
    direct = subprocess.run(
        [
            str(pool.stub / "bin" / "leanpool-pki"),
            "sign-csr",
            "--ca-dir",
            str(pool.path(f"{POOL}/pki/ca")),
            "--csr",
            str(tmp_path / "hostile.csr"),
            "--out",
            str(tmp_path / "out.crt"),
            "--allow-dns",
            "box-f",
        ],
        capture_output=True,
        text=True,
    )
    assert (
        direct.returncode == 1 and "\x1b" in direct.stderr and not (tmp_path / "out.crt").exists()
    )


# --- G. hosts that cannot install are told so, plainly, before anything is done -----------------------------------------

SNAP = "/var/snap/docker/common/var-lib-docker"


@pytest.mark.parametrize("arguments", [("--instance", "demo", "--no-door"), ("--no-door",)])
def test_docker_from_the_snap_package_is_refused_before_anything_is_installed(
    tmp_path, deploy, arguments
):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash("install.sh", *arguments, STUB_DOCKER_ROOT=SNAP)
    assert result.returncode == 1 and "the snap package, which is not supported" in result.stderr
    assert (
        "cannot mount /srv or /tmp into a container" in result.stderr
        and "Nothing was installed" in result.stderr
    )
    assert list(machine.path("/srv").iterdir()) == []
    assert not any(
        re.search(r"^docker (build|run|pull|compose .* up)", call) for call in machine.calls
    )


def test_the_pool_installer_refuses_snap_docker_too_and_touches_nothing(tmp_path, today):
    machine = today.copy_to(tmp_path / "host")
    mark = machine.mark()
    result = machine.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_DOCKER_ROOT=SNAP
    )
    assert result.returncode == 1 and "the snap package, which is not supported" in result.stderr
    assert changing(machine.calls_since(mark)) == [] and not machine.path(f"{POOL}/pki").exists()


def test_a_docker_data_directory_this_system_cannot_see_is_said_and_the_install_goes_on(
    tmp_path, deploy
):
    """`df` on a directory that is not in this filesystem view (Docker Desktop, also under
    WSL) ended the installer with only df's message. What could not be checked is now said, in words."""
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        STUB_DOCKER_ROOT="/var/lib/docker-in-its-own-vm",
    )
    assert result.returncode == 0, result.stderr
    assert (
        "COULD NOT CHECK free disk space: Docker's data directory (/var/lib/docker-in-its-own-vm) is not a directory this system can see"
        in result.stderr
    )
    assert (
        "This install needs 60 GB there (for a build of the image)" in result.stderr
        and "INSTALLED AND VERIFIED" in result.stdout
    )
    # Where it CAN be measured, too little is still a refusal, before anything is installed.
    other = Machine(tmp_path / "other", deploy)
    small = other.bash("install.sh", "--instance", "demo", "--no-door", STUB_DISK_FREE="10")
    assert (
        small.returncode == 1
        and "need at least 60 GB free" in small.stderr
        and list(other.path("/srv").iterdir()) == []
    )


def test_a_host_without_systemd_is_told_to_pass_no_firewall_before_anything_is_built(
    tmp_path, deploy
):
    """The firewall rules are kept by systemd units. Docker inside WSL often has no systemd: the installer
    used to find out at its firewall step, after the build."""
    machine = Machine(tmp_path / "host", deploy)
    shutil.rmtree(machine.path("/run/systemd"))
    result = machine.bash("install.sh", "--instance", "demo", "--no-door")
    assert (
        result.returncode == 1
        and "this host does not run systemd" in result.stderr
        and "--no-firewall" in result.stderr
    )
    assert list(machine.path("/srv").iterdir()) == [] and not any(
        call.startswith("docker build") for call in machine.calls
    )
    without = machine.bash("install.sh", "--instance", "demo", "--no-door", "--no-firewall")
    assert without.returncode == 0 and "INSTALLED AND VERIFIED" in without.stdout
    assert not any(call.startswith("systemctl") for call in machine.calls)


# --- H. a host with Docker but without its compose plugin gets the plugin ----------------------------------------------


def package_calls(machine: Machine) -> list[str]:
    """The package managers that were called, and any attempt to fetch the release binary."""
    return [
        call for call in machine.calls if call.startswith(("apt-get ", "dnf ", "yum ", "curl "))
    ]


def test_the_pre_flight_alone_reports_a_missing_compose_plugin_and_installs_nothing(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "box", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        "--tls-dir",
        "/not/there/yet",
        "--server-name",
        "box-a",
        "--preflight-only",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
    )
    assert result.returncode == 0, result.stderr
    assert (
        "the docker compose plugin is not installed: the install adds it once the checks below have passed"
        in result.stdout
    )
    assert (
        "pre-flight passed, with one thing left to the install: the docker compose plugin is missing here"
        in result.stdout
    )
    assert "Whether that works is known only then. Nothing was installed." in result.stdout
    assert (
        "pre-flight passed: this host can take the install" not in result.stdout
    )  # it does not claim more
    assert package_calls(machine) == []
    assert (
        list(machine.path("/srv").iterdir()) == []
        and list(machine.path("/usr/local/lib").iterdir()) == []
    )


def test_an_install_on_a_host_without_the_compose_plugin_adds_it_before_anything_is_built(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        "installed the docker compose plugin: the apt package docker-compose-v2. "
        "To remove it: sudo apt-get remove docker-compose-v2"
    ) in result.stdout
    assert "INSTALLED AND VERIFIED" in result.stdout
    assert package_calls(machine) == [
        "apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-plugin",
        "apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends docker-compose-v2",
    ]
    calls = machine.calls
    assert calls.index(package_calls(machine)[-1]) < next(
        index for index, call in enumerate(calls) if call.startswith("docker build")
    )


def test_a_host_on_which_the_plugin_cannot_be_added_is_told_every_reason_and_nothing_else_is_installed(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash("install.sh", "--instance", "demo", "--no-door", STUB_COMPOSE_MISSING="1")
    assert (
        result.returncode == 1
        and "the docker compose plugin is not installed and could not be added" in result.stderr
    )
    for reason in (
        "apt-get install docker-compose-plugin: E: Unable to locate package docker-compose-plugin",
        "apt-get install docker-compose-v2: E: Unable to locate package docker-compose-v2",
        "dnf install docker-compose-plugin: Error: Unable to find a match: docker-compose-plugin",
        "Nothing else was installed.",
    ):
        assert reason in result.stderr, reason
    # The release binary was the last resort, and the stand-in for curl did not fetch it.
    assert (
        "the download of https://github.com/docker/compose/releases/download/" in result.stderr
        or shutil.which("curl") is None
    )
    assert (
        list(machine.path("/srv").iterdir()) == []
        and list(machine.path("/usr/local/lib").iterdir()) == []
    )
    assert not any(call.startswith("docker build") for call in machine.calls)


def test_a_host_that_fails_another_check_gets_no_plugin_installed(tmp_path, deploy):
    """The plugin is added at the END of the pre-flight: too little disk is said first, and nothing is installed."""
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
        STUB_DISK_FREE="10",
    )
    assert result.returncode == 1 and "need at least 60 GB free" in result.stderr
    assert package_calls(machine) == [] and list(machine.path("/srv").iterdir()) == []


def test_snap_docker_is_refused_before_the_plugin_is_looked_at(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        STUB_DOCKER_ROOT=SNAP,
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
    )
    assert result.returncode == 1 and "the snap package, which is not supported" in result.stderr
    assert package_calls(machine) == [] and not any(
        call.startswith("docker compose") for call in machine.calls
    )


def test_the_pool_installer_adds_a_missing_compose_plugin_too(tmp_path, today):
    machine = today.copy_to(tmp_path / "host")
    mark = machine.mark()
    result = machine.bash(
        "install.sh",
        "--proxy",
        "demo",
        "--admit-cases",
        ADMIT_CASES,
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-plugin",
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert (
        "installed the docker compose plugin: the apt package docker-compose-plugin"
        in result.stdout
    )
    assert "POOL INSTALLED AND VERIFIED" in result.stdout
    calls = machine.calls_since(mark)
    added = next(index for index, call in enumerate(calls) if call.startswith("apt-get "))
    assert added < next(
        index for index, call in enumerate(calls) if call.startswith("docker build")
    )


def test_a_new_box_without_the_compose_plugin_joins(joining):
    """The whole join, with the real join service and the real installer, on a box without the plugin."""
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-v2",
    )
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert "installed the docker compose plugin: the apt package docker-compose-v2" in result.stdout
    assert "JOINED: " in result.stdout
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )


def test_a_new_box_on_which_the_plugin_cannot_be_added_keeps_its_window(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    failed = join(box, window, "--name", "box-a", "--workers", "2", STUB_COMPOSE_MISSING="1")
    assert (
        failed.returncode == 1
        and "step 4 failed" in failed.stderr
        and "could not be added" in failed.stderr
    )
    assert "Nothing else was installed and the pool has signed nothing" in failed.stderr
    assert not box.path(INSTANCE).exists()
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*")) and not any(
        "sign-csr" in call for call in pool.calls
    )
    assert window.process.poll() is None and window.service_runs()
    # The plugin put there (here: the package appears), the SAME command joins in the SAME window.
    result = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_COMPOSE_MISSING="1",
        STUB_APT_HAS="docker-compose-plugin",
    )
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output


# --- I. a Docker without the buildx component builds with its own builder ----------------------------------------------


def lean_builds(machine: Machine) -> list[str]:
    """The builds of the Lean server's image (not the questions put to `docker build --help`)."""
    return [
        call
        for call in machine.calls
        if call.startswith("docker build") and "--help" not in call and "acme/kimina:" in call
    ]


def test_a_docker_without_buildx_builds_without_the_progress_flag_and_ends_verified(
    tmp_path, deploy
):
    """A join stopped at step 7 on "unknown flag: --progress": Ubuntu's docker.io has no buildx."""
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash("install.sh", "--instance", "demo", "--no-door", STUB_LEGACY_BUILDER="1")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    assert lean_builds(machine) and not any("--progress" in call for call in lean_builds(machine))


def test_a_docker_with_buildkit_is_asked_for_plain_progress(tmp_path, deploy):
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash("install.sh", "--instance", "demo", "--no-door")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert lean_builds(machine) and all("--progress=plain" in call for call in lean_builds(machine))


def test_the_stand_in_refuses_the_flag_as_the_real_legacy_builder_did(tmp_path, deploy):
    """The harness must be able to fail the way the real box did: the installer as it was stops on the flag."""
    before = copy_deploy(deploy, tmp_path / "deploy")
    installer = before / "pool-lib.sh"  # build_with_heartbeat, every image build's
    asks = "  if docker build --help 2>/dev/null | grep -q -- '--progress'; then progress=(--progress=plain); fi\n"
    assert installer.read_text().count(asks) == 1
    installer.write_text(installer.read_text().replace(asks, "  progress=(--progress=plain)\n"))
    machine = Machine(tmp_path / "host", before)
    result = machine.bash("install.sh", "--instance", "demo", "--no-door", STUB_LEGACY_BUILDER="1")
    assert (
        result.returncode == 1
        and "unknown flag: --progress" in result.stderr
        and "the image build failed" in result.stderr
    )


def test_a_new_box_without_buildx_joins(joining):
    """The whole join on such a box, with the real join service and the real installer."""
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_LEGACY_BUILDER="1",
        STUB_MACHINE="riscv64",
    )
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert "JOINED: " in result.stdout
    assert lean_builds(box) and not any("--progress" in call for call in lean_builds(box))
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )


# --- J. the image is built before the pool is asked, and not twice -----------------------------------------------------

LEAN_IMAGE_STATE = "built.acme_kimina_lean-demo-1"  # where the stand-in keeps "this image exists"


def test_build_only_builds_the_image_and_nothing_else(tmp_path, deploy):
    machine = Machine(tmp_path / "box", deploy)
    result = machine.bash(
        "install.sh",
        "--instance",
        "demo",
        "--no-door",
        "--tls-dir",
        "/not/there/yet",
        "--server-name",
        "box-a",
        "--build-only",
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "BUILT acme/kimina:lean-demo-1, and nothing else" in result.stdout
    assert sorted(path.name for path in machine.path(INSTANCE).iterdir()) == [
        "Dockerfile.demo",
        "build.log",
        "image.built",
        "setup-demo.sh",
    ]
    assert len(lean_builds(machine)) == 1
    # No key, no settings, no certificate, no container, no firewall unit, no door, no package manager.
    for directory in ("/usr/local/sbin", "/usr/local/bin", "/etc/systemd/system", "/etc/sudoers.d"):
        assert list(machine.path(directory).iterdir()) == [], directory
    assert not any(
        re.search(r"^docker (run|pull|compose .* up)|^systemctl|^apt-get|^dnf|^yum", call)
        for call in machine.calls
    )
    assert not [
        path
        for path in machine.root.rglob("*")
        if path.is_file() and b"LEAN_SERVER_API_KEY" in path.read_bytes()
    ]
    # The stamp: the inputs' digest, then the image they made.
    inputs, image = machine.path(f"{INSTANCE}/image.built").read_text().split()
    assert (
        re.fullmatch(r"[0-9a-f]{64}", inputs)
        and image == "sha256:" + (machine.stub / "state" / LEAN_IMAGE_STATE).read_text()
    )


def test_the_install_after_a_build_only_run_does_not_build_again_and_a_changed_build_file_does(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "host", deploy)
    options = ("--instance", "demo", "--no-door")
    assert machine.bash("install.sh", *options, "--build-only").returncode == 0
    stamp = machine.path(f"{INSTANCE}/image.built").read_text()
    result = machine.bash("install.sh", *options)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "is already built from these build files: not building it again" in result.stdout
    assert "INSTALLED AND VERIFIED" in result.stdout
    assert (
        len(lean_builds(machine)) == 1
        and machine.path(f"{INSTANCE}/image.built").read_text() == stamp
    )
    # Another job count is another build: Docker would not answer it from what is built either.
    assert machine.bash("install.sh", *options, "--build-jobs", "3").returncode == 0
    assert len(lean_builds(machine)) == 2
    jobs_stamp = machine.path(f"{INSTANCE}/image.built").read_text()
    assert jobs_stamp != stamp
    # A build file that changed: built again, once, and the stamp names the new inputs and the new image.
    changed = copy_deploy(deploy, tmp_path / "deploy")
    with (changed / "site" / "setup-demo.sh").open("a") as handle:
        handle.write("# a later edit\n")
    machine.deploy = changed
    assert machine.bash("install.sh", *options, "--build-jobs", "3").returncode == 0
    assert (
        len(lean_builds(machine)) == 3
        and machine.path(f"{INSTANCE}/image.built").read_text() != jobs_stamp
    )
    assert machine.bash("install.sh", *options, "--build-jobs", "3").returncode == 0
    assert len(lean_builds(machine)) == 3  # and then it is settled again
    # An image that is gone (removed by hand) is built again, whatever the stamp says.
    (machine.stub / "state" / LEAN_IMAGE_STATE).unlink()
    assert machine.bash("install.sh", *options, "--build-jobs", "3").returncode == 0
    assert len(lean_builds(machine)) == 4


def test_a_build_only_run_whose_build_fails_leaves_no_stamp_and_says_so(tmp_path, deploy):
    machine = Machine(tmp_path / "box", deploy)
    result = machine.bash(
        "install.sh", "--instance", "demo", "--no-door", "--build-only", STUB_BUILD_FAILS="1"
    )
    assert result.returncode == 1 and "the image build failed; the full log is" in result.stderr
    assert "Could not resolve host: github.com" in result.stderr  # the build's own last lines
    assert sorted(path.name for path in machine.path(INSTANCE).iterdir()) == [
        "Dockerfile.demo",
        "build.log",
        "setup-demo.sh",
    ]
    assert "BUILT" not in result.stdout


def test_a_new_box_whose_image_does_not_build_keeps_its_window_and_joins_with_the_same_command(
    joining,
):
    """A join that stops inside the image build after the pool has signed costs a window. The build comes
    before the pool is asked for anything."""
    window, box, pool = joining.open(), joining.box, joining.pool
    failed = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_BUILD_FAILS="1",
        STUB_MACHINE="riscv64",
    )  # a box that builds
    assert failed.returncode == 1 and "step 5 failed" in failed.stderr, failed.stderr
    assert (
        "Could not resolve host: github.com" in failed.stderr
        and "the image build failed" in failed.stderr
    )
    assert "The pool has signed nothing, so this join window is still good" in failed.stderr
    # The pool was asked for nothing: no signing request reached the spool, nothing was signed, the window is open.
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*")) and not any(
        "sign-csr" in call for call in pool.calls
    )
    assert window.process.poll() is None and window.service_runs()
    # On the box: the build files, the log and the installer copy. No settings, no key, no certificate, no container.
    assert sorted(path.name for path in box.path(INSTANCE).iterdir()) == [
        "Dockerfile.demo",
        "build.log",
        "installer",
        "setup-demo.sh",
    ]
    assert not any(re.search(r"^docker compose .* up", call) for call in box.calls)
    assert not [
        path
        for path in box.root.rglob("*")
        if path.is_file() and b"PRIVATE KEY" in path.read_bytes()
    ]
    # The cause put right, the SAME command joins in the SAME window; the image is built once more, not twice.
    result = join(box, window, "--name", "box-a", "--workers", "2", STUB_MACHINE="riscv64")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert "is already built from these build files: not building it again" in result.stdout
    assert len(lean_builds(box)) == 2  # the build that failed, the one that passed
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )


def test_what_a_failed_build_left_is_removed_by_the_one_command_it_was_told(joining):
    window, box = joining.open(), joining.box
    failed = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_BUILD_FAILS="1",
        STUB_MACHINE="riscv64",
    )
    assert failed.returncode == 1 and "to remove them: sudo bash " in failed.stderr
    command = failed.stderr.split("to remove them: ", 1)[1].split()[:7]
    assert command[:2] == ["sudo", "bash"] and command[3:] == [
        "--instance",
        "demo",
        "--uninstall",
        "--purge",
    ]
    removed = box.bash(Path(command[2]), *command[3:])
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not box.path(INSTANCE).exists()


def test_a_join_that_builds_builds_before_it_makes_a_key_or_asks_the_pool_to_sign(joining):
    window, box = joining.open(), joining.box
    result = join(box, window, "--name", "box-a", "--workers", "2", STUB_MACHINE="riscv64")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    calls = box.calls
    build = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("docker build") and "acme/kimina:" in call
    )
    key = next(index for index, call in enumerate(calls) if "leanpool-pki csr" in call)
    assert build < key and len(lean_builds(box)) == 1  # built once: the install found it
    steps = [
        line.split()[1] for line in result.stdout.splitlines() if re.match(r"==> \d+/10 ", line)
    ]
    assert steps == [f"{number}/10" for number in range(1, 11)]
    assert result.stdout.index("5/10 The Lean server's image") < result.stdout.index(
        "7/10 Asking pool demo for a certificate"
    )


# --- K. a joining box gets the Lean image from the pool's host, and builds only as a fallback --------------------------

LEAN_TAG = "acme/kimina:lean-demo-1"
EXPORT = f"{POOL}/join/image"


def manifest_of(machine: Machine) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(machine.path(f"{EXPORT}/manifest.json").read_text())
    return manifest


def exported_files(machine: Machine) -> list[str]:
    directory = machine.path(EXPORT)
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def change_the_hosts_image(machine: Machine, digest: str = "c" * 64) -> None:
    """The host's Lean image is another one now (as after its instance installer rebuilt it)."""
    (machine.stub / "state" / LEAN_IMAGE_STATE).write_text(digest)


def test_the_pools_host_exported_its_lean_image_once_for_the_join_services_group_alone(upgraded):
    machine = upgraded.machine
    manifest = manifest_of(machine)
    content = machine.path(f"{EXPORT}/{manifest['file']}").read_bytes()
    assert exported_files(machine) == sorted([manifest["file"], "manifest.json"])
    assert (
        manifest["tag"] == LEAN_TAG
        and manifest["format"] == "docker-save"
        and manifest["docker"] == "28.0.4"
    )
    assert manifest["machine"] == os.uname().machine
    assert (
        manifest["bytes"] == len(content)
        and manifest["sha256"] == hashlib.sha256(content).hexdigest()
    )
    assert manifest["file"] == f"image-{manifest['sha256'][:12]}.tar"
    assert (
        manifest["image_id"] == "sha256:" + (machine.stub / "state" / LEAN_IMAGE_STATE).read_text()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["layers"])
    # Readable by root and the pool image's group (the join service's), by nobody else.
    assert machine.owner(EXPORT) == ("root", "10001", 0o750)
    assert machine.owner(f"{EXPORT}/{manifest['file']}") == ("root", "10001", 0o640)
    assert machine.owner(f"{EXPORT}/manifest.json") == ("root", "10001", 0o640)
    # Once, with its size and its seconds said; and before the pool's containers are touched.
    assert sum(call.startswith("docker save ") for call in upgraded.calls) == 1
    assert re.search(
        rf"exported {re.escape(LEAN_TAG)}: 0\.\d GB in \d+ s \(docker save \d+ s, SHA-256 \d+ s\) to ",
        upgraded.result.stdout,
    )
    assert (
        f"a joining {os.uname().machine} box loads it instead of building" in upgraded.result.stdout
    )


def test_the_export_is_kept_while_the_hosts_image_is_the_same_and_made_again_when_it_changes(pool):
    before, mark = manifest_of(pool), pool.mark()
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert (
        f"keeping the exported image (0.0 GB, {before['file']}): this host's image has not changed"
        in result.stdout
    )
    assert (
        not any(call.startswith("docker save ") for call in pool.calls_since(mark))
        and manifest_of(pool) == before
    )
    change_the_hosts_image(pool)
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    after = manifest_of(pool)
    assert re.search(
        rf"exported {re.escape(LEAN_TAG)}: 0\.\d GB in \d+ s \(docker save \d+ s, SHA-256 \d+ s\)",
        result.stdout,
    )
    assert after["image_id"] == "sha256:" + "c" * 64 and after["sha256"] != before["sha256"]
    assert exported_files(pool) == sorted(
        [after["file"], "manifest.json"]
    )  # the older file is gone


def test_an_image_that_carries_the_api_key_variable_is_never_exported_and_an_older_export_goes(
    pool,
):
    change_the_hosts_image(pool)
    result = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_IMAGE_CARRIES_KEY="1"
    )
    assert result.returncode == 0, (
        result.stdout[-2000:] + result.stderr[-2000:]
    )  # the pool itself is fine
    assert (
        f"the image {LEAN_TAG} carries LEAN_SERVER_API_KEY in its config: it is NOT exported"
        in result.stderr
    )
    assert exported_files(pool) == []
    window = Window(pool, **THE_LAN_KNOWS_BOX_A)
    window.wait_for_the_command()
    assert (
        "This window ships no image (this host has exported none): a box builds its own."
        in window.output
    )
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130


def test_a_host_without_the_image_or_without_the_disk_exports_nothing_and_the_pool_still_installs(
    pool,
):
    change_the_hosts_image(pool)
    result = pool.bash(
        "install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES, STUB_DISK_FREE="10"
    )  # the image is 12 GiB
    assert result.returncode == 0 and exported_files(pool) == []
    assert (
        f"exporting {LEAN_TAG} needs 14 GB free under" in result.stderr
        and "10 GB are" in result.stderr
    )
    (pool.stub / "state" / LEAN_IMAGE_STATE).unlink()
    result = pool.bash("install.sh", "--proxy", "demo", "--admit-cases", ADMIT_CASES)
    assert result.returncode == 0 and exported_files(pool) == []
    assert (
        f"this host holds no image {LEAN_TAG}: nothing to ship, a joining box builds its own"
        in result.stdout
    )


def test_a_window_says_what_it_ships_and_does_not_ship_an_export_of_an_image_the_host_no_longer_holds(
    joining,
):
    window, pool, box = joining.open(), joining.pool, joining.box
    assert (
        f"This window ships the Lean server's image {LEAN_TAG} (0.0 GB, for {os.uname().machine} boxes): "
        "a box loads it instead of building."
    ) in window.output
    started = next(
        call for call in pool.calls if call.startswith("docker run -d --name lean-pool-demo-join ")
    )
    assert f"{pool.path(EXPORT)}:/run/join-image:ro" in started and started.endswith(
        "--image-directory /run/join-image"
    )
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130
    change_the_hosts_image(pool)  # the host's image changed; nobody ran install.sh --proxy
    window = joining.open()
    assert (
        "This window ships no image (" in window.output
        and "the host's image changed" in window.output
    )
    assert "install.sh --proxy demo exports it again): a box builds its own." in window.output
    started = [
        call for call in pool.calls if call.startswith("docker run -d --name lean-pool-demo-join ")
    ][-1]
    assert "/run/join-image" not in started
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        "the Lean server's image is built here: the pool ships no image in this window"
        in result.stdout
    )
    assert len(lean_builds(box)) == 1


def test_a_box_loads_the_image_its_pool_ships_and_never_builds(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    manifest = manifest_of(pool)
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        f"the Lean server's image comes from the pool: {LEAN_TAG}, 0.0 GB to download and load"
        in result.stdout
    )
    assert (
        f"loaded {LEAN_TAG} from the pool" in result.stdout
        and "came from this box's pool: not building it" in result.stdout
    )
    assert "INSTALLED AND VERIFIED" in result.stdout and "JOINED: " in result.stdout
    assert (
        lean_builds(box) == [] and sum(call.startswith("docker load ") for call in box.calls) == 1
    )
    # The image on the box is the pool's: the same layers. Where it came from is recorded.
    assert (box.stub / "state" / LEAN_IMAGE_STATE).read_text() == (
        pool.stub / "state" / LEAN_IMAGE_STATE
    ).read_text()
    assert box.path(f"{INSTANCE}/image.built").read_text() == f"pool {manifest['layers']}\n"
    # The file is gone, and nothing of a build was put on the box.
    assert (
        sorted(path.name for path in box.path(INSTANCE).iterdir() if "pool-image" in path.name)
        == []
    )
    assert not box.path(f"{INSTANCE}/build.log").exists()
    # The image was fetched before anything was signed, from the join service, which logged no token.
    calls = box.calls
    assert next(i for i, call in enumerate(calls) if call.startswith("docker load ")) < next(
        i for i, call in enumerate(calls) if "leanpool-pki csr" in call
    )
    log = (pool.stub / "join.log").read_text()
    assert (
        "the image's manifest was fetched from 127.0.0.1" in log
        and "the image is being sent to 127.0.0.1" in log
    )
    assert window.token not in log


def test_a_box_whose_docker_keeps_images_the_other_way_still_takes_the_pools_image(joining):
    """Docker's image ID is the config's digest under one image store and the manifest's under the other. The
    box compares layers, which are the same list under both."""
    window, box = joining.open(), joining.box
    result = join(box, window, "--name", "box-a", "--workers", "2", STUB_IMAGE_STORE="containerd")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert f"loaded {LEAN_TAG} from the pool" in result.stdout and lean_builds(box) == []


def test_a_box_of_another_machine_type_builds_its_own_image_and_says_why(joining):
    window, box = joining.open(), joining.box
    result = join(box, window, "--name", "box-a", "--workers", "2", STUB_MACHINE="riscv64")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        f"the Lean server's image is built here: the pool's image is for {os.uname().machine} machines "
        "and this box is riscv64"
    ) in result.stdout
    assert len(lean_builds(box)) == 1 and not any(
        call.startswith("docker load ") for call in box.calls
    )


def test_a_box_told_to_build_builds_though_the_pool_ships_an_image_and_never_asks_for_it(joining):
    """The way round a box whose Docker cannot take the pool's image: `--build`."""
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-a", "--workers", "2", "--build")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        "the Lean server's image is built here: --build was given" in result.stdout
        and "JOINED: " in result.stdout
    )
    assert len(lean_builds(box)) == 1 and not any(
        call.startswith("docker load ") for call in box.calls
    )
    log = (pool.stub / "join.log").read_text()
    assert "the image's manifest was fetched" not in log and "the image is being sent" not in log


def test_a_download_that_is_cut_short_goes_on_where_it_stopped_with_a_real_curl_and_the_real_service(
    joining,
):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(box, window, "--name", "box-a", "--workers", "2", STUB_CUT_FIRST_DOWNLOAD="1")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    stopped = re.search(
        r"the download stopped at (\d+) of (\d+) bytes: going on from there \(attempt 2 of 5\)",
        result.stdout,
    )
    assert (
        stopped and 0 < int(stopped.group(1)) < int(stopped.group(2)) == manifest_of(pool)["bytes"]
    )
    assert (
        "the image is being sent to 127.0.0.1 (a part of it: a download goes on)"
        in (pool.stub / "join.log").read_text()
    )
    assert (
        f"loaded {LEAN_TAG} from the pool" in result.stdout and lean_builds(box) == []
    )  # and its SHA-256 held


def test_a_tampered_export_is_an_alarm_nothing_is_loaded_or_built_and_the_window_is_still_good(
    joining,
):
    window, box, pool = joining.open(), joining.box, joining.pool
    file = pool.path(f"{EXPORT}/{manifest_of(pool)['file']}")
    content = file.read_bytes()
    file.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))  # the same size, one bit changed
    failed = join(box, window, "--name", "box-a", "--workers", "2")
    assert failed.returncode == 1, failed.stdout[-1500:]
    assert (
        "step 5 ALARM: the file the pool sent does not have the SHA-256 its manifest names"
        in failed.stderr
    )
    assert "NOTHING was built in its place" in failed.stderr
    assert lean_builds(box) == [] and not any(call.startswith("docker load ") for call in box.calls)
    assert not (box.stub / "state" / LEAN_IMAGE_STATE).exists()
    assert not [path for path in box.root.rglob("*") if "pool-image" in path.name]
    # Nothing was signed and the window is as it was.
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*")) and not any(
        "sign-csr" in call for call in pool.calls
    )
    assert window.process.poll() is None and window.service_runs()


def test_an_export_that_holds_a_second_tag_is_an_alarm_and_both_are_removed_from_the_box(joining):
    pool, box = joining.pool, joining.box
    change_the_hosts_image(pool)
    assert (
        pool.bash(
            "install.sh",
            "--proxy",
            "demo",
            "--admit-cases",
            ADMIT_CASES,
            STUB_SAVE_EXTRA_TAGS="haproxy:3.0",
        ).returncode
        == 0
    )
    window = joining.open()
    failed = join(box, window, "--name", "box-a", "--workers", "2")
    assert failed.returncode == 1
    assert (
        f"step 5 ALARM: the load brought in something other than the one image {LEAN_TAG}"
        in failed.stderr
    )
    assert (box.stub / "image-removals").read_text().split() == [LEAN_TAG, "haproxy:3.0"]
    assert lean_builds(box) == [] and not (box.stub / "state" / LEAN_IMAGE_STATE).exists()
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*"))


def test_a_loaded_image_with_other_layers_is_an_alarm_and_is_removed(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    failed = join(box, window, "--name", "box-a", "--workers", "2", STUB_LOAD_OTHER_LAYERS="1")
    assert failed.returncode == 1
    assert (
        f"step 5 ALARM: the image now under {LEAN_TAG} does not have the layers the pool's manifest names"
        in failed.stderr
    )
    assert (box.stub / "image-removals").read_text().split() == [LEAN_TAG] and lean_builds(
        box
    ) == []
    assert not any(pool.path(POOL).glob(".join.*/spool/requests/*")) and window.service_runs()


def test_a_box_whose_docker_cannot_load_the_export_builds_its_own_and_joins_in_the_same_window(
    joining,
):
    window, box, pool = joining.open(), joining.box, joining.pool
    result = join(
        box,
        window,
        "--name",
        "box-a",
        "--workers",
        "2",
        STUB_LOAD_FAILS="1",
        STUB_DOCKER_VERSION="24.0.7",
    )
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        "building the image here instead: this box's Docker could not load the pool's image: open /var/lib/docker/tmp/"
        "docker-import-1/blobs/sha256: unsupported image format: this Docker cannot read it "
        "(the pool's Docker is 28.0.4, this box's is 24.0.7)"
    ) in result.stdout
    assert len(lean_builds(box)) == 1 and not [
        path for path in box.root.rglob("*") if "pool-image" in path.name
    ]
    assert box.path(f"{INSTANCE}/image.built").read_text().split()[0] != "pool"
    assert (
        pool.path(f"{POOL}/servers").read_text().splitlines()[-1] == "box-a box-a.lan:18001 2 18201"
    )


def test_an_export_cut_short_on_the_pools_host_is_not_served_and_the_box_builds(joining):
    window, box, pool = joining.open(), joining.box, joining.pool
    file = pool.path(f"{EXPORT}/{manifest_of(pool)['file']}")
    file.write_bytes(file.read_bytes()[:1000])  # not the size the manifest records
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert (
        "the Lean server's image is built here: the pool ships no image in this window"
        in result.stdout
    )
    assert len(lean_builds(box)) == 1


def test_a_box_that_loads_needs_the_loads_disk_and_not_the_builds(joining):
    """A build needs 60 GB where Docker keeps its images; a load needs the file and the image."""
    window, box = joining.open(), joining.box
    failed = join(box, window, "--name", "box-a", "--workers", "2", STUB_DISK_FREE="3")
    assert (
        failed.returncode == 1
        and "step 4 failed" in failed.stderr
        and "need at least 4 GB free" in failed.stderr
    )
    assert (
        "a join that loads its pool's image needs room for the file and the image, not for a build"
        in failed.stderr
    )
    assert "Nothing was installed and the pool has signed nothing" in failed.stderr
    result = join(
        box, window, "--name", "box-a", "--workers", "2", STUB_DISK_FREE="10"
    )  # far too little for a build
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    assert lean_builds(box) == [] and f"loaded {LEAN_TAG} from the pool" in result.stdout


def test_a_box_keeps_the_pools_image_on_a_later_install_though_its_build_files_differ(
    joining, tmp_path
):
    """The pool's image is the pool's pin. The host's image may have been built from older build files than a
    refresh ships: a box that loaded it must not rebuild because its own copy of the Dockerfile differs."""
    window, box = joining.open(), joining.box
    result = join(box, window, "--name", "box-a", "--workers", "2")
    assert window.finish() == 0 and result.returncode == 0, result.stderr + window.output
    stamp = box.path(f"{INSTANCE}/image.built").read_text()
    changed = copy_deploy(box.deploy, tmp_path / "deploy")
    with (changed / "Dockerfile.demo").open("a") as handle:
        handle.write("# a later edit of the build files\n")
    box.deploy = changed
    again = box.bash("install.sh", "--instance", "demo", "--no-door", STUB_DISK_FREE="10")
    assert again.returncode == 0, again.stdout[-2000:] + again.stderr[-2000:]
    assert "is this box's pool's, loaded by a join: keeping it, not building" in again.stdout
    assert lean_builds(box) == [] and box.path(f"{INSTANCE}/image.built").read_text() == stamp
    # Taking the record away is how the box is told to build its own.
    box.path(f"{INSTANCE}/image.built").unlink()
    assert box.bash("install.sh", "--instance", "demo", "--no-door").returncode == 0
    assert len(lean_builds(box)) == 1


def test_the_installer_refuses_an_image_that_is_not_the_one_it_was_told_came_from_the_pool(
    tmp_path, deploy
):
    machine = Machine(tmp_path / "box", deploy)
    options = ("install.sh", "--instance", "demo", "--no-door")
    absent = machine.bash(*options, "--image-from-pool", "a" * 64)
    assert (
        absent.returncode == 1
        and f"--image-from-pool: there is no image {LEAN_TAG} on this host; nothing was built"
        in absent.stderr
    )
    assert machine.bash(*options, "--build-only").returncode == 0
    other = machine.bash(*options, "--image-from-pool", "a" * 64)
    assert (
        other.returncode == 1
        and "does not have the layers given" in other.stderr
        and "nothing was built" in other.stderr
    )
    assert machine.bash(*options, "--image-from-pool", "not-an-identity").returncode == 1
    assert (
        machine.bash("install.sh", "--no-door", "--image-from-pool", "a" * 64).returncode == 1
    )  # instance 1 builds its own
    assert len(lean_builds(machine)) == 1  # the one build-only run, and no other


def test_the_image_requests_answer_a_real_curl_with_ranges_and_only_with_the_token(joining):
    window, pool = joining.open(), joining.pool
    manifest = manifest_of(pool)
    content = pool.path(f"{EXPORT}/{manifest['file']}").read_bytes()
    status, body = ask(window, "/image.json")
    assert status == "200" and json.loads(body) == manifest
    part = subprocess.run(
        ["curl", "-sSk", "--pinnedpubkey", window.pin, "-r", "1000-1999", window.url + "/image"],
        capture_output=True,
        timeout=60,
    )
    assert part.returncode == 0 and part.stdout == content[1000:2000]
    rest = subprocess.run(
        [
            "curl",
            "-sSk",
            "--pinnedpubkey",
            window.pin,
            "-H",
            f"Range: bytes={len(content) - 300}-",
            window.url + "/image",
        ],
        capture_output=True,
        timeout=60,
    )
    assert rest.stdout == content[-300:]
    wrong = window.url[:-1] + ("A" if window.url[-1] != "A" else "B")
    refused = subprocess.run(
        [
            "curl",
            "-sSk",
            "--pinnedpubkey",
            window.pin,
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            wrong + "/image",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert refused.stdout == "404"
    assert not any(
        pool.path(POOL).glob(".join.*/spool/requests/*")
    )  # none of this bound the window
    window.process.send_signal(signal.SIGINT)
    assert window.finish() == 130


# --- the control group, on the pool's own host and on a box that joined before it ------------------------


def test_the_upgrade_puts_this_hosts_lean_server_in_its_control_group_and_leaves_instance_one_in_its_own(
    upgraded,
):
    """`install.sh --proxy demo` is how the pool's own host is refreshed: its Lean server (the `demo` instance)
    gets the group there. Instance 1 is not this command's to touch."""
    machine = upgraded.machine
    assert upgraded.result.returncode == 0, (
        upgraded.result.stdout[-3000:] + upgraded.result.stderr[-3000:]
    )
    said = dict(
        line.split("=", 1)
        for line in machine.path(f"{INSTANCE}/kimina.env").read_text().splitlines()
        if "=" in line
    )
    assert (
        said["LEAN_SERVER_CPU_PRIORITY"] == "lowest"
        and said["LEAN_SERVER_CGROUP_PARENT"] == "leanpool-demo.slice"
    )
    assert (
        said["LEANPOOL_AGENT_LEAN_CPU_STAT_PATH"]
        == "/host-cgroup/leanpool.slice/leanpool-demo.slice/cpu.stat"
    )
    assert machine.control_group(LEAN) == "leanpool-demo.slice"
    assert "CPUWeight=1" in machine.path("/etc/systemd/system/leanpool.slice").read_text()
    # The box was asked before the server was made again: the group first, then the compose project.
    calls = upgraded.calls
    tried = next(
        index
        for index, call in enumerate(calls)
        if call.startswith("docker run --rm --cgroup-parent leanpool-demo.slice ")
    )
    started = next(
        index
        for index, call in enumerate(calls)
        if "--project-name lean-server-demo " in call and call.endswith(" up -d")
    )
    assert tried < started
    assert (
        machine.control_group("lean-server-kimina-1") == "leanpool-main.slice"
    )  # instance 1: its own, from its own install
    output = status(machine, "@demo").stdout
    assert (
        "--- cpu priority: lowest (control group leanpool-demo.slice under leanpool.slice, CPU weight 1"
        in output
    )
    assert "--- usage agent says: ready up 97% maxconn:8 (maxconn = the open slots" in output


def status_program() -> str:
    """The program `lean-pool-ctl status` runs inside the pool, up to where it starts asking the proxy."""
    script = (Path(__file__).resolve().parents[2] / "deploy/install/lean-pool-ctl").read_text()
    program = script.split("inside_pool <<'PY' || echo \"--- the pool did not answer", 1)[1].split(
        "\n", 1
    )[1]
    return program.split("\nkey = open(", 1)[0]


def test_the_pools_status_line_says_in_flight_open_slots_and_workers(monkeypatch):
    """`in flight   3/  5 of 13`. HAProxy's statistics hold a server's limit as it is now;
    the worker count it was listed with comes from the server list."""
    monkeypatch.setenv("POOL_SERVER_WORKERS", "desk-1=13 workstation=12 ")
    scope: dict[str, Any] = {}
    exec(status_program(), scope)  # its own definitions, nothing asked
    row = {
        "svname": "desk-1",
        "status": "UP",
        "weight": "22",
        "scur": "3",
        "slim": "5",
        "qcur": "0",
        "stot": "941",
    }
    assert scope["server_line"](row, scope["listed"]) == (
        "--- server desk-1               UP         weight   22  in flight   3/  5 of 13  queued   0  total 941"
        "  version ?"
    )
    drained = {**row, "status": "DRAIN (agent)", "weight": "0", "scur": "0", "slim": "13"}
    assert "DRAIN (agent) weight    0  in flight   0/ 13 of 13" in scope["server_line"](
        drained, scope["listed"]
    )
    # A server the list does not name (an older tool beside a newer list) is shown without a list's name.
    unknown = {**row, "svname": "laptop"}
    assert scope["server_line"](unknown, scope["listed"]).endswith(
        "in flight   3/  5  queued   0  total 941  version ?"
    )


@pytest.mark.parametrize(
    ("last_agt", "shown"),
    [
        ("via agent : up (version 0123456789abcdef0123456789abcdef)", "0123456789ab"),
        ("via agent : up (version 0123ab)", "0123ab"),
        # Installed before it stated one, a drain (HAProxy keeps no text of it), no agent at all.
        ("via agent : up", "?"),
        ("", "?"),
        ("agent reports an error : nonsense", "?"),
    ],
)
def test_the_pools_status_line_ends_with_the_version_the_boxs_agent_states(last_agt, shown):
    scope: dict[str, Any] = {}
    exec(status_program(), scope)
    row = {
        "svname": "desk-1",
        "status": "UP",
        "weight": "22",
        "scur": "3",
        "slim": "5",
        "qcur": "0",
        "stot": "941",
        "last_agt": last_agt,
    }
    assert scope["server_line"](row, {}).endswith(f"total 941  version {shown}")


def test_status_hands_the_listed_worker_counts_to_the_program(pool):
    mark = pool.mark()
    result = pool.bash(pool.path("/usr/local/sbin/lean-pool-ctl"), "demo", "status")
    assert result.returncode == 0, result.stderr
    (asked,) = [
        call
        for call in pool.calls_since(mark)
        if call.startswith("docker run --rm -i ") and "POOL_SERVER_WORKERS=" in call
    ]
    workers = (
        pool.path(f"{INSTANCE}/kimina.env")
        .read_text()
        .split("LEAN_SERVER_MAX_REPLS=")[1]
        .split()[0]
    )
    assert f"POOL_SERVER_WORKERS={listed(pool)}={workers} " in asked
