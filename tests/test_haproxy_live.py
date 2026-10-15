"""The generated configuration on a running HAProxy, in front of the real cache and fake Lean
servers. Everything listens on loopback; nothing needs Docker or the network.

These tests are skipped where ``haproxy`` is not installed. They are the only tests that show
what HAProxy *does* with the configuration; ``test_haproxy_config.py`` shows what it says.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from fake_dns import FakeDns
from fake_lean_server import FakeLeanServer, SnippetReply, always, failing_with, lean_answer
from fake_lean_server import lean_timeout as lean_timeout_reply
from live_pool import HAPROXY, WORKERS, CheckReply, LivePool, eventually, free_port
from support import API_KEY, AUTHORIZED, PIN, StartLeanServer

from leanpool.agent import AgentSettings, UsageAgent, UsageSampler
from leanpool.haproxy import LeanServer
from leanpool.haproxy.config import AGENT_INTERVAL_SECONDS, render_haproxy_config
from leanpool.signals import PoolCapacity

pytestmark = pytest.mark.skipif(HAPROXY is None, reason="HAProxy is not installed")

StartPool = Callable[..., Awaitable[LivePool]]
GIBIBYTE = 1024**3
FULL_WEIGHT = 256


@pytest.fixture
async def start_pool(tmp_path: Path) -> AsyncIterator[StartPool]:
    """Create pools on request (started unless told otherwise); all are stopped afterwards."""
    pools: list[LivePool] = []

    async def start(*lean_servers: FakeLeanServer, started: bool = True) -> LivePool:
        pool = LivePool(tmp_path, list(lean_servers))
        pools.append(pool)
        if started:
            await pool.start()
        return pool

    yield start
    for pool in pools:
        await pool.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as client_session:
        yield client_session


def proof(tag: object, size: int = 0) -> str:
    return f"-- proof {tag} " + "x" * size


async def test_a_check_goes_through_the_cache_and_a_repeat_never_reaches_a_lean_server(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b)

    first = await pool.check(session, proof("one"))
    repeat = await pool.check(session, proof("one"))

    assert "cached" not in first.result
    assert repeat.result["cached"] is True
    (request,) = lean_a.requests + lean_b.requests
    assert request.headers["X-Lean-Pool-Hop"] == "checkers"
    assert request.headers["Authorization"] == AUTHORIZED["Authorization"]
    async with session.get(f"{pool.url}/status", headers=AUTHORIZED) as reply:
        assert (await reply.json())["hits"] == 1


async def test_health_is_answered_by_haproxy_from_the_lean_servers_that_are_up(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean)

    async def health() -> tuple[int, Any]:
        async with session.get(f"{pool.url}/health") as reply:
            return reply.status, await reply.json()

    up = {"status": "ok", "workers": WORKERS, "queued": 0, "servers": 1}
    assert await health() == (200, up)
    await pool.stop_cache()
    assert await health() == (200, up)  # it does not depend on the cache

    await lean.close()

    async def reports_no_server() -> bool:
        down = {
            "status": "no Lean server is taking checks",
            "workers": 0,
            "queued": 0,
            "servers": 0,
        }
        return await health() == (503, down)

    await eventually(reports_no_server)


@pytest.mark.parametrize("failure_status", [500, 502, 503, 504])
@pytest.mark.parametrize("proof_bytes", [100, 200_000], ids=["short-proof", "200kB-proof"])
async def test_a_failed_check_is_replayed_on_the_other_server(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    failure_status: int,
    proof_bytes: int,
) -> None:
    broken = await start_lean_server(failing_with(failure_status))
    healthy = await start_lean_server()
    pool = await start_pool(broken, healthy)

    replies = [await pool.check(session, proof(number, proof_bytes)) for number in range(4)]

    assert [reply.status for reply in replies] == [200, 200, 200, 200]
    assert len(healthy.requests) == 4
    assert len(broken.requests) >= 1  # the failure did happen; no caller saw it


async def test_a_check_longer_than_the_buffer_is_forwarded_but_not_replayed(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    broken = await start_lean_server(failing_with(503))
    healthy = await start_lean_server()
    pool = await start_pool(broken, healthy)
    longer_than_the_buffer = pool.settings.maximum_request_bytes + 40_000

    replies = [
        await pool.check(session, proof(number, longer_than_the_buffer)) for number in range(4)
    ]

    statuses = [reply.status for reply in replies]
    assert set(statuses) == {200, 503}
    assert statuses.count(503) == len(broken.requests)
    assert statuses.count(200) == len(healthy.requests)


async def test_a_proof_that_crashes_every_worker_is_stopped_after_three(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean_a = await start_lean_server(failing_with(500, "the worker died"))
    lean_b = await start_lean_server(failing_with(500, "the worker died"))
    pool = await start_pool(lean_a, lean_b)

    reply = await pool.check(session, proof("crash"))

    assert (reply.status, reply.body) == (500, "the worker died")
    assert len(lean_a.requests) + len(lean_b.requests) == 3
    assert min(len(lean_a.requests), len(lean_b.requests)) == 1  # the retries changed server


async def test_a_lean_timeout_is_passed_through_and_not_retried(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean_a = await start_lean_server(always(lean_timeout_reply()))
    lean_b = await start_lean_server(always(lean_timeout_reply()))
    pool = await start_pool(lean_a, lean_b)

    reply = await pool.check(session, proof("slow"))

    assert "timed out" in reply.result["error"]
    assert len(lean_a.requests) + len(lean_b.requests) == 1


async def test_a_lean_server_that_is_gone_costs_no_check(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    gone, healthy = await start_lean_server(), await start_lean_server()
    pool = await start_pool(gone, healthy)
    await gone.close()

    replies = [await pool.check(session, proof(number)) for number in range(4)]

    assert [reply.status for reply in replies] == [200, 200, 200, 200]
    assert len(healthy.requests) == 4


async def test_stopping_the_cache_costs_no_check(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b)
    assert (await pool.check(session, proof("before"))).status == 200

    await pool.stop_cache()
    one_after_another = [await pool.check(session, proof(number)) for number in range(3)]
    all_at_once = await asyncio.gather(
        *(pool.check(session, proof(f"burst {number}")) for number in range(30))
    )

    assert {reply.status for reply in one_after_another + list(all_at_once)} == {200}
    assert one_after_another[0].seconds < 5
    assert (await pool.server_state("cache", "cache"))["status"] == "DOWN"
    async with session.get(f"{pool.url}/status", headers=AUTHORIZED) as reply:
        assert (reply.status, await reply.json()) == (503, {"status": "the cache is down"})
    # The first one met the dead cache and was sent again through the backup; the rest went
    # straight to the Lean servers.
    await pool.logged("cache/checkers_door", times=1)
    await pool.logged(" lean_pool checkers/", times=32)


async def test_the_cache_is_used_again_when_it_returns(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean)
    await pool.stop_cache()
    assert (await pool.check(session, proof("while down"))).status == 200

    await pool.start_cache()

    async def cache_is_up() -> bool:
        return (await pool.server_state("cache", "cache"))["status"] == "UP"

    await eventually(cache_is_up)
    assert "cached" not in (await pool.check(session, proof("after"))).result
    assert (await pool.check(session, proof("after"))).result["cached"] is True


@contextlib.contextmanager
def cache_process(pool: LivePool) -> Iterator[subprocess.Popen[bytes]]:
    """Run the cache as its own process, so that it can be killed outright."""
    settings = pool.cache_settings
    environment = {
        "PATH": os.environ["PATH"],
        "LEANPOOL_CACHE_PIN": PIN,
        "LEANPOOL_CACHE_API_KEY": API_KEY,
        "LEANPOOL_CACHE_DATABASE": str(settings.database_path),
        "LEANPOOL_CACHE_PORT": str(settings.port),
        "LEANPOOL_CACHE_UPSTREAM_URL": settings.upstream_url,
    }
    process = subprocess.Popen([sys.executable, "-m", "leanpool.cache"], env=environment)
    try:
        yield process
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize("proof_bytes", [100, 200_000], ids=["short-proof", "200kB-proof"])
async def test_checks_inside_the_cache_when_it_is_killed_are_replayed_around_it(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    proof_bytes: int,
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    pool = await start_pool(lean, started=False)
    with cache_process(pool) as cache:

        async def cache_is_serving() -> bool:
            with contextlib.suppress(aiohttp.ClientError):
                async with session.get(f"http://127.0.0.1:{pool.cache_port}/health") as reply:
                    return reply.status == 200
            return False

        await eventually(cache_is_serving)
        await pool.start_haproxy(pool.render())
        in_flight = [
            asyncio.create_task(pool.check(session, proof(number, proof_bytes)))
            for number in range(3)
        ]

        async def all_reached_the_lean_server() -> bool:
            return lean.in_flight == 3

        await eventually(all_reached_the_lean_server)
        cache.kill()
        cache.wait()
        release.set()
        replies = await asyncio.gather(*in_flight)

    assert [reply.status for reply in replies] == [200, 200, 200]
    await pool.logged("cache/checkers_door", times=3)


async def test_a_server_whose_name_does_not_resolve_does_not_stop_the_proxy(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean, started=False)
    await pool.start_cache()
    unresolvable = LeanServer("ghost", "ghost.invalid", 8000, WORKERS, free_port())
    await pool.start_haproxy(pool.render([*pool.servers, unresolvable]))

    replies = [await pool.check(session, proof(number)) for number in range(4)]

    assert [reply.status for reply in replies] == [200, 200, 200, 200]
    assert len(lean.requests) == 4
    assert (await pool.server_state("checkers", "ghost"))["status"] == "MAINT (resolution)"
    # The server given by address is untouched by the resolver options it inherits.
    assert (await pool.server_state("checkers", "lean-0"))["status"].startswith("UP")


def with_name_server(config: str, port: int) -> str:
    """Point the generated ``resolvers`` section at a test DNS server.

    ``parse-resolv-conf`` always reads the machine's ``/etc/resolv.conf``, which a test cannot
    replace. That one line is swapped; every other line is the generated configuration.
    """
    generated = "    parse-resolv-conf\n"
    assert config.count(generated) == 1
    return config.replace(generated, f"    nameserver test 127.0.0.1:{port}\n")


async def test_a_server_whose_address_changes_is_followed(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    port = free_port()
    await start_lean_server(always(lean_answer(("info", "first address"))), port=port)
    try:
        await start_lean_server(
            always(lean_answer(("info", "second address"))), host="127.0.0.2", port=port
        )
    except OSError:
        pytest.skip("this machine has no second loopback address to move the server to")
    dns = FakeDns(addresses={"moving.test": "127.0.0.1"})
    dns_port = await dns.start()
    pool = await start_pool(started=False)
    await pool.start_cache()
    moving = LeanServer("moving", "moving.test", port, WORKERS, free_port())

    async def answered_from(number: int) -> str:
        reply = await pool.check(session, proof(number), no_cache=True)
        if reply.status != 200:
            return f"HTTP {reply.status}"
        message: str = reply.result["response"]["messages"][0]["data"]
        return message

    try:
        # The system's resolver does not know the name, so the server starts without an address.
        await pool.start_haproxy(with_name_server(pool.render([moving]), dns_port))

        async def resolved() -> bool:
            return (await pool.server_state("checkers", "moving"))["status"].startswith("UP")

        await eventually(resolved)
        assert await answered_from(0) == "first address"

        dns.addresses["moving.test"] = "127.0.0.2"
        attempts = iter(range(1, 10_000))

        async def follows() -> bool:
            return await answered_from(next(attempts)) == "second address"

        await eventually(follows)
        assert "moving.test" in dns.queried_names
    finally:
        dns.close()


def write_box(directory: Path, busy_ticks: int, idle_ticks: int, available_bytes: int) -> None:
    """Write the ``/proc`` files of a one-core box."""
    (directory / "stat").write_text(
        f"cpu  {busy_ticks} 0 0 {idle_ticks} 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0 0\n"
    )
    (directory / "meminfo").write_text(f"MemAvailable: {available_bytes // 1024} kB\n")


async def test_haproxy_applies_what_the_usage_agent_reports(
    start_lean_server: StartLeanServer, start_pool: StartPool, tmp_path: Path
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    agent_port = free_port()
    write_box(tmp_path, busy_ticks=0, idle_ticks=0, available_bytes=64 * GIBIBYTE)
    agent = UsageAgent(
        dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=agent_port,
            stat_path=tmp_path / "stat",
            meminfo_path=tmp_path / "meminfo",
        )
    )
    await agent.start()
    servers = [
        LeanServer("with-agent", "127.0.0.1", lean_a.port, WORKERS, agent_port),
        LeanServer("without-agent", "127.0.0.1", lean_b.port, WORKERS, free_port()),
    ]

    async def reaches(status: str, weight: int) -> None:
        async def reached() -> bool:
            state = await pool.server_state("checkers", "with-agent")
            return (state["status"], state["weight"]) == (status, str(weight))

        await eventually(reached)

    try:
        await pool.start_cache()
        await pool.start_haproxy(pool.render(servers))

        write_box(tmp_path, busy_ticks=500, idle_ticks=500, available_bytes=64 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == "ready up 50%\n"
        await reaches("UP", FULL_WEIGHT // 2)

        write_box(tmp_path, busy_ticks=1490, idle_ticks=510, available_bytes=64 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == "ready up 1%\n"
        await reaches("UP", FULL_WEIGHT // 100)  # still in rotation: the weight is not zero

        write_box(tmp_path, busy_ticks=1490, idle_ticks=1510, available_bytes=1 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == "drain\n"
        await reaches("DRAIN (agent)", FULL_WEIGHT // 100)

        write_box(tmp_path, busy_ticks=1490, idle_ticks=2510, available_bytes=64 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == "ready up 100%\n"
        await reaches("UP", FULL_WEIGHT)

        # A server whose agent cannot be reached keeps its configured weight.
        unreached = await pool.server_state("checkers", "without-agent")
        assert (unreached["status"], unreached["weight"]) == ("UP", str(FULL_WEIGHT))
    finally:
        await agent.close()


INSTALLER_VERSION = "0123456789abcdef0123456789abcdef"


async def test_haproxy_keeps_the_installers_version_the_usage_agent_states(
    start_lean_server: StartLeanServer, start_pool: StartPool, tmp_path: Path
) -> None:
    """An answer that ends ``# version V`` sets the weight as before, and after an ``up`` HAProxy
    keeps the text as the server's agent description (``last_agt``), where the pool's status
    reads it. After a ``drain`` it keeps none."""
    lean = await start_lean_server()
    pool = await start_pool(lean, started=False)
    agent_port = free_port()
    write_box(tmp_path, busy_ticks=0, idle_ticks=0, available_bytes=64 * GIBIBYTE)
    agent = UsageAgent(
        dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=agent_port,
            stat_path=tmp_path / "stat",
            meminfo_path=tmp_path / "meminfo",
            installer_version=INSTALLER_VERSION,
        )
    )
    await agent.start()
    servers = [LeanServer("with-agent", "127.0.0.1", lean.port, WORKERS, agent_port)]

    async def reaches(status: str, weight: int, description: str) -> None:
        async def reached() -> bool:
            state = await pool.server_state("checkers", "with-agent")
            return (state["status"], state["weight"], state["last_agt"]) == (
                status,
                str(weight),
                description,
            )

        await eventually(reached)

    try:
        await pool.start_cache()
        await pool.start_haproxy(pool.render(servers))

        write_box(tmp_path, busy_ticks=500, idle_ticks=500, available_bytes=64 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == f"ready up 50% # version {INSTALLER_VERSION}\n"
        await reaches("UP", FULL_WEIGHT // 2, f"via agent : up (version {INSTALLER_VERSION})")

        write_box(tmp_path, busy_ticks=500, idle_ticks=1500, available_bytes=1 * GIBIBYTE)
        agent.sampler.sample()
        assert agent.sampler.reply() == f"drain # version {INSTALLER_VERSION}\n"
        await reaches("DRAIN (agent)", FULL_WEIGHT // 2, "")
    finally:
        await agent.close()


async def test_health_carries_the_installers_version_when_the_settings_name_one(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean, started=False)
    settings = dataclasses.replace(pool.settings, installer_version=INSTALLER_VERSION)
    await pool.start_cache()
    await pool.start_haproxy(render_haproxy_config(pool.servers, settings))
    async with session.get(f"{pool.url}/health") as reply:
        assert reply.status == 200
        assert await reply.json() == {
            "status": "ok",
            "workers": WORKERS,
            "queued": 0,
            "servers": 1,
            "version": INSTALLER_VERSION,
        }


async def test_a_drained_server_keeps_its_workers_in_the_count_and_takes_no_checks(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    """What the pool says about itself while usage agents that state no slots drain its servers.

    ``workers`` keeps a server drained by a bare ``drain``: it is up, and its weight is not 0.
    ``servers`` and the status of ``/health`` count the servers that take checks. With every
    server drained the pool is up and takes nothing: ``/health`` and a check are both
    answered 503.
    """
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    boxes = [tmp_path / "first", tmp_path / "second"]
    agent_ports = [free_port(), free_port()]
    agents = []
    for box, agent_port in zip(boxes, agent_ports, strict=True):
        box.mkdir()
        write_box(box, busy_ticks=0, idle_ticks=1000, available_bytes=64 * GIBIBYTE)
        settings = dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=agent_port,
            stat_path=box / "stat",
            meminfo_path=box / "meminfo",
        )
        agents.append(UsageAgent(settings))
    servers = [
        LeanServer("first", "127.0.0.1", lean_a.port, WORKERS, agent_ports[0]),
        LeanServer("second", "127.0.0.1", lean_b.port, WORKERS, agent_ports[1]),
    ]

    async def set_memory(number: int, available_bytes: int, status: str) -> None:
        write_box(boxes[number], busy_ticks=10, idle_ticks=2000, available_bytes=available_bytes)
        agents[number].sampler.sample()

        async def reached() -> bool:
            name = servers[number].name
            return (await pool.server_state("checkers", name))["status"] == status

        await eventually(reached)

    async def health() -> tuple[int, Any]:
        status, body, _capacity = await pool.health(session)
        return status, body

    def size(status: str, servers_taking_checks: int) -> dict[str, Any]:
        return {
            "status": status,
            "workers": 2 * WORKERS,
            "queued": 0,
            "servers": servers_taking_checks,
        }

    try:
        for agent in agents:
            await agent.start()
        await pool.start_cache()
        await pool.start_haproxy(pool.render(servers))
        assert await health() == (200, size("ok", 2))

        await set_memory(0, 1 * GIBIBYTE, "DRAIN (agent)")
        assert await health() == (200, size("ok", 1))
        assert (await pool.check(session, "theorem one_drained : True := trivial")).status == 200
        assert len(lean_a.requests) == 0

        await set_memory(1, 1 * GIBIBYTE, "DRAIN (agent)")
        assert await health() == (503, size("no Lean server is taking checks", 0))
        assert (await pool.check(session, "theorem all_drained : True := trivial")).status == 503

        await set_memory(0, 64 * GIBIBYTE, "UP")
        assert await health() == (200, size("ok", 1))
        assert (await pool.check(session, "theorem recovered : True := trivial")).status == 200
    finally:
        for agent in agents:
            await agent.close()


# --- priority, and the pool's size on every answer --------------------------------------------


async def test_every_answer_says_how_many_workers_are_on_servers_that_are_up(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b)
    both = PoolCapacity(workers=2 * WORKERS, queued=0, servers=2)
    one = PoolCapacity(workers=WORKERS, queued=0, servers=1)

    body = {"status": "ok", "workers": 2 * WORKERS, "queued": 0, "servers": 2}
    assert await pool.health(session) == (200, body, both)
    first = await pool.check(session, proof("size"))
    hit = await pool.check(session, proof("size"))
    assert first.capacity == both
    assert hit.result["cached"] is True
    assert hit.capacity == both  # an answer from the cache too
    async with session.get(f"{pool.url}/status", headers=AUTHORIZED) as reply:
        assert reply.headers["X-Lean-Pool-Workers"] == str(2 * WORKERS)

    port = lean_b.port
    await lean_b.close()

    async def one_server_is_counted() -> bool:
        return (await pool.health(session))[2] == one

    await eventually(one_server_is_counted)
    assert (await pool.check(session, proof("one server"))).capacity == one

    await start_lean_server(port=port)

    async def both_are_counted_again() -> bool:
        return (await pool.health(session))[2] == both

    await eventually(both_are_counted_again)


def holding(gates: dict[str, asyncio.Event], arrived: list[str]) -> Any:
    """A Lean server that records each check it is given and holds the gated ones until released."""

    async def behaviour(snippet: dict[str, Any]) -> SnippetReply:
        arrived.append(snippet["code"])
        gate = gates.get(snippet["code"])
        if gate is not None:
            await gate.wait()
        return lean_answer()

    return behaviour


async def held_one_at_a_time(
    pool: LivePool, session: aiohttp.ClientSession, codes: Iterable[str], arrived: Callable[[], int]
) -> list[asyncio.Task[CheckReply]]:
    """Send checks that a Lean server will hold, each once the one before has reached a worker.

    Checks sent at once to SEVERAL servers are placed by several of HAProxy's threads at once, and
    on 3.0.29 that is not exact: of 450 lots of eight sent to two servers of four workers, seven
    ended with one server holding five, or with the eighth check waiting in a full server's own
    queue while the other server had a worker free. Either mends itself when a check on that
    server ends, which a held check never does; so a test that counts who holds what sends its
    held checks one at a time. ``arrived`` counts the checks that have reached a Lean server.
    """
    tasks = []
    for code in codes:
        before = arrived()
        tasks.append(asyncio.create_task(pool.check(session, code)))

        async def reached(before: int = before) -> bool:
            return arrived() > before

        await eventually(reached)
    return tasks


@pytest.mark.parametrize("through_cache", [True, False], ids=["through-the-cache", "cache-stopped"])
async def test_a_waiting_background_check_is_overtaken_by_normal_checks_that_arrive_after_it(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    through_cache: bool,
) -> None:
    """One server of four workers, every worker held. Then, in this order: a background check, a
    check with a mistyped priority (a normal check), a normal check. One worker is freed: the
    three are served one after the other on it, the background one last."""
    gates = {proof(f"hold {number}"): asyncio.Event() for number in range(WORKERS)}
    arrived: list[str] = []
    lean = await start_lean_server(holding(gates, arrived))
    pool = await start_pool(lean)
    if not through_cache:
        await pool.stop_cache()

        async def cache_is_down() -> bool:
            return (await pool.server_state("cache", "cache"))["status"] == "DOWN"

        await pool.check(session, proof("meets the dead cache"))
        await eventually(cache_is_down)
        arrived.clear()

    async def waiting(count: int) -> None:
        async def reached() -> bool:
            capacity = (await pool.health(session))[2]
            return capacity is not None and capacity.queued == count

        await eventually(reached)

    held = [asyncio.create_task(pool.check(session, code)) for code in gates]

    async def every_worker_is_busy() -> bool:
        return len(arrived) == WORKERS

    await eventually(every_worker_is_busy)

    background = asyncio.create_task(
        pool.check(session, proof("background"), priority="background")
    )
    await waiting(1)
    mistyped = asyncio.create_task(pool.check(session, proof("mistyped"), priority="bakground"))
    await waiting(2)
    normal = asyncio.create_task(pool.check(session, proof("normal")))
    await waiting(3)
    assert len(arrived) == WORKERS  # none of the three has a worker yet
    assert (await pool.health(session))[2] == PoolCapacity(workers=WORKERS, queued=3, servers=1)

    gates[proof("hold 0")].set()
    replies = await asyncio.gather(background, mistyped, normal)

    assert [reply.status for reply in replies] == [200, 200, 200]
    assert arrived[WORKERS:] == [proof("mistyped"), proof("normal"), proof("background")]
    for gate in gates.values():
        gate.set()
    assert {reply.status for reply in await asyncio.gather(*held)} == {200}
    assert lean.peak_in_flight == WORKERS


async def test_background_checks_are_served_when_nothing_else_is_waiting(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    lean = await start_lean_server()
    pool = await start_pool(lean)
    replies = await asyncio.gather(
        *(
            pool.check(session, proof(f"bulk {number}"), priority="Background")
            for number in range(12)
        )
    )
    assert {reply.status for reply in replies} == {200}
    # The Lean server is told too, in the one spelling, on the second hop.
    assert {request.headers.get("X-Lean-Priority") for request in lean.requests} == {"background"}


# --- a paused server is listed and sent nothing ---------------------------------------------------


def size_said(reply: CheckReply) -> tuple[int, int]:
    """The workers and the servers an answer states. The checks queued depend on the moment."""
    assert reply.capacity is not None
    return reply.capacity.workers, reply.capacity.servers


async def test_a_paused_server_stays_out_though_its_agent_says_ready(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    """The usage agent answers ``ready`` with every normal reply, and HAProxy takes ``ready`` from
    an agent as the end of a maintenance: a paused server is therefore declared with no agent
    check. Here its agent is up and says ``ready up 100%`` to whoever asks, all the while."""
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_pool(lean_a, lean_b, started=False)
    agent_port = free_port()
    write_box(tmp_path, busy_ticks=0, idle_ticks=0, available_bytes=64 * GIBIBYTE)
    agent = UsageAgent(
        dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=agent_port,
            stat_path=tmp_path / "stat",
            meminfo_path=tmp_path / "meminfo",
        )
    )
    await agent.start()
    write_box(tmp_path, busy_ticks=0, idle_ticks=1000, available_bytes=64 * GIBIBYTE)
    agent.sampler.sample()
    assert agent.sampler.reply() == "ready up 100%\n"
    servers = [
        LeanServer("taking-checks", "127.0.0.1", lean_a.port, WORKERS, free_port()),
        LeanServer("paused", "127.0.0.1", lean_b.port, WORKERS, agent_port, paused=True),
    ]
    one = PoolCapacity(workers=WORKERS, queued=0, servers=1)
    try:
        await pool.start_cache()
        await pool.start_haproxy(pool.render(servers))

        async def the_other_server_is_up() -> bool:
            return (await pool.server_state("checkers", "taking-checks"))["status"] == "UP"

        await eventually(the_other_server_is_up)
        # Longer than the agent's interval and the health check's: a `ready` would have landed.
        await asyncio.sleep(AGENT_INTERVAL_SECONDS + 2)

        assert (await pool.server_state("checkers", "paused"))["status"] == "MAINT"
        body = {"status": "ok", "workers": WORKERS, "queued": 0, "servers": 1}
        assert await pool.health(session) == (200, body, one)
        replies = await asyncio.gather(
            *(pool.check(session, proof(f"while paused {number}")) for number in range(3 * WORKERS))
        )
        assert {reply.status for reply in replies} == {200}
        assert {size_said(reply) for reply in replies} == {(WORKERS, 1)}
        assert len(lean_a.requests) == 3 * WORKERS
        assert lean_b.requests == []  # no check
        assert lean_b.health_checks == 0  # and no health check
    finally:
        await agent.close()


async def test_a_pause_by_reload_loses_no_check_and_a_resume_brings_the_server_back(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """A pause reaches a running proxy as a new configuration and a reload: HAProxy's old
    process finishes the checks it holds, those on the paused server included, and the new one
    sends that server nothing."""
    gates = {proof(f"held {number}"): asyncio.Event() for number in range(2 * WORKERS)}
    arrived_a: list[str] = []
    arrived_b: list[str] = []
    lean_a = await start_lean_server(holding(gates, arrived_a))
    lean_b = await start_lean_server(holding(gates, arrived_b))
    pool = await start_pool(lean_a, lean_b, started=False)
    listed = pool.servers
    paused = [listed[0], dataclasses.replace(listed[1], paused=True)]
    both = PoolCapacity(workers=2 * WORKERS, queued=0, servers=2)
    one = PoolCapacity(workers=WORKERS, queued=0, servers=1)

    async def the_pool_says(capacity: PoolCapacity) -> None:
        async def said() -> bool:
            with contextlib.suppress(aiohttp.ClientError):
                return (await pool.health(session))[2] == capacity
            return False  # the old process closed the connection it was asked on

        await eventually(said)

    await pool.start_cache()
    await pool.start_haproxy(pool.render(listed))
    await the_pool_says(both)

    def arrived() -> int:
        return len(arrived_a) + len(arrived_b)

    held = await held_one_at_a_time(pool, session, list(gates), arrived)
    assert (len(arrived_a), len(arrived_b)) == (WORKERS, WORKERS)  # every worker holds a check

    pool.reload_haproxy(pool.render(paused))
    await the_pool_says(one)
    assert (await pool.server_state("checkers", "lean-1"))["status"] == "MAINT"
    after = await asyncio.gather(
        *(pool.check(session, proof(f"after the pause {number}")) for number in range(3 * WORKERS))
    )
    assert {reply.status for reply in after} == {200}
    assert {size_said(reply) for reply in after} == {(WORKERS, 1)}
    assert len(arrived_a) == WORKERS + 3 * WORKERS
    assert len(arrived_b) == WORKERS  # nothing new reached the paused server

    for gate in gates.values():
        gate.set()
    finished = await asyncio.gather(*held)
    assert [reply.status for reply in finished] == [200] * (2 * WORKERS)  # no check was lost

    pool.reload_haproxy(pool.render(listed))
    await the_pool_says(both)
    again = {proof(f"held again {number}"): asyncio.Event() for number in range(2 * WORKERS)}
    gates.update(again)
    resumed = await held_one_at_a_time(pool, session, list(again), arrived)
    assert len(arrived_b) == 2 * WORKERS  # the resumed server has its share
    for gate in again.values():
        gate.set()
    assert {reply.status for reply in await asyncio.gather(*resumed)} == {200}


# --- open slots, and a health check that waits for a slow answer ----------------------------------


class SharedBoxFiles:
    """The ``/proc/stat`` and the Lean server's ``cpu.stat`` of a 16-CPU box that also runs other
    work, written five seconds of that work at a time. The Lean server itself is idle throughout:
    what its own use does to the slots is the agent's own tests' matter."""

    CORES = 16
    TICKS_PER_SECOND = 100

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._busy = self._idle = 0
        (directory / "meminfo").write_text(f"MemAvailable: {64 * GIBIBYTE // 1024} kB\n")
        (directory / "cpu.stat").write_text("usage_usec 0\n")
        self._write()

    def settings(self, port: int) -> AgentSettings:
        return dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=port,
            stat_path=self.directory / "stat",
            meminfo_path=self.directory / "meminfo",
            workers=WORKERS,
            lean_cpu_stat_path=self.directory / "cpu.stat",
            cpu_topology_path=self.directory / "no-topology",  # one thread a core
            clock_ticks_per_second=self.TICKS_PER_SECOND,
        )

    def run(self, agent: UsageAgent, others: float, seconds: float = 5.0) -> str:
        """Let ``seconds`` pass with other work on ``others`` CPUs; return the agent's new reply."""
        busy = round(others * self.TICKS_PER_SECOND * seconds)
        self._busy += busy
        self._idle += round(self.CORES * self.TICKS_PER_SECOND * seconds) - busy
        self._write()
        agent.sampler.sample()
        return agent.sampler.reply()

    def _write(self) -> None:
        cores = "".join(f"cpu{core} 0 0 0 0 0 0 0 0 0 0\n" for core in range(self.CORES))
        (self.directory / "stat").write_text(
            f"cpu  {self._busy} 0 0 {self._idle} 0 0 0 0 0 0\n{cores}"
        )


