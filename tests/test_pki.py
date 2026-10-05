"""The pool's certificate authority: what it signs, what a certificate may do, what it refuses."""

from __future__ import annotations

import base64
import ipaddress
import shutil
import ssl
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPrivateKeyTypes
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from tls_support import ThrowawayAuthority, handshake, strict_client_context, tls_server

from leanpool.pki import (
    CertificateAuthority,
    PkiError,
    SubjectNames,
    Usage,
    check_authority,
    create_authority,
    days_left,
    issue_certificate,
    make_request,
    new_private_key,
    public_key_pin,
    sign_request,
    subject_names,
)
from leanpool.pki.names import parse_common_name, parse_dns_name, parse_ip_address

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)
OPENSSL = shutil.which("openssl")
LEAF_EXTENSIONS = {
    x509.BasicConstraints,
    x509.KeyUsage,
    x509.ExtendedKeyUsage,
    x509.SubjectAlternativeName,
    x509.SubjectKeyIdentifier,
    x509.AuthorityKeyIdentifier,
}


@pytest.fixture(scope="module")
def authority() -> CertificateAuthority:
    return create_authority(now=NOW)


def issue(
    authority: CertificateAuthority,
    usage: Usage = Usage.SERVER,
    names: SubjectNames | None = None,
    lifetime: timedelta = 1825 * DAY,
    now: datetime = NOW,
) -> x509.Certificate:
    names = names or subject_names(["lean-a"])
    return issue_certificate(
        authority, new_private_key().public_key(), "lean-a", names, usage, lifetime, now
    )


def extension_types(certificate: x509.Certificate) -> set[type]:
    return {type(extension.value) for extension in certificate.extensions}


def extension(certificate: x509.Certificate, kind: type[x509.ExtensionType]) -> x509.Extension:  # type: ignore[type-arg]
    return certificate.extensions.get_extension_for_class(kind)


def set_key_usages(certificate: x509.Certificate) -> set[str]:
    usage = extension(certificate, x509.KeyUsage).value
    flags = (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
    )
    return {flag for flag in flags if getattr(usage, flag)}


def alternative_names(certificate: x509.Certificate) -> list[str]:
    names = extension(certificate, x509.SubjectAlternativeName).value
    return [str(name.value) for name in names]


def common_names(name: x509.Name) -> list[str | bytes]:
    return [attribute.value for attribute in name.get_attributes_for_oid(NameOID.COMMON_NAME)]


