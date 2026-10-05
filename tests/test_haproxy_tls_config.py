"""The generated haproxy.cfg with TLS: what is encrypted, whose name is checked, what is refused.

Without TLS settings the configuration is the plain one of ``test_haproxy_config.py``, with a
first line that says so.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from config_sections import directives, sections
from tls_support import ThrowawayAuthority

from leanpool.haproxy import (
    LeanServer,
    PoolSettings,
    PoolSettingsError,
    TlsSettings,
    agent_tunnel_ports,
    render_haproxy_config,
)
from leanpool.haproxy.config import UNENCRYPTED_NOTICE

TLS = TlsSettings(
    front_door_pem="/etc/leanpool/tls/front.pem",
    ca_file="/etc/leanpool/tls/ca.crt",
    client_pem="/etc/leanpool/tls/proxy-client.pem",
)
SETTINGS = PoolSettings(tls=TLS)
SERVERS = (
    LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4),
    LeanServer(name="lean-b", host="192.0.2.20", port=8000, workers=8, agent_port=18250),
)
MUTUAL_TLS = (
    "ssl verify required ca-file /etc/leanpool/tls/ca.crt crt /etc/leanpool/tls/proxy-client.pem "
    "ssl-min-ver TLSv1.3"
)
LOOPBACK = "127.0.0.1:"


def with_tls(**changes: Any) -> PoolSettings:
    return dataclasses.replace(SETTINGS, **changes)


@pytest.fixture
def config() -> str:
    return render_haproxy_config(SERVERS, SETTINGS)


def test_the_front_door_serves_https(config: str) -> None:
    frontend = sections(config)["frontend lean_pool"]
    assert frontend[0] == "bind :18100 ssl crt /etc/leanpool/tls/front.pem ssl-min-ver TLSv1.3"
    assert [line for line in frontend if line.startswith("bind ")] == [frontend[0]]


def test_nothing_below_tls_1_3_is_spoken_on_any_line(config: str) -> None:
    global_section = sections(config)["global"]
    assert "ssl-default-bind-options ssl-min-ver TLSv1.3" in global_section
    assert "ssl-default-server-options ssl-min-ver TLSv1.3" in global_section


def test_every_lean_server_is_reached_over_mutual_tls_and_checked_by_its_name(
    config: str,
) -> None:
    checkers = sections(config)["backend checkers"]
    assert [line for line in checkers if line.startswith("server ")] == [
        f"server lean-a lean-a.example:8000 {MUTUAL_TLS} sni str(lean-a) verifyhost lean-a "
        "check check-ssl check-sni lean-a maxconn 4 weight 128 "
        "agent-check agent-addr 127.0.0.1 agent-port 18300 agent-inter 5s",
        f"server lean-b 192.0.2.20:8000 {MUTUAL_TLS} sni str(lean-b) verifyhost lean-b "
        "check check-ssl check-sni lean-b maxconn 8 weight 256 "
        "agent-check agent-addr 127.0.0.1 agent-port 18301 agent-inter 5s",
    ]


@pytest.mark.parametrize("host", ["10-0-0-5.dhcp.example", "192.0.2.77", "LAPTOP.lan"])
def test_a_servers_identity_is_its_name_whatever_address_it_is_dialled_at(host: str) -> None:
    """Addresses change, and HAProxy does not match an address in a certificate."""
    servers = [LeanServer(name="Laptop", host=host, port=8000, workers=4)]
    config = render_haproxy_config(servers, SETTINGS)
    lines = [line for _title, line in directives(config, "server") if "ssl" in line.split()]
    assert len(lines) == 2  # the Lean server, and its agent through the tunnel
    for line in lines:
        assert " sni str(laptop) verifyhost laptop" in line
        assert f" {servers[0].host}:" in line
    assert " check check-ssl check-sni laptop " in lines[0]


def test_each_agent_is_asked_through_a_loopback_tunnel_that_speaks_mutual_tls(
    config: str,
) -> None:
    """HAProxy's agent check cannot speak TLS, so it asks this proxy, which can."""
    tunnels = {title: lines for title, lines in sections(config).items() if "tunnel" in title}
    assert tunnels == {
        "defaults agent_tunnels": [
            "mode tcp",
            "log global",
            "option tcplog",
            "option dontlog-normal",
            "timeout connect 5s",
            "timeout client 10s",
            "timeout server 10s",
            "default-server init-addr last,libc,none resolvers pool_dns resolve-prefer ipv4",
        ],
        "listen agent_tunnel_lean-a": [
            "bind 127.0.0.1:18300",
            f"server agent lean-a.example:18200 {MUTUAL_TLS} sni str(lean-a) verifyhost lean-a",
        ],
        "listen agent_tunnel_lean-b": [
            "bind 127.0.0.1:18301",
            f"server agent 192.0.2.20:18250 {MUTUAL_TLS} sni str(lean-b) verifyhost lean-b",
        ],
    }