async def test_a_server_holds_no_more_checks_than_the_open_slots_its_agent_states(
    start_lean_server: StartLeanServer,
    start_pool: StartPool,
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    """Two servers of four workers; one is on a box that also runs other work, and its usage
    agent says how many checks fit beside that work. With a queue standing all the while (the
    case in which a weight changes nothing), HAProxy holds that server to the slots stated:
    two, then four, then none. The pool's three numbers follow."""
    gates = {proof(f"hold {number}"): asyncio.Event() for number in range(2 * WORKERS)}
    on_shared: list[str] = []
    on_free: list[str] = []
    lean_shared = await start_lean_server(holding(gates, on_shared))
    lean_free = await start_lean_server(holding(gates, on_free))
    pool = await start_pool(lean_shared, lean_free, started=False)
    box = SharedBoxFiles(tmp_path)
    agent_port = free_port()
    agent = UsageAgent(box.settings(agent_port))
    await agent.start()
    servers = [
        LeanServer("shared", "127.0.0.1", lean_shared.port, WORKERS, agent_port),
        LeanServer("free", "127.0.0.1", lean_free.port, WORKERS, free_port()),
    ]

    async def shared_is(status: str, limit: int) -> None:
        async def reached() -> bool:
            state = await pool.server_state("checkers", "shared")
            return (state["status"], state["slim"]) == (status, str(limit))

        await eventually(reached)

    async def pool_says(workers: int, servers_up: int, queued: int = 0) -> None:
        said = PoolCapacity(workers=workers, queued=queued, servers=servers_up)

        async def reached() -> bool:
            return (await pool.health(session))[2] == said

        await eventually(reached)

    try:
        await pool.start_cache()
        await pool.start_haproxy(pool.render(servers))

        # Other work on 13 of the box's 16 CPUs: with one CPU kept free, two checks fit.
        assert box.run(agent, others=13.0) == "ready up 19% maxconn:2\n"
        await shared_is("UP", 2)
        await pool_says(workers=2 + WORKERS, servers_up=2)
        first = await pool.check(session, proof("size"))
        hit = await pool.check(session, proof("size"))
        assert hit.result["cached"] is True
        assert size_said(first) == size_said(hit) == (2 + WORKERS, 2)
        on_shared.clear()
        on_free.clear()

        # Eight checks that are held: six have a worker, two wait, though the shared server has
        # two workers of its own doing nothing.
        codes = list(gates)
        held = await held_one_at_a_time(
            pool, session, codes[: 2 + WORKERS], lambda: len(on_shared) + len(on_free)
        )
        held += [asyncio.create_task(pool.check(session, code)) for code in codes[2 + WORKERS :]]
        await pool_says(workers=2 + WORKERS, servers_up=2, queued=2)
        assert (len(on_shared), len(on_free)) == (2, WORKERS)
        await asyncio.sleep(1.0)
        assert (len(on_shared), lean_shared.peak_in_flight) == (2, 2)

        # The other work goes away. The slots come back one reading at a time, and the two
        # checks that waited are taken as they do.
        assert box.run(agent, others=0.0) == "ready up 100% maxconn:3\n"
        assert box.run(agent, others=0.0) == "ready up 100% maxconn:4\n"
        await shared_is("UP", WORKERS)
        await pool_says(workers=2 * WORKERS, servers_up=2)

        async def every_worker_holds_a_check() -> bool:
            # A check has left the queue a moment before it reaches its server.
            return (len(on_shared), len(on_free)) == (WORKERS, WORKERS)

        await eventually(every_worker_holds_a_check)

        # No room for a check at all: the server is drained. It is up, it finishes the four
        # checks it holds, and the pool's size is the other server's.
        assert box.run(agent, others=15.5) == "drain 0%\n"
        await shared_is("DRAIN (agent)", WORKERS)
        await pool_says(workers=WORKERS, servers_up=1)
        for gate in gates.values():
            gate.set()
        assert {reply.status for reply in await asyncio.gather(*held)} == {200}
        on_shared.clear()
        on_free.clear()
        drained = await asyncio.gather(
            *(pool.check(session, proof(f"while drained {number}")) for number in range(6))
        )
        assert {reply.status for reply in drained} == {200}
        assert (len(on_shared), len(on_free)) == (0, 6)
        assert {size_said(reply) for reply in drained} == {(WORKERS, 1)}

        # Room again: "ready" ends the drain, and the server is back with one slot.
        assert box.run(agent, others=0.0) == "ready up 100% maxconn:1\n"
        await shared_is("UP", 1)
        await pool_says(workers=1 + WORKERS, servers_up=2)
    finally:
        for gate in gates.values():
            gate.set()
        await agent.close()


async def test_a_limit_of_zero_would_mean_no_limit_which_is_why_the_agent_never_states_it(
    start_lean_server: StartLeanServer, start_pool: StartPool, tmp_path: Path
) -> None:
    """What the agent's rule rests on, shown on the HAProxy under test: ``maxconn:0`` lifts a
    server's limit altogether, and a limit once stated stays until another is stated."""

    class Stated(UsageSampler):
        """A sampler that answers whatever line it was last given."""

        line = "ready up 100% maxconn:2\n"

        def sample(self) -> None:
            return None

        def reply(self) -> str:
            return self.line

    lean = await start_lean_server()
    pool = await start_pool(lean, started=False)
    agent_port = free_port()
    agent = UsageAgent(dataclasses.replace(AgentSettings(), host="127.0.0.1", port=agent_port))
    stated = Stated(dataclasses.replace(AgentSettings(), stat_path=tmp_path / "unused"))
    agent.sampler = stated
    await agent.start()

    async def limit_is(limit: str) -> None:
        async def reached() -> bool:
            return (await pool.server_state("checkers", "lean"))["slim"] == limit

        await eventually(reached)

    try:
        await pool.start_cache()
        await pool.start_haproxy(
            pool.render([LeanServer("lean", "127.0.0.1", lean.port, WORKERS, agent_port)])
        )
        await limit_is("2")
        stated.line = "ready up 100%\n"
        await asyncio.sleep(2 * AGENT_INTERVAL_SECONDS + 1)
        assert (await pool.server_state("checkers", "lean"))["slim"] == "2"  # it stays
        stated.line = "ready up 100% maxconn:0\n"
        await limit_is("")  # no limit at all
        stated.line = f"ready up 100% maxconn:{WORKERS}\n"
        await limit_is(str(WORKERS))
    finally:
        await agent.close()


async def test_a_lean_server_that_answers_health_slowly_stays_up(
    start_lean_server: StartLeanServer, start_pool: StartPool, session: aiohttp.ClientSession
) -> None:
    """A health answer that takes three seconds was three misses in a row to the
    check this pool had before (two seconds to answer), and the server went down."""
    slow, quick = await start_lean_server(), await start_lean_server()
    slow.health_seconds = 3.0
    pool = await start_pool(slow, quick)
    watched_until = asyncio.get_running_loop().time() + 20.0
    while asyncio.get_running_loop().time() < watched_until:
        assert (await pool.server_state("checkers", "lean-0"))["status"].startswith("UP")
        await asyncio.sleep(0.5)
    assert slow.health_checks >= 2
    capacity = (await pool.health(session))[2]
    assert capacity == PoolCapacity(workers=2 * WORKERS, queued=0, servers=2)
