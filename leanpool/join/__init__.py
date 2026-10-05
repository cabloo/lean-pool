"""The join service: how a new Lean server box asks to join a pool, with nothing run for it.

A box fetches a script, sends a signing request for a key it made itself, receives its
certificate and the pool's key, says when its server is ready, and waits for the verdict. The
service only moves these between the box and a spool directory; whoever watches the spool
decides and acts.
"""

from leanpool.join.app import create_application
from leanpool.join.image import IMAGE_CHUNK_BYTES, ShippedImage, shipped_image
from leanpool.join.messages import (
    CertificateAnswer,
    InvalidMessageError,
    SigningRequest,
    Verdict,
    parse_certificate_answer,
    parse_signing_request,
    parse_verdict,
)
from leanpool.join.settings import (
    MAXIMUM_REQUEST_BYTES,
    JoinSettings,
    TokenError,
    validate_token,
)
from leanpool.join.spool import Spool, SpoolError, Stored

__all__ = [
    "IMAGE_CHUNK_BYTES",
    "MAXIMUM_REQUEST_BYTES",
    "CertificateAnswer",
    "InvalidMessageError",
    "JoinSettings",
    "ShippedImage",
    "SigningRequest",
    "Spool",
    "SpoolError",
    "Stored",
    "TokenError",
    "Verdict",
    "create_application",
    "parse_certificate_answer",
    "parse_signing_request",
    "parse_verdict",
    "shipped_image",
    "validate_token",
]
