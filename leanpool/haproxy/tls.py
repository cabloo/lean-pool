"""TLS in the generated configurations: which files, which version, and whose name is checked.

With TLS every hop that leaves a machine is encrypted, version 1.3 or later, and both ends of
the hop between the proxy and a Lean server box prove who they are:

* the box presents a certificate for its **name in the server list**, and the proxy checks that
  name whatever host or address it dialled. Addresses change (DHCP) and HAProxy's ``verifyhost``
  does not match IP addresses in a certificate, so the name is the identity;
* the proxy presents its client certificate, and the box accepts nothing else.

Every value here ends up inside ``haproxy.cfg``, so each is validated strictly. Standard library
only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanpool.environment import MAXIMUM_PORT
from leanpool.names import is_dns_name, looks_numeric

MINIMUM_TLS_VERSION = "TLSv1.3"
DEFAULT_AGENT_TUNNEL_PORT = 18300
# X.509 allows a common name at most this long, and the proxy's client name is one.
MAXIMUM_CLIENT_NAME_LENGTH = 64

_PATH_PATTERN = re.compile(r"(/[A-Za-z0-9._-]+)+")


class TlsSettingsError(ValueError):
    """A TLS setting cannot be written into a configuration. The message says which, and why."""


@dataclass(frozen=True)
class TlsSettings:
    """The pool proxy's certificates, as paths seen by HAProxy (inside its container).

    * ``front_door_pem``: the front door's certificate and key in one file, shown to clients.
    * ``ca_file``: the pool authority's certificate, against which every box is checked.
    * ``client_pem``: the proxy's client certificate and key in one file, shown to every box.
    * ``agent_tunnel_port``: the first of the loopback ports through which the usage agents are
      asked (one port per server, counting up).
    """

    front_door_pem: str
    ca_file: str
    client_pem: str
    agent_tunnel_port: int = DEFAULT_AGENT_TUNNEL_PORT


def validate_tls_settings(settings: TlsSettings) -> None:
    """Refuse paths that are not plain absolute paths and a tunnel port that is not a port."""
    validate_path(settings.front_door_pem, "the front door's certificate file")
    validate_path(settings.ca_file, "the authority's certificate file")
    validate_path(settings.client_pem, "the proxy's client certificate file")
    if not 1 <= settings.agent_tunnel_port <= MAXIMUM_PORT:
        raise TlsSettingsError(
            f"agent tunnel port {settings.agent_tunnel_port} is outside 1-{MAXIMUM_PORT}"
        )


def validate_path(path: str, what: str) -> None:
    """Refuse a path that is not absolute or holds anything but letters, digits, ``._-/``.

    A space, a quote, a ``#`` or a line break in a path would let it rewrite the configuration
    it is written into.
    """
    if not _PATH_PATTERN.fullmatch(path):
        raise TlsSettingsError(
            f"{what} {path!r} must be an absolute path of letters, digits, '.', '_', '-' and '/'"
        )


def tls_identity(name: str) -> str:
    """Return the name a server's certificate must carry: its list name, in lower case.

    A list name may hold an underscore; a certificate's DNS name may not. A name of digits and
    dots would read as an address, which is never checked as a name.
    """
    if looks_numeric(name) or not is_dns_name(name):
        raise TlsSettingsError(
            f"with TLS the server name {name!r} is checked against the server's certificate, so "
            "it must be a DNS name: labels of letters, digits and '-', separated by '.'"
        )
    return name.lower()


def validate_client_name(name: str) -> None:
    """Refuse a proxy client name that could not be a certificate's common name."""
    if looks_numeric(name) or not is_dns_name(name) or len(name) > MAXIMUM_CLIENT_NAME_LENGTH:
        raise TlsSettingsError(
            f"the proxy's client name {name!r} must be a DNS name of at most "
            f"{MAXIMUM_CLIENT_NAME_LENGTH} characters: labels of letters, digits and '-', "
            "separated by '.'"
        )


def bind_options(server_pem: str) -> str:
    """The options of a ``bind`` line that serves TLS with the certificate in ``server_pem``."""
    return f"ssl crt {server_pem} ssl-min-ver {MINIMUM_TLS_VERSION}"


def mutual_bind_options(server_pem: str, ca_file: str) -> str:
    """A TLS ``bind`` that also requires a client certificate signed by the authority."""
    return f"{bind_options(server_pem)} ca-file {ca_file} verify required"


def server_options(settings: TlsSettings, identity: str) -> str:
    """The options of a ``server`` line that speaks mutual TLS to the box called ``identity``.

    ``verify required`` with ``ca-file`` accepts only a certificate the pool's authority
    signed; ``sni`` and ``verifyhost`` make that certificate's name the server's list name, for
    checks (which send no SNI of their own) as well as for traffic; ``crt`` is the proxy's
    client certificate.
    """
    return (
        f"ssl verify required ca-file {settings.ca_file} crt {settings.client_pem} "
        f"ssl-min-ver {MINIMUM_TLS_VERSION} sni str({identity}) verifyhost {identity}"
    )
