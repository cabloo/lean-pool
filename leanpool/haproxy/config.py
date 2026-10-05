"""Generating ``haproxy.cfg`` for a pool: a server list in, a complete configuration out.

The generated configuration (HAProxy 3.0 syntax) has these parts:

* ``resolvers pool_dns``: how server names are looked up while the proxy runs.
* frontend ``lean_pool`` on the public port, the one address clients know. It answers
  ``GET /health`` itself, sends checks to the cache first, and sends them straight to the Lean
  servers when the cache is down. Every answer that leaves it says how big the pool is (the
  workers on servers that are up, the checks queued, the servers taking checks); nothing reads
  that back.
* frontend ``checkers_door`` on loopback, where the cache sends what it could not answer.
* backend ``cache``: the cache service, with the checkers door as its backup.
* backend ``checkers``: the Lean servers, least connections first, never more concurrent checks
  per server than it has workers, weighted by what each server's usage agent reports. A check
  marked as background work waits here behind every normal check.
* listener ``stats`` on loopback: HAProxy's own statistics.

Given TLS settings (``PoolSettings.tls``), every hop that leaves the proxy's machine is
encrypted, TLS 1.3 or later:

* the public port serves HTTPS with the front door's certificate;
* every Lean server is spoken to over mutual TLS, health checks included: the proxy presents its
  client certificate and checks the server's certificate against the server's name in the list;
* the usage agents are asked through one loopback listener per server (``agent_tunnel_<name>``),
  which forwards over the same mutual TLS. HAProxy's agent check cannot speak TLS itself.

The hops that stay inside the proxy's own network namespace (the cache, the checkers door,
statistics) are on loopback and stay plain. Without TLS settings the configuration is plain
throughout, and its first line says so.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from leanpool.environment import MAXIMUM_PORT
from leanpool.haproxy.servers import (
    LeanServer,
    ServerListError,
    parse_address,
    refuse_duplicate,
    total_workers,
)
from leanpool.haproxy.tls import (
    MINIMUM_TLS_VERSION,
    TlsSettings,
    TlsSettingsError,
    bind_options,
    server_options,
    tls_identity,
    validate_tls_settings,
)
from leanpool.loop_guard import DEFAULT_HOP_HEADER, HOP_HEADER_VALUE
from leanpool.signals import (
    BACKGROUND_PRIORITY,
    BACKGROUND_PRIORITY_CLASS,
    PRIORITY_HEADER,
    QUEUED_FIELD,
    QUEUED_HEADER,
    SERVERS_FIELD,
    SERVERS_HEADER,
    WORKERS_FIELD,
    WORKERS_HEADER,
)

MAXIMUM_WEIGHT = 256
# The variable the workers on servers that are up are summed into: once while a request is read
# (for the body of /health) and once as an answer leaves (for the response header).
_REQUEST_WORKERS = "txn.pool_workers"
_RESPONSE_WORKERS = "res.pool_workers"
AGENT_INTERVAL_SECONDS = 5

# A check that failed without Lean's answer is sent to another Lean server, at most this many
# times, so a proof that crashes every worker it meets is stopped after three workers.
CHECKER_RETRIES = 2
# The statuses that count as "failed without Lean's answer". 500 is what Kimina answers when a
# worker crashed. A Lean timeout is a 200 and is deliberately absent, as is HAProxy's own
# "response-timeout": a check that ran out of time would run out of time again.
CHECKER_RETRY_STATUSES = (500, 502, 503, 504)
# A request the cache dropped unanswered needs two retries to reach the backup: the first finds
# the cache gone (which marks it down), the second is sent to the backup.
CACHE_RETRIES = 3

RESOLVERS_NAME = "pool_dns"
RESOLVE_INTERVAL_SECONDS = 10
# HAProxy takes a server out when its name has failed to resolve for a "hold" period. The
# largest period it accepts is 2147483647 ms (about 24.8 days).
DNS_FAILURE_HOLD_DAYS = 24

# HAProxy's own default buffer size. Its manual warns that smaller buffers break some services.
MINIMUM_REQUEST_BYTES = 16384
# Per connection: the request buffer, the response buffer, and the copy of the request that is
# kept so it can be replayed on another server.
BUFFERS_PER_CONNECTION = 3

# An agent check is one line each way; a tunnel that is silent this long has lost its agent.
AGENT_TUNNEL_TIMEOUT_SECONDS = 10
AGENT_TUNNEL_PREFIX = "agent_tunnel_"
# The first line of a configuration rendered without TLS settings.
UNENCRYPTED_NOTICE = (
    "# UNENCRYPTED: plain HTTP to clients and to Lean servers, the API key included; "
    "render with TLS to encrypt."
)

_LOOPBACK = "127.0.0.1"
_HEADER_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_INDENT = "    "
_BYTES_PER_MEBIBYTE = 1024**2


class PoolSettingsError(ValueError):
    """The pool's settings or its server list cannot produce a safe configuration."""


