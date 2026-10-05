"""``leanpool-haproxy-config``: edit a pool's server list and render its ``haproxy.cfg``.

``render`` writes the pool proxy's configuration and ``render-box`` the configuration of a Lean
server box's TLS front; both print to standard output.

Exit status: 0 on success, 1 when the input was refused (nothing is written), 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from leanpool.environment import (
    SettingsParser,
    parse_port,
    parse_positive_integer,
    parse_whole_number,
)
from leanpool.haproxy.box import (
    DEFAULT_LEAN_PORT,
    BoxSettings,
    BoxSettingsError,
    render_box_config,
)
from leanpool.haproxy.config import PoolSettings, PoolSettingsError, render_haproxy_config
from leanpool.haproxy.files import edit_server_file, read_server_file
from leanpool.haproxy.servers import (
    DEFAULT_AGENT_PORT,
    ServerListError,
    add_server,
    format_server_line,
    parse_server,
    parse_server_list,
    remove_server,
    total_workers,
)
from leanpool.haproxy.tls import DEFAULT_AGENT_TUNNEL_PORT, TlsSettings

if TYPE_CHECKING:
    # Subscriptable only for the type checker; at run time it is used in annotations alone.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]

_REFUSED = 1
_SERVERS_REQUIRED = {"servers": "--servers (or LEANPOOL_HAPROXY_SERVERS)"}
# What each subcommand cannot run without, by the name argparse stores it under.
_REQUIRED = {
    "render": _SERVERS_REQUIRED,
    "add": _SERVERS_REQUIRED,
    "remove": _SERVERS_REQUIRED,
    "list": _SERVERS_REQUIRED,
    "render-box": {
        "tls_server_pem": "--tls-server-pem (or LEANPOOL_HAPROXY_BOX_TLS_SERVER_PEM)",
        "tls_ca_file": "--tls-ca-file (or LEANPOOL_HAPROXY_TLS_CA_FILE)",
        "proxy_client_name": "--proxy-client-name (or LEANPOOL_HAPROXY_BOX_PROXY_CLIENT_NAME)",
        "lean_upstream": "--lean-upstream (or LEANPOOL_HAPROXY_BOX_LEAN_UPSTREAM)",
        "agent_upstream": "--agent-upstream (or LEANPOOL_HAPROXY_BOX_AGENT_UPSTREAM)",
    },
}


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run one subcommand and return the process exit status."""
    parser = build_parser(os.environ if environment is None else environment)
    options = parser.parse_args(arguments)
    for attribute, flag in _REQUIRED[options.command].items():
        if getattr(options, attribute) is None:
            parser.error(f"{flag} is required")
    try:
        _COMMANDS[options.command](options)
    except (ServerListError, PoolSettingsError, BoxSettingsError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return _REFUSED
    return 0


def build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    """Build the parser for the five subcommands."""
    parser = argparse.ArgumentParser(
        prog="leanpool-haproxy-config",
        description="Edit a lean-pool server list and render the pool's haproxy.cfg.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_render_command(commands, environment)
    _add_render_box_command(commands, environment)
    _add_add_command(commands, environment)
    _add_remove_command(commands, environment)
    _add_list_command(commands, environment)
    return parser


def _add_servers_option(parser: argparse.ArgumentParser, environment: Mapping[str, str]) -> None:
    SettingsParser(parser, environment).add(
        "--servers", "LEANPOOL_HAPROXY_SERVERS", Path, None, "the server list file"
    )


def _add_duration_options(settings: SettingsParser, defaults: PoolSettings) -> None:
    """The three durations every timeout is derived from, the same for the pool and a box."""
    settings.add(
        "--lean-timeout",
        "LEANPOOL_HAPROXY_LEAN_TIMEOUT_SECONDS",
        parse_positive_integer,
        defaults.lean_timeout_seconds,
        "the largest Lean timeout, in seconds, any client sends with a check",
    )
    settings.add(
        "--server-wait",
        "LEANPOOL_HAPROXY_SERVER_WAIT_SECONDS",
        parse_whole_number,
        defaults.server_wait_seconds,
        "seconds a Lean server waits for a free worker (Kimina's LEAN_SERVER_MAX_WAIT)",
    )
    settings.add(
        "--margin",
        "LEANPOOL_HAPROXY_MARGIN_SECONDS",
        parse_positive_integer,
        defaults.margin_seconds,
        "seconds added on top of each derived timeout",
    )


def _add_render_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser(
        "render",
        help="print the haproxy.cfg for the server list to standard output",
        allow_abbrev=False,
    )
    _add_servers_option(parser, environment)
    defaults = PoolSettings()
    settings = SettingsParser(parser, environment)
    settings.add(
        "--public-port",
        "LEANPOOL_HAPROXY_PUBLIC_PORT",
        parse_port,
        defaults.public_port,
        "the port clients send every check to",
    )
    settings.add(
        "--checkers-port",
        "LEANPOOL_HAPROXY_CHECKERS_PORT",
        parse_port,
        defaults.checkers_port,
        "the loopback port the cache forwards a miss to",
    )
    settings.add(
        "--cache-address",
        "LEANPOOL_HAPROXY_CACHE_ADDRESS",
        str,
        defaults.cache_address,
        "HOST:PORT of the cache service",
    )
    settings.add(
        "--stats-port",
        "LEANPOOL_HAPROXY_STATS_PORT",
        parse_port,
        defaults.stats_port,
        "the loopback port of HAProxy's statistics page",
    )
    _add_duration_options(settings, defaults)
    settings.add(
        "--queue-timeout",
        "LEANPOOL_HAPROXY_QUEUE_TIMEOUT_SECONDS",
        parse_positive_integer,
        None,
        "seconds a check may wait in the proxy for a free worker; must be above the Lean "
        "timeout; derived as 2 x Lean timeout + margin when not given",
        optional=True,
    )
    settings.add(
        "--hop-header",
        "LEANPOOL_HOP_HEADER",
        str,
        defaults.hop_header,
        "the loop-guard header; must match the cache's",
    )
    settings.add(
        "--max-request-bytes",
        "LEANPOOL_HAPROXY_MAX_REQUEST_BYTES",
        parse_positive_integer,
        defaults.maximum_request_bytes,
        "HAProxy's buffer size: the largest request (headers and body) that can be replayed on "
        "another server after a failure",
    )
    settings.add(
        "--maximum-connections",
        "LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS",
        parse_positive_integer,
        defaults.maximum_connections,
        "HAProxy's global connection limit; a check through the cache holds two, and clients "
        "waiting in the queue count",
    )
    together = "give the three TLS files together to encrypt the pool, or none of them"
    settings.add(
        "--tls-front-door-pem",
        "LEANPOOL_HAPROXY_TLS_FRONT_DOOR_PEM",
        str,
        None,
        f"the front door's certificate and key in one file, as HAProxy sees the path; {together}",
        optional=True,
    )
    settings.add(
        "--tls-ca-file",
        "LEANPOOL_HAPROXY_TLS_CA_FILE",
        str,
        None,
        "the pool authority's certificate, as HAProxy sees the path; every Lean server's "
        "certificate is checked against it",
        optional=True,
    )
    settings.add(
        "--tls-client-pem",
        "LEANPOOL_HAPROXY_TLS_CLIENT_PEM",
        str,
        None,
        "the proxy's client certificate and key in one file, as HAProxy sees the path; shown "
        "to every Lean server",
        optional=True,
    )
    settings.add(
        "--agent-tunnel-port",
        "LEANPOOL_HAPROXY_AGENT_TUNNEL_PORT",
        parse_port,
        DEFAULT_AGENT_TUNNEL_PORT,
        "with TLS, the first of the loopback ports the usage agents are asked through: one "
        "per server, counting up",
    )


def _add_render_box_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser(
        "render-box",
        help="print the haproxy.cfg of a Lean server box's TLS front to standard output",
        allow_abbrev=False,
    )
    defaults = PoolSettings()
    settings = SettingsParser(parser, environment)
    settings.add(
        "--tls-server-pem",
        "LEANPOOL_HAPROXY_BOX_TLS_SERVER_PEM",
        str,
        None,
        "the box's certificate and key in one file, as HAProxy sees the path",
    )
    settings.add(
        "--tls-ca-file",
        "LEANPOOL_HAPROXY_TLS_CA_FILE",
        str,
        None,
        "the pool authority's certificate, as HAProxy sees the path; a caller must present a "
        "certificate it signed",
    )
    settings.add(
        "--proxy-client-name",
        "LEANPOOL_HAPROXY_BOX_PROXY_CLIENT_NAME",
        str,
        None,
        "the name of the pool proxy's client certificate; no other caller is accepted",
    )
    settings.add(
        "--lean-upstream",
        "LEANPOOL_HAPROXY_BOX_LEAN_UPSTREAM",
        str,
        None,
        "HOST:PORT of the Lean server behind the front, reached in plain TCP",
    )
    settings.add(
        "--agent-upstream",
        "LEANPOOL_HAPROXY_BOX_AGENT_UPSTREAM",
        str,
        None,
        "HOST:PORT of the usage agent behind the front, reached in plain TCP",
    )
    settings.add(
        "--lean-port",
        "LEANPOOL_HAPROXY_BOX_LEAN_PORT",
        parse_port,
        DEFAULT_LEAN_PORT,
        "the port the front serves the Lean server on: the one the pool's server list names",
    )
    settings.add(
        "--agent-port",
        "LEANPOOL_HAPROXY_BOX_AGENT_PORT",
        parse_port,
        DEFAULT_AGENT_PORT,
        "the port the front serves the usage agent on: the one the pool's server list names",
    )
    _add_duration_options(settings, defaults)
    settings.add(
        "--maximum-connections",
        "LEANPOOL_HAPROXY_MAXIMUM_CONNECTIONS",
        parse_positive_integer,
        BoxSettings.maximum_connections,
        "HAProxy's global connection limit",
    )


def _add_add_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser("add", help="add a Lean server to the list", allow_abbrev=False)
    _add_servers_option(parser, environment)
    parser.add_argument("name", metavar="NAME", help="a unique name for the server")
    parser.add_argument("address", metavar="HOST:PORT", help="where the Lean server listens")
    parser.add_argument("workers", metavar="WORKERS", help="how many checks it runs at once")
    parser.add_argument(
        "--agent-port", default=None, help="the usage agent's port on that box (default 18200)"
    )


def _add_remove_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser(
        "remove", help="remove a Lean server from the list", allow_abbrev=False
    )
    _add_servers_option(parser, environment)
    parser.add_argument("name", metavar="NAME", help="the server to remove")


def _add_list_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser("list", help="print the validated server list", allow_abbrev=False)
    _add_servers_option(parser, environment)


def _render(options: argparse.Namespace) -> None:
    servers = parse_server_list(read_server_file(options.servers))
    settings = PoolSettings(
        public_port=options.public_port,
        checkers_port=options.checkers_port,
        cache_address=options.cache_address,
        stats_port=options.stats_port,
        lean_timeout_seconds=options.lean_timeout,
        server_wait_seconds=options.server_wait,
        margin_seconds=options.margin,
        queue_timeout_seconds=options.queue_timeout,
        hop_header=options.hop_header,
        maximum_request_bytes=options.max_request_bytes,
        maximum_connections=options.maximum_connections,
        tls=_tls_settings(options),
    )
    sys.stdout.write(render_haproxy_config(servers, settings))


def _tls_settings(options: argparse.Namespace) -> TlsSettings | None:
    """Return the TLS settings, or None when no TLS file was given.

    Some of the three files without the others is refused: a pool that was meant to be
    encrypted must never be rendered plain because one option was left out.
    """
    files = {
        "--tls-front-door-pem": options.tls_front_door_pem,
        "--tls-ca-file": options.tls_ca_file,
        "--tls-client-pem": options.tls_client_pem,
    }
    missing = [flag for flag, path in files.items() if path is None]
    if len(missing) == len(files):
        return None
    if missing:
        raise PoolSettingsError(
            f"TLS needs {', '.join(files)} together; missing: {', '.join(missing)}. "
            "Nothing was rendered"
        )
    return TlsSettings(
        front_door_pem=options.tls_front_door_pem,
        ca_file=options.tls_ca_file,
        client_pem=options.tls_client_pem,
        agent_tunnel_port=options.agent_tunnel_port,
    )


def _render_box(options: argparse.Namespace) -> None:
    settings = BoxSettings(
        server_pem=options.tls_server_pem,
        ca_file=options.tls_ca_file,
        proxy_client_name=options.proxy_client_name,
        lean_upstream=options.lean_upstream,
        agent_upstream=options.agent_upstream,
        lean_port=options.lean_port,
        agent_port=options.agent_port,
        lean_timeout_seconds=options.lean_timeout,
        server_wait_seconds=options.server_wait,
        margin_seconds=options.margin,
        maximum_connections=options.maximum_connections,
    )
    sys.stdout.write(render_box_config(settings))


def _add(options: argparse.Namespace) -> None:
    server = parse_server(options.name, options.address, options.workers, options.agent_port)
    edit_server_file(options.servers, lambda text: add_server(text, server), may_create=True)


def _remove(options: argparse.Namespace) -> None:
    edit_server_file(options.servers, lambda text: remove_server(text, options.name))


def _list(options: argparse.Namespace) -> None:
    servers = parse_server_list(read_server_file(options.servers))
    for server in servers:
        print(format_server_line(server))
    print(f"# {len(servers)} servers, {total_workers(servers)} workers")


_COMMANDS = {
    "render": _render,
    "render-box": _render_box,
    "add": _add,
    "remove": _remove,
    "list": _list,
}
