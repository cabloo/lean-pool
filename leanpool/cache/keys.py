"""The cache key: what makes two checks the same check."""

from __future__ import annotations

import hashlib
import json


def check_key(pin: str, code: str, timeout: int | None) -> bytes:
    """Return the SHA-256 of (pin, code, timeout), the identity of a check.

    * ``pin`` names the pool's Lean and Mathlib versions. The same code can check differently on
      another toolchain, so a pool whose image changes must change its pin, and every earlier
      entry then stops matching.
    * ``timeout`` is included because the answer is only known to hold within that limit. None
      is the server's own default, which is its own key.
    * The snippet's ``id`` is deliberately absent: it names the attempt, not the proof, and
      repeated attempts at the same proof are exactly what the cache exists to answer.

    The three values are hashed as one JSON array, which keeps them unambiguous: no choice of
    pin and code can produce the bytes of a different pin and code.
    """
    material = json.dumps([pin, code, timeout], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("ascii")).digest()
