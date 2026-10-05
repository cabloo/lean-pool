"""Generating the ``haproxy.cfg`` of a Lean server box's TLS front.

With TLS a box no longer publishes its Lean server or its usage agent. It publishes this front
instead: an HAProxy that terminates TLS on the box's Lean port and on its usage-agent port,
requires the pool proxy's client certificate on both, and forwards what it accepts, unchanged,
to the Lean server and the agent over a network that never leaves the box (another container on
an internal Docker network, or loopback).

The front works in TCP mode: it does not read the requests it carries, so nothing about a check
(its size, its headers, how long it takes) is limited or altered here, and the pool proxy's
retry and timeout rules see the Lean server exactly as they would without it.
"""

from __future__ import annotations

from dataclasses import dataclass

from leanpool.environment import MAXIMUM_PORT
from leanpool.haproxy.config import (
    AGENT_TUNNEL_TIMEOUT_SECONDS,
    PoolSettings,
    default_server_line,
    derive_timeouts,
    resolvers_section,
    section,
)
from leanpool.haproxy.servers import DEFAULT_AGENT_PORT, ServerListError, parse_address
from leanpool.haproxy.tls import (
    MINIMUM_TLS_VERSION,
    TlsSettingsError,
    mutual_bind_options,
    validate_client_name,
    validate_path,
)

DEFAULT_LEAN_PORT = 8000


class BoxSettingsError(ValueError):
    """The settings cannot produce a safe configuration for a box's TLS front."""


@dataclass(frozen=True)
class BoxSettings:
    """Everything about one box's TLS front.

    * ``server_pem``: the box's certificate and key in one file, as HAProxy sees the path.
    * ``ca_file``: the pool authority's certificate; a caller must present a certificate it
      signed.
    * ``proxy_client_name``: the common name of the pool proxy's client certificate. A caller
      with any other certificate of the same authority is turned away too.
    * ``lean_upstream`` and ``agent_upstream``: ``HOST:PORT`` of the Lean server and the usage
      agent, reached in plain TCP. They must not be reachable from outside the box.
    * ``lean_port`` and ``agent_port``: the ports the front listens on, which are the ports the
      pool's server list names for this box.

    The three durations mean what they mean for the pool (``PoolSettings``) and must be the
    pool's values.
    """

    server_pem: str
    ca_file: str
    proxy_client_name: str
    lean_upstream: str
    agent_upstream: str
    lean_port: int = DEFAULT_LEAN_PORT
    agent_port: int = DEFAULT_AGENT_PORT
    lean_timeout_seconds: int = 60
    server_wait_seconds: int = 60
    margin_seconds: int = 30
    maximum_connections: int = 1024


def box_timeout_seconds(settings: BoxSettings) -> int:
    """How long a connection through the front may stay silent before it is cut.

    While Lean works on a check nothing is sent either way, so this must cover the slowest
    check. It is the pool proxy's own limit for a Lean server (wait for a worker, header import
    and body, a margin) plus one more margin: of the two, the pool proxy always gives up first,
    and a check is never cut here that the pool would still have waited for.
    """
    pool = PoolSettings(
        lean_timeout_seconds=settings.lean_timeout_seconds,
        server_wait_seconds=settings.server_wait_seconds,
        margin_seconds=settings.margin_seconds,
    )
    return derive_timeouts(pool).checker_seconds + settings.margin_seconds


def render_box_config(settings: BoxSettings) -> str:
    """Return a complete ``haproxy.cfg`` for the box's TLS front, or refuse the settings."""
    validate_box(settings)
    timeout = box_timeout_seconds(settings)
    sections = [
        _header_comment(settings, timeout),
        _global_section(settings),
        resolvers_section(),
        _defaults_section(timeout),
        _front("lean", settings, settings.lean_port, settings.lean_upstream, []),
        _front(
            "agent",
            settings,
            settings.agent_port,
            settings.agent_upstream,
            [
                "# One line each way: a silent agent is a lost agent.",
                f"timeout client {AGENT_TUNNEL_TIMEOUT_SECONDS}s",
                f"timeout server {AGENT_TUNNEL_TIMEOUT_SECONDS}s",
            ],
        ),
    ]
    return "\n\n".join(sections) + "\n"


