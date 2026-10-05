"""A check's priority, and the numbers the pool says about itself."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fake_lean_server import SnippetReply, lean_answer
from multidict import CIMultiDict
from support import AUTHORIZED, StartCache, StartLeanServer, check_body, status

from leanpool.signals import (
    BACKGROUND_PRIORITY,
    PRIORITY_HEADER,
    QUEUED_HEADER,
    SERVERS_HEADER,
    WORKERS_HEADER,
    PoolCapacity,
    capacity_from_headers,
    capacity_from_health,
    is_background,
    read_count,
)

PROOF = "theorem two : 1 + 1 = 2 := by norm_num"
OTHER_PROOF = "theorem three : 1 + 2 = 3 := by norm_num"


@pytest.mark.parametrize("value", ["background", "Background", " BACKGROUND "])
def test_the_one_value_marks_background_work_in_any_letter_case(value: str) -> None:
    assert is_background(value)


@pytest.mark.parametrize("value", [None, "", "bakground", "low", "background,urgent", "0"])
def test_any_other_value_and_no_value_is_a_normal_check(value: str | None) -> None:
    assert not is_background(value)


@pytest.mark.parametrize(("value", "count"), [("0", 0), ("28", 28), (" 7 ", 7), (12, 12)])
def test_a_count_is_a_whole_number_that_is_not_negative(value: object, count: int) -> None:
    assert read_count(value) == count


@pytest.mark.parametrize(
    "value",
    [None, "", " ", "-1", "2.5", "1e3", "twelve", "0x1c", "²", True, 2.0, -3, 10**9, "9" * 12],
)
def test_anything_else_is_not_a_count(value: object) -> None:
    assert read_count(value) is None


def test_the_three_headers_are_read_whatever_their_letter_case() -> None:
    headers = CIMultiDict(
        {"x-lean-pool-workers": "28", "X-LEAN-POOL-QUEUED": "3", SERVERS_HEADER: "3"}
    )
    assert capacity_from_headers(headers) == PoolCapacity(workers=28, queued=3, servers=3)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {WORKERS_HEADER: "28"},
        {WORKERS_HEADER: "", QUEUED_HEADER: "0", SERVERS_HEADER: "3"},
        {WORKERS_HEADER: "many", QUEUED_HEADER: "0", SERVERS_HEADER: "3"},
    ],
)
def test_headers_that_are_missing_empty_or_garbled_say_nothing(headers: dict[str, str]) -> None:
    assert capacity_from_headers(CIMultiDict(headers)) is None


def test_the_health_body_carries_the_same_numbers() -> None:
    body = {"status": "ok", "workers": 28, "queued": 0, "servers": 3}
    assert capacity_from_health(body) == PoolCapacity(workers=28, queued=0, servers=3)


@pytest.mark.parametrize(
    "body",
    [None, [], "ok", {"status": "ok"}, {"status": "ok", "workers": "x", "queued": 0, "servers": 1}],
)
def test_a_health_body_without_the_numbers_says_nothing(body: object) -> None:
    assert capacity_from_health(body) is None


async def test_a_background_check_says_so_on_the_second_hop_and_nothing_else_does(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server()
    cache = await start_cache(lean.url)
    sent = [
        ("-- one", {PRIORITY_HEADER: "Background"}, {}),
        ("-- two", {PRIORITY_HEADER: "bakground"}, {}),
        ("-- three", {}, {}),
        ("-- four", {PRIORITY_HEADER: "background"}, {"no_cache": True}),
    ]
    for code, headers, extra in sent:
        body = check_body(code, **extra)
        async with cache.post("/api/check", json=body, headers={**AUTHORIZED, **headers}) as reply:
            assert reply.status == 200, await reply.text()
    forwarded = [request.headers.get(PRIORITY_HEADER) for request in lean.requests]
    assert forwarded == [BACKGROUND_PRIORITY, None, None, BACKGROUND_PRIORITY]


async def _post(cache: Any, code: str, identifier: str, background: bool) -> dict[str, Any]:
    body = check_body(code)
    body["snippets"][0]["id"] = identifier
    headers = {**AUTHORIZED, **({PRIORITY_HEADER: BACKGROUND_PRIORITY} if background else {})}
    async with cache.post("/api/check", json=body, headers=headers) as reply:
        assert reply.status == 200, await reply.text()
        result: dict[str, Any] = (await reply.json())["results"][0]
    return result


async def test_a_normal_check_never_waits_on_a_background_flight_and_later_callers_join_it(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer(seconds=2.0)

    lean = await start_lean_server(answer_when_released)
    cache = await start_cache(lean.url)

    async def upstream_requests(count: int) -> None:
        while len(lean.requests) < count:
            await asyncio.sleep(0.01)

    background = asyncio.create_task(_post(cache, PROOF, "background", True))
    await upstream_requests(1)
    normal = asyncio.create_task(_post(cache, PROOF, "normal", False))
    await upstream_requests(2)  # its own request: it did not join the background flight
    late_background = asyncio.create_task(_post(cache, PROOF, "late-background", True))
    late_normal = asyncio.create_task(_post(cache, PROOF, "late-normal", False))
    while (await status(cache))["coalesced"] < 2:
        await asyncio.sleep(0.01)
    assert len(lean.requests) == 2  # the two late callers joined the normal flight
    assert [request.headers.get(PRIORITY_HEADER) for request in lean.requests] == [
        BACKGROUND_PRIORITY,
        None,
    ]
    release.set()

    results = await asyncio.gather(background, normal, late_background, late_normal)
    assert [result["id"] for result in results] == [
        "background",
        "normal",
        "late-background",
        "late-normal",
    ]
    assert all(result["time"] == 2.0 for result in results)
    counters = await status(cache)
    assert (counters["misses"], counters["coalesced"], counters["in_flight"]) == (2, 2, 0)
    # Both flights are gone: the answer is in the store, and the next check is a hit.
    assert (await _post(cache, PROOF, "after", False))["cached"] is True
    assert len(lean.requests) == 2


async def test_a_background_check_joins_a_normal_flight(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    release = asyncio.Event()

    async def answer_when_released(_snippet: dict[str, Any]) -> SnippetReply:
        await release.wait()
        return lean_answer()

    lean = await start_lean_server(answer_when_released)
    cache = await start_cache(lean.url)
    normal = asyncio.create_task(_post(cache, OTHER_PROOF, "normal", False))
    while not lean.requests:
        await asyncio.sleep(0.01)
    background = asyncio.create_task(_post(cache, OTHER_PROOF, "background", True))
    while (await status(cache))["coalesced"] < 1:
        await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(normal, background)
    assert len(lean.requests) == 1
    assert PRIORITY_HEADER not in lean.requests[0].headers
