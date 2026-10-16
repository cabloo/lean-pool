"""The generated haproxy.cfg with the screened door: a second HTTPS way in that leads to the cache
alone (docs/cache.md). ``test_tls_live.py`` shows what HAProxy does with it.
"""

from __future__ import annotations

import dataclasses

import pytest
from config_sections import sections

from leanpool.doors import DOOR_HEADER
from leanpool.haproxy import (
    LeanServer,
    PoolSettings,
    PoolSettingsError,
    TlsSettings,
    agent_tunnel_ports,
    render_haproxy_config,
)

TLS = TlsSettings(
    front_door_pem="/etc/leanpool/tls/front.pem",
    ca_file="/etc/leanpool/tls/ca.crt",
    client_pem="/etc/leanpool/tls/proxy-client.pem",
)
DOOR_PORT = 18120
WITHOUT = PoolSettings(tls=TLS)
WITH = dataclasses.replace(WITHOUT, screened_door_port=DOOR_PORT)
SERVERS = (
    LeanServer(name="lean-a", host="lean-a.example", port=8000, workers=4),
    LeanServer(name="lean-b", host="192.0.2.20", port=8000, workers=8),
    LeanServer(name="lean-c", host="192.0.2.30", port=8000, workers=16, paused=True),
)
# The pool's workers taking checks: lean-c is paused.
WORKERS = 12


def door_lines(config: str) -> list[str]:
    """The lines the door adds anywhere: its two sections, and its lines in the others."""
    return [
        line
        for line in config.splitlines()
        if "screened" in line.lower() or DOOR_HEADER in line or "the cache alone, which" in line
    ]


def test_without_the_door_the_configuration_is_the_one_before_it_existed() -> None:
    without = render_haproxy_config(SERVERS, WITHOUT)
    with_door = render_haproxy_config(SERVERS, WITH)
    assert door_lines(without) == []
    # Take the door's two sections and its lines elsewhere away, and nothing else differs.
    kept = [
        block
        for block in with_door.split("\n\n")
        if not block.startswith(("frontend screened_door", "backend screened_cache"))
    ]
    remainder = [
        line
        for line in "\n\n".join(kept).splitlines()
        if line not in door_lines(with_door)
        and line.strip()
        != "# Only the screened door may tell the cache that a check came in by it."
    ]
    assert "\n".join(remainder) == without.rstrip("\n")


def test_the_door_serves_https_with_the_front_doors_certificate_on_its_port_alone() -> None:
    door = sections(render_haproxy_config(SERVERS, WITH))["frontend screened_door"]
    binds = [line for line in door if line.startswith("bind ")]
    assert binds == [
        f"bind :{DOOR_PORT} ssl crt /etc/leanpool/tls/front.pem ssl-min-ver TLSv1.3 alpn http/1.1"
    ]


def test_the_doors_only_backend_is_the_cache_server_alone_with_no_backup() -> None:
    config = sections(render_haproxy_config(SERVERS, WITH))
    door = config["frontend screened_door"]
    assert [line for line in door if line.startswith(("use_backend", "default_backend"))] == [
        "default_backend screened_cache"
    ]
    backend = config["backend screened_cache"]
    assert [line for line in backend if line.startswith("server ")] == [
        f"server cache 127.0.0.1:18102 track cache/cache maxconn {WORKERS}"
    ]
    assert not any("backup" in line or "checkers" in line for line in backend)


def test_the_door_takes_checks_alone_and_holds_as_many_waiting_as_the_pool_has_workers() -> None:
    door = sections(render_haproxy_config(SERVERS, WITH))["frontend screened_door"]
    assert (
        "http-request return status 404 content-type application/json string "
        """'{"detail":"this door takes checks alone"}' unless METH_POST is_check"""
    ) in door
    assert (
        "http-request return status 503 content-type application/json string "
        """'{"detail":"the screened door is full"}' """
        f"if {{ queue(screened_cache) ge {WORKERS} }}"
    ) in door


def test_the_door_keeps_a_share_of_the_proxys_connections_of_its_own() -> None:
    door = sections(render_haproxy_config(SERVERS, WITH))["frontend screened_door"]
    assert [line for line in door if line.startswith("maxconn ")] == [f"maxconn {2 * WORKERS + 8}"]


def test_the_door_bounds_what_each_connection_holds_of_the_proxy() -> None:
    """HTTP/1.1 alone (one request at a time on a connection), a request sent whole within 5s, and
    a body of a stated length no longer than one buffer: what 2W + 8 connections can hold."""
    door = sections(render_haproxy_config(SERVERS, WITH))["frontend screened_door"]
    assert next(line for line in door if line.startswith("bind ")).endswith(" alpn http/1.1")
    assert [line for line in door if line.startswith("timeout ")] == ["timeout http-request 5s"]
    # Refused on its headers: the body is not waited for here, and is in the backend.
    assert "no option http-buffer-request" in door
    buffer = WITH.maximum_request_bytes
    assert (
        "http-request return status 411 content-type application/json string "
        """'{"detail":"a check at the screened door states its Content-Length"}' """
        "unless { req.hdr_cnt(content-length) eq 1 }"
    ) in door
    assert (
        "http-request return status 413 content-type application/json string "
        """'{"detail":"a check at the screened door is too large"}' """
        f"if {{ req.hdr_val(content-length) gt {buffer} }}"
    ) in door
    # Both after the rule that takes checks alone, so /health is answered as before.
    refusals = [line for line in door if line.startswith("http-request return status 4")]
    assert [line.split()[3] for line in refusals] == ["404", "411", "413"]


def test_the_door_says_so_to_the_cache_and_takes_a_senders_loop_guard_away() -> None:
    config = sections(render_haproxy_config(SERVERS, WITH))
    door = config["frontend screened_door"]
    assert f"http-request set-header {DOOR_HEADER} screened" in door
    assert "http-request del-header X-Lean-Pool-Hop" in door
    # The front door takes the door's header away from every request it receives.
    assert f"http-request del-header {DOOR_HEADER}" in config["frontend lean_pool"]


def test_the_doors_health_and_numbers_are_the_front_doors() -> None:
    config = sections(render_haproxy_config(SERVERS, WITH))

    def numbers(frontend: str) -> list[str]:
        return [
            line
            for line in config[frontend]
            if line.startswith("http-after-response") or "is_health" in line
        ]

    assert numbers("frontend screened_door") == numbers("frontend lean_pool")
    assert numbers("frontend lean_pool")


def test_the_door_is_refused_without_tls() -> None:
    with pytest.raises(PoolSettingsError, match="over TLS alone"):
        render_haproxy_config(SERVERS, PoolSettings(screened_door_port=DOOR_PORT))


@pytest.mark.parametrize("port", [18100, 18101, 18102, 18103, 0, 70000])
def test_a_door_port_taken_or_out_of_range_is_refused(port: int) -> None:
    with pytest.raises(PoolSettingsError, match="screened-door port"):
        render_haproxy_config(SERVERS, dataclasses.replace(WITH, screened_door_port=port))


def test_no_agent_tunnel_takes_the_doors_port() -> None:
    first = TLS.agent_tunnel_port
    settings = dataclasses.replace(WITH, screened_door_port=first + 1)
    assert agent_tunnel_ports(SERVERS, settings) == {
        "lean-a": first,
        "lean-b": first + 2,
        "lean-c": first + 3,
    }
