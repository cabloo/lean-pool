"""The cache's second key: a check sent with it passes the screen first, and goes on with the
pool's own key. Each key is taken at its own door alone: the screened key from what the proxy's
screened door forwards (it says so in one header), the pool's key from everything else."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fake_lean_server import SnippetReply, lean_answer
from support import API_KEY, AUTHORIZED, StartCache, StartLeanServer, check, check_body

from leanpool.cache import CacheSettings
from leanpool.doors import DOOR_HEADER, SCREENED_DOOR

SCREENED_KEY = "screened-key"
PLACES = 2
SCREENED_SETTINGS: dict[str, Any] = {"screened_api_key": SCREENED_KEY, "screened_in_flight": PLACES}
AT_THE_SCREENED_DOOR = {DOOR_HEADER: SCREENED_DOOR}
SCREENED = {"Authorization": f"Bearer {SCREENED_KEY}", **AT_THE_SCREENED_DOOR}
PROOF = "theorem two : 1 + 1 = 2 := by rfl"
RUNS_CODE = '#eval IO.println "hello"'


async def screened_check(client: Any, *codes: str, **fields: Any) -> tuple[int, dict[str, Any]]:
    body = {**check_body(*codes), **fields}
    async with client.post("/api/check", json=body, headers=SCREENED) as response:
        return response.status, await response.json()


async def test_a_screened_check_that_passes_is_answered_and_reaches_lean_with_the_pools_key(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(cache, PROOF)
    assert status == 200, reply
    assert reply["results"][0]["id"] == "attempt-0"
    (request,) = lean.requests
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert all(SCREENED_KEY not in value for value in request.headers.values())
    assert DOOR_HEADER not in request.headers


async def test_a_screened_check_that_fails_is_refused_reaches_nothing_and_stores_nothing(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(cache, RUNS_CODE)
    assert status == 422
    assert reply == {
        "detail": "refused by the pool's screen: snippet 'attempt-0' holds the command `#eval`"
    }
    assert lean.requests == []
    # Nothing was stored: the pool's own key gets the check from Lean, not from the store.
    result = await check(cache, RUNS_CODE)
    assert "cached" not in result
    assert len(lean.requests) == 1


async def test_one_refused_snippet_refuses_the_whole_request(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(cache, PROOF, "theorem t : True := by run_tac pure ()")
    assert status == 422
    assert "snippet 'attempt-1' holds the word `run_tac`" in reply["detail"]
    assert lean.requests == []


async def test_the_pools_own_key_is_not_screened(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    await check(cache, RUNS_CODE)
    assert lean.checked_codes == [RUNS_CODE]


async def test_a_screened_check_and_a_home_check_share_one_stored_answer(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, _reply = await screened_check(cache, PROOF)
    assert status == 200
    assert (await check(cache, PROOF))["cached"] is True
    status, reply = await screened_check(cache, PROOF)
    assert status == 200
    assert reply["results"][0]["cached"] is True
    assert len(lean.requests) == 1


async def test_status_takes_the_pools_key_alone(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    async with cache.get("/status", headers=SCREENED) as response:
        assert response.status == 401
    async with cache.get("/status", headers={**AUTHORIZED, **AT_THE_SCREENED_DOOR}) as response:
        assert response.status == 401
    async with cache.get("/status", headers=AUTHORIZED) as response:
        assert response.status == 200


async def test_the_screened_key_is_refused_anywhere_but_at_the_screened_door(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    headers = {"Authorization": f"Bearer {SCREENED_KEY}"}
    async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
        assert response.status == 401
        assert await response.json() == {
            "detail": "the screened key is taken at the screened door alone"
        }
    assert lean.requests == []


@pytest.mark.parametrize("screened_api_key", [SCREENED_KEY, None])
async def test_the_pools_key_is_refused_at_the_screened_door(
    start_lean_server: StartLeanServer, start_cache: StartCache, screened_api_key: str | None
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(
        lean.url, screened_api_key=screened_api_key, screened_in_flight=PLACES
    )
    headers = {**AUTHORIZED, **AT_THE_SCREENED_DOOR}
    async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
        assert response.status == 401
        assert await response.json() == {"detail": "the screened door takes the screened key alone"}
    assert lean.requests == []


@pytest.mark.parametrize("headers", [SCREENED, {"Authorization": "Bearer nobody"}, {}])
async def test_without_a_screened_key_set_that_key_is_refused_as_any_wrong_key(
    start_lean_server: StartLeanServer, start_cache: StartCache, headers: dict[str, str]
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url)
    async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
        assert response.status == 401
    assert lean.requests == []


async def test_a_wrong_key_is_refused_beside_a_screened_key(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    for headers in ({"Authorization": "Bearer nobody"}, {"Authorization": "Bearer "}, {}):
        async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
            assert response.status == 401
    assert lean.requests == []


@pytest.mark.parametrize(
    ("api_key", "places", "message"),
    [
        (SCREENED_KEY, PLACES, "must differ"),
        (None, PLACES, "needs the pool's own key"),
        (API_KEY, None, "at least 1 check in flight"),
        (API_KEY, 0, "at least 1 check in flight"),
    ],
)
def test_settings_refuse_a_screened_key_equal_to_the_pools_or_without_one_or_without_a_limit(
    tmp_path: Any, api_key: str | None, places: int | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CacheSettings(
            pin="p",
            api_key=api_key,
            database_path=tmp_path / "c.sqlite3",
            screened_api_key=SCREENED_KEY,
            screened_in_flight=places,
        )


async def test_a_root_the_pool_adds_may_be_imported_and_nothing_else(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(
        lean.url, **SCREENED_SETTINGS, screened_imports=("FormalConjectures",)
    )
    statement = "import FormalConjectures.Util.ProblemImports\ntheorem t : True := trivial"
    status, reply = await screened_check(cache, statement)
    assert status == 200, reply
    assert lean.checked_codes == [statement]
    status, reply = await screened_check(cache, "import Lean\ntheorem t : True := trivial")
    assert status == 422
    assert "only Mathlib, Aesop and FormalConjectures may be imported" in reply["detail"]


async def test_without_an_added_root_another_librarys_import_is_refused(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(
        cache, "import FormalConjectures.Util.ProblemImports\ntheorem t : True := trivial"
    )
    assert status == 422
    assert "only Mathlib and Aesop may be imported" in reply["detail"]
    assert lean.requests == []


@pytest.mark.parametrize("root", ["formal", "Formal.Conjectures", ""])
def test_settings_refuse_an_import_root_that_is_not_one(tmp_path: Any, root: str) -> None:
    with pytest.raises(ValueError, match="not the root of a Lean module's name"):
        CacheSettings(
            pin="p",
            api_key=API_KEY,
            database_path=tmp_path / "c.sqlite3",
            screened_api_key=SCREENED_KEY,
            screened_in_flight=PLACES,
            screened_imports=(root,),
        )


def test_settings_refuse_a_screened_request_limit_below_one_byte(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="at least 1 byte"):
        CacheSettings(
            pin="p",
            api_key=API_KEY,
            database_path=tmp_path / "c.sqlite3",
            screened_api_key=SCREENED_KEY,
            screened_in_flight=PLACES,
            screened_max_request_bytes=0,
        )


# --- what a screened request may carry, and what its checks may cost ----------------------------


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"infotree": "full"}, "the screened door does not take `infotree`"),
        ({"gc": True}, "the screened door does not take `gc`"),
        ({"reuse": False}, "the screened door takes `reuse` true alone"),
        ({"no_cache": True}, "the screened door does not take `no_cache`"),
        ({"timeout": 301}, "a check at the screened door has a timeout of at most 300 seconds"),
    ],
)
async def test_a_screened_request_carries_the_doors_fields_alone(
    start_lean_server: StartLeanServer,
    start_cache: StartCache,
    fields: dict[str, Any],
    detail: str,
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(cache, PROOF, **fields)
    assert (status, reply) == (422, {"detail": detail})
    assert lean.requests == []


async def test_a_screened_request_with_kiminas_own_fields_is_taken(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    status, reply = await screened_check(
        cache, PROOF, timeout=300, debug=False, reuse=True, no_cache=False
    )
    assert status == 200, reply
    (request,) = lean.requests
    assert request.body["timeout"] == 300
    # The pool's own checks keep every field: what a newer Kimina adds still reaches it.
    await check(cache, "theorem other : True := trivial", infotree="original")
    assert lean.requests[-1].body["infotree"] == "original"


async def test_a_screened_request_that_names_no_timeout_is_given_the_doors_longest(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    """Never the servers' own default, which may be longer than the pool lets its senders ask
    for: the timeout is in what is forwarded, and in the answer's key."""
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS, screened_max_timeout_seconds=120)
    body = check_body(PROOF, timeout=None)
    async with cache.post("/api/check", json=body, headers=SCREENED) as response:
        assert response.status == 200, await response.text()
    (request,) = lean.requests
    assert request.body["timeout"] == 120
    # Stored under that timeout: the same check naming it is answered from the store.
    status, reply = await screened_check(cache, PROOF, timeout=120)
    assert status == 200
    assert reply["results"][0]["cached"] is True
    assert len(lean.requests) == 1


