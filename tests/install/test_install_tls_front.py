"""A box's TLS front, and what it must not disturb.

With TLS a Lean server instance publishes neither its server nor its usage agent: one more HAProxy container
on the instance's own Docker network does, and lets in only the pool proxy's certificate. That network is
the one `kimina-firewall` forbids to start connections, so the front needs exactly one exception, and
instance 1 (the instance with no name, which stays plain HTTP) must get exactly the rules it had.

What runs here for real: the firewall script against a recording `iptables`, the shell functions of
pool-lib.sh that are pure text, and install.sh's argument checks as a non-root user. What needs root and
Docker is held by text checks, each saying so.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent / "site"
INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"
not_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="argument validation is observable only as non-root"
)


def pool_lib(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet with pool-lib.sh sourced, the way install.sh and install-pool.sh source it."""
    script = (
        f"set -euo pipefail\nSRC={shlex.quote(str(INSTALLERS))}\nsay() {{ :; }}\nwarn() {{ :; }}\n"
        f'die() {{ echo "die: $*" >&2; exit 1; }}\n. "$SRC/pool-lib.sh"\n{snippet}\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


# --- kimina-firewall, run against a recording iptables ---------------------------------------------------------


def firewall(tmp_path: Path, verb: str, instance: str, conf: str) -> tuple[int, list[str], str]:
    """Run the shipped kimina-firewall with its /srv prefix moved under tmp_path and `iptables` replaced by
    a recorder (a `-C` check answers "no such rule", everything else succeeds)."""
    directory = tmp_path / "srv" / ("kimina" + (f"-{instance}" if instance else ""))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "firewall.conf").write_text(conf)
    (tmp_path / "bin").mkdir(exist_ok=True)
    stub = tmp_path / "bin" / "iptables"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$IPTABLES_LOG"\n[ "$1" != -C ]\n'
    )
    stub.chmod(0o755)
    script = tmp_path / "kimina-firewall"
    text = (INSTALLERS / "kimina-firewall").read_text()
    assert text.count('"/srv/kimina') == 1
    script.write_text(text.replace('"/srv/kimina', f'"{tmp_path}/srv/kimina'))
    log = tmp_path / "iptables.log"
    log.write_text("")
    result = subprocess.run(
        ["bash", str(script), verb, *([instance] if instance else [])],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PATH": f"{tmp_path}/bin:{os.environ['PATH']}",
            "IPTABLES_LOG": str(log),
        },
    )
    return result.returncode, log.read_text().splitlines(), result.stderr


INSTANCE_ONE = "KIMINA_FW_PORT=18000\nKIMINA_FW_SUBNET=192.168.50.0/24\nKIMINA_FW_CONTAINER_NET=172.29.53.0/24\n"
DEMO = "KIMINA_FW_PORT=18001\nKIMINA_FW_SUBNET=192.168.50.0/24\nKIMINA_FW_CONTAINER_NET=172.29.54.0/24\n"
# What the firewall script did for instance 1 before it knew of TLS fronts (an earlier release, run
# against the same recorder): every command that changes a rule, in order.
INSTANCE_ONE_RULES_BEFORE_TLS = [
    "-N KIMINA-LAN",
    "-A KIMINA-LAN -s 192.168.50.0/24 -j RETURN",
    "-A KIMINA-LAN -s 172.16.0.0/12 -j RETURN",
    "-A KIMINA-LAN -s 127.0.0.0/8 -j RETURN",
    "-A KIMINA-LAN -j DROP",
    "-I DOCKER-USER 1 -p tcp -m conntrack --ctorigdstport 18000 --ctdir ORIGINAL -j KIMINA-LAN",
    "-N KIMINA-EGRESS",
    "-A KIMINA-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
    "-A KIMINA-EGRESS -j DROP",
    "-I DOCKER-USER 1 -s 172.29.53.0/24 -j KIMINA-EGRESS",
    "-I INPUT 1 -s 172.29.53.0/24 -j KIMINA-EGRESS",
]


def changes(commands: list[str]) -> list[str]:
    """The commands that add something: checks, flushes and deletions of what is not there change nothing."""
    return [command for command in commands if command.split()[0] in ("-N", "-A", "-I")]


