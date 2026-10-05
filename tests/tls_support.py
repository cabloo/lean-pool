"""Throwaway certificates for the tests, made by the project's own certificate code."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509

from leanpool.pki import (
    CERTIFICATE_LIFETIME,
    CertificateAuthority,
    Usage,
    create_authority,
    issue_certificate,
    new_private_key,
    save_authority,
    save_identity,
    subject_names,
)
from leanpool.pki.files import AUTHORITY_CERTIFICATE_NAME


@dataclass
class ThrowawayAuthority:
    """A certificate authority in a directory, and the certificates it has issued there."""

    directory: Path
    authority: CertificateAuthority

    @classmethod
    def create(cls, directory: Path, name: str = "lean-pool test CA") -> ThrowawayAuthority:
        authority = create_authority(name)
        save_authority(directory, authority)
        return cls(directory=directory, authority=authority)

    @property
    def certificate_path(self) -> Path:
        return self.directory / AUTHORITY_CERTIFICATE_NAME

    def issue(
        self,
        name: str,
        usage: Usage,
        dns: tuple[str, ...] = (),
        addresses: tuple[str, ...] = (),
        stem: str | None = None,
        expired: bool = False,
    ) -> tuple[Path, x509.Certificate]:
        """Issue a certificate and return its combined PEM file and the certificate.

        The files are named after ``stem``, or after the certificate's name. An ``expired``
        certificate was made a month ago for a week.
        """
        private_key = new_private_key()
        certificate = issue_certificate(
            self.authority,
            private_key.public_key(),
            name,
            subject_names([name, *dns], addresses),
            usage,
            lifetime=timedelta(days=7) if expired else CERTIFICATE_LIFETIME,
            now=datetime.now(UTC) - timedelta(days=30) if expired else None,
        )
        stem = stem or name
        save_identity(self.directory, stem, certificate, private_key, replace=True)
        return self.directory / f"{stem}.pem", certificate

    def server(self, name: str, *dns: str, addresses: tuple[str, ...] = ()) -> Path:
        """A server certificate for ``name`` (and more names), as a combined PEM file."""
        return self.issue(name, Usage.SERVER, dns, addresses)[0]

    def client(self, name: str) -> Path:
        """A client certificate for ``name``, as a combined PEM file."""
        return self.issue(name, Usage.CLIENT)[0]


def strict_client_context(authority_file: Path) -> ssl.SSLContext:
    """What a pool's client builds: the default context trusting only the pool's authority.

    Python 3.13 turned ``VERIFY_X509_STRICT`` on by default; it is set here so the strict rules
    are exercised on older interpreters as well.
    """
    context = ssl.create_default_context(cafile=str(authority_file))
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


@contextlib.contextmanager
def tls_server(
    server_pem: Path, client_authority_file: Path | None = None
) -> Iterator[tuple[int, list[str]]]:
    """Serve TLS on a loopback port; each accepted connection is sent ``ok`` and closed.

    With ``client_authority_file`` a client certificate signed by that authority is required,
    verified strictly. Yields the port and the list of handshake failures seen by the server.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(server_pem))
    if client_authority_file is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        context.load_verify_locations(cafile=str(client_authority_file))
    failures: list[str] = []
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stopping = threading.Event()

    def serve() -> None:
        while not stopping.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            connection.settimeout(5.0)
            try:
                with context.wrap_socket(connection, server_side=True) as secured:
                    secured.sendall(b"ok")
            except (ssl.SSLError, OSError) as error:
                failures.append(str(error))
            finally:
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], failures
    finally:
        stopping.set()
        thread.join(timeout=5.0)
        listener.close()


def handshake(
    port: int, context: ssl.SSLContext, server_hostname: str, timeout: float = 5.0
) -> bytes:
    """Connect to the loopback port, complete a handshake and return what the server sent."""
    with (
        socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection,
        context.wrap_socket(connection, server_hostname=server_hostname) as secured,
    ):
        return secured.recv(16)
