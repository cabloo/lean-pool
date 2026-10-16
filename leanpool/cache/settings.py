"""The cache service's settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanpool.cache.policy import DEFAULT_EXHAUSTION_PATTERNS
from leanpool.loop_guard import DEFAULT_HOP_HEADER


@dataclass(frozen=True)
class CacheSettings:
    """Everything the cache service needs to run.

    * ``pin`` names the pool's Lean and Mathlib versions and is part of every cache key. Change
      it whenever the Lean servers' image changes; every earlier entry then stops matching.
    * ``api_key`` is the pool's key, or None for a pool whose Lean servers have none. The cache
      checks it itself, because a request answered from the store never reaches a Lean server
      that would.
    * ``screened_api_key`` is a second key, for senders trusted less than the pool's own, or None.
      A check sent with it must pass the screen (``leanpool.screen``), and is forwarded with the
      pool's key, so no Lean server ever holds it. It needs ``api_key`` and must differ from it.
    * ``upstream_timeout_seconds`` must cover the proxy's queue wait plus the slowest check; the
      generated ``haproxy.cfg`` states the minimum in its first lines.
    """

    pin: str
    api_key: str | None
    database_path: Path
    screened_api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 18102
    upstream_url: str = "http://127.0.0.1:18101"
    upstream_timeout_seconds: float = 1800.0
    maximum_bytes: int = 4 * 1024**3
    maximum_request_bytes: int = 16 * 1024**2
    exhaustion_patterns: tuple[str, ...] = DEFAULT_EXHAUSTION_PATTERNS
    hop_header: str = DEFAULT_HOP_HEADER

    def __post_init__(self) -> None:
        if self.screened_api_key is not None and self.api_key is None:
            raise ValueError("a screened key needs the pool's own key")
        if self.screened_api_key is not None and self.screened_api_key == self.api_key:
            raise ValueError("the screened key must differ from the pool's own key")
