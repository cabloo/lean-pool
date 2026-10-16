"""The cache's second key: a check sent with it passes the screen first, and goes on with the
pool's own key."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from support import API_KEY, AUTHORIZED, StartCache, StartLeanServer, check, check_body

from leanpool.cache import CacheSettings

SCREENED_KEY = "rental-key"
SCREENED = {"Authorization": f"Bearer {SCREENED_KEY}"}
PROOF = "theorem two : 1 + 1 = 2 := by rfl"
RUNS_CODE = '#eval IO.println "hello"'


async def screened_check(client: Any, *codes: str) -> tuple[int, dict[str, Any]]:
    async with client.post("/api/check", json=check_body(*codes), headers=SCREENED) as response:
        return response.status, await response.json()


async def test_a_screened_check_that_passes_is_answered_and_reaches_lean_with_the_pools_key(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    status, reply = await screened_check(cache, PROOF)
    assert status == 200, reply
    assert reply["results"][0]["id"] == "attempt-0"
    (request,) = lean.requests
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert all(SCREENED_KEY not in value for value in request.headers.values())


async def test_a_screened_check_that_fails_is_refused_reaches_nothing_and_stores_nothing(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
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
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    status, reply = await screened_check(cache, PROOF, "theorem t : True := by run_tac pure ()")
    assert status == 422
    assert "snippet 'attempt-1' holds the word `run_tac`" in reply["detail"]
    assert lean.requests == []


async def test_the_pools_own_key_is_not_screened(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    await check(cache, RUNS_CODE)
    assert lean.checked_codes == [RUNS_CODE]


async def test_a_screened_check_and_a_home_check_share_one_stored_answer(
    start_lean_server: StartLeanServer, start_cache: StartCache
) -> None:
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
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
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    async with cache.get("/status", headers=SCREENED) as response:
        assert response.status == 401
    async with cache.get("/status", headers=AUTHORIZED) as response:
        assert response.status == 200


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
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    for headers in ({"Authorization": "Bearer nobody"}, {"Authorization": "Bearer "}, {}):
        async with cache.post("/api/check", json=check_body(PROOF), headers=headers) as response:
            assert response.status == 401
    assert lean.requests == []


@pytest.mark.parametrize(
    ("api_key", "message"),
    [(SCREENED_KEY, "must differ"), (None, "needs the pool's own key")],
)
def test_settings_refuse_a_screened_key_equal_to_the_pools_or_without_one(
    tmp_path: Any, api_key: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CacheSettings(
            pin="p",
            api_key=api_key,
            database_path=tmp_path / "c.sqlite3",
            screened_api_key=SCREENED_KEY,
        )


async def test_no_key_reaches_a_log_line_or_an_error(
    start_lean_server: StartLeanServer,
    start_cache: StartCache,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lean = await start_lean_server(api_key=API_KEY)
    cache = await start_cache(lean.url, screened_api_key=SCREENED_KEY)
    replies = [await screened_check(cache, PROOF), await screened_check(cache, RUNS_CODE)]
    text = caplog.text + repr(replies)
    assert SCREENED_KEY not in text
    assert API_KEY not in text