def test_instance_one_gets_exactly_the_firewall_rules_it_had(tmp_path):
    """Instance 1 must not change at all. The script is shared by every instance on the host, so this
    runs it for instance 1 and compares every rule it adds with what the previous script added."""
    code, commands, error = firewall(tmp_path, "apply", "", INSTANCE_ONE)
    assert code == 0, error
    assert changes(commands) == INSTANCE_ONE_RULES_BEFORE_TLS
    assert not any("ACCEPT" in command for command in commands)
    # All it does beyond that is ask about, and try to clear, a KIMINA-FRONT chain that does not exist.
    asked = [command for command in commands if "KIMINA-FRONT" in command]
    assert asked == [
        "-C DOCKER-USER -s 172.29.53.0/24 -d 172.29.53.0/24 -j KIMINA-FRONT",
        "-F KIMINA-FRONT",
        "-X KIMINA-FRONT",
    ]


def test_a_fronted_instance_gets_the_same_rules_and_one_exception_for_the_fronts_address(tmp_path):
    code, commands, error = firewall(
        tmp_path, "apply", "demo", DEMO + "KIMINA_FW_FRONT_ADDRESS=172.29.54.250\n"
    )
    assert code == 0, error
    added = changes(commands)
    # The egress block is what it was: the whole network, replies only, in DOCKER-USER and INPUT.
    assert added[:11] == [
        rule.replace("KIMINA-LAN", "KIMINA-LAN-DEMO")
        .replace("KIMINA-EGRESS", "KIMINA-EGRESS-DEMO")
        .replace("18000", "18001")
        .replace("172.29.53.", "172.29.54.")
        for rule in INSTANCE_ONE_RULES_BEFORE_TLS
    ]
    # The exception: TCP from the front's one address, and only towards its own network.
    assert added[11:] == [
        "-N KIMINA-FRONT-DEMO",
        "-A KIMINA-FRONT-DEMO -s 172.29.54.250 -p tcp -j ACCEPT",
        "-I DOCKER-USER 1 -s 172.29.54.0/24 -d 172.29.54.0/24 -j KIMINA-FRONT-DEMO",
    ]
    # It is inserted AFTER the egress jump, so it sits above it; and never in INPUT: the front may not
    # call this host either.
    assert not any(command.startswith("-I INPUT") and "FRONT" in command for command in commands)
    assert [command for command in commands if "ACCEPT" in command] == [
        "-A KIMINA-FRONT-DEMO -s 172.29.54.250 -p tcp -j ACCEPT"
    ]


def test_the_exception_goes_when_the_rules_are_removed_or_the_front_is_gone(tmp_path):
    code, commands, error = firewall(
        tmp_path, "remove", "demo", DEMO + "KIMINA_FW_FRONT_ADDRESS=172.29.54.250\n"
    )
    assert code == 0, error
    assert (
        "-F KIMINA-FRONT-DEMO" in commands
        and "-X KIMINA-FRONT-DEMO" in commands
        and not changes(commands)
    )
    # Re-applied for an instance that no longer names a front: the chain is cleared and not made again.
    code, commands, error = firewall(tmp_path, "apply", "demo", DEMO)
    assert code == 0, error
    assert "-X KIMINA-FRONT-DEMO" in commands and not any(
        command.startswith("-N KIMINA-FRONT") for command in commands
    )


@pytest.mark.parametrize(
    "address", ["172.29.54.0/24", "front", "0.0.0.0/0 -j ACCEPT", "172.29.54.250 -p udp"]
)
def test_a_front_address_that_is_not_one_ipv4_address_is_refused_before_any_rule(tmp_path, address):
    code, commands, error = firewall(
        tmp_path, "apply", "demo", DEMO + f'KIMINA_FW_FRONT_ADDRESS="{address}"\n'
    )
    assert code == 2 and "must be an IPv4 address" in error and commands == []


# --- the Lean server's compose file, with and without its published port -----------------------------------------


def test_the_lean_servers_compose_file_still_publishes_its_port_as_before():
    """Instance 1 runs from this file as it is: its published port is what a re-run of install.sh must find."""
    yaml = pytest.importorskip("yaml")
    kimina = yaml.safe_load((INSTALLERS / "docker-compose.yml").read_text())["services"]["kimina"]
    assert kimina["ports"] == ["0.0.0.0:${KIMINA_PORT:-18000}:8000"]


