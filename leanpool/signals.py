"""What a check tells the pool beside itself, and what the pool tells a client beside its answer.

*Priority.* A client marks a check as background work with one request header. The proxy's queue
then takes it only when no normal check is waiting. Anything but the one value is a normal check, so
an old client and a mistyped value behave as they always did.

*Capacity.* Every answer that leaves the pool's front door carries three response headers: the
workers on servers that are up, the checks waiting for a worker, and the servers taking checks
(up, and not drained by their usage agent).
``GET /health`` carries the same numbers in its body. They are advisory: nothing in the pool reads
them back, and a client that cannot read them keeps the number it was configured with.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PRIORITY_HEADER = "X-Lean-Priority"
BACKGROUND_PRIORITY = "background"
# HAProxy serves the lowest priority class first and every request starts in class 0.
BACKGROUND_PRIORITY_CLASS = 100

WORKERS_HEADER = "X-Lean-Pool-Workers"
QUEUED_HEADER = "X-Lean-Pool-Queued"
SERVERS_HEADER = "X-Lean-Pool-Servers"

# The same three numbers in the body of ``GET /health``.
WORKERS_FIELD = "workers"
QUEUED_FIELD = "queued"
SERVERS_FIELD = "servers"

_LARGEST_COUNT = 1_000_000


def is_background(value: str | None) -> bool:
    """Whether a ``X-Lean-Priority`` value marks background work (the one value, in any case)."""
    return value is not None and value.strip().lower() == BACKGROUND_PRIORITY


@dataclass(frozen=True)
class PoolCapacity:
    """What the pool said about itself with one answer."""

    workers: int
    queued: int
    servers: int


def read_count(value: object) -> int | None:
    """A count as the pool writes it: a whole number, not negative. Anything else is ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _LARGEST_COUNT else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if number <= _LARGEST_COUNT else None


def capacity_from_headers(headers: Mapping[str, str]) -> PoolCapacity | None:
    """Read the three response headers; ``None`` unless all three are whole numbers.

    ``headers`` must find a name whatever its letter case, as HTTP header collections do.
    """
    counts = [
        read_count(headers.get(name)) for name in (WORKERS_HEADER, QUEUED_HEADER, SERVERS_HEADER)
    ]
    if any(count is None for count in counts):
        return None
    workers, queued, servers = (count for count in counts if count is not None)
    return PoolCapacity(workers=workers, queued=queued, servers=servers)


def capacity_from_health(body: object) -> PoolCapacity | None:
    """Read the three numbers from the body of ``GET /health``; ``None`` unless all are there."""
    if not isinstance(body, Mapping):
        return None
    counts = [read_count(body.get(name)) for name in (WORKERS_FIELD, QUEUED_FIELD, SERVERS_FIELD)]
    if any(count is None for count in counts):
        return None
    workers, queued, servers = (count for count in counts if count is not None)
    return PoolCapacity(workers=workers, queued=queued, servers=servers)