def request(
    common_name: str | None = "lean-a",
    alternative: Sequence[x509.GeneralName] = (x509.DNSName("lean-a"),),
    extensions: Sequence[tuple[x509.ExtensionType, bool]] = (),
    private_key: CertificateIssuerPrivateKeyTypes | None = None,
) -> x509.CertificateSigningRequest:
    """A signing request built by hand, so a test can ask for anything."""
    attributes = (
        [] if common_name is None else [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    builder = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes))
    if alternative:
        builder = builder.add_extension(x509.SubjectAlternativeName(alternative), critical=False)
    for value, critical in extensions:
        builder = builder.add_extension(value, critical=critical)
    private_key = private_key or new_private_key()
    algorithm = None if isinstance(private_key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    return builder.sign(private_key, algorithm)


def key_usage(**flags: bool) -> x509.KeyUsage:
    names = (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
    )
    values = {name: flags.get(name, False) for name in names}
    return x509.KeyUsage(**values, encipher_only=False, decipher_only=False)


def test_the_authority_is_a_p256_key_that_may_sign_certificates_and_nothing_else(
    authority: CertificateAuthority,
) -> None:
    certificate = authority.certificate
    assert isinstance(authority.private_key, ec.EllipticCurvePrivateKey)
    assert isinstance(authority.private_key.curve, ec.SECP256R1)
    assert certificate.issuer == certificate.subject
    certificate.verify_directly_issued_by(certificate)
    constraints = extension(certificate, x509.BasicConstraints)
    assert constraints.critical
    assert (constraints.value.ca, constraints.value.path_length) == (True, 0)
    assert extension(certificate, x509.KeyUsage).critical
    assert set_key_usages(certificate) == {"key_cert_sign", "crl_sign"}
    assert extension_types(certificate) == {
        x509.BasicConstraints,
        x509.KeyUsage,
        x509.SubjectKeyIdentifier,
    }


def test_the_authority_lasts_ten_years(authority: CertificateAuthority) -> None:
    assert authority.certificate.not_valid_after_utc == NOW + 3650 * DAY
    assert days_left(authority.certificate, NOW) == 3650


@pytest.mark.parametrize(
    ("usage", "purpose"),
    [
        (Usage.SERVER, ExtendedKeyUsageOID.SERVER_AUTH),
        (Usage.CLIENT, ExtendedKeyUsageOID.CLIENT_AUTH),
    ],
)
def test_a_certificate_has_one_usage_only(
    authority: CertificateAuthority, usage: Usage, purpose: x509.ObjectIdentifier
) -> None:
    certificate = issue(authority, usage)
    assert list(extension(certificate, x509.ExtendedKeyUsage).value) == [purpose]


def test_a_certificate_cannot_sign_and_carries_every_extension_a_strict_verifier_wants(
    authority: CertificateAuthority,
) -> None:
    certificate = issue(authority)
    certificate.verify_directly_issued_by(authority.certificate)
    constraints = extension(certificate, x509.BasicConstraints)
    assert constraints.critical
    assert constraints.value.ca is False
    assert extension(certificate, x509.KeyUsage).critical
    assert set_key_usages(certificate) == {"digital_signature"}
    assert extension_types(certificate) == LEAF_EXTENSIONS
    authority_identifier = extension(certificate, x509.AuthorityKeyIdentifier).value
    issuer_identifier = extension(authority.certificate, x509.SubjectKeyIdentifier).value
    assert authority_identifier.key_identifier == issuer_identifier.digest
    assert not extension(certificate, x509.SubjectKeyIdentifier).critical
    assert not extension(certificate, x509.AuthorityKeyIdentifier).critical


def test_a_certificate_carries_dns_names_and_ip_addresses(
    authority: CertificateAuthority,
) -> None:
    names = subject_names(["Pool.Example", "pool"], ["192.0.2.7", "2001:db8::7"])
    certificate = issue(authority, names=names)
    assert alternative_names(certificate) == ["pool.example", "pool", "192.0.2.7", "2001:db8::7"]
    assert common_names(certificate.subject) == ["lean-a"]


def test_a_certificate_lasts_five_years_and_starts_a_day_early(
    authority: CertificateAuthority,
) -> None:
    certificate = issue(authority)
    assert certificate.not_valid_after_utc == NOW + 1825 * DAY
    # A box whose clock is behind must not refuse a certificate made a moment ago.
    assert certificate.not_valid_before_utc == NOW - DAY
    assert days_left(certificate, NOW) == 1825


def test_a_certificate_never_outlives_its_authority() -> None:
    short_lived = create_authority(lifetime=30 * DAY, now=NOW)
    certificate = issue(short_lived)
    assert certificate.not_valid_after_utc == short_lived.certificate.not_valid_after_utc


def test_an_expired_authority_signs_nothing() -> None:
    expired = create_authority(lifetime=30 * DAY, now=NOW - 31 * DAY)
    with pytest.raises(PkiError, match="expired on 2026-10-02"):
        issue(expired)


def test_every_certificate_has_its_own_serial_number(authority: CertificateAuthority) -> None:
    serials = {issue(authority).serial_number for _ in range(5)}
    assert len(serials) == 5


def test_a_certificate_needs_a_name_and_a_positive_lifetime(
    authority: CertificateAuthority,
) -> None:
    with pytest.raises(PkiError, match="at least one name"):
        issue_certificate(
            authority, new_private_key().public_key(), "lean-a", SubjectNames(), Usage.SERVER
        )
    with pytest.raises(PkiError, match="lifetime must be positive"):
        issue(authority, lifetime=timedelta(0))
    with pytest.raises(PkiError, match="lifetime must be positive"):
        create_authority(lifetime=-DAY)


@pytest.mark.parametrize("name", ["", " lean", "lean\nCA", "léan", "x" * 65])
def test_an_authority_name_must_be_plain(name: str) -> None:
    with pytest.raises(PkiError, match="authority's name"):
        create_authority(name)


def test_an_authority_must_hold_its_own_key_and_be_a_ca(authority: CertificateAuthority) -> None:
    check_authority(authority, NOW)
    with pytest.raises(PkiError, match="does not belong"):
        check_authority(CertificateAuthority(authority.certificate, new_private_key()), NOW)
    leaf_key = new_private_key()
    leaf = issue_certificate(
        authority, leaf_key.public_key(), "lean-a", subject_names(["lean-a"]), Usage.SERVER, now=NOW
    )
    with pytest.raises(PkiError, match="not a CA certificate"):
        check_authority(CertificateAuthority(leaf, leaf_key), NOW)


def test_a_certificate_signed_by_a_leaf_is_never_made(authority: CertificateAuthority) -> None:
    leaf_key = new_private_key()
    leaf = issue_certificate(
        authority, leaf_key.public_key(), "lean-a", subject_names(["lean-a"]), Usage.SERVER, now=NOW
    )
    with pytest.raises(PkiError, match="not a CA certificate"):
        issue(CertificateAuthority(leaf, leaf_key))


def test_a_request_made_here_asks_for_its_names(authority: CertificateAuthority) -> None:
    private_key = new_private_key()
    made = make_request(private_key, "lean-a", subject_names(["lean-a"], ["192.0.2.7"]))
    assert made.is_signature_valid
    assert common_names(made.subject) == ["lean-a"]
    certificate = sign_request(authority, made, subject_names(["lean-a"], ["192.0.2.7"]), now=NOW)
    assert certificate.public_key() == private_key.public_key()
    with pytest.raises(PkiError, match="at least one name"):
        make_request(private_key, "lean-a", SubjectNames())


def test_a_signed_request_is_a_server_certificate_for_exactly_the_allowed_names(
    authority: CertificateAuthority,
) -> None:
    """The names come from what the pool allows, never from what the request asks."""
    asked = request(common_name="lean-a", alternative=[x509.DNSName("lean-a")])
    allowed = subject_names(["lean-a", "lean-a.example"], ["192.0.2.7"])
    certificate = sign_request(authority, asked, allowed, now=NOW)
    assert alternative_names(certificate) == ["lean-a", "lean-a.example", "192.0.2.7"]
    assert common_names(certificate.subject) == ["lean-a"]
    assert list(extension(certificate, x509.ExtendedKeyUsage).value) == [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    assert certificate.public_key() == asked.public_key()
    assert certificate.not_valid_after_utc == NOW + 1825 * DAY
    certificate.verify_directly_issued_by(authority.certificate)


@pytest.mark.parametrize(
    ("common_name", "alternative", "outside"),
    [
        ("lean-b", [x509.DNSName("lean-a")], "lean-b"),
        ("lean-a", [x509.DNSName("lean-a"), x509.DNSName("lean-b")], "lean-b"),
        (None, [x509.DNSName("lean-b")], "lean-b"),
        ("lean-b", [], "lean-b"),
        ("lean-a", [x509.DNSName("*.example")], r"\*\.example"),
        ("lean-a", [x509.IPAddress(ipaddress.ip_address("192.0.2.8"))], r"192\.0\.2\.8"),
        ("192.0.2.8", [x509.DNSName("lean-a")], r"192\.0\.2\.8"),
        ("lean-pool-proxy", [x509.DNSName("lean-a")], "lean-pool-proxy"),
    ],
)
def test_a_request_for_a_name_outside_the_allowed_ones_is_refused(
    authority: CertificateAuthority,
    common_name: str | None,
    alternative: list[x509.GeneralName],
    outside: str,
) -> None:
    allowed = subject_names(["lean-a"], ["192.0.2.7"])
    with pytest.raises(PkiError, match=f"asks for {outside}, which is outside the allowed names"):
        sign_request(authority, request(common_name, alternative), allowed, now=NOW)


@pytest.mark.parametrize(
    "name",
    [
        x509.RFC822Name("root@lean-a"),
        x509.UniformResourceIdentifier("https://lean-a"),
        x509.IPAddress(ipaddress.ip_network("192.0.2.0/24")),
    ],
)
def test_a_request_for_any_other_kind_of_name_is_refused(
    authority: CertificateAuthority, name: x509.GeneralName
) -> None:
    asked = request(alternative=[x509.DNSName("lean-a"), name])
    with pytest.raises(PkiError, match="neither a DNS name nor an IP address"):
        sign_request(authority, asked, subject_names(["lean-a"], ["192.0.2.7"]), now=NOW)


def test_requested_names_are_compared_without_regard_to_case(
    authority: CertificateAuthority,
) -> None:
    asked = request(common_name="Lean-A", alternative=[x509.DNSName("LEAN-A")])
    certificate = sign_request(authority, asked, subject_names(["lean-a"]), now=NOW)
    assert alternative_names(certificate) == ["lean-a"]


def test_a_request_may_ask_for_less_than_it_is_allowed(authority: CertificateAuthority) -> None:
    asked = request(common_name=None, alternative=[])
    certificate = sign_request(authority, asked, subject_names(["lean-a"]), now=NOW)
    assert alternative_names(certificate) == ["lean-a"]
    assert common_names(certificate.subject) == ["lean-a"]


@pytest.mark.parametrize(
    ("asked_extension", "reason"),
    [
        (x509.BasicConstraints(ca=True, path_length=None), "asks to be a certificate authority"),
        (x509.BasicConstraints(ca=True, path_length=0), "asks to be a certificate authority"),
        (key_usage(key_cert_sign=True), "asks to sign certificates"),
        (key_usage(digital_signature=True, crl_sign=True), "asks to sign certificates"),
    ],
)
def test_a_request_asking_to_be_a_ca_is_refused(
    authority: CertificateAuthority, asked_extension: x509.ExtensionType, reason: str
) -> None:
    asked = request(extensions=[(asked_extension, True)])
    with pytest.raises(PkiError, match=reason):
        sign_request(authority, asked, subject_names(["lean-a"]), now=NOW)


def test_every_requested_extension_is_ignored(authority: CertificateAuthority) -> None:
    """A box cannot ask its way to a client certificate, or to anything else."""
    asked = request(
        extensions=[
            (x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), True),
            (x509.BasicConstraints(ca=False, path_length=None), True),
            (key_usage(key_encipherment=True, data_encipherment=True), True),
            (
                x509.UnrecognizedExtension(
                    x509.ObjectIdentifier("1.3.6.1.4.1.99999.1"), b"\x05\x00"
                ),
                False,
            ),
            (x509.OCSPNoCheck(), False),
        ]
    )
    certificate = sign_request(authority, asked, subject_names(["lean-a"]), now=NOW)
    assert extension_types(certificate) == LEAF_EXTENSIONS
    assert list(extension(certificate, x509.ExtendedKeyUsage).value) == [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    assert set_key_usages(certificate) == {"digital_signature"}


def test_other_subject_fields_of_a_request_are_not_copied(authority: CertificateAuthority) -> None:
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "lean-pool-proxy"),
            x509.NameAttribute(NameOID.COMMON_NAME, "lean-a"),
        ]
    )
    asked = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .sign(new_private_key(), hashes.SHA256())
    )
    certificate = sign_request(authority, asked, subject_names(["lean-a"]), now=NOW)
    assert certificate.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lean-a")])