@dataclass(frozen=True)
class PoolSettings:
    """Everything about a pool's proxy except its servers.

    ``lean_timeout_seconds`` is the largest ``timeout`` any client will send with a check, and
    ``server_wait_seconds`` is how long a Lean server waits for a free worker before giving up
    (Kimina's ``LEAN_SERVER_MAX_WAIT``). The proxy's timeouts are derived from these two so that
    it never cuts off a check the Lean server would still have answered.

    ``maximum_request_bytes`` is HAProxy's buffer size: a failed check can be replayed on another
    server only if the whole request (headers and body) fits in one buffer.

    ``tls`` turns encryption on: with it the public port serves HTTPS and every Lean server and
    usage agent is reached over mutual TLS; without it everything is plain HTTP.
    """

    public_port: int = 18100
    checkers_port: int = 18101
    cache_address: str = "127.0.0.1:18102"
    stats_port: int = 18103
    lean_timeout_seconds: int = 60
    server_wait_seconds: int = 60
    margin_seconds: int = 30
    queue_timeout_seconds: int | None = None
    hop_header: str = DEFAULT_HOP_HEADER
    maximum_request_bytes: int = 262144
    maximum_connections: int = 1024
    tls: TlsSettings | None = None


@dataclass(frozen=True)
class PoolTimeouts:
    """The proxy's timeouts, in seconds, derived from the Lean timeout."""

    queue_seconds: int
    checker_seconds: int
    cache_seconds: int

    @property
    def minimum_cache_upstream_seconds(self) -> int:
        """The least the cache must wait for the checkers before giving up on a check."""
        return self.queue_seconds + self.checker_seconds


def derive_timeouts(settings: PoolSettings) -> PoolTimeouts:
    """Size every timeout from the Lean timeout so a legitimate slow check is never cut.

    * A Lean server may hold a check while it waits for a free worker, then imports the header,
      then runs the body, and Kimina allows the header and the body the full Lean timeout each.
      ``timeout server`` for the checkers covers all three plus a margin.
    * A check may wait in the proxy's queue for a worker. By default it may wait as long as one
      worst-case check (header plus body) plus a margin, which is always above the Lean timeout.
    * The cache holds a client's request for the queue wait and the check together, so the
      first hop is allowed their sum plus a margin.
    """
    slowest_check = 2 * settings.lean_timeout_seconds
    queue_seconds = settings.queue_timeout_seconds
    if queue_seconds is None:
        queue_seconds = slowest_check + settings.margin_seconds
    checker_seconds = settings.server_wait_seconds + slowest_check + settings.margin_seconds
    return PoolTimeouts(
        queue_seconds=queue_seconds,
        checker_seconds=checker_seconds,
        cache_seconds=queue_seconds + checker_seconds + settings.margin_seconds,
    )


def server_weights(servers: Sequence[LeanServer]) -> dict[str, int]:
    """Give each server a weight proportional to its worker count, as large as HAProxy allows.

    The usage agent reports a percentage and HAProxy sets the server's weight to
    ``configured weight * percentage / 100`` in whole numbers. With the worker count itself as
    the weight, a 4-worker server would reach weight 0 (out of rotation) at any report below 25%,
    and a pool that is merely busy would drain itself. Scaling every weight by the same factor
    keeps the ratios between servers (which is all that least-connections balancing reads) while
    leaving room below: the largest server gets close to 256, so a report of 1% still leaves it
    in rotation.
    """
    scale = MAXIMUM_WEIGHT // max(server.workers for server in servers)
    return {server.name: server.workers * scale for server in servers}


