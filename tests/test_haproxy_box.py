"""The generated haproxy.cfg of a Lean server box's TLS front."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from config_sections import directives, sections
from tls_support import ThrowawayAuthority

from leanpool.haproxy import (
    BoxSettings,
    BoxSettingsError,
    LeanServer,
    PoolSettings,
    box_timeout_seconds,
    derive_timeouts,
    render_box_config,
    render_haproxy_config,
)

SETTINGS = BoxSettings(
    server_pem="/etc/leanpool/tls/box.pem",
    ca_file="/etc/leanpool/tls/ca.crt",
    proxy_client_name="lean-pool-proxy",
    lean_upstream="kimina:8000",
    agent_upstream="agent:18200",
)
MUTUAL_TLS = (
    "ssl crt /etc/leanpool/tls/box.pem ssl-min-ver TLSv1.3 "
    "ca-file /etc/leanpool/tls/ca.crt verify required"
)
ONLY_THE_PROXY = "tcp-request session reject unless { ssl_c_s_dn(cn) -m str lean-pool-proxy }"


def changed(**changes: Any) -> BoxSettings:
    return dataclasses.replace(SETTINGS, **changes)


@pytest.fixture
def config() -> str:
    return render_box_config(SETTINGS)


def test_the_lean_port_requires_the_proxys_certificate_and_forwards_in_plain(config: str) -> None:
    assert sections(config)["listen lean"] == [
        f"bind :8000 {MUTUAL_TLS}",
        ONLY_THE_PROXY,
        "server lean kimina:8000",
    ]


def test_the_agent_port_requires_the_proxys_certificate_and_forwards_in_plain(config: str) -> None:
    assert sections(config)["listen agent"] == [
        f"bind :18200 {MUTUAL_TLS}",
        ONLY_THE_PROXY,
        "timeout client 10s",
        "timeout server 10s",
        "server agent agent:18200",
    ]


def test_every_listener_is_tls_1_3_and_requires_a_client_certificate(config: str) -> None:
    binds = [line for _title, line in directives(config, "bind")]
    assert len(binds) == 2
    for bind in binds:
        assert " ssl crt " in bind
        assert " ssl-min-ver TLSv1.3 " in bind
        assert bind.endswith(" ca-file /etc/leanpool/tls/ca.crt verify required")
    assert "ssl-default-bind-options ssl-min-ver TLSv1.3" in sections(config)["global"]


def test_the_only_sections_are_the_two_fronts(config: str) -> None:
    assert list(sections(config)) == [
        "global",
        "resolvers pool_dns",
        "defaults",
        "listen lean",
        "listen agent",
    ]


def test_the_front_carries_bytes_and_never_reads_a_request(config: str) -> None:
    defaults = sections(config)["defaults"]
    assert defaults[0] == "mode tcp"
    assert "mode http" not in config
    assert not any(line.startswith(("http-request", "option http")) for line in defaults)


def test_a_caller_is_judged_before_anything_is_forwarded(config: str) -> None:
    for title in ("listen lean", "listen agent"):
        lines = sections(config)[title]
        server = next(index for index, line in enumerate(lines) if line.startswith("server "))
        assert lines.index(ONLY_THE_PROXY) < server


def test_the_fronts_patience_is_above_the_pool_proxys_for_a_lean_server(config: str) -> None:
    """With the defaults the pool proxy waits 210s for a Lean server; the front waits 240s."""
    defaults = sections(config)["defaults"]
    assert derive_timeouts(PoolSettings()).checker_seconds == 210
    assert box_timeout_seconds(SETTINGS) == 240
    assert "timeout client 240s" in defaults
    assert "timeout server 240s" in defaults
    assert "# Sized for checks with a Lean timeout of up to 60s: a check may take 240s." in config


@pytest.mark.parametrize("lean_timeout", [1, 30, 60, 300, 1800])
@pytest.mark.parametrize("server_wait", [0, 60, 600])
def test_no_timeout_of_the_front_can_cut_a_check_the_pool_would_wait_for(
    lean_timeout: int, server_wait: int
) -> None:
    durations: dict[str, Any] = {
        "lean_timeout_seconds": lean_timeout,
        "server_wait_seconds": server_wait,
    }
    pool = derive_timeouts(PoolSettings(**durations))
    front = box_timeout_seconds(changed(**durations))
    slowest_check = server_wait + 2 * lean_timeout
    assert front > pool.checker_seconds > slowest_check
    defaults = sections(render_box_config(changed(**durations)))["defaults"]
    assert f"timeout client {front}s" in defaults
    assert f"timeout server {front}s" in defaults


def test_the_front_is_sized_like_the_pool_that_was_rendered_with_the_same_durations() -> None:
    durations: dict[str, Any] = {
        "lean_timeout_seconds": 300,
        "server_wait_seconds": 10,
        "margin_seconds": 5,
    }
    pool_config = render_haproxy_config(
        [LeanServer("lean-a", "192.0.2.10", 8000, 4)], PoolSettings(**durations)
    )
    assert "timeout server 615s" in sections(pool_config)["defaults"]
    assert "timeout server 620s" in sections(render_box_config(changed(**durations)))["defaults"]


def test_upstreams_are_looked_up_again_while_the_front_runs(config: str) -> None:
    """A Lean server container that was restarted has a new address on the box's network."""
    assert sections(config)["defaults"][-1] == (
        "default-server init-addr last,libc,none resolvers pool_dns resolve-prefer ipv4"
    )
    pool_config = render_haproxy_config([LeanServer("lean-a", "192.0.2.10", 8000, 4)])
    assert sections(config)["resolvers pool_dns"] == sections(pool_config)["resolvers pool_dns"]