def test_a_request_not_signed_by_its_own_key_is_refused(authority: CertificateAuthority) -> None:
    encoded = bytearray(request().public_bytes(Encoding.DER))
    encoded[-1] ^= 0x01  # the last byte of the signature
    tampered = x509.load_der_x509_csr(bytes(encoded))
    with pytest.raises(PkiError, match="signature is wrong"):
        sign_request(authority, tampered, subject_names(["lean-a"]), now=NOW)


def test_a_weak_or_unusual_key_is_refused(authority: CertificateAuthority) -> None:
    allowed = subject_names(["lean-a"])
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(PkiError, match="1024 bits is too short"):
        sign_request(authority, request(private_key=weak), allowed, now=NOW)
    with pytest.raises(PkiError, match="neither an elliptic-curve key nor an RSA key"):
        sign_request(
            authority, request(private_key=ed25519.Ed25519PrivateKey.generate()), allowed, now=NOW
        )
    odd_curve = ec.generate_private_key(ec.SECP256K1())
    with pytest.raises(PkiError, match="secp256k1 is not accepted"):
        sign_request(authority, request(private_key=odd_curve), allowed, now=NOW)


def test_an_rsa_key_of_2048_bits_is_signed(authority: CertificateAuthority) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = sign_request(
        authority, request(private_key=private_key), subject_names(["lean-a"]), now=NOW
    )
    assert certificate.public_key() == private_key.public_key()