async def test_a_screened_body_longer_than_its_limit_is_refused_before_it_is_read(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    limit = 1024
    cache = await start_cache(lean.url, **SCREENED_SETTINGS, screened_max_request_bytes=limit)
    long_proof = "theorem long : True := trivial -- " + "x" * limit
    refused = (413, {"detail": f"a check at the screened door is at most {limit} bytes"})
    # As its Content-Length says, and as its bytes say when it states no length (chunked).
    status, reply = await screened_check(cache, long_proof)
    assert (status, reply) == refused

    async def chunks() -> AsyncIterator[bytes]:
        yield json.dumps(check_body(long_proof)).encode()

    async with cache.post("/api/check", data=chunks(), headers=SCREENED) as response:
        assert (response.status, await response.json()) == refused
    assert lean.requests == []
    # The pool's own key keeps the pool's limit.
    await check(cache, long_proof)
    assert len(lean.requests) == 1
    # And a screened body within it is served.
    status, _reply = await screened_check(cache, PROOF)
    assert status == 200


async def test_the_screen_runs_off_the_event_loop(
    start_lean_server: StartLeanServer,
    start_cache: StartCache,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A screen that takes a while holds up no other request: here /health, answered while it
    runs."""
    entered, release = threading.Event(), threading.Event()

    def slow_screen(_code: str, _imports: Sequence[str] = ()) -> str | None:
        entered.set()
        release.wait(10)
        return None

    monkeypatch.setattr("leanpool.cache.app.screen", slow_screen)
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    sent = asyncio.create_task(screened_check(cache, PROOF))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        async with cache.get("/health") as response:
            assert response.status == 200
        assert not sent.done()
    finally:
        release.set()
    status, _reply = await sent
    assert status == 200


async def test_a_screened_request_carries_at_most_as_many_snippets_as_its_places(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    codes = [f"theorem t{n} : True := trivial" for n in range(PLACES + 1)]
    status, reply = await screened_check(cache, *codes)
    assert (status, reply) == (
        422,
        {"detail": f"a check at the screened door carries at most {PLACES} snippets"},
    )
    assert lean.requests == []
    status, _reply = await screened_check(cache, *codes[:PLACES])
    assert status == 200


class HeldServer:
    """A behaviour for the fake Lean server that answers nothing until it is let go."""

    def __init__(self) -> None:
        self.released = asyncio.Event()

    async def __call__(self, _snippet: dict[str, Any]) -> SnippetReply:
        await self.released.wait()
        return lean_answer()


async def wait_for(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


async def test_screened_checks_take_their_places_and_one_past_the_waiting_room_is_refused(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    held = HeldServer()
    lean = await start_lean_server(api_key=API_KEY, behaviour=held)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    codes = [f"theorem t{n} : True := trivial" for n in range(2 * PLACES + 1)]
    # PLACES at the servers and PLACES waiting, each in a request of its own.
    waiting = [asyncio.create_task(screened_check(cache, code)) for code in codes[: 2 * PLACES]]
    await wait_for(lambda: lean.in_flight == PLACES)
    await asyncio.sleep(0.05)
    assert lean.in_flight == PLACES
    status, reply = await screened_check(cache, codes[-1])
    assert (status, reply) == (503, {"detail": "the screened door is full"})
    # The pool's own checks take no place: one goes straight to the servers.
    home = asyncio.create_task(check(cache, "theorem home : True := trivial"))
    await wait_for(lambda: lean.in_flight == PLACES + 1)
    held.released.set()
    replies = await asyncio.gather(*waiting)
    assert [status for status, _reply in replies] == [200] * (2 * PLACES)
    assert "cached" not in await home
    assert lean.peak_in_flight == PLACES + 1


async def test_a_screened_sender_that_hangs_up_keeps_its_place_until_its_check_ends(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    held = HeldServer()
    lean = await start_lean_server(api_key=API_KEY, behaviour=held)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    codes = [f"theorem t{n} : True := trivial" for n in range(2 * PLACES + 1)]
    gone = [asyncio.create_task(screened_check(cache, code)) for code in codes[: 2 * PLACES]]
    await wait_for(lambda: lean.in_flight == PLACES)
    for task in gone:
        task.cancel()
    await asyncio.gather(*gone, return_exceptions=True)
    await asyncio.sleep(0.05)
    # Their checks run on, at the servers and waiting, so the door is still full.
    assert lean.in_flight == PLACES
    status, _reply = await screened_check(cache, codes[-1])
    assert status == 503
    held.released.set()
    await wait_for(lambda: len(lean.requests) == 2 * PLACES)
    status, reply = await screened_check(cache, codes[0])
    assert status == 200
    assert reply["results"][0]["cached"] is True


async def test_a_pool_check_never_waits_on_a_screened_flight(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    held = HeldServer()
    lean = await start_lean_server(api_key=API_KEY, behaviour=held)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    screened = asyncio.create_task(screened_check(cache, PROOF))
    await wait_for(lambda: lean.in_flight == 1)
    # The same check with the pool's key starts a flight of its own.
    home = asyncio.create_task(check(cache, PROOF))
    await wait_for(lambda: lean.in_flight == 2)
    held.released.set()
    status, _reply = await screened
    assert status == 200
    assert "cached" not in await home
    # And a screened check joins a flight the pool's key started.
    lean.requests.clear()
    held.released.clear()
    other = "theorem other : True := trivial"
    home = asyncio.create_task(check(cache, other))
    await wait_for(lambda: lean.in_flight == 1)
    screened = asyncio.create_task(screened_check(cache, other))
    await asyncio.sleep(0.05)
    assert lean.in_flight == 1
    held.released.set()
    assert (await screened)[0] == 200
    await home
    assert len(lean.requests) == 1


async def test_no_key_reaches_a_log_line_or_an_error(
    start_lean_server: StartLeanServer,
    start_cache: StartCache,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, **SCREENED_SETTINGS)
    replies = [await screened_check(cache, PROOF), await screened_check(cache, RUNS_CODE)]
    text = caplog.text + repr(replies)
    assert SCREENED_KEY not in text
    assert API_KEY not in text
