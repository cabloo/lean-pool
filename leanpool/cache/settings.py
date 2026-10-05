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
    * ``upstream_timeout_seconds`` must cover the proxy's queue wait plus the slowest check; the
      generated ``haproxy.cfg`` states the minimum in its first lines.
    """

    pin: str
    api_key: str | None
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 18102
    upstream_url: str = "http://127.0.0.1:18101"
    upstream_timeout_seconds: float = 1800.0
    maximum_bytes: int = 4 * 1024**3
    maximum_request_bytes: int = 16 * 1024**2
    exhaustion_patterns: tuple[str, ...] = DEFAULT_EXHAUSTION_PATTERNS
    hop_header: str = DEFAULT_HOP_HEADER