def validate_box(settings: BoxSettings) -> None:
    """Refuse settings that would produce a wrong or unsafe configuration."""
    try:
        validate_path(settings.server_pem, "the box's certificate file")
        validate_path(settings.ca_file, "the authority's certificate file")
        validate_client_name(settings.proxy_client_name)
        parse_address(settings.lean_upstream)
        parse_address(settings.agent_upstream)
    except (TlsSettingsError, ServerListError) as error:
        raise BoxSettingsError(str(error)) from error
    for name, port in (("Lean", settings.lean_port), ("agent", settings.agent_port)):
        if not 1 <= port <= MAXIMUM_PORT:
            raise BoxSettingsError(f"{name} port {port} is outside 1-{MAXIMUM_PORT}")
    if settings.lean_port == settings.agent_port:
        raise BoxSettingsError(
            f"the Lean port and the agent port must differ: both are {settings.lean_port}"
        )
    if settings.lean_timeout_seconds < 1:
        raise BoxSettingsError("the Lean timeout must be at least 1 second")
    if settings.server_wait_seconds < 0:
        raise BoxSettingsError("the server wait cannot be negative")
    if settings.margin_seconds < 1:
        raise BoxSettingsError("the margin must be at least 1 second")
    if settings.maximum_connections < 1:
        raise BoxSettingsError("maximum connections must be at least 1")


def _header_comment(settings: BoxSettings, timeout: int) -> str:
    return "\n".join(
        [
            "# haproxy.cfg for the TLS front of a lean-pool Lean server box, generated by",
            "# leanpool-haproxy-config render-box. Render again instead of editing this file.",
            "#",
            f"# TLS ({MINIMUM_TLS_VERSION} or later) on the Lean port {settings.lean_port} and "
            f"the usage-agent port {settings.agent_port}.",
            "# A caller must present the pool proxy's client certificate "
            f"({settings.proxy_client_name}), signed by the",
            "# pool's authority. What is accepted is forwarded unchanged, in plain TCP, to the "
            "Lean server",
            "# and the agent beside this front.",
            f"# Sized for checks with a Lean timeout of up to {settings.lean_timeout_seconds}s: "
            f"a check may take {timeout}s.",
        ]
    )


def _global_section(settings: BoxSettings) -> str:
    return section(
        "global",
        [
            "log stdout format raw local0",
            f"# Nothing below {MINIMUM_TLS_VERSION}, also on a line a later edit adds.",
            f"ssl-default-bind-options ssl-min-ver {MINIMUM_TLS_VERSION}",
            f"maxconn {settings.maximum_connections}",
        ],
    )


def _defaults_section(timeout: int) -> str:
    return section(
        "defaults",
        [
            "# TCP mode: the front carries bytes and never reads a request.",
            "mode tcp",
            "log global",
            "option tcplog",
            "# Only failures are logged: the pool proxy's checks arrive every few seconds.",
            "option dontlog-normal",
            "timeout connect 5s",
            "# Nothing is sent while Lean works, so a connection may stay silent for the whole",
            "# of the slowest check. This is above the pool proxy's own limit for a Lean server.",
            f"timeout client {timeout}s",
            f"timeout server {timeout}s",
            "# The Lean server and the agent are looked up again while the front runs: a",
            "# container that was restarted, or started late, is found at its new address.",
            default_server_line(),
        ],
    )


def _front(
    name: str, settings: BoxSettings, port: int, upstream: str, extra_lines: list[str]
) -> str:
    upstream_host, upstream_port = parse_address(upstream)
    return section(
        f"listen {name}",
        [
            f"bind :{port} {mutual_bind_options(settings.server_pem, settings.ca_file)}",
            "# The authority signs every box's certificate too. Those are server certificates,",
            "# which TLS itself refuses from a client; this turns away anything else that is",
            "# not the pool proxy's own.",
            "tcp-request session reject unless "
            f"{{ ssl_c_s_dn(cn) -m str {settings.proxy_client_name} }}",
            *extra_lines,
            f"server {name} {upstream_host}:{upstream_port}",
        ],
    )