def test_only_failures_are_logged(config: str) -> None:
    defaults = sections(config)["defaults"]
    assert "option tcplog" in defaults
    assert "option dontlog-normal" in defaults


def test_ports_upstreams_and_the_connection_limit_are_configurable() -> None:
    settings = changed(
        lean_port=18001,
        agent_port=18201,
        lean_upstream="Kimina-Lean:9000",
        agent_upstream="127.0.0.1:9001",
        maximum_connections=64,
    )
    config = render_box_config(settings)
    assert sections(config)["listen lean"][0].startswith("bind :18001 ssl ")
    assert sections(config)["listen lean"][-1] == "server lean kimina-lean:9000"
    assert sections(config)["listen agent"][0].startswith("bind :18201 ssl ")
    assert sections(config)["listen agent"][-1] == "server agent 127.0.0.1:9001"
    assert "maxconn 64" in sections(config)["global"]
    assert "maxconn 1024" in sections(render_box_config(SETTINGS))["global"]


def test_the_same_settings_render_the_same_configuration() -> None:
    assert render_box_config(SETTINGS) == render_box_config(SETTINGS)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"server_pem": "box.pem"}, "the box's certificate file 'box.pem' must be an absolute"),
        ({"server_pem": "/etc/box.pem ca-file /etc/other"}, "must be an absolute path"),
        ({"ca_file": ""}, "the authority's certificate file '' must be an absolute path"),
        ({"ca_file": "/etc/ca.crt\n    server evil 192.0.2.1:1"}, "must be an absolute path"),
        ({"proxy_client_name": ""}, "the proxy's client name"),
        ({"proxy_client_name": "lean pool proxy"}, "the proxy's client name"),
        ({"proxy_client_name": "proxy } accept #"}, "the proxy's client name"),
        ({"proxy_client_name": "proxy\n    server evil 192.0.2.1:1"}, "the proxy's client name"),
        ({"proxy_client_name": "lean_pool_proxy"}, "the proxy's client name"),
        ({"proxy_client_name": "p" * 65}, "at most 64 characters"),
        ({"lean_upstream": "kimina"}, "HOST:PORT"),
        ({"lean_upstream": "kimina_lean:8000"}, "host"),
        ({"agent_upstream": "agent:0"}, "port"),
        ({"agent_upstream": "agent:18200 check"}, "port"),
        ({"lean_port": 0}, "Lean port 0 is outside 1-65535"),
        ({"agent_port": 65536}, "agent port 65536 is outside 1-65535"),
        ({"lean_port": 18200}, "must differ: both are 18200"),
        ({"lean_timeout_seconds": 0}, "Lean timeout"),
        ({"server_wait_seconds": -1}, "server wait"),
        ({"margin_seconds": 0}, "margin"),
        ({"maximum_connections": 0}, "maximum connections"),
    ],
)
def test_unusable_settings_are_refused(change: dict[str, Any], reason: str) -> None:
    with pytest.raises(BoxSettingsError, match=reason):
        render_box_config(changed(**change))


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="HAProxy is not installed")
@pytest.mark.parametrize("upstream_host", ["127.0.0.1", "kimina.invalid"])
def test_haproxy_accepts_the_box_configuration_without_a_warning(
    tmp_path: Path, upstream_host: str
) -> None:
    authority = ThrowawayAuthority.create(tmp_path / "tls")
    settings = changed(
        server_pem=str(authority.server("lean-a")),
        ca_file=str(authority.certificate_path),
        lean_upstream=f"{upstream_host}:8000",
        agent_upstream=f"{upstream_host}:18201",
    )
    path = tmp_path / "haproxy.cfg"
    path.write_text(render_box_config(settings))
    result = subprocess.run(
        ["haproxy", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "WARNING" not in output
    assert "ALERT" not in output
