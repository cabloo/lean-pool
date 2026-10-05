"""What a certificate says about itself: the pin of its public key, and the days it has left."""

from __future__ import annotations

import base64
import hashlib
import math
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PIN_PREFIX = "sha256//"


def public_key_pin(certificate: x509.Certificate) -> str:
    """Return the certificate's public-key pin, the value ``curl --pinnedpubkey`` takes.

    It is ``sha256//`` followed by the base64 of the SHA-256 of the DER-encoded
    SubjectPublicKeyInfo. The pin names the key, not the certificate: a renewed certificate for
    the same key has the same pin.
    """
    public_key = certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    return PIN_PREFIX + base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")


def days_left(certificate: x509.Certificate, now: datetime | None = None) -> int:
    """Return the whole days until the certificate expires; negative once it has."""
    remaining = certificate.not_valid_after_utc - (now or datetime.now(UTC))
    return math.floor(remaining / timedelta(days=1))
