"""The example client: it follows the pool's stated size and tells a verdict from no verdict."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fake_lean_server import lean_answer, lean_timeout
from pool_client import ClientSettings, Outcome, PoolClient, Verdict, read_verdict

from leanpool.haproxy import PoolSettings, derive_timeouts
from leanpool.signals import (
    BACKGROUND_PRIORITY,
    PRIORITY_HEADER,
    QUEUED_HEADER,
    SERVERS_HEADER,
    WORKERS_HEADER,
)

ACCEPTED = "theorem two : 1 + 1 = 2 := by rfl"


@dataclass
class FakePool:
    """A pool's front door: ``/health`` and ``/api/check``, saying its size as a pool does."""

    workers: int | None = 2  # None: a pool that says nothing about itself
    refusals: list[int] = field(default_factory=list)  # statuses answered before any 200
    garbled: dict[str, str] = field(default_factory=dict)  # code -> a 200 body that is no result
    seconds: float = 0.02
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[web.Request] = field(default_factory=list)
    in_flight: int = 0
    peak_in_flight: int = 0
    _server: TestServer | None = None

    async def start(self) -> str:
        application = web.Application()
        application.router.add_get("/health", self._health)
        application.router.add_post("/api/check", self._check)
        self._server = TestServer(application)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    def _size(self) -> dict[str, str]:
        if self.workers is None:
            return {}
        return {WORKERS_HEADER: str(self.workers), QUEUED_HEADER: "0", SERVERS_HEADER: "1"}

    async def _health(self, _request: web.Request) -> web.Response:
        body: dict[str, Any] = {"status": "ok"}
        if self.workers is not None:
            body |= {"workers": self.workers, "queued": 0, "servers": 1}
        return web.json_response(body)

    async def _check(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        if self.refusals:
            return web.Response(status=self.refusals.pop(0), text="refused", headers=self._size())
        (snippet,) = (await request.json())["snippets"]
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.seconds)
        finally:
            self.in_flight -= 1
        if snippet["code"] in self.garbled:
            body = self.garbled[snippet["code"]]
            return web.Response(text=body, content_type="application/json")
        result = {"id": snippet["id"], **self.answers.get(snippet["code"], lean_answer())}
        return web.json_response({"results": [result]}, headers=self._size())


Check = Callable[..., Awaitable[tuple[list[Verdict], PoolClient]]]


@pytest.fixture
async def check_files() -> AsyncIterator[Check]:
    pools: list[FakePool] = []

    async def check(pool: FakePool, files: dict[str, str], **settings: Any) -> Any:
        pools.append(pool)
        client_settings = ClientSettings(url=await pool.start(), pause_seconds=0.0, **settings)
        async with aiohttp.ClientSession() as session:
            client = PoolClient(session, client_settings)
            return await client.check_all(files), client

    yield check
    for pool in pools:
        await pool.close()


def many(count: int) -> dict[str, str]:
    return {f"t{number}.lean": f"theorem t{number} : True := trivial" for number in range(count)}


# --- reading an answer ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "outcome", "detail"),
    [
        (lean_answer(), "verified", ""),
        (lean_answer(("warning", "unused variable `h`")), "verified", ""),
        (lean_answer(("error", "unsolved goals\n⊢ False")), "rejected", "unsolved goals"),
        (lean_answer(sorries=1), "rejected", "the proof uses sorry"),
        (
            lean_timeout(60),
            "no verdict",
            "the server reported an error: Lean REPL command timed out in 60 seconds",
        ),
    ],
)
def test_a_result_is_verified_rejected_or_no_verdict(
    result: dict[str, Any], outcome: Outcome, detail: str
) -> None:
    assert read_verdict("a.lean", {"id": "a.lean", **result}) == Verdict(
        "a.lean",
        outcome,
        False,
        detail,
    )


def test_an_answer_from_the_cache_is_marked() -> None:
    assert read_verdict("a.lean", {"id": "a.lean", **lean_answer(), "cached": True}).cached


async def test_verdicts_come_back_in_the_files_order(check_files: Check) -> None:
    wrong = "theorem wrong : 2 + 2 = 5 := by rfl"
    pool = FakePool(answers={wrong: lean_answer(("error", "The rfl tactic failed"))})

    verdicts, _client = await check_files(pool, {"right.lean": ACCEPTED, "wrong.lean": wrong})

    assert [(verdict.name, verdict.outcome) for verdict in verdicts] == [
        ("right.lean", "verified"),
        ("wrong.lean", "rejected"),
    ]


# --- following the pool's size ----------------------------------------------------------------


async def test_it_keeps_a_quarter_more_in_flight_than_the_pool_has_workers(
    check_files: Check,
) -> None:
    pool = FakePool(workers=4)

    _verdicts, client = await check_files(pool, many(20), in_flight=1)

    assert client.in_flight_limit == 5
    assert pool.peak_in_flight == 5


async def test_it_follows_a_size_that_changes_while_it_runs(check_files: Check) -> None:
    pool = FakePool(workers=None)

    async def grow_later() -> None:
        await asyncio.sleep(0.05)
        pool.workers = 8

    grower = asyncio.create_task(grow_later())
    _verdicts, client = await check_files(pool, many(40), in_flight=2)
    await grower

    assert client.in_flight_limit == 10
    assert 2 < pool.peak_in_flight <= 10


