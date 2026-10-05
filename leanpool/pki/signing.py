"""Signing requests: a box makes one for its own key, and the pool decides what to sign.

A box's private key never leaves the box. The box sends a signing request instead, and the pool
signs a certificate for the request's public key. What that certificate says is decided by the
pool alone:

* it is valid for exactly the names the pool was told to allow, never for what the request asks;
* it is a server certificate, whatever the request asks;
* no extension of the request is copied.

A request is still refused, rather than quietly corrected, when it asks for a name outside the
allowed ones or asks to be a CA: a box that asks for more than it was offered is either broken
or hostile, and neither should be handed a certificate.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from leanpool.pki.authority import (
    CERTIFICATE_LIFETIME,
    CertificateAuthority,
    Usage,
    accept_public_key,
    issue_certificate,
)
from leanpool.pki.names import IpAddress, PkiError, SubjectNames


def make_request(
    private_key: ec.EllipticCurvePrivateKey, common_name: str, names: SubjectNames
) -> x509.CertificateSigningRequest:
    """Make a signing request for ``private_key`` that asks for ``names``."""
    if not names:
        raise PkiError("a signing request needs at least one name")
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .add_extension(x509.SubjectAlternativeName(names.general_names()), critical=False)
        .sign(private_key, hashes.SHA256())
    )


def sign_request(
    authority: CertificateAuthority,
    request: x509.CertificateSigningRequest,
    allowed: SubjectNames,
    lifetime: timedelta = CERTIFICATE_LIFETIME,
    now: datetime | None = None,
) -> x509.Certificate:
    """Sign a server certificate for the request's key, valid for exactly ``allowed``.

    The certificate's common name is the first allowed DNS name. HAProxy accepts a certificate
    whose common name matches the server it expects even when the alternative names do not, so
    the common name is chosen here and never taken from the request.
    """
    if not allowed.dns:
        raise PkiError("at least one DNS name must be allowed: it becomes the certificate's name")
    if not request.is_signature_valid:
        raise PkiError("the request's signature is wrong: it was not made with its own key")
    accept_public_key(request.public_key())
    refuse_authority_request(request)
    refuse_names_outside(request, allowed)
    return issue_certificate(
        authority,
        request.public_key(),
        common_name=allowed.dns[0],
        names=allowed,
        usage=Usage.SERVER,
        lifetime=lifetime,
        now=now,
    )


def refuse_authority_request(request: x509.CertificateSigningRequest) -> None:
    """Refuse a request that asks to be a CA or to sign certificates."""
    for extension in _extensions(request):
        value = extension.value
        if isinstance(value, x509.BasicConstraints) and value.ca:
            raise PkiError("the request asks to be a certificate authority")
        if isinstance(value, x509.KeyUsage) and (value.key_cert_sign or value.crl_sign):
            raise PkiError("the request asks to sign certificates")


def refuse_names_outside(request: x509.CertificateSigningRequest, allowed: SubjectNames) -> None:
    """Refuse a request that names anything outside ``allowed``.

    Every common name of the subject and every alternative name counts. A common name is read
    as an address if it is one, and as a DNS name otherwise.
    """
    dns_names, addresses = _names_asked_for(request)
    outside = [name for name in dns_names if name not in allowed.dns]
    outside += [str(address) for address in addresses if address not in allowed.addresses]
    if outside:
        raise PkiError(
            f"the request asks for {', '.join(dict.fromkeys(outside))}, which is outside the "
            f"allowed names ({allowed.describe()})"
        )


def _extensions(request: x509.CertificateSigningRequest) -> x509.Extensions:
    try:
        return request.extensions
    except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType) as error:
        raise PkiError(f"the request's extensions cannot be read: {error}") from error


def _names_asked_for(
    request: x509.CertificateSigningRequest,
) -> tuple[list[str], list[IpAddress]]:
    dns_names: list[str] = []
    addresses: list[IpAddress] = []
    for attribute in request.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        if not isinstance(attribute.value, str):
            raise PkiError("the request's common name is not text")
        try:
            addresses.append(ipaddress.ip_address(attribute.value))
        except ValueError:
            dns_names.append(attribute.value.lower())
    for extension in _extensions(request):
        if not isinstance(extension.value, x509.SubjectAlternativeName):
            continue
        for name in extension.value:
            if isinstance(name, x509.DNSName):
                dns_names.append(name.value.lower())
            elif isinstance(name, x509.IPAddress) and isinstance(
                name.value, ipaddress.IPv4Address | ipaddress.IPv6Address
            ):
                addresses.append(name.value)
            else:
                raise PkiError(
                    "the request asks for a name that is neither a DNS name nor an IP address"
                )
    return dns_names, addresses
