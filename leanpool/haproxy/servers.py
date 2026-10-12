"""The server list: which Lean servers are in the pool, and how to edit that list safely.

The list is a plain text file, one server per line, that a person can read and edit::

    # name    host:port              workers  agent-port
    lean-a    lean-a.example:8000    4        18200
    lean-b    192.0.2.20:8000        8          # agent port left out: the default, 18200
    lean-c    lean-c.example:8000    8        18200   paused

``#`` starts a comment and blank lines are ignored. The word ``paused`` as a line's last field
keeps the server in the list while the pool sends it nothing. Every field ends up inside
``haproxy.cfg``,
so each is validated strictly here: a name or host containing a space, a ``#`` or a line break
would otherwise let one bad entry rewrite the proxy's configuration.

Everything in this module is a pure function of text, so each rule is tested without a file.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from leanpool.environment import MAXIMUM_PORT, parse_whole_number
from leanpool.names import is_dns_name, looks_numeric

DEFAULT_AGENT_PORT = 18200
# HAProxy's largest server weight. A server's weight is proportional to its worker count, so a
# server with more workers than this could not be weighted correctly.
MAXIMUM_WORKERS = 256

# The last field of a paused server's line. It is a word, so it is never read as an agent port.
PAUSED_MARK = "paused"

_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_COMMENT_MARKER = "#"
_PAUSED_FIELD = re.compile(rf"\s+{PAUSED_MARK}(?=\s*$)")
# A line's third field, after its name and address: group 1 is everything before it.
_WORKERS_FIELD = re.compile(r"\A(\s*\S+\s+\S+\s+)\S+")
_FIELDS_WITHOUT_AGENT_PORT = 3
_FIELDS_WITH_AGENT_PORT = 4


class ServerListError(ValueError):
    """A server entry or a server list was refused. The message says which rule it broke."""


@dataclass(frozen=True)
class LeanServer:
    """One Lean server in the pool. Constructing one validates every field.

    ``workers`` is how many checks the server runs at once (Kimina's ``LEAN_SERVER_MAX_REPLS``).
    The proxy never sends it more concurrent checks than that. ``agent_port`` is where the usage
    agent on the same box answers. A ``paused`` server stays in the list and is sent nothing: no
    check, no health check and no agent check.
    """

    name: str
    host: str
    port: int
    workers: int
    agent_port: int = DEFAULT_AGENT_PORT
    paused: bool = False

    def __post_init__(self) -> None:
        validate_name(self.name)
        object.__setattr__(self, "host", normalize_host(self.host))
        _validate_port(self.port, "server")
        _validate_workers(self.workers)
        _validate_port(self.agent_port, "agent")

    @property
    def address(self) -> str:
        """The server's ``host:port``."""
        return f"{self.host}:{self.port}"


def parse_server(
    name: str, address: str, workers: str, agent_port: str | None = None
) -> LeanServer:
    """Build a server from its fields as text, refusing any malformed field."""
    host, port = parse_address(address)
    return LeanServer(
        name=name,
        host=host,
        port=port,
        workers=_parse_number(workers, "workers"),
        agent_port=(
            DEFAULT_AGENT_PORT if agent_port is None else _parse_number(agent_port, "agent port")
        ),
    )


def validate_name(name: str) -> None:
    """Refuse a server name that is not letters, digits, ``_``, ``.`` and ``-`` (at most 63)."""
    if not _NAME_PATTERN.fullmatch(name):
        raise ServerListError(
            f"name {name!r} must start with a letter or digit and contain only letters, digits, "
            "'_', '.' and '-' (at most 63 characters)"
        )


def parse_address(text: str) -> tuple[str, int]:
    """Split and validate ``host:port``. The host is returned in lower case.

    The host is an IPv4 address or a host name; IPv6 literals are not supported (give the
    server a name instead).
    """
    host, separator, port_text = text.rpartition(":")
    if not separator or not host:
        raise ServerListError(f"address {text!r} must be HOST:PORT")
    port = _parse_number(port_text, "server port")
    _validate_port(port, "server")
    return normalize_host(host), port


def normalize_host(host: str) -> str:
    """Validate an IPv4 address or an RFC 1123 host name and return it in lower case."""
    if looks_numeric(host):
        return _normalize_ipv4_address(host)
    if not is_dns_name(host):
        raise ServerListError(f"host {host!r} is neither an IPv4 address nor a valid host name")
    return host.lower()


def parse_server_list(text: str) -> tuple[LeanServer, ...]:
    """Read every server from a list, refusing malformed lines and duplicates."""
    servers: list[LeanServer] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = _fields(line)
        if not fields:
            continue
        try:
            server = _server_from_fields(fields)
            refuse_duplicate(servers, server)
        except ServerListError as error:
            raise ServerListError(f"line {line_number}: {error}") from error
        servers.append(server)
    return tuple(servers)


def refuse_duplicate(servers: Sequence[LeanServer], candidate: LeanServer) -> None:
    """Refuse ``candidate`` if its name or its ``host:port`` is already among ``servers``.

    HAProxy requires unique server names, and the same server listed twice would be sent twice
    as many concurrent checks as it has workers.
    """
    for server in servers:
        if server.name == candidate.name:
            raise ServerListError(f"the name {candidate.name!r} is already in the list")
        if server.address == candidate.address:
            raise ServerListError(
                f"the address {candidate.address} is already in the list as {server.name!r}"
            )


def format_server_line(server: LeanServer) -> str:
    """Write one server as a line of the list, with every field explicit."""
    line = f"{server.name} {server.address} {server.workers} {server.agent_port}"
    return f"{line} {PAUSED_MARK}" if server.paused else line


def add_server(text: str, server: LeanServer) -> str:
    """Return the list with ``server`` appended, leaving every existing line as it was.

    Refused when the existing list is itself malformed (editing a file nobody can parse would
    hide the damage) and when the name or the ``host:port`` is already present.
    """
    refuse_duplicate(parse_server_list(text), server)
    separator = "" if text == "" or text.endswith("\n") else "\n"
    return f"{text}{separator}{format_server_line(server)}\n"


def remove_server(text: str, name: str) -> str:
    """Return the list without the server called ``name``, leaving every other line as it was."""
    if all(server.name != name for server in parse_server_list(text)):
        raise ServerListError(f"no server is named {name!r}")
    kept_lines = [line for line in text.splitlines(keepends=True) if _fields(line)[:1] != [name]]
    return "".join(kept_lines)


def pause_server(text: str, name: str) -> str:
    """Return the list with the server called ``name`` paused, every other character as it was.

    Refused when that would leave no server taking checks: a pool whose every server is paused
    answers nothing, exactly as an empty one does.
    """
    servers = parse_server_list(text)
    if _listed(servers, name).paused:
        raise ServerListError(f"{name!r} is already paused")
    if all(server.paused for server in servers if server.name != name):
        raise ServerListError(
            f"pausing {name!r} would leave no server taking checks: a pool keeps at least one"
        )
    return _edit_line(text, name, _with_the_mark)


def resume_server(text: str, name: str) -> str:
    """Return the list with the server called ``name`` no longer paused, the rest as it was."""
    if not _listed(parse_server_list(text), name).paused:
        raise ServerListError(f"{name!r} is not paused")
    return _edit_line(text, name, _without_the_mark)


def set_server_workers(text: str, name: str, workers: int) -> str:
    """Return the list with ``name`` listed at ``workers``, every other character as it was.

    The count in the list is the pool's record of what the box runs; the box itself is changed
    there (Kimina's ``LEAN_SERVER_MAX_REPLS``). Refused when the server already has that count: a
    command that would change nothing is most likely aimed at the wrong server.
    """
    _validate_workers(workers)
    if _listed(parse_server_list(text), name).workers == workers:
        raise ServerListError(f"{name!r} is already listed with {workers} workers")
    return _edit_line(text, name, lambda fields: _WORKERS_FIELD.sub(rf"\g<1>{workers}", fields))


def servers_taking_checks(servers: Sequence[LeanServer]) -> tuple[LeanServer, ...]:
    """The servers that are not paused, in the order of the list."""
    return tuple(server for server in servers if not server.paused)


def total_workers(servers: Sequence[LeanServer]) -> int:
    """How many checks the pool runs at once: what a client should size its concurrency to.

    A paused server runs none, so its workers are not counted.
    """
    return sum(server.workers for server in servers_taking_checks(servers))


def _listed(servers: Sequence[LeanServer], name: str) -> LeanServer:
    for server in servers:
        if server.name == name:
            return server
    raise ServerListError(f"no server is named {name!r}")


def _edit_line(text: str, name: str, edit: Callable[[str], str]) -> str:
    """Apply ``edit`` to the fields of the one line that lists ``name``; its comment is kept."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _fields(line)[:1] == [name]:
            ending = line[len(line.rstrip("\r\n")) :]
            fields, marker, comment = line.rstrip("\r\n").partition(_COMMENT_MARKER)
            lines[index] = f"{edit(fields)}{marker}{comment}{ending}"
    return "".join(lines)


def _with_the_mark(fields: str) -> str:
    """Put the mark after the last field, before whatever spacing led to a comment."""
    written = fields.rstrip()
    return f"{written} {PAUSED_MARK}{fields[len(written) :]}"


def _without_the_mark(fields: str) -> str:
    return _PAUSED_FIELD.sub("", fields, count=1)


def _fields(line: str) -> list[str]:
    """Split a line into its fields, ignoring a comment."""
    return line.split(_COMMENT_MARKER, 1)[0].split()


def _server_from_fields(fields: list[str]) -> LeanServer:
    paused = len(fields) > _FIELDS_WITHOUT_AGENT_PORT and fields[-1] == PAUSED_MARK
    own = fields[:-1] if paused else fields
    if len(own) == _FIELDS_WITHOUT_AGENT_PORT:
        server = parse_server(own[0], own[1], own[2])
    elif len(own) == _FIELDS_WITH_AGENT_PORT:
        server = parse_server(own[0], own[1], own[2], own[3])
    else:
        raise ServerListError(
            f"expected NAME HOST:PORT WORKERS [AGENT_PORT] [{PAUSED_MARK}], "
            f"got {len(fields)} fields"
        )
    return replace(server, paused=paused)


def _parse_number(text: str, what: str) -> int:
    try:
        return parse_whole_number(text)
    except ValueError as error:
        raise ServerListError(f"{what}: {error}") from error


def _validate_port(port: int, what: str) -> None:
    if not 1 <= port <= MAXIMUM_PORT:
        raise ServerListError(f"{what} port {port} is outside 1-{MAXIMUM_PORT}")


def _validate_workers(workers: int) -> None:
    if not 1 <= workers <= MAXIMUM_WORKERS:
        raise ServerListError(
            f"workers must be between 1 and {MAXIMUM_WORKERS} (HAProxy's largest weight), "
            f"got {workers}"
        )


def _normalize_ipv4_address(text: str) -> str:
    try:
        address = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        raise ServerListError(f"host {text!r} is not a valid IPv4 address") from None
    if address.is_unspecified:
        # HAProxy reads 0.0.0.0 as "connect to wherever the client was going", not as a server.
        raise ServerListError(f"host {text!r} is not the address of a server")
    return str(address)