def test_signing_needs_an_allowed_dns_name(authority: CertificateAuthority) -> None:
    with pytest.raises(PkiError, match="at least one DNS name must be allowed"):
        sign_request(authority, request(), subject_names([], ["192.0.2.7"]), now=NOW)


MALFORMED_DNS_NAMES = ["", "*.example", "lean_a", "lean a", "lean-a.", ".lean-a", "-lean", "lean-"]
MALFORMED_DNS_NAMES += [
    "a..b",
    "lean\na",
    "192.0.2.7",
    "1234",
    "léan",
    "a" * 64,
    "lean#a",
    "lean/a",
]


@pytest.mark.parametrize("name", MALFORMED_DNS_NAMES)
def test_a_malformed_dns_name_is_refused(name: str) -> None:
    with pytest.raises(PkiError, match="is not a DNS name"):
        parse_dns_name(name)


def test_dns_names_are_lower_case_and_repeats_are_dropped() -> None:
    names = subject_names(["Lean-A", "lean-a", "LEAN-A.Example"], ["192.0.2.7", "192.0.2.7"])
    assert names.dns == ("lean-a", "lean-a.example")
    assert names.addresses == (ipaddress.ip_address("192.0.2.7"),)
    assert names.describe() == "lean-a, lean-a.example, 192.0.2.7"


