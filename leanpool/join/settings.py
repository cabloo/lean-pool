"""The join service's settings, and the rule for a window's token."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 128 bits written in URL-safe base64 are 22 characters; in hexadecimal, 32.
MINIMUM_TOKEN_LENGTH = 22
MAXIMUM_TOKEN_LENGTH = 128
# The largest request body accepted, from the service's interface: 16 KiB.
MAXIMUM_REQUEST_BYTES = 16 * 1024

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class TokenError(ValueError):
    """The window's token is missing or malformed. The message never contains the token."""


def validate_token(text: str) -> str:
    """Return the token with surrounding whitespace removed, or refuse it.

    The token is the last part of a URL, so it is letters, digits, ``-`` and ``_`` only. It
    must be long enough to hold 128 random bits: a shorter one could be guessed within a
    window.
    """
    token = text.strip()
    if not token:
        raise TokenError("the token is empty")
    if not _TOKEN_PATTERN.fullmatch(token):
        raise TokenError("the token must be letters, digits, '-' and '_' only")
    if not MINIMUM_TOKEN_LENGTH <= len(token) <= MAXIMUM_TOKEN_LENGTH:
        raise TokenError(
            f"the token must be {MINIMUM_TOKEN_LENGTH} to {MAXIMUM_TOKEN_LENGTH} characters "
            "(128 random bits are 22 in URL-safe base64, 32 in hexadecimal)"
        )
    return token


@dataclass(frozen=True)
class JoinSettings:
    """Everything the join service needs for one window.

    * ``token`` opens the window: a request without it is answered 404, whatever it asks.
    * ``script`` is what ``GET /j/<token>`` serves. It must hold no secret.
    * ``spool`` is the directory shared with the pool's operator side (``leanpool.join.spool``).
    * ``api_key`` is the pool's key, handed to the box together with its certificate, or None
      for a pool whose Lean servers have none.
    * ``tls_pem`` is the certificate and key the service presents: the pool's front door's, so
      that the pin a joining box checks is the front door's.
    * ``image_directory`` holds the Lean server image this window ships and its manifest
      (``leanpool.join.image``), or is None for a window that ships none: a box then builds
      its own.
    """

    token: str
    script: bytes
    spool: Path
    api_key: str | None
    tls_pem: Path
    host: str = "0.0.0.0"
    port: int = 18110
    image_directory: Path | None = None