def test_no_agent_check_goes_to_a_box_in_plain(config: str) -> None:
    for _title, line in directives(config, "server"):
        if "agent-check" in line:
            assert " agent-check agent-addr 127.0.0.1 agent-port 183" in line
    assert "agent-port 18200" not in config
    assert "agent-port 18250" not in config


def test_the_tunnels_come_after_every_http_section(config: str) -> None:
    """A ``defaults`` section applies to what follows it; the tunnels' is TCP."""
    titles = list(sections(config))
    tunnel_defaults = titles.index("defaults agent_tunnels")
    assert titles[:tunnel_defaults] == [
        "global",
        "resolvers pool_dns",
        "defaults",
        "frontend lean_pool",
        "frontend checkers_door",
        "backend cache",
        "backend checkers",
        "listen stats",
    ]
    assert titles[tunnel_defaults + 1 :] == [
        "listen agent_tunnel_lean-a",
        "listen agent_tunnel_lean-b",
    ]


SERVER_LISTS = [
    SERVERS,
    (LeanServer("only", "192.0.2.1", 9000, 1),),
    tuple(LeanServer(f"box{index}", f"box{index}.example", 8000, 4) for index in range(12)),
]


@pytest.mark.parametrize("servers", SERVER_LISTS, ids=["two", "one", "twelve"])
def test_no_listener_off_loopback_and_no_line_to_a_lean_server_or_agent_is_plain(
    servers: Sequence[LeanServer],
) -> None:
    config = render_haproxy_config(servers, SETTINGS)

    binds = directives(config, "bind")
    off_loopback = [line for _title, line in binds if not line.startswith(f"bind {LOOPBACK}")]
    assert off_loopback == ["bind :18100 ssl crt /etc/leanpool/tls/front.pem ssl-min-ver TLSv1.3"]
    for _title, line in binds:
        if line.startswith(f"bind {LOOPBACK}"):
            assert " ssl" not in line  # a loopback hop inside the proxy stays plain

    server_lines = directives(config, "server")
    plain = [(title, line) for title, line in server_lines if "ssl" not in line.split()]
    # The only plain hops: the cache and this proxy's own door, both on loopback.
    assert [title for title, _line in plain] == ["backend cache", "backend cache"]
    for _title, line in plain:
        assert line.split()[2].startswith(LOOPBACK)
    encrypted = [line for _title, line in server_lines if "ssl" in line.split()]
    assert len(encrypted) == 2 * len(servers)  # each Lean server, and each agent
    for line in encrypted:
        assert f" {MUTUAL_TLS} sni str(" in line
        assert " verifyhost " in line
    # Every address of a Lean server or agent appears on an encrypted line only.
    for server in servers:
        for address in (server.address, f"{server.host}:{server.agent_port}"):
            carrying = [line for _title, line in server_lines if f" {address} " in line]
            assert carrying
            assert all(line in encrypted for line in carrying)


def test_everything_that_is_not_tls_is_the_plain_configuration(config: str) -> None:
    plain = sections(render_haproxy_config(SERVERS))
    encrypted = sections(config)
    unchanged = ["resolvers pool_dns", "defaults", "frontend checkers_door", "backend cache"]
    for title in [*unchanged, "listen stats"]:
        assert encrypted[title] == plain[title]
    assert encrypted["frontend lean_pool"][1:] == plain["frontend lean_pool"][1:]

    def not_servers(lines: list[str]) -> list[str]:
        return [line for line in lines if not line.startswith("server ")]

    assert not_servers(encrypted["backend checkers"]) == not_servers(plain["backend checkers"])


