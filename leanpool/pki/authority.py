"""The pool's certificate authority, and the certificates it signs.

One authority per pool signs three kinds of certificate, and each kind can do one thing only:

* a **server** certificate (the front door, each Lean server box) carries the extended key usage
  ``serverAuth`` and nothing else, so it cannot be presented as a client certificate;
* a **client** certificate (the proxy's, towards the boxes) carries ``clientAuth`` and nothing
  else;
* the authority's own certificate may sign certificates and nothing below it may.

Every function here is a pure function of keys, names and a clock, so each rule is tested
without a file. Certificates are valid from one day before they are made: a box whose clock is
behind would otherwise refuse a certificate issued a moment ago as "not yet valid".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.types import CertificatePublicKeyTypes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from leanpool.pki.names import MAXIMUM_COMMON_NAME_LENGTH, PkiError, SubjectNames

DEFAULT_AUTHORITY_NAME = "lean-pool CA"
AUTHORITY_LIFETIME = timedelta(days=3650)
CERTIFICATE_LIFETIME = timedelta(days=1825)
BACKDATED_BY = timedelta(days=1)

MINIMUM_RSA_BITS = 2048
_ACCEPTED_CURVES = (ec.SECP256R1, ec.SECP384R1, ec.SECP521R1)

IssuerKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey


class Usage(enum.Enum):
    """The one thing a certificate may be used for."""

    SERVER = ExtendedKeyUsageOID.SERVER_AUTH
    CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH


@dataclass(frozen=True)
class CertificateAuthority:
    """The authority's certificate and the private key that signs with it."""

    certificate: x509.Certificate
    private_key: IssuerKey


def new_private_key() -> ec.EllipticCurvePrivateKey:
    """Make a private key: elliptic curve P-256, which every TLS 1.3 implementation supports."""
    return ec.generate_private_key(ec.SECP256R1())


def create_authority(
    name: str = DEFAULT_AUTHORITY_NAME,
    lifetime: timedelta = AUTHORITY_LIFETIME,
    now: datetime | None = None,
) -> CertificateAuthority:
    """Make a new authority: a key and a self-signed certificate that may only sign leaves.

    ``pathLenConstraint`` is 0: the authority signs the pool's certificates directly, and a
    certificate it signed can never sign another.
    """
    _validate_authority_name(name)
    _validate_lifetime(lifetime)
    issued_at = _clock(now)
    private_key = new_private_key()
    subject = _common_name(name)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(issued_at - BACKDATED_BY)
        .not_valid_after(issued_at + lifetime)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(signs_certificates=True), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False
        )
        .sign(private_key, hashes.SHA256())
    )
    return CertificateAuthority(certificate=certificate, private_key=private_key)


def check_authority(authority: CertificateAuthority, now: datetime | None = None) -> None:
    """Refuse an authority that cannot sign: a mismatched key, not a CA, or expired."""
    certificate = authority.certificate
    if _public_bytes(certificate.public_key()) != _public_bytes(authority.private_key.public_key()):
        raise PkiError("the authority's private key does not belong to its certificate")
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        raise PkiError("the authority's certificate is not a CA certificate") from None
    if not constraints.ca:
        raise PkiError("the authority's certificate is not a CA certificate")
    if certificate.not_valid_after_utc <= _clock(now):
        raise PkiError(
            f"the authority's certificate expired on {certificate.not_valid_after_utc:%Y-%m-%d}"
        )


def issue_certificate(
    authority: CertificateAuthority,
    public_key: CertificatePublicKeyTypes,
    common_name: str,
    names: SubjectNames,
    usage: Usage,
    lifetime: timedelta = CERTIFICATE_LIFETIME,
    now: datetime | None = None,
) -> x509.Certificate:
    """Sign a certificate for ``public_key`` that is valid for ``names`` and for one usage.

    The certificate never outlives the authority: a certificate that did would be refused by
    every verifier from the day the authority expired, with nothing saying why.
    """
    if not names:
        raise PkiError("a certificate needs at least one name")
    _validate_lifetime(lifetime)
    accept_public_key(public_key)
    issued_at = _clock(now)
    check_authority(authority, issued_at)
    expires_at = min(issued_at + lifetime, authority.certificate.not_valid_after_utc)
    return (
        x509.CertificateBuilder()
        .subject_name(_common_name(common_name))
        .issuer_name(authority.certificate.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(issued_at - BACKDATED_BY)
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(signs_certificates=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage.value]), critical=False)
        .add_extension(x509.SubjectAlternativeName(names.general_names()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(_authority_key_identifier(authority.certificate), critical=False)
        .sign(authority.private_key, hashes.SHA256())
    )


def accept_public_key(public_key: CertificatePublicKeyTypes) -> None:
    """Refuse a key this pool does not sign: anything but P-256/384/521 or RSA of 2048 bits up."""
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if isinstance(public_key.curve, _ACCEPTED_CURVES):
            return
        raise PkiError(f"the key's curve {public_key.curve.name} is not accepted")
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size >= MINIMUM_RSA_BITS:
            return
        raise PkiError(
            f"an RSA key of {public_key.key_size} bits is too short (at least {MINIMUM_RSA_BITS})"
        )
    raise PkiError("the key is neither an elliptic-curve key nor an RSA key")


def _clock(now: datetime | None) -> datetime:
    """The time to issue at, in UTC and in whole seconds (a certificate holds no finer time)."""
    return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)


def _validate_lifetime(lifetime: timedelta) -> None:
    if lifetime <= timedelta(0):
        raise PkiError("a certificate's lifetime must be positive")


def _validate_authority_name(name: str) -> None:
    is_plain = name.isascii() and name.isprintable() and name == name.strip()
    if not is_plain or not 1 <= len(name) <= MAXIMUM_COMMON_NAME_LENGTH:
        raise PkiError(
            f"the authority's name must be 1 to {MAXIMUM_COMMON_NAME_LENGTH} printable ASCII "
            "characters"
        )


def _common_name(name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])


def _key_usage(*, signs_certificates: bool) -> x509.KeyUsage:
    """An authority signs certificates and revocation lists; a leaf signs TLS handshakes."""
    return x509.KeyUsage(
        digital_signature=not signs_certificates,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=signs_certificates,
        crl_sign=signs_certificates,
        encipher_only=False,
        decipher_only=False,
    )


def _authority_key_identifier(certificate: x509.Certificate) -> x509.AuthorityKeyIdentifier:
    try:
        identifier = certificate.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    except x509.ExtensionNotFound:
        return x509.AuthorityKeyIdentifier.from_issuer_public_key(_issuer_key(certificate))
    return x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(identifier.value)


def _issuer_key(certificate: x509.Certificate) -> ec.EllipticCurvePublicKey | rsa.RSAPublicKey:
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey | rsa.RSAPublicKey):
        raise PkiError("the authority's key is neither an elliptic-curve key nor an RSA key")
    return public_key


def _public_bytes(public_key: CertificatePublicKeyTypes) -> bytes:
    return public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
