"""Certificates and keys on disk: PEM files that are complete or absent, never half-written.

File names, in the directory a command is given:

* an authority: ``ca.crt`` and ``ca.key``;
* an issued certificate: ``<stem>.crt``, ``<stem>.key`` and ``<stem>.pem``, the certificate
  followed by the key in one file, which is what HAProxy's ``crt`` takes;
* a signing request: ``<stem>.key`` and ``<stem>.csr``.

A file holding a private key has mode 0600 from the moment it exists. Every file is written
under a temporary name and then put in place in one step; by default it is put in place only if
nothing is there, so an existing key is never overwritten by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)

from leanpool.atomic import write_atomically
from leanpool.pki.authority import CertificateAuthority, IssuerKey, check_authority
from leanpool.pki.names import PkiError

AUTHORITY_CERTIFICATE_NAME = "ca.crt"
AUTHORITY_KEY_NAME = "ca.key"
PUBLIC_MODE = 0o644
PRIVATE_MODE = 0o600
_NEW_DIRECTORY_MODE = 0o700


def write_file(path: Path, data: bytes, mode: int, *, replace: bool = False) -> None:
    """Put ``data`` at ``path`` in one step, with ``mode``; refuse an existing file.

    The file has ``mode`` before it has any content, and it is put in place only if nothing is
    there (see ``leanpool.atomic``), unless ``replace`` says to overwrite.
    """
    try:
        write_atomically(path, data, mode, replace=replace)
    except FileExistsError:
        raise PkiError(f"{path} already exists; it was not overwritten") from None


def refuse_existing(paths: Sequence[Path], *, replace: bool = False) -> None:
    """Refuse before anything is made if a target exists and replacing was not asked for."""
    if replace:
        return
    existing = [str(path) for path in paths if path.exists() or path.is_symlink()]
    if existing:
        raise PkiError(f"{', '.join(existing)} already exists; nothing was written")


def ensure_directory(directory: Path) -> None:
    """Create ``directory`` (for its owner only) if it is missing."""
    directory.mkdir(mode=_NEW_DIRECTORY_MODE, parents=True, exist_ok=True)


def certificate_pem(certificate: x509.Certificate) -> bytes:
    """The certificate as PEM."""
    return certificate.public_bytes(Encoding.PEM)


def private_key_pem(private_key: IssuerKey) -> bytes:
    """The private key as unencrypted PKCS #8 PEM."""
    return private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def read_certificate(path: Path) -> x509.Certificate:
    """Read the first certificate in a PEM file (a combined certificate-and-key file works)."""
    data = _read(path, "certificate")
    try:
        return x509.load_pem_x509_certificate(data)
    except ValueError:
        raise PkiError(f"{path} does not hold a PEM certificate") from None


def read_request(path: Path) -> x509.CertificateSigningRequest:
    """Read a PEM signing request."""
    data = _read(path, "signing request")
    try:
        return x509.load_pem_x509_csr(data)
    except ValueError:
        raise PkiError(f"{path} does not hold a PEM signing request") from None


def read_private_key(path: Path) -> IssuerKey:
    """Read an unencrypted PEM private key. Its content never appears in an error."""
    data = _read(path, "private key")
    try:
        private_key = load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise PkiError(f"{path} does not hold an unencrypted PEM private key") from None
    if not isinstance(private_key, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey):
        raise PkiError(f"the key in {path} is neither an elliptic-curve key nor an RSA key")
    return private_key


def save_authority(directory: Path, authority: CertificateAuthority) -> list[Path]:
    """Write a new authority into ``directory``. An existing one is never overwritten."""
    key_path = directory / AUTHORITY_KEY_NAME
    certificate_path = directory / AUTHORITY_CERTIFICATE_NAME
    refuse_existing([certificate_path, key_path])
    ensure_directory(directory)
    write_file(key_path, private_key_pem(authority.private_key), PRIVATE_MODE)
    write_file(certificate_path, certificate_pem(authority.certificate), PUBLIC_MODE)
    return [certificate_path, key_path]


def load_authority(directory: Path) -> CertificateAuthority:
    """Read the authority in ``directory`` and refuse one that cannot sign."""
    authority = CertificateAuthority(
        certificate=read_certificate(directory / AUTHORITY_CERTIFICATE_NAME),
        private_key=read_private_key(directory / AUTHORITY_KEY_NAME),
    )
    check_authority(authority)
    return authority


def identity_paths(directory: Path, stem: str) -> list[Path]:
    """Where an issued certificate goes: the certificate, the key, and both in one file."""
    return [directory / f"{stem}.crt", directory / f"{stem}.key", directory / f"{stem}.pem"]


def save_identity(
    directory: Path,
    stem: str,
    certificate: x509.Certificate,
    private_key: IssuerKey,
    *,
    replace: bool = False,
) -> list[Path]:
    """Write a certificate, its key, and the combined file HAProxy's ``crt`` takes.

    The combined file holds the key, so it is as private as the key file.
    """
    certificate_path, key_path, combined_path = identity_paths(directory, stem)
    certificate_text, key_text = certificate_pem(certificate), private_key_pem(private_key)
    write_file(key_path, key_text, PRIVATE_MODE, replace=replace)
    write_file(certificate_path, certificate_text, PUBLIC_MODE, replace=replace)
    write_file(combined_path, certificate_text + key_text, PRIVATE_MODE, replace=replace)
    return [certificate_path, key_path, combined_path]


def request_paths(directory: Path, stem: str) -> list[Path]:
    """Where a signing request goes: the request, and the key it was made for."""
    return [directory / f"{stem}.csr", directory / f"{stem}.key"]


def save_request(
    directory: Path,
    stem: str,
    request: x509.CertificateSigningRequest,
    private_key: IssuerKey,
    *,
    replace: bool = False,
) -> list[Path]:
    """Write a signing request and the private key it was made for."""
    request_path, key_path = request_paths(directory, stem)
    write_file(key_path, private_key_pem(private_key), PRIVATE_MODE, replace=replace)
    write_file(request_path, request.public_bytes(Encoding.PEM), PUBLIC_MODE, replace=replace)
    return [request_path, key_path]


def _read(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PkiError(f"cannot read the {what} {path}: {error.strerror or error}") from None