def test_behind_a_front_the_same_file_loses_its_ports_block_and_nothing_else():
    yaml = pytest.importorskip("yaml")
    result = pool_lib('unpublished_compose "$SRC/docker-compose.yml"')
    assert result.returncode == 0, result.stderr
    original = yaml.safe_load((INSTALLERS / "docker-compose.yml").read_text())
    unpublished = yaml.safe_load(result.stdout)
    assert "ports" not in unpublished["services"]["kimina"]
    del original["services"]["kimina"]["ports"]
    assert unpublished == original  # every other setting, hardening included
    kept = set(result.stdout.splitlines())
    removed = [
        line
        for line in (INSTALLERS / "docker-compose.yml").read_text().splitlines()
        if line not in kept
    ]
    assert (
        removed[0] == "    ports:" and removed[-1] == '      - "0.0.0.0:${KIMINA_PORT:-18000}:8000"'
    )
    assert all(line.startswith("      ") for line in removed[1:])


def test_the_lean_server_has_an_init_process_with_and_without_its_published_port():
    """The server is its container's first process and collects no process but its own children, so every
    worker it closes leaves a zombie (test_install_reaping.py runs that mechanism).
    `init: true` puts Docker's init in front of it: in the file instance 1 runs from, and in the form a pool
    member runs from, which must lose its ports and not this line."""
    yaml = pytest.importorskip("yaml")
    text = (INSTALLERS / "docker-compose.yml").read_text()
    result = pool_lib('unpublished_compose "$SRC/docker-compose.yml"')
    assert result.returncode == 0, result.stderr
    for form in (text, result.stdout):
        service = yaml.safe_load(form)["services"]["kimina"]
        assert service["init"] is True
        # What would get in an init's way is not there, and the limit the zombies filled still is. (A read-only
        # root is no such thing: the installer decides it, and with nothing decided it stays writable.)
        assert "entrypoint" not in service and service["pids_limit"] == 4096
        assert service["read_only"] == "${KIMINA_ROOT_READ_ONLY:-false}"
        # A key of the service itself (four spaces), as the port-stripping awk needs every line it keeps to be.
        assert form.count("\n    init: true\n") == 1
    # The comment beside the line names the test that runs the mechanism, and that test is there.
    assert "tests/install/test_install_reaping.py" in text
    assert (Path(__file__).resolve().parent / "test_install_reaping.py").is_file()


def test_the_fronts_address_is_the_250_of_a_slash_24_and_nothing_is_guessed_for_other_networks():
    assert pool_lib("front_address_for 172.29.54.0/24").stdout.strip() == "172.29.54.250"
    for network in ("172.29.0.0/16", "172.29.54.128/25", "172.29.54.7/24", "nonsense"):
        assert pool_lib(f"front_address_for {network}").returncode != 0


def test_the_front_and_the_agent_behind_it():
    """The compose file of a front: what is published, what is not, and how it is locked down."""
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((INSTALLERS / "docker-compose.front.yml").read_text())
    front, agent = compose["services"]["front"], compose["services"]["agent"]
    # The front publishes the Lean port and the usage port; the agent publishes nothing.
    assert front["ports"] == [
        "0.0.0.0:${KIMINA_PORT:?set by install.sh}:${KIMINA_PORT}",
        "0.0.0.0:${KIMINA_AGENT_PORT:?set by install_tls_front}:${KIMINA_AGENT_PORT}",
    ]
    assert "ports" not in agent
    for service in (front, agent):
        assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
    # Both on the instance's own network, which the Lean server's project made; the front at a FIXED address.
    assert compose["networks"]["kimina"] == {
        "name": "${KIMINA_NETWORK:?set by install.sh}",
        "external": True,
    }
    assert front["networks"]["kimina"]["ipv4_address"].startswith("${KIMINA_FRONT_ADDRESS:?")
    assert agent["networks"] == ["kimina"]
    # The certificate directory is mounted read-only, where pool-lib.sh renders the config to look for it.
    assert (
        "./tls:/etc/leanpool/tls:ro" in front["volumes"]
        and "./front:/usr/local/etc/haproxy:ro" in front["volumes"]
    )
    assert "LEAN_POOL_TLS_MOUNT=/etc/leanpool/tls" in (INSTALLERS / "pool-lib.sh").read_text()