def worst_case_buffer_bytes(settings: PoolSettings) -> int:
    """Return the most memory HAProxy's buffers can take: every connection holding full ones.

    HAProxy's manual counts two buffers per connection; replaying a failed request keeps a
    third, the copy of the request. Raising the buffer size raises this in proportion, which is
    why the connection limit is kept low.
    """
    return settings.maximum_connections * BUFFERS_PER_CONNECTION * settings.maximum_request_bytes


def agent_tunnel_ports(servers: Sequence[LeanServer], settings: PoolSettings) -> dict[str, int]:
    """Give each server's agent tunnel a loopback port; empty without TLS.

    The ports are the first free ones counting up from ``tls.agent_tunnel_port``, in the order
    of the server list, skipping the pool's own ports (public, checkers, statistics and the
    cache's). The same list and settings therefore always give the same ports, and a server
    appended to the list changes no other server's port.
    """
    if settings.tls is None:
        return {}
    taken = {settings.public_port, settings.checkers_port, settings.stats_port}
    taken.add(parse_address(settings.cache_address)[1])
    ports: dict[str, int] = {}
    candidate = settings.tls.agent_tunnel_port
    for server in servers:
        while candidate in taken:
            candidate += 1
        if candidate > MAXIMUM_PORT:
            raise PoolSettingsError(
                f"{len(servers)} agent tunnels do not fit in the ports from "
                f"{settings.tls.agent_tunnel_port} to {MAXIMUM_PORT}"
            )
        ports[server.name] = candidate
        candidate += 1
    return ports


def render_haproxy_config(
    servers: Sequence[LeanServer], settings: PoolSettings | None = None
) -> str:
    """Return a complete ``haproxy.cfg`` for the pool, or refuse the input.

    An empty server list is refused: a pool with no Lean server answers nothing, and a list that
    is empty by accident (a truncated file) must not be installed as if it were intended.
    """
    settings = settings or PoolSettings()
    validate_pool(servers, settings)
    timeouts = derive_timeouts(settings)
    tunnel_ports = agent_tunnel_ports(servers, settings)
    sections = [
        _header_comment(servers, settings, timeouts),
        _global_section(settings),
        resolvers_section(),
        _defaults_section(timeouts),
        _public_frontend(settings, servers),
        _checkers_door(settings),
        _cache_backend(settings, timeouts),
        _checkers_backend(servers, settings.tls, tunnel_ports),
        _stats_listener(settings),
    ]
    if settings.tls is not None:
        sections.append(_agent_tunnel_defaults())
        sections += [
            _agent_tunnel(server, settings.tls, tunnel_ports[server.name]) for server in servers
        ]
    return "\n\n".join(sections) + "\n"


def validate_pool(servers: Sequence[LeanServer], settings: PoolSettings) -> None:
    """Refuse a server list or settings that would produce a wrong or unsafe configuration."""
    if not servers:
        raise PoolSettingsError("the server list is empty")
    try:
        for index, server in enumerate(servers):
            refuse_duplicate(servers[:index], server)
        parse_address(settings.cache_address)
    except ServerListError as error:
        raise PoolSettingsError(str(error)) from error
    _validate_ports(settings)
    _validate_durations(settings)
    _validate_capacity(settings)
    if not _HEADER_NAME_PATTERN.fullmatch(settings.hop_header):
        raise PoolSettingsError(
            f"hop header {settings.hop_header!r} must be letters, digits and '-', "
            "starting with a letter"
        )
    if settings.tls is not None:
        _validate_tls(servers, settings, settings.tls)


def _validate_tls(servers: Sequence[LeanServer], settings: PoolSettings, tls: TlsSettings) -> None:
    """Refuse what would leave a hop unencrypted or a server's identity unclear."""
    try:
        validate_tls_settings(tls)
        identities = [tls_identity(server.name) for server in servers]
    except TlsSettingsError as error:
        raise PoolSettingsError(str(error)) from error
    for index, identity in enumerate(identities):
        if identity in identities[:index]:
            raise PoolSettingsError(
                f"with TLS two servers cannot be named {identity!r} in different letter case: "
                "one certificate would pass for both"
            )
    cache_host = parse_address(settings.cache_address)[0]
    if not _is_loopback(cache_host):
        raise PoolSettingsError(
            f"with TLS the cache must be on a loopback address, not {cache_host}: the hop to "
            "the cache is not encrypted"
        )
    agent_tunnel_ports(servers, settings)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ipaddress.AddressValueError:
        return False