def test_a_common_name_holds_at_most_64_characters() -> None:
    longest = ".".join(["a" * 31, "b" * 32])
    assert parse_common_name(longest) == longest
    with pytest.raises(PkiError, match="longer than 64 characters"):
        parse_common_name(longest + "c")


@pytest.mark.parametrize("address", ["", "lean-a", "192.0.2", "192.0.2.256", "192.0.2.7/24"])
def test_a_malformed_ip_address_is_refused(address: str) -> None:
    with pytest.raises(PkiError, match="is not an IP address"):
        parse_ip_address(address)


def test_the_pin_names_the_key_not_the_certificate(authority: CertificateAuthority) -> None:
    private_key = new_private_key()
    names = subject_names(["lean-a"])
    first = issue_certificate(
        authority, private_key.public_key(), "lean-a", names, Usage.SERVER, now=NOW
    )
    renewed = issue_certificate(
        authority, private_key.public_key(), "lean-a", names, Usage.SERVER, now=NOW + 400 * DAY
    )
    pin = public_key_pin(first)
    assert pin.startswith("sha256//")
    assert len(base64.b64decode(pin.removeprefix("sha256//"), validate=True)) == 32
    assert public_key_pin(renewed) == pin
    assert public_key_pin(issue(authority)) != pin


def test_days_left_are_whole_days_and_negative_after_expiry(
    authority: CertificateAuthority,
) -> None:
    certificate = issue(authority, lifetime=10 * DAY)
    assert days_left(certificate, NOW) == 10
    assert days_left(certificate, NOW + timedelta(seconds=1)) == 9
    assert days_left(certificate, NOW + 10 * DAY - timedelta(seconds=1)) == 0
    assert days_left(certificate, NOW + 10 * DAY + timedelta(seconds=1)) == -1


