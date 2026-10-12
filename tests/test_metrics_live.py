"""The three metrics routes on a running HAProxy, in front of the real cache and fake Lean
servers: what a Prometheus scraper that has no key is answered. Everything listens on loopback.

``test_haproxy_metrics.py`` shows what the configuration says; these show what HAProxy does
with it. They are skipped where ``haproxy`` is not installed. On an ``haproxy`` that was built
without its Prometheus exporter they FAIL, and say so: the pool would not start there either.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from fake_lean_server import FakeLeanServer, SnippetReply, lean_answer
from live_pool import (
    HAPROXY,
    MISSING_EXPORTER,
    WORKERS,
    LivePool,
    eventually,
    free_port,
    haproxy_has_exporter,
)
from live_tls import FRONT_DOOR_NAME, PROXY_CLIENT_NAME, BoxFront, PoolCertificates, TlsLivePool
from support import API_KEY, StartLeanServer

from leanpool.haproxy import (
    maximum_pool_metrics_bytes,
    read_certificate_dates,
    render_pool_metrics,
    write_pool_metrics,
)
from leanpool.haproxy.cli import main
from leanpool.haproxy.servers import format_server_line
from leanpool.metrics import CACHE_METRICS_PATH, CONTENT_TYPE, METRICS_PATH, POOL_METRICS_PATH
from leanpool.pki import read_certificate
from leanpool.signals import PoolCapacity, capacity_from_headers

pytestmark = pytest.mark.skipif(HAPROXY is None, reason="HAProxy is not installed")

StartPool = Callable[..., Awaitable[LivePool]]
FACTS_FILE = "pool.metrics"
ALL_THREE = (METRICS_PATH, CACHE_METRICS_PATH, POOL_METRICS_PATH)
SCRAPE_SECONDS = 5.0
# What a site's dashboards and alert rules read from HAProxy's exporter.
EXPORTER_NAMES = {
    "haproxy_server_status",
    "haproxy_server_current_sessions",
    "haproxy_server_limit_sessions",
    "haproxy_server_current_queue",
    "haproxy_server_weight",
    "haproxy_server_http_responses_total",
    "haproxy_server_response_time_average_seconds",
    "haproxy_server_queue_time_average_seconds",
    "haproxy_server_check_up_down_total",
    "haproxy_server_check_failures_total",
    "haproxy_backend_current_queue",
    "haproxy_backend_active_servers",
    "haproxy_process_start_time_seconds",
}
CACHE_NAMES = {
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
}


@dataclass
class Scrape:
    """What one ``GET`` of a metrics path brought back."""

    status: int
    content_type: str
    text: str
    capacity: PoolCapacity | None  # the three numbers every answer of the door carries

    @property
    def samples(self) -> dict[str, float]:
        """Every sample by its name and labels, as written (``name{label="value"}``)."""
        lines = [line for line in self.text.splitlines() if line and not line.startswith("#")]
        return {
            sample: float(value) for sample, _, value in (line.rpartition(" ") for line in lines)
        }

    @property
    def names(self) -> set[str]:
        return {sample.split("{")[0] for sample in self.samples}


async def scrape(
    session: aiohttp.ClientSession, pool: LivePool, path: str, method: str = "GET"
) -> Scrape:
    """Ask the pool's public port for ``path`` as a scraper does: no key, and it never waits."""
    async with asyncio.timeout(SCRAPE_SECONDS):
        async with session.request(method, f"{pool.url}{path}") as reply:
            return Scrape(
                reply.status,
                reply.headers.get("Content-Type", ""),
                await reply.text(),
                capacity_from_headers(reply.headers),
            )


@pytest.fixture
async def start_pool(tmp_path: Path) -> AsyncIterator[StartPool]:
    """Create pools with a facts file on request (started unless told otherwise)."""
    pools: list[LivePool] = []

    async def start(*lean_servers: FakeLeanServer, started: bool = True) -> LivePool:
        pool = LivePool(tmp_path, list(lean_servers), pool_metrics_file=FACTS_FILE)
        pools.append(pool)
        if started:
            await pool.start()
        return pool

    yield start
    for pool in pools:
        await pool.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """A client that sends no key."""
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as client_session:
        yield client_session