def test_the_plain_configuration_says_first_that_it_is_unencrypted() -> None:
    plain = render_haproxy_config(SERVERS)
    lines = plain.splitlines()
    assert lines[0] == UNENCRYPTED_NOTICE
    assert lines[0].startswith("# UNENCRYPTED: ")
    assert lines[1] == "# haproxy.cfg for a lean-pool, generated by leanpool-haproxy-config."
    assert plain.count("UNENCRYPTED") == 1


def test_the_plain_configuration_has_no_tls_and_no_tunnel() -> None:
    plain = render_haproxy_config(SERVERS)
    words = {
        word
        for line in plain.splitlines()
        if not line.lstrip().startswith("#")
        for word in line.split()
    }
    assert not words & {"ssl", "crt", "ca-file", "verifyhost", "sni", "agent-addr"}
    assert "ssl-min-ver" not in plain
    assert "agent_tunnel" not in plain
    assert list(sections(plain)).count("defaults") == 1
    assert agent_tunnel_ports(SERVERS, PoolSettings()) == {}


def test_the_tls_configuration_does_not_call_itself_unencrypted(config: str) -> None:
    assert "UNENCRYPTED" not in config
    assert config.startswith("# haproxy.cfg for a lean-pool, generated by")
    assert "# TLS (TLSv1.3 or later): clients are served HTTPS" in config


def test_tunnel_ports_count_up_from_the_base_in_list_order() -> None:
    assert agent_tunnel_ports(SERVERS, SETTINGS) == {"lean-a": 18300, "lean-b": 18301}
    moved = with_tls(tls=dataclasses.replace(TLS, agent_tunnel_port=20000))
    assert agent_tunnel_ports(SERVERS, moved) == {"lean-a": 20000, "lean-b": 20001}
    config = render_haproxy_config(SERVERS, moved)
    assert "bind 127.0.0.1:20001" in sections(config)["listen agent_tunnel_lean-b"]


def test_tunnel_ports_are_the_same_for_the_same_list_and_stable_when_a_server_is_appended() -> None:
    assert agent_tunnel_ports(SERVERS, SETTINGS) == agent_tunnel_ports(SERVERS, SETTINGS)
    assert render_haproxy_config(SERVERS, SETTINGS) == render_haproxy_config(SERVERS, SETTINGS)
    appended = (*SERVERS, LeanServer("lean-c", "lean-c.example", 8000, 4))
    assert agent_tunnel_ports(appended, SETTINGS) == {
        "lean-a": 18300,
        "lean-b": 18301,
        "lean-c": 18302,
    }


def test_tunnel_ports_never_collide_with_the_pools_own_ports() -> None:
    """Public 18100, checkers 18101, the cache's 18102 and statistics 18103 are skipped."""
    crowded = with_tls(tls=dataclasses.replace(TLS, agent_tunnel_port=18099))
    servers = [LeanServer(f"box{index}", f"box{index}.example", 8000, 4) for index in range(3)]
    assert agent_tunnel_ports(servers, crowded) == {"box0": 18099, "box1": 18104, "box2": 18105}
    config = render_haproxy_config(servers, crowded)
    binds = [line.removeprefix("bind ") for _title, line in directives(config, "bind")]
    ports = [bind.split()[0].rpartition(":")[2] for bind in binds]
    assert len(set(ports)) == len(ports)

    elsewhere = with_tls(
        public_port=9000,
        checkers_port=9001,
        cache_address="127.0.0.1:9002",
        stats_port=9004,
        tls=dataclasses.replace(TLS, agent_tunnel_port=9000),
    )
    assert agent_tunnel_ports(servers, elsewhere) == {"box0": 9003, "box1": 9005, "box2": 9006}


def test_tunnels_that_do_not_fit_below_the_last_port_are_refused() -> None:
    last = with_tls(tls=dataclasses.replace(TLS, agent_tunnel_port=65535))
    assert agent_tunnel_ports(SERVERS[:1], last) == {"lean-a": 65535}
    with pytest.raises(PoolSettingsError, match="2 agent tunnels do not fit"):
        render_haproxy_config(SERVERS, last)


@pytest.mark.parametrize("name", ["lean_a", "lean-a_", "10.0.0.5", "42", "lean-", "a..b", "a.-b"])
def test_with_tls_a_server_name_that_cannot_be_in_a_certificate_is_refused(name: str) -> None:
    """Each of these is a valid name in a plain pool's server list."""
    servers = [LeanServer(name=name, host="lean.example", port=8000, workers=4)]
    assert f"server {name} " in render_haproxy_config(servers)
    with pytest.raises(PoolSettingsError, match="it must be a DNS name"):
        render_haproxy_config(servers, SETTINGS)