def _validate_ports(settings: PoolSettings) -> None:
    ports = {
        "public": settings.public_port,
        "checkers": settings.checkers_port,
        "stats": settings.stats_port,
    }
    for name, port in ports.items():
        if not 1 <= port <= MAXIMUM_PORT:
            raise PoolSettingsError(f"{name} port {port} is outside 1-{MAXIMUM_PORT}")
    if len(set(ports.values())) != len(ports):
        raise PoolSettingsError(f"the public, checkers and stats ports must differ: {ports}")


def _validate_durations(settings: PoolSettings) -> None:
    if settings.lean_timeout_seconds < 1:
        raise PoolSettingsError("the Lean timeout must be at least 1 second")
    if settings.server_wait_seconds < 0:
        raise PoolSettingsError("the server wait cannot be negative")
    if settings.margin_seconds < 1:
        raise PoolSettingsError("the margin must be at least 1 second")
    queue_seconds = settings.queue_timeout_seconds
    if queue_seconds is not None and queue_seconds <= settings.lean_timeout_seconds:
        raise PoolSettingsError(
            f"the queue timeout ({queue_seconds}s) must be above the Lean timeout "
            f"({settings.lean_timeout_seconds}s): a check waiting behind one slow check "
            "would otherwise be dropped"
        )


def _validate_capacity(settings: PoolSettings) -> None:
    if settings.maximum_connections < 1:
        raise PoolSettingsError("maximum connections must be at least 1")
    if settings.maximum_request_bytes < MINIMUM_REQUEST_BYTES:
        raise PoolSettingsError(
            f"the largest replayable request ({settings.maximum_request_bytes} bytes) cannot be "
            f"below HAProxy's default buffer size, {MINIMUM_REQUEST_BYTES}"
        )


def section(title: str, lines: Sequence[str]) -> str:
    """One section of a configuration: its title, then its lines indented."""
    return "\n".join([title, *(f"{_INDENT}{line}" for line in lines)])


def _header_comment(
    servers: Sequence[LeanServer], settings: PoolSettings, timeouts: PoolTimeouts
) -> str:
    lines = [
        "# haproxy.cfg for a lean-pool, generated by leanpool-haproxy-config.",
        "# Edit the server list and render again instead of editing this file.",
        "#",
        f"# Pool: {len(servers)} Lean servers, {total_workers(servers)} workers.",
        f"# Sized for checks with a Lean timeout of up to {settings.lean_timeout_seconds}s:",
        f"#   a check may wait {timeouts.queue_seconds}s in the queue for a free worker,",
        f"#   and a Lean server may take {timeouts.checker_seconds}s to answer it.",
        "# The cache must wait at least "
        f"{timeouts.minimum_cache_upstream_seconds}s for the checkers",
        "# (LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS).",
    ]
    if settings.tls is None:
        return "\n".join([UNENCRYPTED_NOTICE, *lines])
    return "\n".join(
        [
            *lines,
            "#",
            f"# TLS ({MINIMUM_TLS_VERSION} or later): clients are served HTTPS; every Lean server "
            "and usage agent is",
            "# reached over mutual TLS and must present a certificate for its name in the "
            "server list.",
            "# Only loopback hops inside this proxy (the cache, the checkers door, statistics, "
            "the agent",
            "# tunnels' own listeners) are plain.",
        ]
    )


def _global_section(settings: PoolSettings) -> str:
    worst_case_mebibytes = math.ceil(worst_case_buffer_bytes(settings) / _BYTES_PER_MEBIBYTE)
    tls_lines = (
        []
        if settings.tls is None
        else [
            f"# Nothing below {MINIMUM_TLS_VERSION}, also on a line a later edit adds.",
            f"ssl-default-bind-options ssl-min-ver {MINIMUM_TLS_VERSION}",
            f"ssl-default-server-options ssl-min-ver {MINIMUM_TLS_VERSION}",
        ]
    )
    return section(
        "global",
        [
            "log stdout format raw local0",
            *tls_lines,
            "# A failed check can be replayed on another server only if the whole request fits",
            f"# in one buffer, so a buffer holds the largest check (HAProxy's default is "
            f"{MINIMUM_REQUEST_BYTES}).",
            f"tune.bufsize {settings.maximum_request_bytes}",
            "# Larger buffers need fewer connections. Worst case for buffer memory:",
            f"# {settings.maximum_connections} connections x {BUFFERS_PER_CONNECTION} buffers "
            "(request, response, the copy kept for a replay)",
            f"# x {settings.maximum_request_bytes} bytes = {worst_case_mebibytes} MiB. "
            "A check through the cache holds two connections.",
            f"maxconn {settings.maximum_connections}",
        ],
    )


