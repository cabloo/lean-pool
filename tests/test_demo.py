"""The demo: its stand-in Lean server, the files its compose example mounts, its walkthrough."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from live_pool import HAPROXY
from native_demo import NativeDemo
from standin_lean_server import (
    CRASH_MARKER,
    ERROR_MARKER,
    TIMEOUT_MARKER,
    StandInSettings,
    create_application,
)
from support import API_KEY, AUTHORIZED, StartCache, check, check_body, status

from leanpool.haproxy import parse_server_list, render_haproxy_config
from leanpool.kimina import IndefiniteResultError, read_lean_answer

PROJECT = Path(__file__).parent.parent
DEMO = PROJECT / "examples" / "demo"
ACCEPTED = "theorem two : 1 + 1 = 2 := by rfl"
USES_SORRY = "theorem hard : 2 + 2 = 5 := by sorry"
WRONG = f"-- {ERROR_MARKER}\ntheorem wrong : 2 + 2 = 5 := by rfl"
TIMES_OUT = f"-- {TIMEOUT_MARKER}\ntheorem slow : True := by trivial"
CRASHES = f"-- {CRASH_MARKER}\ntheorem fatal : True := by trivial"

StandInClient = TestClient[web.Request, web.Application]
StartStandIn = Callable[..., Awaitable[StandInClient]]


@pytest.fixture
async def start_stand_in() -> AsyncIterator[StartStandIn]:
    clients: list[StandInClient] = []

    async def start(**settings: Any) -> StandInClient:
        defaults: dict[str, Any] = {"name": "lean-a", "seconds": 0.0, "api_key": API_KEY}
        application = create_application(StandInSettings(**{**defaults, **settings}))
        client = TestClient(TestServer(application))
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


async def ask(client: StandInClient, *codes: str, **extra: Any) -> tuple[int, Any]:
    body = check_body(*codes, **extra)
    async with client.post("/api/check", json=body, headers=AUTHORIZED) as response:
        return response.status, await response.json()


# --- the stand-in Lean server ----------------------------------------------------------------


async def test_the_stand_in_accepts_code_without_a_marker_and_says_it_ran_no_lean(
    start_stand_in: StartStandIn,
) -> None:
    status_code, reply = await ask(await start_stand_in(), ACCEPTED)
    (result,) = reply["results"]
    answer = read_lean_answer(result)

    assert (status_code, result["id"]) == (200, "attempt-0")
    assert not answer.has_errors
    assert not answer.uses_sorry
    assert [message.text for message in answer.messages] == [
        "stand-in lean-a: accepted without running Lean"
    ]


@pytest.mark.parametrize(
    ("code", "has_errors", "uses_sorry"),
    [(USES_SORRY, False, True), (WRONG, True, False)],
)
async def test_the_stand_in_rejects_a_sorry_and_an_error_marker_as_lean_would(
    start_stand_in: StartStandIn, code: str, has_errors: bool, uses_sorry: bool
) -> None:
    _status, reply = await ask(await start_stand_in(), code)
    answer = read_lean_answer(reply["results"][0])

    assert (answer.has_errors, answer.uses_sorry) == (has_errors, uses_sorry)


async def test_a_timeout_marker_is_answered_as_kimina_answers_a_lean_timeout(
    start_stand_in: StartStandIn,
) -> None:
    status_code, reply = await ask(await start_stand_in(), TIMES_OUT, timeout=30)
    (result,) = reply["results"]

    assert status_code == 200
    assert result["error"] == "Lean REPL command timed out in 30 seconds"
    with pytest.raises(IndefiniteResultError):
        read_lean_answer(result)


async def test_a_crash_marker_fails_the_whole_request_with_500(
    start_stand_in: StartStandIn,
) -> None:
    status_code, reply = await ask(await start_stand_in(), ACCEPTED, CRASHES)

    assert status_code == 500
    assert "results" not in reply


async def test_the_stand_in_requires_its_key_and_serves_health_without_it(
    start_stand_in: StartStandIn,
) -> None:
    stand_in = await start_stand_in()

    async with stand_in.post("/api/check", json=check_body(ACCEPTED)) as without_key:
        assert without_key.status == 401
    async with stand_in.get("/health") as health:
        assert (health.status, await health.json()) == (200, {"status": "ok"})


async def test_diagnostics_follow_kiminas_debug_rule(start_stand_in: StartStandIn) -> None:
    stand_in = await start_stand_in()

    _status, plain = await ask(stand_in, ACCEPTED, TIMES_OUT)
    _status, debugged = await ask(stand_in, ACCEPTED, debug=True)

    assert "diagnostics" not in plain["results"][0]
    assert "diagnostics" in plain["results"][1]  # a result with an error keeps them
    assert debugged["results"][0]["diagnostics"] == {"repl_uuid": "stand-in-lean-a"}


async def test_the_stand_in_runs_no_more_checks_at_once_than_it_has_workers(
    start_stand_in: StartStandIn,
) -> None:
    stand_in = await start_stand_in(workers=2, seconds=0.1)

    started = time.monotonic()
    status_code, _reply = await ask(
        stand_in, *(f"theorem t{n} : True := trivial" for n in range(6))
    )

    assert status_code == 200
    assert time.monotonic() - started >= 0.3  # six checks, two at a time


def test_the_settings_are_read_from_the_environment() -> None:
    settings = StandInSettings.from_environment(
        {"STANDIN_NAME": "lean-b", "STANDIN_WORKERS": "4", "STANDIN_SECONDS": "0.5"}
    )

    assert settings == StandInSettings(name="lean-b", workers=4, seconds=0.5)
    assert (settings.host, settings.port, settings.api_key) == ("0.0.0.0", 8000, None)


# --- what the real cache makes of the stand-in's answers --------------------------------------


async def test_the_cache_stores_the_stand_ins_answers_and_not_its_timeouts_or_crashes(
    start_stand_in: StartStandIn, start_cache: StartCache
) -> None:
    stand_in = await start_stand_in()
    cache = await start_cache(str(stand_in.make_url("")).rstrip("/"))

    for code in (ACCEPTED, USES_SORRY, WRONG):
        assert "cached" not in await check(cache, code)
        assert (await check(cache, code, identifier="again"))["cached"] is True
    for _attempt in range(2):
        assert "cached" not in await check(cache, TIMES_OUT)
    async with cache.post("/api/check", json=check_body(CRASHES), headers=AUTHORIZED) as crashed:
        assert crashed.status == 500

    counters = await status(cache)
    assert (counters["hits"], counters["stored_entries"]) == (3, 3)


# --- the files the compose example mounts -----------------------------------------------------


def test_the_demos_haproxy_configuration_is_what_render_writes_for_its_server_list() -> None:
    servers = parse_server_list((DEMO / "servers").read_text())

    assert (DEMO / "haproxy.cfg").read_text() == render_haproxy_config(servers)


def test_the_compose_file_runs_the_servers_the_list_names() -> None:
    services = yaml.safe_load((DEMO / "compose.yaml").read_text())["services"]
    servers = parse_server_list((DEMO / "servers").read_text())
    cache_key = services["cache"]["environment"]["LEANPOOL_CACHE_API_KEY"]

    for server in servers:
        stand_in = services[server.host]
        assert (server.port, server.agent_port) == (8000, 18200)
        assert stand_in["environment"] == {
            "STANDIN_NAME": server.name,
            "STANDIN_WORKERS": str(server.workers),
            "STANDIN_API_KEY": cache_key,
        }
        agent = services[f"agent-{server.name.removeprefix('lean-')}"]
        assert agent["command"] == ["leanpool-agent"]
        assert agent["network_mode"] == f"service:{server.host}"
    assert services["haproxy"]["volumes"] == ["./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro"]
    assert services["cache"]["network_mode"] == "service:haproxy"


# --- the walkthrough, against the demo run as plain processes ----------------------------------

needs_a_real_pool = pytest.mark.skipif(
    HAPROXY is None or shutil.which("bash") is None or shutil.which("curl") is None,
    reason="the walkthrough needs haproxy, bash and curl",
)


def run_walkthrough(demo: NativeDemo, **environment: str) -> subprocess.CompletedProcess[str]:
    compose = f"{sys.executable} {Path(__file__).parent / 'native_demo.py'} {demo.directory}"
    return subprocess.run(
        ["bash", str(DEMO / "walkthrough.sh")],
        env={
            **os.environ,
            "LEANPOOL_DEMO_URL": demo.url,
            "LEANPOOL_DEMO_COMPOSE": compose,
            **environment,
        },
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


@pytest.fixture
def native_demo(tmp_path: Path) -> Iterator[NativeDemo]:
    assert HAPROXY is not None
    demo = NativeDemo.create(tmp_path, HAPROXY)
    demo.start_all()
    try:
        yield demo
    finally:
        demo.stop_all()


@needs_a_real_pool
def test_every_step_of_the_walkthrough_behaves_as_it_says(native_demo: NativeDemo) -> None:
    walkthrough = run_walkthrough(native_demo)

    assert walkthrough.returncode == 0, walkthrough.stdout + walkthrough.stderr
    assert "Every step behaved as described" in walkthrough.stdout


@needs_a_real_pool
def test_the_walkthrough_can_be_run_again_against_the_same_pool(native_demo: NativeDemo) -> None:
    first = run_walkthrough(native_demo)
    time.sleep(1.1)  # a run is told from the last one by the second it started in
    again = run_walkthrough(native_demo)

    assert first.returncode == 0, first.stdout + first.stderr
    assert again.returncode == 0, again.stdout + again.stderr


@needs_a_real_pool
def test_the_walkthrough_stops_at_the_first_step_that_does_not_behave(
    native_demo: NativeDemo,
) -> None:
    walkthrough = run_walkthrough(native_demo, LEANPOOL_DEMO_API_KEY="not-the-key")

    assert walkthrough.returncode == 1
    assert "walkthrough: the pool refused the API key" in walkthrough.stderr
    assert "== 2." not in walkthrough.stdout