def proof(tag: object) -> str:
    return f"-- proof {tag}"


def test_the_haproxy_these_tests_run_on_has_the_prometheus_exporter() -> None:
    """Not skipped without it: a pool on such an HAProxy does not start, and this says why."""
    assert haproxy_has_exporter(), MISSING_EXPORTER


async def test_a_scraper_without_the_key_reads_all_three_routes(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    listed = pool.servers
    servers = [listed[0], dataclasses.replace(listed[1], paused=True)]
    await pool.start(servers)
    for tag in ("one", "two", "three", "one", "two"):
        assert (await pool.check(session, proof(tag))).status == 200

    exporter = await scrape(session, pool, METRICS_PATH)
    cache = await scrape(session, pool, CACHE_METRICS_PATH)
    facts = await scrape(session, pool, POOL_METRICS_PATH)

    for answer in (exporter, cache, facts):
        assert (answer.status, answer.content_type) == (200, CONTENT_TYPE)
        # Like every answer that leaves the door, it says how big the pool is.
        assert answer.capacity == PoolCapacity(workers=WORKERS, queued=0, servers=1)
    # HAProxy's own exporter: every name the dashboards read, and the servers by their names.
    assert exporter.names >= EXPORTER_NAMES
    status = exporter.samples
    assert status['haproxy_server_status{proxy="checkers",server="lean-0",state="UP"}'] == 1
    assert status['haproxy_server_status{proxy="checkers",server="lean-1",state="MAINT"}'] == 1
    assert status['haproxy_server_status{proxy="checkers",server="lean-1",state="UP"}'] == 0
    assert status['haproxy_server_limit_sessions{proxy="checkers",server="lean-0"}'] == WORKERS
    assert status['haproxy_server_weight{proxy="checkers",server="lean-0"}'] == 256
    assert status['haproxy_backend_active_servers{proxy="checkers"}'] == 1
    answered = 'haproxy_server_http_responses_total{proxy="checkers",server="lean-0",code="2xx"}'
    assert status[answered] == 3  # the three checks the cache did not hold
    assert len(lean_a.requests) == 3
    # The cache's counters, as the cache counts them.
    assert cache.names == CACHE_NAMES
    assert cache.samples["leanpool_cache_hits_total"] == 2
    assert cache.samples["leanpool_cache_misses_total"] == 3
    assert cache.samples["leanpool_cache_stored_entries"] == 3
    # The pool's facts: the file beside the configuration, byte for byte.
    assert facts.text == (tmp_path / FACTS_FILE).read_text() == render_pool_metrics(servers)
    assert facts.samples == {
        'leanpool_server_listed_workers{server="lean-0"}': WORKERS,
        'leanpool_server_listed_workers{server="lean-1"}': WORKERS,
        'leanpool_server_paused{server="lean-0"}': 0,
        'leanpool_server_paused{server="lean-1"}': 1,
    }
    # No key was sent, and none is needed for these three paths alone.
    async with session.get(f"{pool.url}/status") as reply:
        assert reply.status == 401
    # None of the three was asked of a Lean server: they saw the checks and nothing else.
    assert lean_a.asked == ["POST /api/check"] * 3
    assert lean_b.asked == []


async def test_no_answer_holds_the_key_a_proof_or_an_address(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean)
    assert (await pool.check(session, proof("a secret lemma"))).status == 200

    for path in ALL_THREE:
        text = (await scrape(session, pool, path)).text
        assert text
        for hidden in (
            API_KEY,
            "Bearer",
            "a secret lemma",
            "-- proof",
            "127.0.0.1",
            "localhost",
            f":{lean.port}",
            f":{pool.cache_port}",
            f":{pool.checkers_port}",
            "nameserver",
        ):
            assert hidden not in text, f"{path} holds {hidden!r}"


async def test_the_scrapers_query_is_not_read_by_the_exporter(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """Whatever is asked for, the answer is the one the configuration fixed.

    Left to the caller, ``scope=resolvers`` would return the name servers' addresses, and
    ``no-maint`` would hide a paused server.
    """
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    listed = pool.servers
    await pool.start([listed[0], dataclasses.replace(listed[1], paused=True)])
    plain = await scrape(session, pool, METRICS_PATH)

    for query in ("?scope=resolvers", "?no-maint", "?scope=*&extra-counters", "?scope=", "?x"):
        asked = await scrape(session, pool, f"{METRICS_PATH}{query}")
        assert (asked.status, asked.content_type) == (200, CONTENT_TYPE)
        assert asked.names == plain.names, query
        assert set(asked.samples) == set(plain.samples), query
    assert not [name for name in plain.names if "resolver" in name]
    assert 'haproxy_server_status{proxy="checkers",server="lean-1",state="MAINT"}' in plain.samples
    # The other two paths are taken whatever follows the question mark.
    for path in (CACHE_METRICS_PATH, POOL_METRICS_PATH):
        assert (await scrape(session, pool, f"{path}?scope=resolvers")).status == 200


async def test_while_the_cache_is_down_its_metrics_are_refused_at_the_door_and_no_lean_server_asked(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean)
    assert (await scrape(session, pool, CACHE_METRICS_PATH)).status == 200

    await pool.stop_cache()
    # From the first moment: 503, whether the door already knows or meets the dead cache itself.
    assert (await scrape(session, pool, CACHE_METRICS_PATH)).status == 503

    async def cache_is_marked_down() -> bool:
        return (await pool.server_state("cache", "cache"))["status"].startswith("DOWN")

    await eventually(cache_is_marked_down)
    refused = await scrape(session, pool, CACHE_METRICS_PATH)
    assert (refused.status, refused.text) == (503, "the cache is down\n")
    assert refused.content_type == "text/plain"
    for _ in range(5):
        assert (await scrape(session, pool, CACHE_METRICS_PATH)).status == 503
    # The cache's backup leads to the Lean servers. For a check that is right, and it works:
    assert (await pool.check(session, proof("without the cache"))).status == 200
    # for the cache's counters it is never taken. The Lean server saw that check and no scrape.
    assert lean.asked == ["POST /api/check"]
    # The other two paths, and /health, do not need the cache.
    exporter = await scrape(session, pool, METRICS_PATH)
    assert exporter.status == 200
    for backend in ("cache", "cache_metrics"):
        down = f'haproxy_server_status{{proxy="{backend}",server="cache",state="DOWN"}}'
        assert exporter.samples[down] == 1
    assert (await scrape(session, pool, POOL_METRICS_PATH)).status == 200
    assert (await pool.health(session))[0] == 200

    await pool.start_cache()

    async def counters_are_back() -> bool:
        return (await scrape(session, pool, CACHE_METRICS_PATH)).status == 200

    await eventually(counters_are_back)
    # A cache that was made again counts from nothing; what it had stored is on its disk.
    back = await scrape(session, pool, CACHE_METRICS_PATH)
    assert back.samples["leanpool_cache_misses_total"] == 0
    assert lean.asked == ["POST /api/check"]


async def test_only_a_get_or_a_head_of_the_exact_path_is_answered_at_the_door(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """Everything else goes where it went before these routes: to a Lean server."""
    lean = await start_lean_server()
    pool = await start_pool(lean)

    for path in ALL_THREE:
        head = await scrape(session, pool, path, method="HEAD")
        assert (head.status, head.content_type, head.text) == (200, CONTENT_TYPE, "")
    assert lean.asked == []

    passed_on = [
        *((method, path) for method in ("POST", "PUT", "DELETE") for path in ALL_THREE),
        ("GET", "/metrics/"),
        ("GET", "/metrics/other"),
        ("GET", "/metrics/cache/"),
        ("GET", "/metrics/pool/x"),
        ("GET", "/metricsx"),
        ("GET", "/Metrics"),
    ]
    for method, path in passed_on:
        answer = await scrape(session, pool, path, method=method)
        assert answer.status == 404, (method, path)  # the Lean server's own answer
    assert lean.asked == [f"{method} {path}" for method, path in passed_on]
    await pool.logged("lean_pool checkers/lean-0 ", len(passed_on))


async def test_a_scrape_is_answered_while_every_worker_is_busy_and_checks_wait(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """A scrape takes no place among the checks, and reads the queue it did not join."""
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    pool = await start_pool(lean)
    waiting = 2
    checks = [
        asyncio.create_task(pool.check(session, proof(number)))
        for number in range(WORKERS + waiting)
    ]

    async def the_queue_has_formed() -> bool:
        cache = await scrape(session, pool, CACHE_METRICS_PATH)
        return (
            lean.in_flight == WORKERS
            and cache.samples["leanpool_cache_in_flight"] == WORKERS + waiting
        )

    await eventually(the_queue_has_formed)
    try:
        exporter = await scrape(session, pool, METRICS_PATH)
        facts = await scrape(session, pool, POOL_METRICS_PATH)
        assert (exporter.status, facts.status) == (200, 200)
        # Open slots 0, checks waiting 2: on the answer's headers as on any other answer.
        assert exporter.capacity == PoolCapacity(workers=WORKERS, queued=waiting, servers=1)
        samples = exporter.samples
        held = 'haproxy_server_current_sessions{proxy="checkers",server="lean-0"}'
        assert samples[held] == WORKERS
        queued = (
            samples['haproxy_backend_current_queue{proxy="checkers"}']
            + samples['haproxy_server_current_queue{proxy="checkers",server="lean-0"}']
        )
        assert queued == waiting
    finally:
        release.set()
    assert [reply.status for reply in await asyncio.gather(*checks)] == [200] * len(checks)


async def test_a_pause_and_a_resume_are_in_the_facts_once_the_proxy_is_reloaded(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """The file is written from the list with every render, and read again at each reload."""
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    listed = pool.servers
    paused = [listed[0], dataclasses.replace(listed[1], paused=True)]
    await pool.start(listed)
    key = 'leanpool_server_paused{server="lean-1"}'
    state = 'haproxy_server_status{proxy="checkers",server="lean-1",state="MAINT"}'
    assert (await scrape(session, pool, POOL_METRICS_PATH)).samples[key] == 0

    async def both_say(value: int) -> bool:
        facts = await scrape(session, pool, POOL_METRICS_PATH)
        exporter = await scrape(session, pool, METRICS_PATH)
        return facts.samples[key] == value and exporter.samples[state] == value

    for servers, value in ((paused, 1), (listed, 0)):
        # A list was edited: the facts and the configuration are rendered again, then reloaded.
        text = pool.write_pool_metrics(servers)
        pool.reload_haproxy(pool.render(servers))
        await eventually(functools.partial(both_say, value))
        assert (await scrape(session, pool, POOL_METRICS_PATH)).text == text


async def test_a_facts_file_as_large_as_the_renderer_allows_is_served_whole(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean, started=False)
    limit = maximum_pool_metrics_bytes(pool.settings)
    line = "# " + "x" * 61 + "\n"
    text = line * (limit // len(line))
    assert len(text) == limit == 131072
    await pool.start_cache()
    write_pool_metrics(tmp_path / FACTS_FILE, text, limit)
    await pool.start_haproxy(pool.render())

    facts = await scrape(session, pool, POOL_METRICS_PATH)

    assert (facts.status, facts.text) == (200, text)
    assert "WARNING" not in pool.log


async def test_a_tls_pool_answers_the_scraper_over_https_with_its_certificates_last_days(
    start_lean_server: StartLeanServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole path of an installed pool: ``render --pool-metrics --certificates``, then
    HAProxy on what it printed, then a scraper that trusts the pool's authority and has no key.
    """
    certificates = PoolCertificates.create(tmp_path / "tls")
    authority = certificates.authority
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    boxes = [
        BoxFront(
            directory=tmp_path,
            name=name,
            server_pem=authority.server(name),
            authority_file=certificates.authority_file,
            lean_upstream_port=lean.port,
            agent_upstream_port=free_port(),  # no agent: its checks read nothing, as ignored
        )
        for name, lean in (("box-a", lean_a), ("box-b", lean_b))
    ]
    pool = TlsLivePool(
        tmp_path, [], certificates=certificates, boxes=boxes, pool_metrics_file=FACTS_FILE
    )
    # A pool's public certificates, laid out as its host keeps them.
    public = tmp_path / "certs"
    (public / "servers").mkdir(parents=True)
    for source, name in (
        ("ca.crt", "ca.crt"),
        (f"{FRONT_DOOR_NAME}.crt", "front.crt"),
        (f"{PROXY_CLIENT_NAME}.crt", "proxy-client.crt"),
        ("box-a.crt", "servers/box-a.crt"),
        ("box-b.crt", "servers/box-b.crt"),
    ):
        shutil.copy(authority.directory / source, public / name)
    servers = pool.servers
    server_list = tmp_path / "servers"
    server_list.write_text("".join(f"{format_server_line(server)}\n" for server in servers))
    assert pool.settings.tls is not None
    try:
        for box in boxes:
            await box.start()
        await pool.start_cache()
        status = main(
            [
                "render",
                *("--servers", str(server_list)),
                *("--public-port", str(pool.public_port)),
                *("--checkers-port", str(pool.checkers_port)),
                *("--stats-port", str(pool.stats_port)),
                *("--cache-address", f"127.0.0.1:{pool.cache_port}"),
                *("--tls-front-door-pem", pool.settings.tls.front_door_pem),
                *("--tls-ca-file", pool.settings.tls.ca_file),
                *("--tls-client-pem", pool.settings.tls.client_pem),
                *("--agent-tunnel-port", str(pool.agent_tunnel_port)),
                *("--pool-metrics", str(tmp_path / FACTS_FILE)),
                *("--certificates", str(public)),
            ],
            {},
        )
        rendered = capsys.readouterr()
        assert (status, rendered.err) == (0, "")
        assert rendered.out == pool.render(servers)
        await pool.start_haproxy(rendered.out)

        async with certificates.session() as session:

            async def both_boxes_are_up() -> bool:
                return bool((await pool.health(session))[1]["servers"] == len(boxes))

            await eventually(both_boxes_are_up)
            assert (await pool.check(session, proof("over https"))).status == 200
            exporter = await scrape(session, pool, METRICS_PATH)
            cache = await scrape(session, pool, CACHE_METRICS_PATH)
            facts = await scrape(session, pool, POOL_METRICS_PATH)
    finally:
        await pool.close()

    for answer in (exporter, cache, facts):
        assert (answer.status, answer.content_type) == (200, CONTENT_TYPE)
        for hidden in (API_KEY, "127.0.0.1", "-- proof", "nameserver", str(tmp_path)):
            assert hidden not in answer.text
    assert exporter.names >= EXPORTER_NAMES
    for name in ("box-a", "box-b"):
        up = f'haproxy_server_status{{proxy="checkers",server="{name}",state="UP"}}'
        assert exporter.samples[up] == 1
        # With TLS each server's usage agent is asked through a listener of the proxy's own.
        tunnel = f'haproxy_server_status{{proxy="agent_tunnel_{name}",server="agent",state="UP"}}'
        assert tunnel in exporter.samples
    assert cache.samples["leanpool_cache_misses_total"] == 1
    # Each certificate's last moment, as the certificate itself has it.
    last = {
        name: int(read_certificate(public / name).not_valid_after_utc.timestamp())
        for name in (
            "ca.crt",
            "front.crt",
            "proxy-client.crt",
            "servers/box-a.crt",
            "servers/box-b.crt",
        )
    }
    assert read_certificate_dates(public).not_after == last
    assert facts.text == render_pool_metrics(servers, last)
    for name, seconds in last.items():
        sample = f'leanpool_certificate_not_after_seconds{{certificate="{name}"}}'
        assert facts.samples[sample] == seconds
