"""Reaching a Lean server through its box's TLS front, to test that server alone.

With TLS a Lean server answers only through its box's front, and that front lets in nothing but
the pool proxy's client certificate. The admission test therefore presents that certificate, and
holds the server to the proof the proxy will ask of it: a certificate signed by the pool's
authority, for the server's name in the server list, whatever host or address is dialled.

Before any check is sent, ``probe`` makes one connection and asks for ``/health`` without the
API key. A TLS client finishes its handshake before the server has judged the client's
certificate, so only an answer shows that the server accepted it. Every way this can fail is
reported in words that say which side refused which certificate, within a fixed time, and the
API key has not been sent to anyone.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from leanpool.names import is_dns_name, looks_numeric

HTTPS_SCHEME = "https://"
_DEFAULT_HTTPS_PORT = 443
_FIRST_BYTES = 256

# OpenSSL's verification results (X509_V_ERR_*), as ``SSLCertVerificationError.verify_code``.
_NOT_YET_VALID = 9
_EXPIRED = 10
_HOSTNAME_MISMATCH = 62
_UNKNOWN_AUTHORITY = frozenset(
    {
        2,  # unable to get issuer certificate
        7,  # certificate signature failure
        18,  # self-signed certificate
        19,  # self-signed certificate in certificate chain
        20,  # unable to get local issuer certificate
        21,  # unable to verify the first certificate
    }
)
# Why a server turned our client certificate away, by the TLS alert it sent.
_CLIENT_CERTIFICATE_ALERTS = {
    "CERTIFICATE_REQUIRED": "the server received no client certificate",
    "UNSUPPORTED_CERTIFICATE": "it is not a client certificate",
    "UNKNOWN_CA": "it was not signed by the authority the server trusts",
    "CERTIFICATE_EXPIRED": "it has expired",
    "BAD_CERTIFICATE": "the server could not use it",
    "CERTIFICATE_UNKNOWN": "the server did not accept it",
    "ACCESS_DENIED": "the server denied it access",
}
CLOSED_WITHOUT_AN_ANSWER = (
    "the server closed the connection after the TLS handshake without an answer; a box's TLS "
    "front does that to a client certificate that is not the pool proxy's (--tls-client-pem)"
)
IS_THIS_A_FRONT = "is this the port of a TLS front?"


class AdmissionTlsError(ValueError):
    """The TLS options are unusable. The message never quotes a file's content."""


class TlsHandshakeError(Exception):
    """The server could not be reached over TLS. The message says who refused what."""


@dataclass(frozen=True)
class AdmissionTls:
    """How to reach a server through its TLS front, and the name it must prove.

    ``context`` trusts the pool's authority and nothing else, and presents the pool proxy's
    client certificate. ``server_name`` is sent as the SNI and is the name the server's
    certificate must carry, whatever host or address the connection is made to.
    """

    context: ssl.SSLContext
    server_name: str


def load_admission_tls(ca_file: Path, client_pem: Path, server_name: str) -> AdmissionTls:
    """Read the authority's certificate and the client's certificate and key, or refuse them.

    ``ssl.create_default_context(cafile=...)`` loads the given authority only: the system's own
    authorities are not trusted, so no public certificate can pass for a pool's server.
    """
    if looks_numeric(server_name) or not is_dns_name(server_name):
        raise AdmissionTlsError(
            f"--tls-server-name {server_name!r} must be a DNS name: the server's name in the "
            "pool's server list, not an address"
        )
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
    except ssl.SSLError:
        raise AdmissionTlsError(
            f"--tls-ca-file {ca_file} does not hold a PEM certificate"
        ) from None
    except OSError as error:
        raise AdmissionTlsError(
            f"cannot read --tls-ca-file {ca_file}: {error.strerror or error}"
        ) from None
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(str(client_pem))
    except ssl.SSLError:
        raise AdmissionTlsError(
            f"--tls-client-pem {client_pem} does not hold a certificate followed by its private key"
        ) from None
    except OSError as error:
        raise AdmissionTlsError(
            f"cannot read --tls-client-pem {client_pem}: {error.strerror or error}"
        ) from None
    return AdmissionTls(context=context, server_name=server_name.lower())