def resolvers_section() -> str:
    """The ``resolvers`` section: how server names are looked up while HAProxy runs."""
    hold = f"{DNS_FAILURE_HOLD_DAYS}d"
    return section(
        f"resolvers {RESOLVERS_NAME}",
        [
            "# The name servers in /etc/resolv.conf of the machine or container HAProxy runs in.",
            "parse-resolv-conf",
            f"# Every server name is looked up again each {RESOLVE_INTERVAL_SECONDS}s, so a box "
            "whose address changed is followed.",
            f"timeout resolve {RESOLVE_INTERVAL_SECONDS}s",
            "# An unanswered query is repeated after 1s; after 3 queries the lookup has failed.",
            "timeout retry 1s",
            "resolve_retries 3",
            "# DNS says where a server is; only its health check says whether it is up. So a",
            "# failing lookup does not take a server out: it keeps its last address for "
            f"{DNS_FAILURE_HOLD_DAYS} days,",
            "# close to the longest period HAProxy accepts.",
            f"hold nx {hold}",
            f"hold refused {hold}",
            f"hold timeout {hold}",
            f"hold other {hold}",
        ],
    )


def _defaults_section(timeouts: PoolTimeouts) -> str:
    return section(
        "defaults",
        [
            "mode http",
            "log global",
            "option httplog",
            "# Wait for the whole request (or a full buffer) before choosing a server: HAProxy",
            "# can replay only a request it had completely received when it first sent it.",
            "option http-buffer-request",
            "timeout connect 5s",
            f"timeout client {timeouts.cache_seconds}s",
            f"timeout server {timeouts.checker_seconds}s",
            f"timeout queue {timeouts.queue_seconds}s",
            "timeout http-request 10s",
            "timeout http-keep-alive 60s",
        ],
    )


def workers_up_rules(rule: str, variable: str, servers: Sequence[LeanServer]) -> list[str]:
    """The rules that sum, into ``variable``, the workers of the servers that are up right now.

    HAProxy has no fetch for it, so the sum is written out: zero, then for each listed server its
    worker count times ``srv_is_up`` (1 or 0). ``rule`` is the rule set the lines belong to
    (``http-request`` or ``http-after-response``). A server its usage agent has drained still
    counts while it is up: the number is the pool's size, not a promise of an idle worker.
    """
    return [
        f"{rule} set-var({variable}) int(0)",
        *(
            f"{rule} set-var({variable}) "
            f"srv_is_up(checkers/{server.name}),mul({server.workers}),add({variable})"
            for server in servers
        ),
    ]


def _capacity_body(status: str) -> str:
    """The body of ``GET /health``: its status and the pool's three numbers, as a log format."""
    return (
        "'{"
        f'"status":"{status}",'
        f'"{WORKERS_FIELD}":%[var({_REQUEST_WORKERS})],'
        f'"{QUEUED_FIELD}":%[queue(checkers)],'
        f'"{SERVERS_FIELD}":%[nbsrv(checkers)]'
        "}'"
    )


