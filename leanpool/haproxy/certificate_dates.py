"""Reading one date from an X.509 certificate: the end of its validity. Standard library only.

The pool's facts for ``GET /metrics/pool`` name each certificate's last moment, and the HAProxy
tooling that writes them must run on a bare Python. So this reads the one field it needs
straight from the certificate's encoding (DER, RFC 5280) and nothing else: it checks no
signature and trusts nothing, and a file it cannot follow is refused, not guessed at.

A certificate is ``SEQUENCE { tbsCertificate, signatureAlgorithm, signature }``, and
``tbsCertificate`` is ``SEQUENCE { [0] version (optional), serialNumber, signature, issuer,
validity, ... }`` with ``validity`` a ``SEQUENCE { notBefore, notAfter }``.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import re

_PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.DOTALL
)
_SEQUENCE = 0x30
_INTEGER = 0x02
_VERSION = 0xA0  # [0], constructed: the version, which a version 1 certificate leaves out
_UTC_TIME = 0x17  # YYMMDDHHMMSSZ, for dates before 2050
_GENERALIZED_TIME = 0x18  # YYYYMMDDHHMMSSZ, from 2050 on
_LONG_FORM = 0x80
_LARGEST_LENGTH_BYTES = 4
# RFC 5280: a two-digit year from 50 on is 19YY, below 50 it is 20YY.
_UTC_TIME_PIVOT = 50
_TIME_PATTERNS = {
    _UTC_TIME: re.compile(rb"(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z"),
    _GENERALIZED_TIME: re.compile(rb"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})Z"),
}


class CertificateDateError(ValueError):
    """The text is not a certificate this reader can follow to its dates."""


def not_after_seconds(pem: str) -> int:
    """Return the end of validity of the first certificate in ``pem``, in seconds since 1970."""
    found = _PEM_CERTIFICATE.search(pem)
    if found is None:
        raise CertificateDateError("it holds no PEM certificate")
    try:
        encoded = base64.b64decode("".join(found.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        raise CertificateDateError("its certificate is not base64") from None
    signed_part = _content(encoded, 0, len(encoded), _SEQUENCE, "the certificate")
    position, end = _content(encoded, *signed_part, _SEQUENCE, "the signed part")
    if position < end and encoded[position] == _VERSION:
        position = _element(encoded, position, end)[2]
    for tag, name in (
        (_INTEGER, "the serial number"),
        (_SEQUENCE, "the signature's kind"),
        (_SEQUENCE, "the issuer"),
    ):
        position = _content(encoded, position, end, tag, name)[1]
    validity = _content(encoded, position, end, _SEQUENCE, "the validity")
    not_before_end = _element(encoded, *validity)[2]
    tag, start, stop = _element(encoded, not_before_end, validity[1])
    return _seconds(tag, encoded[start:stop])


def _element(encoded: bytes, position: int, end: int) -> tuple[int, int, int]:
    """The element at ``position``: its tag, where its content starts, and where it ends.

    ``end`` is where the element's parent ends: an element that would run past it is refused.
    """
    start = position + 2
    if start > end:
        raise CertificateDateError("it is not DER (it ends in the middle of an element)")
    tag, length = encoded[position], encoded[position + 1]
    if length >= _LONG_FORM:
        count = length - _LONG_FORM
        if not 1 <= count <= _LARGEST_LENGTH_BYTES or start + count > end:
            raise CertificateDateError("it is not DER (a length that cannot be read)")
        length = int.from_bytes(encoded[start : start + count], "big")
        start += count
    if start + length > end:
        raise CertificateDateError("it is not DER (an element runs past its parent)")
    return tag, start, start + length


def _content(encoded: bytes, position: int, end: int, tag: int, name: str) -> tuple[int, int]:
    """Where the content of the element at ``position`` starts and ends; it must be a ``tag``."""
    found, start, stop = _element(encoded, position, end)
    if found != tag:
        raise CertificateDateError(f"it is not a certificate ({name} is not where it should be)")
    return start, stop


def _seconds(tag: int, text: bytes) -> int:
    pattern = _TIME_PATTERNS.get(tag)
    matched = None if pattern is None else pattern.fullmatch(text)
    if matched is None:
        raise CertificateDateError("its last day is not written the way X.509 writes a date")
    year, month, day, hour, minute, second = (int(part) for part in matched.groups())
    if tag == _UTC_TIME:
        year += 1900 if year >= _UTC_TIME_PIVOT else 2000
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.UTC)
    except ValueError:
        raise CertificateDateError("its last day is not a date") from None
    return int(moment.timestamp())