def openssl(*arguments: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    assert OPENSSL is not None
    return subprocess.run([OPENSSL, *arguments], input=stdin, capture_output=True, check=False)


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_the_pin_is_what_openssl_computes(tmp_path: Path) -> None:
    pool = ThrowawayAuthority.create(tmp_path)
    _pem, certificate = pool.issue("pool.example", Usage.SERVER)
    public_key = openssl("x509", "-in", str(tmp_path / "pool.example.crt"), "-pubkey", "-noout")
    encoded = openssl("pkey", "-pubin", "-outform", "der", stdin=public_key.stdout)
    digest = openssl("dgst", "-sha256", "-binary", stdin=encoded.stdout)
    assert digest.returncode == 0, digest.stderr
    expected = "sha256//" + base64.b64encode(digest.stdout).decode("ascii")
    assert public_key_pin(certificate) == expected


@pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")
def test_openssl_accepts_each_certificate_for_its_own_purpose_only(tmp_path: Path) -> None:
    pool = ThrowawayAuthority.create(tmp_path)
    pool.server("pool.example")
    pool.client("lean-pool-proxy")

    def verifies(purpose: str, name: str) -> bool:
        result = openssl(
            "verify",
            "-x509_strict",
            "-CAfile",
            str(pool.certificate_path),
            "-purpose",
            purpose,
            str(tmp_path / f"{name}.crt"),
        )
        return result.returncode == 0

    assert verifies("sslserver", "pool.example")
    assert not verifies("sslclient", "pool.example")
    assert verifies("sslclient", "lean-pool-proxy")
    assert not verifies("sslserver", "lean-pool-proxy")


def test_a_strict_python_client_trusts_a_server_certificate_by_name_and_by_address(
    tmp_path: Path,
) -> None:
    """What the pool's clients do: ``ssl.create_default_context(cafile=<the pool's CA>)``."""
    pool = ThrowawayAuthority.create(tmp_path / "pool")
    front_door = pool.server("pool.example", "pool", addresses=("127.0.0.1",))
    context = strict_client_context(pool.certificate_path)
    assert context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.check_hostname
    with tls_server(front_door) as (port, failures):
        assert handshake(port, context, "pool.example") == b"ok"
        assert handshake(port, context, "pool") == b"ok"
        assert handshake(port, context, "127.0.0.1") == b"ok"
        with pytest.raises(ssl.SSLCertVerificationError, match="Hostname mismatch"):
            handshake(port, context, "other.example")
        with pytest.raises(ssl.SSLCertVerificationError, match="IP address mismatch"):
            handshake(port, context, "127.0.0.2")
    assert len(failures) == 2  # the two refused handshakes; the three good ones left no trace


def test_a_strict_python_client_refuses_a_certificate_from_another_authority(
    tmp_path: Path,
) -> None:
    pool = ThrowawayAuthority.create(tmp_path / "pool")
    other = ThrowawayAuthority.create(tmp_path / "other")
    impostor = other.server("pool.example", "pool", addresses=("127.0.0.1",))
    context = strict_client_context(pool.certificate_path)
    with tls_server(impostor) as (port, _failures):
        for name in ("pool.example", "127.0.0.1"):
            with pytest.raises(ssl.SSLCertVerificationError, match="certificate verify failed"):
                handshake(port, context, name)


def client_context(authority_file: Path, client_pem: Path | None) -> ssl.SSLContext:
    context = strict_client_context(authority_file)
    if client_pem is not None:
        context.load_cert_chain(str(client_pem))
    return context


def test_a_server_that_wants_a_client_certificate_accepts_only_a_client_certificate(
    tmp_path: Path,
) -> None:
    """A box's server certificate, presented as a client certificate, opens nothing."""
    pool = ThrowawayAuthority.create(tmp_path / "pool")
    other = ThrowawayAuthority.create(tmp_path / "other")
    box = pool.server("lean-a", addresses=("127.0.0.1",))
    another_box = pool.server("lean-b")
    proxy = pool.client("lean-pool-proxy")
    stranger = other.client("lean-pool-proxy")
    with tls_server(box, client_authority_file=pool.certificate_path) as (port, failures):
        assert handshake(port, client_context(pool.certificate_path, proxy), "lean-a") == b"ok"
        assert failures == []
        for presented in (None, another_box, stranger):
            # In TLS 1.3 the client finishes first; the server's refusal arrives as an alert
            # (or as a closed connection) when the client next reads.
            context = client_context(pool.certificate_path, presented)
            try:
                received = handshake(port, context, "lean-a")
            except (ssl.SSLError, ConnectionError):
                received = b""
            assert received == b""
    assert len(failures) == 3
    assert "PEER_DID_NOT_RETURN_A_CERTIFICATE" in failures[0]
    # OpenSSL's words: "unsuitable certificate purpose" ("unsupported ..." before 3.2).
    assert "certificate purpose" in failures[1]
    assert "issuer certificate" in failures[2]  # no issuer it trusts: the wrong authority