def _public_frontend(settings: PoolSettings, servers: Sequence[LeanServer]) -> str:
    reply = "http-request return status {status} content-type application/json string"
    health = "http-request return status {status} content-type application/json lf-string"
    bind = f"bind :{settings.public_port}"
    if settings.tls is not None:
        bind = f"{bind} {bind_options(settings.tls.front_door_pem)}"
    return section(
        "frontend lean_pool",
        [
            bind,
            "acl is_health path /health",
            "acl is_check path /api/check /api/check/",
            "acl is_cache_status path /status",
            f"acl is_second_hop req.hdr({settings.hop_header}) -m found",
            "acl checkers_up nbsrv(checkers) gt 0",
            "# The cache server itself: nbsrv(cache) would also count its backup.",
            "acl cache_up srv_is_up(cache/cache)",
            "# What the pool says about itself, for clients that size themselves by it: the",
            "# workers on servers that are up, the checks waiting for a worker, and the servers",
            "# taking checks (up, and not drained by their usage agent).",
            "# Advisory: nothing here or behind reads these numbers back.",
            *workers_up_rules("http-request", _REQUEST_WORKERS, servers),
            "# Answered here, so a health probe never queues behind proofs or needs the cache.",
            health.format(status=200)
            + f" {_capacity_body('ok')} if METH_GET is_health checkers_up",
            health.format(status=503)
            + f" {_capacity_body('no Lean server is taking checks')} if METH_GET is_health",
            "# The same three numbers on every answer that leaves this door, summed again as the",
            "# answer leaves: a check may have waited minutes since its request was read.",
            *workers_up_rules("http-after-response", _RESPONSE_WORKERS, servers),
            f"http-after-response set-header {WORKERS_HEADER} %[var({_RESPONSE_WORKERS})]",
            f"http-after-response set-header {QUEUED_HEADER} %[queue(checkers)]",
            f"http-after-response set-header {SERVERS_HEADER} %[nbsrv(checkers)]",
            "# The cache's own counters. While it is down there are none to report, and its",
            "# backup (a Lean server) must not be asked for them.",
            reply.format(status=503)
            + """ '{"status":"the cache is down"}' if is_cache_status !cache_up""",
            "use_backend cache if is_cache_status",
            "# A request that has already been through the cache is never sent to it again.",
            "use_backend checkers if is_second_hop",
            "# Only checks are cached; anything else a Lean server offers goes straight to one.",
            "use_backend checkers if !is_check",
            "# The cache is optional at run time: without it checks keep flowing, uncached.",
            "use_backend checkers if !cache_up",
            "default_backend cache",
        ],
    )


def _checkers_door(settings: PoolSettings) -> str:
    return section(
        "frontend checkers_door",
        [
            f"bind {_LOOPBACK}:{settings.checkers_port}",
            f"http-request set-header {settings.hop_header} {HOP_HEADER_VALUE}",
            "default_backend checkers",
        ],
    )


def _cache_backend(settings: PoolSettings, timeouts: PoolTimeouts) -> str:
    return section(
        "backend cache",
        [
            "option httpchk GET /health",
            "# The cache holds a request for the queue wait and the check together.",
            f"timeout server {timeouts.cache_seconds}s",
            "# Losing the cache must not fail a check. The first refused connection marks the",
            "# cache down, and the request that met it is sent again, to the backup. A request",
            "# the cache dropped unanswered takes two retries: one finds the cache gone, one",
            "# reaches the backup.",
            f"retries {CACHE_RETRIES}",
            "retry-on conn-failure empty-response",
            "option redispatch",
            default_server_line(),
            "# No maxconn: checks queue in front of the Lean servers, not in front of the cache.",
            f"server cache {settings.cache_address} check inter 1s fall 2 rise 2 "
            "observe layer4 error-limit 1 on-error mark-down",
            "# This proxy's own loopback listener, which sets the loop-guard header and leads to",
            "# the Lean servers. Used only while the cache server is down.",
            f"server checkers_door {_LOOPBACK}:{settings.checkers_port} backup",
        ],
    )


def _checkers_backend(
    servers: Sequence[LeanServer], tls: TlsSettings | None, tunnel_ports: dict[str, int]
) -> str:
    weights = server_weights(servers)
    statuses = " ".join(str(status) for status in CHECKER_RETRY_STATUSES)
    if tls is None:
        server_lines = [_server_line(server, weights[server.name]) for server in servers]
    else:
        server_lines = [
            "# Mutual TLS: each server must present a certificate, signed by the pool's "
            "authority, for its",
            "# name here, whatever address it is reached at. The usage agent is asked through "
            "this proxy's",
            "# loopback tunnel for that server, because an agent check cannot speak TLS.",
            *(
                _tls_server_line(server, weights[server.name], tls, tunnel_ports[server.name])
                for server in servers
            ),
        ]
    return section(
        "backend checkers",
        [
            "balance leastconn",
            "option httpchk GET /health",
            "# A check that failed without Lean's answer goes to another server: a failed or",
            "# dropped connection, a crashed worker (500) or a gateway error. At most "
            f"{CHECKER_RETRIES} retries,",
            "# so a proof that crashes every worker it meets is stopped. A Lean timeout is an",
            "# HTTP 200 and is never retried.",
            f"retries {CHECKER_RETRIES}",
            f"retry-on conn-failure empty-response {statuses}",
            "option redispatch 1",
            "# Background work takes only the workers nobody else is waiting for: while every",
            "# worker is busy, a waiting background check is taken after every normal one. Here,",
            "# because this backend's queue is the one that forms, and both ways in pass this",
            "# rule. Any other value of the header, and no header, is a normal check.",
            f"http-request set-priority-class int({BACKGROUND_PRIORITY_CLASS}) "
            f"if {{ req.hdr({PRIORITY_HEADER}) -i -m str {BACKGROUND_PRIORITY} }}",
            "# A name that does not resolve when the proxy starts leaves that server down instead",
            "# of stopping the proxy, and every name is looked up again while the proxy runs.",
            default_server_line(),
            *server_lines,
        ],
    )