async def test_it_never_goes_above_its_own_ceiling(check_files: Check) -> None:
    pool = FakePool(workers=100)

    _verdicts, client = await check_files(pool, many(30), ceiling=6)

    assert client.in_flight_limit == 6
    assert pool.peak_in_flight == 6


async def test_a_pool_that_says_nothing_leaves_the_configured_number(check_files: Check) -> None:
    pool = FakePool(workers=None)

    _verdicts, client = await check_files(pool, many(12), in_flight=3)

    assert client.in_flight_limit == 3
    assert pool.peak_in_flight == 3


async def test_a_pool_with_no_server_up_leaves_the_configured_number(check_files: Check) -> None:
    pool = FakePool(workers=0)

    _verdicts, client = await check_files(pool, many(4), in_flight=2)

    assert client.in_flight_limit == 2


# --- a check the pool did not take ------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 502, 503, 504])
async def test_a_check_the_pool_did_not_take_is_asked_again(
    check_files: Check, status: int
) -> None:
    pool = FakePool(refusals=[status, status])

    (verdict,), _client = await check_files(pool, {"a.lean": ACCEPTED})

    assert verdict.outcome == "verified"
    assert len(pool.requests) == 3


async def test_a_pool_that_keeps_refusing_is_no_verdict_and_never_a_failed_proof(
    check_files: Check,
) -> None:
    pool = FakePool(refusals=[503] * 10)

    (verdict,), _client = await check_files(pool, {"a.lean": ACCEPTED}, attempts=3)

    assert verdict == Verdict("a.lean", "no verdict", False, "the pool answered HTTP 503, 3 times")
    assert len(pool.requests) == 3


async def test_a_refusal_that_is_about_the_request_is_not_asked_again(check_files: Check) -> None:
    pool = FakePool(refusals=[401])

    (verdict,), _client = await check_files(pool, {"a.lean": ACCEPTED})

    assert (verdict.outcome, verdict.detail) == ("no verdict", "HTTP 401: refused")
    assert len(pool.requests) == 1


@pytest.mark.parametrize("body", ["<html>not JSON</html>", "{}", '{"results": []}', "[1, 2]"])
async def test_a_reply_that_is_not_a_check_result_costs_one_file_its_verdict_and_no_other(
    check_files: Check, body: str
) -> None:
    garbled_code = "theorem garbled : True := trivial"
    pool = FakePool(garbled={garbled_code: body})

    verdicts, _client = await check_files(pool, {"a.lean": ACCEPTED, "b.lean": garbled_code})

    assert verdicts == [
        Verdict("a.lean", "verified", False, ""),
        Verdict("b.lean", "no verdict", False, "the reply was not a check's result"),
    ]
    assert len(pool.requests) == 2  # asked once: the same reply would come again


async def test_a_pool_that_cannot_be_reached_is_no_verdict() -> None:
    settings = ClientSettings(url="http://127.0.0.1:9", attempts=2, pause_seconds=0.0)
    async with aiohttp.ClientSession() as session:
        (verdict,) = await PoolClient(session, settings).check_all({"a.lean": ACCEPTED})

    assert verdict.outcome == "no verdict"
    assert verdict.detail.startswith("the pool could not be reached")


# --- how long it waits ------------------------------------------------------------------------


@pytest.mark.parametrize("lean_timeout", [30, 60, 120])
def test_it_waits_as_long_as_a_pool_rendered_for_its_lean_timeout_lets_a_client(
    lean_timeout: int,
) -> None:
    pool = derive_timeouts(PoolSettings(lean_timeout_seconds=lean_timeout))
    settings = ClientSettings(url="http://pool.example:18100", lean_timeout_seconds=lean_timeout)

    assert settings.http_wait_seconds == pool.cache_seconds  # also the proxy's `timeout client`
    assert settings.http_wait_seconds >= pool.queue_seconds + pool.checker_seconds


def test_a_wait_that_was_given_is_kept() -> None:
    settings = ClientSettings(url="http://pool.example:18100", http_timeout_seconds=45.0)

    assert settings.http_wait_seconds == 45.0


# --- what it sends ----------------------------------------------------------------------------


async def test_it_sends_one_file_per_request_with_the_key_and_the_lean_timeout(
    check_files: Check,
) -> None:
    pool = FakePool()

    await check_files(pool, many(3), api_key="pool-key", lean_timeout_seconds=120)

    assert len(pool.requests) == 3
    for request in pool.requests:
        body = await request.json()
        assert request.headers["Authorization"] == "Bearer pool-key"
        assert PRIORITY_HEADER not in request.headers
        assert (len(body["snippets"]), body["timeout"]) == (1, 120)


async def test_background_work_says_so_on_every_check(check_files: Check) -> None:
    pool = FakePool()

    await check_files(pool, many(2), background=True)

    assert [request.headers[PRIORITY_HEADER] for request in pool.requests] == [
        BACKGROUND_PRIORITY,
        BACKGROUND_PRIORITY,
    ]