def validate_https_url(server_url: str) -> None:
    """Refuse an ``https://`` URL that names no host or an unusable port."""
    try:
        url = urlsplit(server_url)
        has_host = bool(url.hostname) and url.port != 0
    except ValueError:
        has_host = False
    if not has_host or not server_url.isascii() or url.query or url.fragment:
        raise AdmissionTlsError(f"--server {server_url!r} must be https://HOST:PORT")


async def probe(server_url: str, tls: AdmissionTls, timeout_seconds: float) -> None:
    """Make one TLS connection and ask for ``/health``, without the API key.

    Returns once the server has answered anything at all: it proved its name, and it accepted
    our certificate. Raises ``TlsHandshakeError`` when either certificate was refused, and
    ``OSError`` or ``TimeoutError`` when the server was not there or stayed silent.
    """
    url = urlsplit(server_url)
    host, port = url.hostname or "", url.port or _DEFAULT_HTTPS_PORT
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=tls.context, server_hostname=tls.server_name),
            timeout_seconds,
        )
    except ssl.SSLCertVerificationError as error:
        raise TlsHandshakeError(describe_server_certificate(error, tls.server_name)) from error
    except ssl.SSLError as error:
        raise TlsHandshakeError(
            f"the TLS handshake failed ({_reason(error)}); {IS_THIS_A_FRONT}"
        ) from error
    except ConnectionRefusedError:
        raise  # nothing listens there: the server is not reachable, which is not about TLS
    except ConnectionError as error:
        raise TlsHandshakeError(
            f"the connection was closed during the TLS handshake; {IS_THIS_A_FRONT}"
        ) from error
    request = f"GET {url.path.rstrip('/')}/health HTTP/1.1\r\nHost: {url.netloc}\r\n"
    try:
        writer.write(f"{request}Connection: close\r\n\r\n".encode("ascii"))
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(_FIRST_BYTES), timeout_seconds)
    except ssl.SSLError as error:
        raise TlsHandshakeError(describe_client_certificate(error)) from error
    except ConnectionError as error:
        raise TlsHandshakeError(CLOSED_WITHOUT_AN_ANSWER) from error
    finally:
        # Dropped, not closed politely: a peer that never answers must not hold the probe.
        writer.transport.abort()
    if not answer:
        raise TlsHandshakeError(CLOSED_WITHOUT_AN_ANSWER)


def describe_server_certificate(error: ssl.SSLCertVerificationError, server_name: str) -> str:
    """Say why the server's certificate was refused, in the words of the option to look at."""
    openssl = f"(OpenSSL: {error.verify_message})"
    if error.verify_code == _HOSTNAME_MISMATCH:
        return (
            f"the server's certificate is not for the name {server_name!r} given as "
            f"--tls-server-name {openssl}"
        )
    if error.verify_code in _UNKNOWN_AUTHORITY:
        return (
            f"the server's certificate was not signed by the authority in --tls-ca-file {openssl}"
        )
    if error.verify_code == _EXPIRED:
        return f"the server's certificate has expired {openssl}"
    if error.verify_code == _NOT_YET_VALID:
        return f"the server's certificate is not valid yet {openssl}"
    return f"the server's certificate was refused {openssl}"


def describe_client_certificate(error: ssl.SSLError) -> str:
    """Say why the server turned away the certificate we presented."""
    reason = _reason(error)
    for alert, meaning in _CLIENT_CERTIFICATE_ALERTS.items():
        if reason.endswith(f"ALERT_{alert}"):
            return (
                "the server refused the client certificate in --tls-client-pem: "
                f"{meaning} (TLS alert: {alert.lower().replace('_', ' ')})"
            )
    return f"the server ended the TLS connection without an answer ({reason})"


def _reason(error: ssl.SSLError) -> str:
    return error.reason or type(error).__name__