def test_with_tls_two_names_that_differ_only_in_letter_case_are_refused() -> None:
    servers = [
        LeanServer(name="lean-a", host="a.example", port=8000, workers=4),
        LeanServer(name="Lean-A", host="b.example", port=8000, workers=4),
    ]
    assert "server Lean-A " in render_haproxy_config(servers)
    with pytest.raises(PoolSettingsError, match="one certificate would pass for both"):
        render_haproxy_config(servers, SETTINGS)


@pytest.mark.parametrize(
    "cache_address", ["cache.example:18102", "192.0.2.5:18102", "localhost:18102"]
)
def test_with_tls_a_cache_that_is_not_on_loopback_is_refused(cache_address: str) -> None:
    """The hop to the cache is plain, so it must not leave the machine."""
    assert "server cache " in render_haproxy_config(
        SERVERS, PoolSettings(cache_address=cache_address)
    )
    with pytest.raises(PoolSettingsError, match="the cache must be on a loopback address"):
        render_haproxy_config(SERVERS, with_tls(cache_address=cache_address))
    assert "server cache 127.0.0.7:9 " in render_haproxy_config(
        SERVERS, with_tls(cache_address="127.0.0.7:9")
    )


@pytest.mark.parametrize("field", ["front_door_pem", "ca_file", "client_pem"])
@pytest.mark.parametrize(
    "path",
    [
        "",
        "front.pem",
        "tls/front.pem",
        "/etc/leanpool/tls/",
        "/etc/lean pool/front.pem",
        "/etc/leanpool/front.pem # the key",
        "/etc/leanpool/front.pem\n    server evil 192.0.2.1:1",
        '/etc/leanpool/"front".pem',
        "/etc/leanpool/$HOME.pem",
        "/etc//front.pem",
    ],
)
def test_a_path_that_is_not_a_plain_absolute_path_is_refused(field: str, path: str) -> None:
    change: dict[str, Any] = {field: path}
    settings = with_tls(tls=dataclasses.replace(TLS, **change))
    with pytest.raises(PoolSettingsError, match="must be an absolute path of letters"):
        render_haproxy_config(SERVERS, settings)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_a_tunnel_port_that_is_not_a_port_is_refused(port: int) -> None:
    settings = with_tls(tls=dataclasses.replace(TLS, agent_tunnel_port=port))
    with pytest.raises(PoolSettingsError, match="agent tunnel port"):
        render_haproxy_config(SERVERS, settings)


def test_the_refusals_of_the_plain_configuration_still_apply() -> None:
    with pytest.raises(PoolSettingsError, match="empty"):
        render_haproxy_config([], SETTINGS)
    with pytest.raises(PoolSettingsError, match="must differ"):
        render_haproxy_config(SERVERS, with_tls(public_port=18101))


@pytest.fixture
def certificate_files(tmp_path: Path) -> TlsSettings:
    """Real files: HAProxy loads every certificate a configuration names when it checks it."""
    authority = ThrowawayAuthority.create(tmp_path / "tls")
    return TlsSettings(
        front_door_pem=str(authority.server("pool.example")),
        ca_file=str(authority.certificate_path),
        client_pem=str(authority.client("lean-pool-proxy")),
    )


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="HAProxy is not installed")
@pytest.mark.parametrize(
    "hosts",
    [("192.0.2.10", "192.0.2.20"), ("lean-a.invalid", "192.0.2.20")],
    ids=["addresses", "a-name-that-does-not-resolve"],
)
def test_haproxy_accepts_the_tls_configuration_without_a_warning(
    tmp_path: Path, certificate_files: TlsSettings, hosts: tuple[str, str]
) -> None:
    servers = [
        LeanServer(name="lean-a", host=hosts[0], port=8000, workers=4),
        LeanServer(name="lean-b", host=hosts[1], port=8000, workers=8),
    ]
    path = tmp_path / "haproxy.cfg"
    path.write_text(render_haproxy_config(servers, PoolSettings(tls=certificate_files)))
    result = subprocess.run(
        ["haproxy", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "WARNING" not in output
    assert "ALERT" not in output
