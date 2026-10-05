"""End to end: client -> cache -> (stand-in for the proxy's checkers backend) -> two fake
Lean servers.

HAProxy cannot run in a test, so ``FakeCheckersDoor`` plays the generated ``checkers`` backend:
it spreads checks over the servers and, like ``retry-on conn-failure empty-response 500 502
503 504`` with ``retries 2`` and ``option redispatch 1``, sends a failed check to the other
server. What these tests establish is the cache's part: it serves repeats, relays what the
checkers answer, and stores nothing that came from a failure. HAProxy's own failover is
asserted as configuration text in ``test_haproxy_config.py`` and is not exercised here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fake_checkers_door import FakeCheckersDoor
from fake_lean_server import FakeLeanServer, always, failing_with, lean_answer, lean_timeout
from support import AUTHORIZED, CacheClient, StartCache, StartLeanServer, check, check_body, status

StartPool = Callable[[FakeLeanServer, FakeLeanServer], Awaitable[CacheClient]]


@pytest.fixture
async def start_pool(start_cache: StartCache) -> AsyncIterator[StartPool]:
    doors: list[FakeCheckersDoor] = []

    async def start(first: FakeLeanServer, second: FakeLeanServer) -> CacheClient:
        door = FakeCheckersDoor([first.url, second.url])
        await door.start()
        doors.append(door)
        return await start_cache(door.url)

    yield start
    for door in doors:
        await door.close()


def proofs(count: int) -> list[str]:
    return [f"theorem t{number} : {number} = {number} := by rfl" for number in range(count)]


async def test_repeats_are_served_from_the_cache_and_new_checks_reach_both_servers(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    cache = await start_pool(lean_a, lean_b)

    first_round = [await check(cache, proof) for proof in proofs(6)]
    second_round = [await check(cache, proof, identifier="retry") for proof in proofs(6)]

    assert all("cached" not in result for result in first_round)
    assert all(result["cached"] is True and result["id"] == "retry" for result in second_round)
    assert len(lean_a.requests) == 3
    assert len(lean_b.requests) == 3
    assert sorted(lean_a.checked_codes + lean_b.checked_codes) == sorted(proofs(6))
    counters = await status(cache)
    assert (counters["hits"], counters["misses"], counters["stored_entries"]) == (6, 6, 6)


@pytest.mark.parametrize("failure_status", [500, 502, 503, 504])
async def test_a_failing_server_is_not_what_the_caller_sees_when_the_other_can_answer(
    start_lean_server: StartLeanServer, start_pool: StartPool, failure_status: int
) -> None:
    broken = await start_lean_server(failing_with(failure_status))
    healthy = await start_lean_server(always(lean_answer(("info", "from the healthy server"))))
    cache = await start_pool(broken, healthy)

    results = [await check(cache, proof) for proof in proofs(4)]

    for result in results:
        assert result["response"]["messages"][0]["data"] == "from the healthy server"
    assert len(healthy.requests) == 4
    assert len(broken.requests) == 2  # the checks that were tried there first
    assert (await status(cache))["stored_entries"] == 4


async def test_a_server_that_is_down_is_not_what_the_caller_sees_when_the_other_can_answer(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    down, healthy = await start_lean_server(), await start_lean_server()
    cache = await start_pool(down, healthy)
    await down.close()

    results = [await check(cache, proof) for proof in proofs(4)]

    assert all("response" in result for result in results)
    assert len(healthy.requests) == 4


async def test_when_no_server_can_answer_the_caller_sees_the_failure_and_nothing_is_stored(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    lean_a = await start_lean_server(failing_with(503, "overloaded"))
    lean_b = await start_lean_server(failing_with(503, "overloaded"))
    cache = await start_pool(lean_a, lean_b)
    (proof,) = proofs(1)

    async with cache.post("/api/check", json=check_body(proof), headers=AUTHORIZED) as response:
        assert response.status == 503
        assert await response.text() == "overloaded"
    assert (await status(cache))["stored_entries"] == 0

    lean_a.behaviour = lean_b.behaviour = always(lean_answer())
    assert "cached" not in await check(cache, proof)
    assert (await check(cache, proof))["cached"] is True


async def test_a_lean_timeout_is_an_answer_passed_through_once_and_never_stored(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    lean_a = await start_lean_server(always(lean_timeout()))
    lean_b = await start_lean_server(always(lean_timeout()))
    cache = await start_pool(lean_a, lean_b)
    (proof,) = proofs(1)

    first = await check(cache, proof)
    assert "timed out" in first["error"]
    assert len(lean_a.requests) + len(lean_b.requests) == 1  # not retried on the other server

    await check(cache, proof)
    assert len(lean_a.requests) + len(lean_b.requests) == 2  # and not served from the cache
    assert (await status(cache))["stored_entries"] == 0


async def test_a_proof_that_crashes_every_worker_is_tried_a_bounded_number_of_times(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    """One attempt and two retries: three workers at most, then the caller gets the 500."""
    lean_a = await start_lean_server(failing_with(500, "the worker died"))
    lean_b = await start_lean_server(failing_with(500, "the worker died"))
    cache = await start_pool(lean_a, lean_b)
    (proof,) = proofs(1)

    async with cache.post("/api/check", json=check_body(proof), headers=AUTHORIZED) as response:
        assert response.status == 500
        assert await response.text() == "the worker died"
    assert len(lean_a.requests) + len(lean_b.requests) == 3
    assert (len(lean_a.requests), len(lean_b.requests)) == (2, 1)  # never twice in a row
    assert (await status(cache))["stored_entries"] == 0


async def test_the_key_reaches_the_lean_servers_through_both_hops(
    start_lean_server: StartLeanServer, start_pool: StartPool
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    cache = await start_pool(lean_a, lean_b)
    for proof in proofs(2):
        await check(cache, proof)
    for request in lean_a.requests + lean_b.requests:
        assert request.headers["Authorization"] == AUTHORIZED["Authorization"]
        assert request.headers["X-Lean-Pool-Hop"] == "checkers"
