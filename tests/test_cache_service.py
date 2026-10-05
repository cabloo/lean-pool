"""The cache service in front of a fake Lean server."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from fake_lean_server import (
    DIAGNOSTICS,
    SnippetReply,
    always,
    failing_with,
    lean_answer,
    lean_timeout,
    repl_error,
)
from support import (
    API_KEY,
    AUTHORIZED,
    PIN,
    StartCache,
    StartLeanServer,
    check,
    check_body,
    status,
)

from leanpool.cache.request import CheckRequest, Snippet
from leanpool.cache.service import CheckService
from leanpool.cache.store import AsyncResultStore
from leanpool.cache.upstream import UpstreamClient, new_upstream_session

PROOF = "theorem two : 1 + 1 = 2 := by rfl"
OTHER_PROOF = "theorem three : 1 + 2 = 3 := by rfl"


async def test_an_identical_second_check_never_reaches_the_upstream(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(always(lean_answer(("info", "no axioms"), seconds=1.5)))
    cache = await start_cache(lean.url)

    first = await check(cache, PROOF, identifier="first-attempt", debug=True)
    second = await check(cache, PROOF, identifier="second-attempt", debug=True)

    assert lean.checked_codes == [PROOF]
    assert first["id"] == "first-attempt"
    assert "cached" not in first
    assert second == {
        "id": "second-attempt",
        "time": 1.5,
        "response": first["response"],
        "diagnostics": DIAGNOSTICS,
        "cached": True,
    }


async def test_a_failed_proof_is_a_definitive_answer_and_is_served_from_the_cache(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(always(lean_answer(("error", "unsolved goals"))))
    cache = await start_cache(lean.url)
    await check(cache, PROOF)
    repeat = await check(cache, PROOF)
    assert repeat["cached"] is True
    assert repeat["response"]["messages"] == [{"severity": "error", "data": "unsolved goals"}]
    assert len(lean.requests) == 1


async def test_a_different_timeout_is_a_miss(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF, timeout=60)
    longer = await check(cache, PROOF, timeout=120)
    unspecified = await check(cache, PROOF, timeout=None)
    assert "cached" not in longer
    assert "cached" not in unspecified
    assert [request.body.get("timeout") for request in lean.requests] == [60, 120, None]


async def test_a_whole_float_timeout_hits_the_integer_entry(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF, timeout=60)
    assert (await check(cache, PROOF, timeout=60.0))["cached"] is True


async def test_a_different_pin_is_a_miss_even_on_the_same_database(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    old_image = await start_cache(lean.url, pin="lean-4.26.0")
    await check(old_image, PROOF)
    await old_image.close()

    new_image = await start_cache(lean.url, pin="lean-4.27.0")
    assert "cached" not in await check(new_image, PROOF)
    assert (await check(new_image, PROOF))["cached"] is True
    assert len(lean.requests) == 2


async def test_the_store_survives_a_restart_of_the_cache(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    before = await start_cache(lean.url)
    await check(before, PROOF)
    await before.close()
    after = await start_cache(lean.url)
    assert (await check(after, PROOF))["cached"] is True
    assert len(lean.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        lean_timeout(),
        {"time": 60, "error": "Lean REPL header command timed out in 60 seconds"},
        lean_answer(("error", "INTERNAL PANIC: out of memory")),
        lean_answer(("error", "Stack overflow detected. Aborting.")),
        repl_error(),
    ],
    ids=["timeout", "header-timeout", "out-of-memory", "stack-overflow", "repl-error"],
)
async def test_an_answer_a_retry_could_change_is_returned_but_never_stored(
    start_lean_server: StartLeanServer, start_cache: StartCache, reply: dict[str, Any]
) -> None:
    lean = await start_lean_server(always(reply))
    cache = await start_cache(lean.url)

    first = await check(cache, PROOF, debug=True)
    second = await check(cache, PROOF, debug=True)

    assert first == second == {"id": "attempt-0", **reply}
    assert len(lean.requests) == 2
    assert (await status(cache))["stored_entries"] == 0

    lean.behaviour = always(lean_answer())
    assert "response" in await check(cache, PROOF)


@pytest.mark.parametrize("upstream_status", [500, 429, 503])
async def test_a_server_error_is_returned_to_the_caller_and_not_stored(
    start_lean_server: StartLeanServer, start_cache: StartCache, upstream_status: int
) -> None:
    lean = await start_lean_server(failing_with(upstream_status, "the worker died"))
    cache = await start_cache(lean.url)

    async with cache.post("/api/check", json=check_body(PROOF), headers=AUTHORIZED) as response:
        assert response.status == upstream_status
        assert await response.text() == "the worker died"

    lean.behaviour = always(lean_answer())
    assert "cached" not in await check(cache, PROOF)
    assert len(lean.requests) == 2


async def test_an_unreachable_upstream_is_a_bad_gateway_and_nothing_is_stored(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await lean.close()

    async with cache.post("/api/check", json=check_body(PROOF), headers=AUTHORIZED) as response:
        assert response.status == 502
        assert "could not reach the Lean servers" in (await response.json())["detail"]
    assert (await status(cache))["stored_entries"] == 0


async def test_an_upstream_that_does_not_answer_in_time_is_a_gateway_timeout(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    answer_late = asyncio.Event()

    async def answers_too_late(_snippet: dict[str, Any]) -> SnippetReply:
        await answer_late.wait()
        return lean_answer()

    lean = await start_lean_server(answers_too_late)
    cache = await start_cache(lean.url, upstream_timeout_seconds=0.2)

    async with cache.post("/api/check", json=check_body(PROOF), headers=AUTHORIZED) as response:
        assert response.status == 504
    answer_late.set()
    assert (await status(cache))["stored_entries"] == 0


@pytest.mark.parametrize(
    "malformed_body",
    [
        "<html>not json</html>",
        '{"results": []}',
        '{"results": [{"id": "someone-else", "time": 1, "response": {}}]}',
        '{"results": [{"id": "attempt-0"}, {"id": "attempt-0"}]}',
        '{"unexpected": true}',
        '["results"]',
    ],
    ids=["not-json", "no-result", "wrong-id", "two-results", "no-results-key", "not-an-object"],
)
async def test_a_reply_that_is_not_one_result_for_the_snippet_is_a_bad_gateway(
    start_lean_server: StartLeanServer, start_cache: StartCache, malformed_body: str
) -> None:
    async def answer_malformed(_snippet: dict[str, Any]) -> SnippetReply:
        return web.Response(text=malformed_body, content_type="application/json")

    lean = await start_lean_server(answer_malformed)
    cache = await start_cache(lean.url)
    async with cache.post("/api/check", json=check_body(PROOF), headers=AUTHORIZED) as response:
        assert response.status == 502
    assert (await status(cache))["stored_entries"] == 0


async def test_no_cache_bypasses_the_lookup(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF)
    fresh = await check(cache, PROOF, no_cache=True)
    assert "cached" not in fresh
    assert len(lean.requests) == 2


async def test_no_cache_bypasses_the_store(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF, no_cache=True)
    assert (await status(cache))["stored_entries"] == 0
    assert "cached" not in await check(cache, PROOF)


async def test_no_cache_is_not_forwarded_and_other_fields_are(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF, no_cache=True, reuse=False, future_option="kept")
    await check(cache, OTHER_PROOF, no_cache=False, reuse=False, future_option="kept")
    for request in lean.requests:
        assert "no_cache" not in request.body
        assert request.body["reuse"] is False
        assert request.body["future_option"] == "kept"
        assert request.body["timeout"] == 60


async def test_an_infotree_request_is_forwarded_every_time(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF, infotree="full")
    await check(cache, PROOF, infotree="full")
    assert [request.body["infotree"] for request in lean.requests] == ["full", "full"]
    assert (await status(cache))["bypasses"] == 2


async def test_two_identical_checks_in_flight_make_one_upstream_call(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer(seconds=2.0)

    lean = await start_lean_server(answer_when_released)
    cache = await start_cache(lean.url)

    first = asyncio.create_task(check(cache, PROOF, identifier="first"))
    second = asyncio.create_task(check(cache, PROOF, identifier="second"))
    while (await status(cache))["coalesced"] < 1:
        await asyncio.sleep(0.01)
    assert (await status(cache))["in_flight"] == 1
    release.set()

    results = await asyncio.gather(first, second)
    assert sorted(result["id"] for result in results) == ["first", "second"]
    assert all(result["time"] == 2.0 and "cached" not in result for result in results)
    assert len(lean.requests) == 1
    counters = await status(cache)
    assert (counters["misses"], counters["coalesced"], counters["in_flight"]) == (1, 1, 0)


async def test_checks_in_flight_that_differ_are_not_coalesced(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await asyncio.gather(
        check(cache, PROOF), check(cache, OTHER_PROOF), check(cache, PROOF, timeout=5)
    )
    assert len(lean.requests) == 3


async def test_a_caller_that_gives_up_does_not_cancel_the_answer_others_wait_for(
    start_lean_server: StartLeanServer, tmp_path: Path
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    store = await AsyncResultStore.open(tmp_path / "cache.sqlite3", maximum_bytes=10_000)
    session = new_upstream_session(timeout_seconds=10)
    service = CheckService(store, UpstreamClient(session, lean.url, "X-Lean-Pool-Hop"), PIN, [])
    request = CheckRequest(
        snippets=(), timeout=60, debug=False, bypass_cache=False, forwarded_fields={"timeout": 60}
    )
    authorization = AUTHORIZED["Authorization"]
    try:
        leader = asyncio.create_task(
            service.check(Snippet("leader", PROOF), request, authorization)
        )
        follower = asyncio.create_task(
            service.check(Snippet("follower", PROOF), request, authorization)
        )
        while not lean.requests:
            await asyncio.sleep(0.01)
        leader.cancel()
        release.set()

        result = await follower
        assert isinstance(result, dict)
        assert result["id"] == "follower"
        assert len(lean.requests) == 1
        assert (await store.statistics()).entries == 1
    finally:
        await session.close()
        await store.close()


async def test_the_cache_does_not_cap_concurrency_below_the_pools(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    """150 distinct checks must all be in flight upstream at once (aiohttp's default cap is 100)."""
    checks = 150
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    cache = await start_cache(lean.url)
    url = cache.make_url("/api/check")

    async def post(session: aiohttp.ClientSession, number: int) -> int:
        body = check_body(f"-- proof {number}")
        async with session.post(url, json=body, headers=AUTHORIZED) as response:
            return response.status

    # The test's own client must not be the limit either.
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:
        pending = [asyncio.create_task(post(session, number)) for number in range(checks)]
        async with asyncio.timeout(30):
            while lean.in_flight < checks:
                await asyncio.sleep(0.01)
        assert (await status(cache))["in_flight"] == checks
        release.set()
        assert await asyncio.gather(*pending) == [200] * checks
    assert lean.peak_in_flight == checks


async def test_a_request_with_several_snippets_is_split_and_answered_in_request_order(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    async def slower_for_earlier_snippets(snippet: dict[str, Any]) -> SnippetReply:
        delays = {"-- a": 0.15, "-- b": 0.1, "-- c": 0.05, "-- d": 0.0}
        await asyncio.sleep(delays[snippet["code"]])
        return lean_answer(("info", snippet["code"]))

    lean = await start_lean_server(slower_for_earlier_snippets)
    cache = await start_cache(lean.url)
    await check(cache, "-- c")  # one of the four is already stored

    body = check_body("-- a", "-- b", "-- c", "-- d")
    async with cache.post("/api/check", json=body, headers=AUTHORIZED) as response:
        assert response.status == 200
        results = (await response.json())["results"]

    assert [result["id"] for result in results] == [f"attempt-{index}" for index in range(4)]
    assert [result["response"]["messages"][0]["data"] for result in results] == [
        "-- a",
        "-- b",
        "-- c",
        "-- d",
    ]
    assert ["cached" in result for result in results] == [False, False, True, False]
    assert all(len(request.body["snippets"]) == 1 for request in lean.requests)
    assert sorted(lean.checked_codes) == ["-- a", "-- b", "-- c", "-- d"]


async def test_one_failed_snippet_fails_the_request_but_the_others_are_kept(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    async def crash_on_poison(snippet: dict[str, Any]) -> SnippetReply:
        if snippet["code"] == "-- poison":
            return web.Response(status=500, text="the worker died")
        return lean_answer()

    lean = await start_lean_server(crash_on_poison)
    cache = await start_cache(lean.url)

    body = check_body("-- good", "-- poison", "-- also good")
    async with cache.post("/api/check", json=body, headers=AUTHORIZED) as response:
        assert response.status == 500
        assert await response.text() == "the worker died"

    # The client's retry of the good snippets costs the Lean servers nothing.
    assert (await check(cache, "-- good"))["cached"] is True
    assert (await check(cache, "-- also good"))["cached"] is True
    assert lean.checked_codes.count("-- good") == 1


async def test_eviction_through_the_service_keeps_the_store_under_its_cap(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url, maximum_bytes=1_000)
    for number in range(12):
        await check(cache, f"-- proof {number}")
        assert (await status(cache))["stored_bytes"] <= 1_000
    counters = await status(cache)
    assert counters["evictions"] > 0
    assert counters["maximum_bytes"] == 1_000

    assert (await check(cache, "-- proof 11"))["cached"] is True  # the most recent survives
    assert "cached" not in await check(cache, "-- proof 0")  # the least recent was evicted


async def test_diagnostics_follow_the_callers_own_debug_flag(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)

    quiet_miss = await check(cache, PROOF)
    quiet_hit = await check(cache, PROOF)
    debug_hit = await check(cache, PROOF, debug=True)
    debug_miss = await check(cache, OTHER_PROOF, debug=True)

    assert "diagnostics" not in quiet_miss
    assert "diagnostics" not in quiet_hit
    assert debug_hit["diagnostics"] == DIAGNOSTICS  # kept from the first caller's check
    assert debug_miss["diagnostics"] == DIAGNOSTICS
    assert all(request.body["debug"] is True for request in lean.requests)


async def test_a_lean_timeout_keeps_its_diagnostics_as_kimina_sends_them(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(always(lean_timeout()))
    cache = await start_cache(lean.url)
    assert (await check(cache, PROOF))["diagnostics"] == {"repl_uuid": "fake-repl"}


async def test_a_bypassed_check_is_forwarded_and_returned_exactly_as_it_is(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    result = await check(cache, PROOF, no_cache=True)
    assert lean.requests[0].body == {
        "snippets": [{"id": "attempt-0", "code": PROOF}],
        "timeout": 60,
    }
    assert result == {"id": "attempt-0", "time": 0.25, "response": lean_answer()["response"]}


async def test_a_request_without_the_key_is_refused_even_for_a_stored_answer(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF)

    wrong_keys: list[dict[str, str]] = [
        {},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {API_KEY}x"},
        {"Authorization": f"Basic {API_KEY[:-1]}"},
    ]
    for headers in wrong_keys:
        async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
            assert response.status == 401
            assert "results" not in await response.json()
    assert len(lean.requests) == 1
    assert (await status(cache))["hits"] == 0


async def test_the_key_is_accepted_with_or_without_the_bearer_prefix_as_kimina_does(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    for headers in ({"Authorization": f"Bearer {API_KEY}"}, {"Authorization": API_KEY}):
        async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
            assert response.status == 200


async def test_the_callers_key_and_the_loop_guard_header_are_sent_upstream(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF)
    await check(cache, OTHER_PROOF, no_cache=True)
    for request in lean.requests:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert request.headers["X-Lean-Pool-Hop"] == "checkers"


async def test_a_request_that_already_passed_through_the_cache_is_refused(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url, hop_header="X-Second-Hop")
    headers = {**AUTHORIZED, "X-Second-Hop": "checkers"}
    async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
        assert response.status == 508
    assert lean.requests == []

    await check(cache, PROOF)
    assert lean.requests[0].headers["X-Second-Hop"] == "checkers"


async def test_a_pool_without_a_key_is_served_without_one(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=None)
    cache = await start_cache(lean.url, api_key=None)
    async with cache.post("/api/check", json=check_body(PROOF)) as response:
        assert response.status == 200
    assert "Authorization" not in lean.requests[0].headers


async def test_health_is_open_and_status_needs_the_key(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    async with cache.get("/health") as response:
        assert response.status == 200
        assert await response.json() == {"status": "ok"}
    async with cache.get("/status") as response:
        assert response.status == 401


async def test_status_counts_each_check_once(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    await check(cache, PROOF)  # miss
    await check(cache, PROOF)  # hit
    await check(cache, PROOF)  # hit
    await check(cache, OTHER_PROOF, no_cache=True)  # bypass
    counters = await status(cache)
    assert counters == {
        "hits": 2,
        "misses": 1,
        "coalesced": 0,
        "bypasses": 1,
        "in_flight": 0,
        "stored_entries": 1,
        "stored_bytes": counters["stored_bytes"],
        "file_bytes": counters["file_bytes"],
        "evictions": 0,
        "maximum_bytes": 4 * 1024**3,
    }
    assert 0 < counters["stored_bytes"] < counters["file_bytes"]


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"snippets": []}',
        b'{"snippets": [{"id": "a", "code": "x"}, {"id": "a", "code": "y"}]}',
        b'{"snippets": [{"id": "a", "code": "x"}], "timeout": "soon"}',
        b'{"snippets": [{"id": "a", "code": "x"}], "no_cache": "yes"}',
    ],
)
async def test_a_malformed_request_is_refused_with_422_and_never_forwarded(
    start_lean_server: StartLeanServer, start_cache: StartCache, body: bytes
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    headers = {**AUTHORIZED, "Content-Type": "application/json"}
    async with cache.post("/api/check", data=body, headers=headers) as response:
        assert response.status == 422
        assert "detail" in await response.json()
    assert lean.requests == []


async def test_a_request_larger_than_the_limit_is_refused(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url, maximum_request_bytes=2_000)
    oversized = check_body("-- " + "x" * 4_000)
    async with cache.post("/api/check", json=oversized, headers=AUTHORIZED) as response:
        assert response.status == 413
    assert lean.requests == []
    assert "response" in await check(cache, PROOF)


async def test_the_trailing_slash_path_is_served_as_kimina_serves_it(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    async with cache.post("/api/check/", json=check_body(PROOF), headers=AUTHORIZED) as response:
        assert response.status == 200


async def test_a_broken_store_costs_the_cache_not_the_check(
    start_lean_server: StartLeanServer, start_cache: StartCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)

    async def broken(*_arguments: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(AsyncResultStore, "get", broken)
    monkeypatch.setattr(AsyncResultStore, "put", broken)
    assert "response" in await check(cache, PROOF)
    assert "response" in await check(cache, PROOF)
    assert len(lean.requests) == 2