# --- install.sh: TLS is for a named instance only -----------------------------------------------------------------


def install(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLERS / "install.sh"), "--site", str(SITE), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )


@not_root
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--tls-dir", "/tmp/x", "--server-name", "laptop"), "instance 1 stays plain HTTP"),
        (("--server-name", "laptop"), "instance 1 stays plain HTTP"),
        (("--pool-lean-timeout", "300"), "instance 1 stays plain HTTP"),
        (("--instance", "demo", "--server-name", "laptop"), "go with --tls-dir"),
        (("--instance", "demo", "--tls-dir", "/tmp/x"), "needs --server-name"),
        (
            ("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "Laptop"),
            "one DNS label in lower case",
        ),
        (
            ("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "lap_top"),
            "one DNS label in lower case",
        ),
        (
            ("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "x" * 41),
            "one DNS label in lower case",
        ),
        (
            ("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "laptop.lan"),
            "no dot",
        ),  # a host's name, not a box's
        (
            ("--tls-dir", "/tmp/x", "--server-name", "laptop", "--preflight-only"),
            "instance 1 stays plain HTTP",
        ),
        (
            ("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "laptop", "--no-agent"),
            "--no-agent",
        ),
        (
            (
                "--instance",
                "demo",
                "--tls-dir",
                "/tmp/x",
                "--server-name",
                "laptop",
                "--pool-lean-timeout",
                "5m",
            ),
            "must be seconds",
        ),
        (
            (
                "--instance",
                "demo",
                "--tls-dir",
                "/tmp/x",
                "--server-name",
                "laptop",
                "--pool-server-wait",
                "-1",
            ),
            "must be seconds",
        ),
        (("--instance", "demo", "--tls-dir", "/tmp/x", "--server-name", "laptop"), "run with sudo"),
        (
            (
                "--instance",
                "demo",
                "--tls-dir",
                "/tmp/x",
                "--server-name",
                "lap-top-2",
                "--pool-lean-timeout",
                "300",
                "--pool-server-wait",
                "120",
            ),
            "run with sudo",
        ),
        (
            (
                "--instance",
                "demo",
                "--tls-dir",
                "/tmp/x",
                "--server-name",
                "laptop",
                "--preflight-only",
            ),
            "run with sudo",
        ),
    ],
)
def test_the_tls_options_are_checked_before_root_is_asked_for(arguments, message):
    result = install(*arguments)
    assert result.returncode == 1 and message in result.stderr, result.stderr


def test_instance_one_can_never_be_put_behind_a_front():
    """Text check (the branch runs only as root): whatever is on disk, TLS turns on only for a named
    instance, and install_tls_front refuses an unnamed one itself. Instance 1 stays plain HTTP."""
    text = (INSTALLERS / "install.sh").read_text()
    detect = text[
        text.index("TLS=0\n") : text.index(
            "# ------------------------------------------------------------------------------------------ pre-flight"
        )
    ]
    assert 'if [ -n "$TLS_DIR" ]; then' in detect  # refused above unless --instance was given
    assert 'elif [ -n "$INSTANCE" ] && [ -s "$DIR/tls/box.pem" ]' in detect
    assert detect.count("TLS=1") == 2
    library = (INSTALLERS / "pool-lib.sh").read_text()
    front = library[library.index("install_tls_front() {") : library.index("remove_tls_front() {")]
    assert '[ -n "$instance" ] || die' in front


def test_install_sh_installs_the_firewall_from_the_canonical_file_and_names_the_fronts_address():
    """Text check (needs root): the heredoc is gone, so one file is what every instance's unit runs and what
    the tests above ran; and a fronted instance's firewall.conf names the front's address."""
    text = (INSTALLERS / "install.sh").read_text()
    assert "cat > /usr/local/sbin/kimina-firewall" not in text and "install_kimina_firewall" in text
    assert 'KIMINA_FW_FRONT_ADDRESS=$(setting_in "$DIR/kimina.env" KIMINA_FRONT_ADDRESS)' in text
    library = (INSTALLERS / "pool-lib.sh").read_text()
    installer = library[
        library.index("install_kimina_firewall() {") : library.index("# set_env_in FILE KEY VALUE")
    ]
    assert installer.index('bash -n "$SRC/kimina-firewall"') < installer.index(
        "/usr/local/sbin/kimina-firewall"
    )
    assert subprocess.run(["bash", "-n", str(INSTALLERS / "kimina-firewall")]).returncode == 0


# --- install_tls_front: text checks, because it needs root and Docker ------------------------------------------------


def front_function() -> str:
    library = (INSTALLERS / "pool-lib.sh").read_text()
    return library[
        library.index("install_tls_front() {") : library.index(
            "# remove_tls_front INSTANCE_DIR INSTANCE"
        )
    ]


def test_the_fronts_key_is_readable_by_the_fronts_group_and_by_nobody_else():
    """Text check (needs root and Docker). A real install once failed because an unprivileged container
    was handed a root-only directory; here every file the front reads gets an explicit owner, group and
    mode, and the group is READ FROM THE IMAGE, never assumed."""
    body = front_function()
    assert 'gid=$(image_group_id "$LEAN_POOL_HAPROXY_IMAGE")' in body
    # Through the helpers (install for root, then chown): the host's `install` may refuse a numeric group
    # that is not in its group database (tests/install/test_install_host_tools.py).
    assert 'install_dir_for_gid 0750 "$gid" "$dir/tls"' in body
    assert 'install_for_gid 0640 "$gid" "$source/box.pem" "$dir/tls/box.pem.new"' in body
    assert 'chmod 0640 "$dir/tls/box.pem"' in body and 'chmod 0750 "$dir/tls"' in body
    for line in body.splitlines():
        if "box.pem" in line and ("chmod" in line or "-m " in line):
            assert not re.search(r"\b0?6[0-7][4-7]\b|\b0?7[0-7][1-7]\b", line), (
                line
            )  # never world-readable
    library = (INSTALLERS / "pool-lib.sh").read_text()
    reader = library[
        library.index("image_group_id() {") : library.index("# A LAN-only inbound rule")
    ]
    assert '--entrypoint id "$1" -g' in reader and "99" not in reader


def test_the_fronts_config_is_judged_by_haproxy_itself_before_any_container_changes():
    """Text check (needs root and Docker). The candidate is rendered into a `mktemp -d` directory, which is
    root-only, so it is given the installed config's modes before HAProxy's own user reads it, with the
    certificate directory mounted where the config names it. Only after that do containers change."""
    body = front_function()
    validate = body.index("haproxy -c -f /cfg/haproxy.cfg")
    assert body.index('chmod 0755 "$work"; chmod 0644 "$work/haproxy.cfg"') < validate
    assert '-v "$work":/cfg:ro -v "$dir/tls":"$LEAN_POOL_TLS_MOUNT":ro' in body
    for change in ('docker rm -f "$agent"', "install_lean_compose", "systemctl restart", "up -d"):
        assert validate < body.index(change), change
    # The front is rendered with the POOL's durations, so it never cuts a check the pool still waits for.
    assert '--lean-timeout "$lean_timeout" --server-wait "$server_wait"' in body
    assert '--proxy-client-name "$LEAN_POOL_PROXY_CLIENT_NAME"' in body


def test_the_lean_server_is_unpublished_before_the_front_takes_its_port_and_nothing_is_forced_to_restart():
    """Text check (needs root and Docker). Order on the first run: the old agent goes, the Lean server is
    recreated without its port, the firewall learns the front's address, then the front starts. A re-run
    recreates nothing: no --force-recreate, and a front that kept running only reloads (HUP)."""
    body = front_function()
    order = [
        body.index(step)
        for step in (
            'docker rm -f "$agent"',
            'install_lean_compose "$dir" 1',
            'set_env_in "$dir/firewall.conf" KIMINA_FW_FRONT_ADDRESS',
            '-f "$dir/docker-compose.front.yml" up -d',
        )
    ]
    assert order == sorted(order)
    assert "--force-recreate" not in body and 'reload_if_kept "$front" "$before"' in body
    assert (
        'if [ -n "$old_project" ] && [ "$old_project" != "$front" ]; then' in body
    )  # only the PLAIN agent goes


def test_a_front_that_keeps_restarting_is_not_taken_for_a_running_one():
    """Text check (needs Docker; the fake-machine run shows a crash-looping front stopping the install).
    Docker reports `.State.Running` true for a container it restarts in a loop, so the front's start is
    judged by three things read a few seconds apart: running, not restarting, restart count unchanged."""
    library = (INSTALLERS / "pool-lib.sh").read_text()
    steady = library[
        library.index("container_is_steady() {") : library.index(
            "# The TLS front of one named Lean server instance"
        )
    ]
    assert (
        steady.index("'{{.RestartCount}}'")
        < steady.index("sleep 6")
        < steady.index("'{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}'")
    )
    assert '[ "$after" = "true false $before" ]' in steady
    body = front_function()
    check = body[body.index('reload_if_kept "$front" "$before"') :]
    assert (
        'if ! container_is_steady "$front"; then' in check
        and 'docker logs --tail 20 "$front" >&2' in check
    )
    assert (
        "the TLS front $front of '$name' does not stay up" in check
    )  # it names the front, not the pool
    assert '{{.State.Running}}\' "$front"' not in body  # the old check is gone


# --- the Lean server's control group, and what the usage agent is given ------------------------------


def test_the_lean_server_is_started_in_the_control_group_its_settings_name():
    """The name comes from the settings (pool-lib.sh's install_cpu_priority decides it, and
    leaves it empty on a box that cannot keep such a group); the ceiling on its CPUs still stands. A key of
    the service itself, in the file instance 1 runs from and in the form a pool member runs from."""
    yaml = pytest.importorskip("yaml")
    text = (INSTALLERS / "docker-compose.yml").read_text()
    result = pool_lib('unpublished_compose "$SRC/docker-compose.yml"')
    assert result.returncode == 0, result.stderr
    for form in (text, result.stdout):
        service = yaml.safe_load(form)["services"]["kimina"]
        assert service["cgroup_parent"] == "${KIMINA_CGROUP_PARENT:-}"
        assert service["cpus"] == "${KIMINA_CPUS:-16}"
        assert form.count("\n    cgroup_parent: ${KIMINA_CGROUP_PARENT:-}\n") == 1


def test_the_usage_agent_is_told_how_to_measure_open_slots_with_a_front_and_without():
    """The worker count, the cpu.stat of the Lean server's control group, the reserve and the
    box's limit beside other work come from the settings (empty means "not set" to the agent), and so does the
    installer's version it states; the box's control groups are its one host mount, read-only. The front beside
    it is given none of this."""
    yaml = pytest.importorskip("yaml")
    plain = yaml.safe_load((INSTALLERS / "docker-compose.agent.yml").read_text())["services"][
        "agent"
    ]
    fronted = yaml.safe_load((INSTALLERS / "docker-compose.front.yml").read_text())["services"]
    for agent in (plain, fronted["agent"]):
        assert agent["environment"] == {
            "LEANPOOL_AGENT_HOST": "0.0.0.0",
            "LEANPOOL_AGENT_PORT": "18200",
            "LEANPOOL_AGENT_WORKERS": "${KIMINA_MAX_REPLS:-}",
            "LEANPOOL_AGENT_LEAN_CPU_STAT_PATH": "${KIMINA_AGENT_LEAN_CPU_STAT:-}",
            "LEANPOOL_AGENT_CPU_RESERVE": "${KIMINA_AGENT_CPU_RESERVE:-}",
            "LEANPOOL_AGENT_SLOTS_BESIDE_OTHER_WORK": "${KIMINA_AGENT_BESIDE_OTHER_WORK:-}",
            "LEANPOOL_AGENT_VERSION": "${KIMINA_INSTALLER_VERSION:-}",
        }
        assert agent["volumes"] == ["/sys/fs/cgroup:/host-cgroup:ro"]
        assert agent["read_only"] is True and agent["cap_drop"] == ["ALL"]
    assert "cgroup" not in str(fronted["front"])