def default_server_line() -> str:
    """The name-resolution options of every server declared after this line.

    ``init-addr`` lists how an address is found at start, in order: the server-state file, the
    system's resolver, and finally ``none``, which starts the server without an address (down)
    rather than refusing to start the proxy. ``resolvers`` keeps looking the name up while the
    proxy runs. IPv4 is preferred because Kimina listens on IPv4 by default. A server given as an
    IPv4 address has no name to look up, and none of this applies to it.
    """
    return f"default-server init-addr last,libc,none resolvers {RESOLVERS_NAME} resolve-prefer ipv4"


def _server_line(server: LeanServer, weight: int) -> str:
    return (
        f"server {server.name} {server.address} check "
        f"maxconn {server.workers} weight {weight} "
        f"agent-check agent-port {server.agent_port} agent-inter {AGENT_INTERVAL_SECONDS}s"
    )


def _tls_server_line(server: LeanServer, weight: int, tls: TlsSettings, tunnel_port: int) -> str:
    """A Lean server reached over mutual TLS, its agent asked through the loopback tunnel.

    ``check-ssl`` and ``check-sni`` put the health check on the same footing as the traffic: a
    check sends no SNI unless told to, and ``verifyhost`` (in the server options) is what checks
    the certificate's name for it.
    """
    identity = tls_identity(server.name)
    return (
        f"server {server.name} {server.address} {server_options(tls, identity)} "
        f"check check-ssl check-sni {identity} "
        f"maxconn {server.workers} weight {weight} "
        f"agent-check agent-addr {_LOOPBACK} agent-port {tunnel_port} "
        f"agent-inter {AGENT_INTERVAL_SECONDS}s"
    )


def _agent_tunnel_defaults() -> str:
    """The defaults of the agent tunnels, which are declared after every HTTP section.

    A ``defaults`` section applies to the sections declared after it, so the tunnels come last.
    Only failures are logged: a tunnel carries one line every few seconds.
    """
    return section(
        "defaults agent_tunnels",
        [
            "mode tcp",
            "log global",
            "option tcplog",
            "option dontlog-normal",
            "timeout connect 5s",
            f"timeout client {AGENT_TUNNEL_TIMEOUT_SECONDS}s",
            f"timeout server {AGENT_TUNNEL_TIMEOUT_SECONDS}s",
            default_server_line(),
        ],
    )


def _agent_tunnel(server: LeanServer, tls: TlsSettings, tunnel_port: int) -> str:
    """The loopback listener through which one server's usage agent is asked.

    HAProxy's agent check connects here in plain TCP, and this listener carries the exchange to
    the box's agent port over mutual TLS, checking the same name as for the server itself. If
    the agent cannot be reached the check reads nothing, which HAProxy ignores: the server
    keeps its last weight, as it does without a tunnel.
    """
    return section(
        f"listen {AGENT_TUNNEL_PREFIX}{server.name}",
        [
            f"bind {_LOOPBACK}:{tunnel_port}",
            f"server agent {server.host}:{server.agent_port} "
            f"{server_options(tls, tls_identity(server.name))}",
        ],
    )


def _stats_listener(settings: PoolSettings) -> str:
    return section(
        "listen stats",
        [
            f"bind {_LOOPBACK}:{settings.stats_port}",
            "stats enable",
            "stats uri /",
            "stats refresh 10s",
        ],
    )
