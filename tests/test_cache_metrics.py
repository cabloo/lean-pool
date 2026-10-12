"""``GET /metrics/cache`` on the cache service: the numbers of ``/status`` as Prometheus text,
for a scraper that has no key. Its exact text is a fixture: dashboards read these names.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fake_lean_server import SnippetReply, lean_answer
from support import AUTHORIZED, CacheClient, StartCache, StartLeanServer, check, status

from leanpool.cache.metrics import render_cache_metrics
from leanpool.metrics import CACHE_METRICS_PATH, CONTENT_TYPE

PROOF = "theorem two : 1 + 1 = 2 := rfl"
OTHER_PROOF = "theorem three : 1 + 2 = 3 := rfl"

# A cache that has served nothing, with the default size cap. The size of the database file is
# the one number that is the machine's own; it is written where the braces are.
FRESH_CACHE = """\
# HELP leanpool_cache_hits_total Checks answered from the store since the cache started.
# TYPE leanpool_cache_hits_total counter
leanpool_cache_hits_total 0
# HELP leanpool_cache_misses_total Checks not in the store, forwarded to the Lean servers, \
since the cache started.
# TYPE leanpool_cache_misses_total counter
leanpool_cache_misses_total 0
# HELP leanpool_cache_coalesced_total Checks that waited for an identical check already in \
flight, since the cache started.
# TYPE leanpool_cache_coalesced_total counter
leanpool_cache_coalesced_total 0
# HELP leanpool_cache_bypasses_total Checks sent with no_cache, forwarded without using the \
store, since the cache started.
# TYPE leanpool_cache_bypasses_total counter
leanpool_cache_bypasses_total 0
# HELP leanpool_cache_evictions_total Entries removed to keep the store under its size cap, \
since the cache started.
# TYPE leanpool_cache_evictions_total counter
leanpool_cache_evictions_total 0
# HELP leanpool_cache_in_flight Checks waiting for the Lean servers right now.
# TYPE leanpool_cache_in_flight gauge
leanpool_cache_in_flight 0
# HELP leanpool_cache_stored_entries Entries in the store.
# TYPE leanpool_cache_stored_entries gauge
leanpool_cache_stored_entries 0
# HELP leanpool_cache_stored_bytes Bytes of keys and payloads in the store: what the size cap \
counts.
# TYPE leanpool_cache_stored_bytes gauge
leanpool_cache_stored_bytes 0
# HELP leanpool_cache_file_bytes Bytes the store's database takes on disk.
# TYPE leanpool_cache_file_bytes gauge
leanpool_cache_file_bytes {file_bytes}
# HELP leanpool_cache_maximum_bytes The store's size cap, in bytes.
# TYPE leanpool_cache_maximum_bytes gauge
leanpool_cache_maximum_bytes 4294967296
"""


async def scrape(cache: CacheClient, **request: Any) -> tuple[int, str, str]:
    """``GET /metrics/cache`` with no key: its status, its content type and its text."""
    async with cache.get(CACHE_METRICS_PATH, **request) as response:
        return response.status, response.headers["Content-Type"], await response.text()


def samples(text: str) -> dict[str, int]:
    """The value of every sample of a scrape, by metric name (none here has a label)."""
    lines = [line.split() for line in text.splitlines() if not line.startswith("#")]
    return {name: int(value) for name, value in lines}


async def test_a_fresh_caches_metrics_are_the_fixture_to_the_character(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    cache = await start_cache((await start_lean_server()).url)
    file_bytes = (await status(cache))["file_bytes"]

    assert await scrape(cache) == (200, CONTENT_TYPE, FRESH_CACHE.format(file_bytes=file_bytes))
    assert CONTENT_TYPE == "text/plain; version=0.0.4"
    assert CACHE_METRICS_PATH == "/metrics/cache"


def test_the_text_names_ten_metrics_each_with_its_help_and_its_type() -> None:
    numbers = dict.fromkeys(
        (
            "hits",
            "misses",
            "coalesced",
            "bypasses",
            "evictions",
            "in_flight",
            "stored_entries",
            "stored_bytes",
            "file_bytes",
        ),
        7,
    )
    lines = render_cache_metrics(numbers, maximum_bytes=9).splitlines()
    names = [line.split()[0] for line in lines if not line.startswith("#")]
    assert names == [
        "leanpool_cache_hits_total",
        "leanpool_cache_misses_total",
        "leanpool_cache_coalesced_total",
        "leanpool_cache_bypasses_total",
        "leanpool_cache_evictions_total",
        "leanpool_cache_in_flight",
        "leanpool_cache_stored_entries",
        "leanpool_cache_stored_bytes",
        "leanpool_cache_file_bytes",
        "leanpool_cache_maximum_bytes",
    ]
    for index, name in enumerate(names):
        kind = "counter" if name.endswith("_total") else "gauge"
        help_line, type_line, sample = lines[3 * index : 3 * index + 3]
        assert help_line.startswith(f"# HELP {name} ")
        assert type_line == f"# TYPE {name} {kind}"
        assert sample == f"{name} {9 if name.endswith('maximum_bytes') else 7}"


async def test_the_metrics_count_what_status_counts(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    cache = await start_cache(lean.url)
    first = asyncio.create_task(check(cache, PROOF, identifier="first"))  # a miss
    second = asyncio.create_task(check(cache, PROOF, identifier="second"))  # joins the first
    while samples((await scrape(cache))[2])["leanpool_cache_coalesced_total"] < 1:
        await asyncio.sleep(0.01)
    assert samples((await scrape(cache))[2])["leanpool_cache_in_flight"] == 1
    release.set()
    await asyncio.gather(first, second)
    await check(cache, PROOF)  # a hit
    await check(cache, OTHER_PROOF, no_cache=True)  # a bypass

    counters = await status(cache)
    scraped = samples((await scrape(cache))[2])

    assert scraped == {
        "leanpool_cache_hits_total": 1,
        "leanpool_cache_misses_total": 1,
        "leanpool_cache_coalesced_total": 1,
        "leanpool_cache_bypasses_total": 1,
        "leanpool_cache_evictions_total": 0,
        "leanpool_cache_in_flight": 0,
        "leanpool_cache_stored_entries": 1,
        "leanpool_cache_stored_bytes": counters["stored_bytes"],
        "leanpool_cache_file_bytes": counters["file_bytes"],
        "leanpool_cache_maximum_bytes": counters["maximum_bytes"],
    }
    assert scraped["leanpool_cache_stored_bytes"] > 0


async def test_the_size_cap_is_the_one_the_cache_was_given(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    cache = await start_cache((await start_lean_server()).url, maximum_bytes=12345)
    assert samples((await scrape(cache))[2])["leanpool_cache_maximum_bytes"] == 12345


async def test_the_metrics_need_no_key_and_status_still_does(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    cache = await start_cache((await start_lean_server()).url)
    assert (await scrape(cache))[0] == 200
    assert (await scrape(cache, headers={"Authorization": "Bearer not-the-key"}))[0] == 200
    async with cache.get("/status") as response:
        assert response.status == 401
    async with cache.get("/status", headers=AUTHORIZED) as response:
        assert response.status == 200  # and exactly what it was: its own tests hold it to that


async def test_the_metrics_hold_no_key_no_proof_and_no_address(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF)
    text = (await scrape(cache))[2]
    for secret in ("pool-key", "theorem", "127.0.0.1", "http", "sqlite"):
        assert secret not in text


async def test_the_route_only_reads(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    """HEAD is answered like GET, without a body; a POST is not a route of the cache."""
    cache = await start_cache((await start_lean_server()).url)
    before = await status(cache)
    async with cache.head(CACHE_METRICS_PATH) as response:
        assert (response.status, await response.read()) == (200, b"")
        assert response.headers["Content-Type"] == CONTENT_TYPE
    async with cache.post(CACHE_METRICS_PATH, headers=AUTHORIZED) as response:
        assert response.status == 405
    assert await status(cache) == before
