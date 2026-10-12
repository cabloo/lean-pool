"""The cache's counters as Prometheus text, for ``GET /metrics/cache``.

They are the numbers of ``GET /status`` and nothing else: how each check since start was served,
what is in flight, and how full the store is. No key, no proof and no address is among them,
which is why this one route of the cache asks for no key.
"""

from __future__ import annotations

from collections.abc import Mapping

from leanpool.metrics import Metric, Sample, render_metrics

# (the name in ``/status``, the metric's name, its help), in the order they are written.
_COUNTERS = (
    (
        "hits",
        "leanpool_cache_hits_total",
        "Checks answered from the store since the cache started.",
    ),
    (
        "misses",
        "leanpool_cache_misses_total",
        "Checks not in the store, forwarded to the Lean servers, since the cache started.",
    ),
    (
        "coalesced",
        "leanpool_cache_coalesced_total",
        "Checks that waited for an identical check already in flight, since the cache started.",
    ),
    (
        "bypasses",
        "leanpool_cache_bypasses_total",
        "Checks sent with no_cache, forwarded without using the store, since the cache started.",
    ),
    (
        "evictions",
        "leanpool_cache_evictions_total",
        "Entries removed to keep the store under its size cap, since the cache started.",
    ),
)
_GAUGES = (
    ("in_flight", "leanpool_cache_in_flight", "Checks waiting for the Lean servers right now."),
    ("stored_entries", "leanpool_cache_stored_entries", "Entries in the store."),
    (
        "stored_bytes",
        "leanpool_cache_stored_bytes",
        "Bytes of keys and payloads in the store: what the size cap counts.",
    ),
    ("file_bytes", "leanpool_cache_file_bytes", "Bytes the store's database takes on disk."),
    ("maximum_bytes", "leanpool_cache_maximum_bytes", "The store's size cap, in bytes."),
)


def render_cache_metrics(status: Mapping[str, int], maximum_bytes: int) -> str:
    """Return what ``CheckService.status`` gives, and the size cap, as Prometheus text."""
    numbers = {**status, "maximum_bytes": maximum_bytes}
    return render_metrics(
        [
            *(
                Metric(name, "counter", text, [Sample(numbers[field])])
                for field, name, text in _COUNTERS
            ),
            *(
                Metric(name, "gauge", text, [Sample(numbers[field])])
                for field, name, text in _GAUGES
            ),
        ]
    )
