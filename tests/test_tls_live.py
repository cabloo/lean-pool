"""The TLS configurations on running HAProxy processes, with throwaway certificates made by
the project's own certificate code. Everything listens on loopback; nothing needs Docker or the
network.

These tests are skipped where ``haproxy`` is not installed. ``test_haproxy_tls_config.py`` shows
what the configurations say; these show what HAProxy does with them: who is let in, who is
turned away, that a check and a usage reading both arrive through the encrypted hops, and
that the admission test reaches a Lean server alone through its box's front.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from admit_support import admit, answering, honest_lean
from fake_lean_server import FakeLeanServer, SnippetReply, lean_answer
from live_pool import HAPROXY, WORKERS, LivePool, eventually, free_port
from live_tls import (
    FRONT_DOOR_NAME,
    PROXY_CLIENT_NAME,
    BoxFront,
    Exchange,
    PoolCertificates,
    TlsLivePool,
    exchange,
)
from support import API_KEY, AUTHORIZED, StartLeanServer
from tls_support import ThrowawayAuthority

from leanpool.agent import AgentSettings, UsageAgent
from leanpool.pki import (
    Usage,
    issue_certificate,
    new_private_key,
    public_key_pin,
    read_certificate,
    save_identity,
    subject_names,
)

pytestmark = pytest.mark.skipif(HAPROXY is None, reason="HAProxy is not installed")

StartBox = Callable[..., Awaitable[BoxFront]]
StartTlsPool = Callable[..., Awaitable[TlsLivePool]]
CURL = shutil.which("curl")
GIBIBYTE = 1024**3
FULL_WEIGHT = 256
HEALTH_REQUEST = b"GET /health HTTP/1.1\r\nHost: box\r\nConnection: close\r\n\r\n"


@pytest.fixture
def certificates(tmp_path: Path) -> PoolCertificates:
    return PoolCertificates.create(tmp_path / "tls")


@pytest.fixture
async def start_box(tmp_path: Path, certificates: PoolCertificates) -> AsyncIterator[StartBox]:
    """Start boxes' TLS fronts on request; all are stopped afterwards."""
    boxes: list[BoxFront] = []

    async def start(
        name: str,
        lean_server: FakeLeanServer,
        agent_port: int | None = None,
        server_pem: Path | None = None,
        **durations: Any,
    ) -> BoxFront:
        box = BoxFront(
            directory=tmp_path,
            name=name,
            server_pem=server_pem or certificates.authority.server(name),
            authority_file=certificates.authority_file,
            lean_upstream_port=lean_server.port,
            agent_upstream_port=free_port() if agent_port is None else agent_port,
            **durations,
        )
        boxes.append(box)
        await box.start()
        return box

    yield start
    for box in boxes:
        box.stop()


@pytest.fixture
async def start_tls_pool(
    tmp_path: Path, certificates: PoolCertificates
) -> AsyncIterator[StartTlsPool]:
    """Create TLS pools on request (started unless told otherwise); all are stopped afterwards."""
    pools: list[TlsLivePool] = []

    async def start(*boxes: BoxFront, started: bool = True, **durations: Any) -> TlsLivePool:
        pool = TlsLivePool(tmp_path, [], certificates=certificates, boxes=list(boxes), **durations)
        pools.append(pool)
        if started:
            await pool.start()
        return pool

    yield start
    for pool in pools:
        await pool.close()


@pytest.fixture
async def session(certificates: PoolCertificates) -> AsyncIterator[aiohttp.ClientSession]:
    """A client that trusts the pool's authority, as every client of a TLS pool must."""
    async with certificates.session() as client_session:
        yield client_session


def proof(tag: object) -> str:
    return f"-- proof {tag}"


async def server_is(pool: LivePool, server: str, status: str) -> None:
    async def reached() -> bool:
        return (await pool.server_state("checkers", server))["status"].startswith(status)

    await eventually(reached)


async def test_a_check_flows_through_https_the_pool_and_a_boxs_tls_front(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    session: aiohttp.ClientSession,
) -> None:
    lean = await start_lean_server()
    pool = await start_tls_pool(await start_box("box-a", lean))
    await server_is(pool, "box-a", "UP")  # the health check passed, over mutual TLS

    first = await pool.check(session, proof("one"))
    repeat = await pool.check(session, proof("one"))

    assert "cached" not in first.result
    assert repeat.result["cached"] is True
    (request,) = lean.requests
    assert request.body["snippets"][0]["code"] == proof("one")
    assert request.headers["X-Lean-Pool-Hop"] == "checkers"
    assert request.headers["Authorization"] == AUTHORIZED["Authorization"]
    async with session.get(f"{pool.url}/health") as reply:
        up = {"status": "ok", "workers": WORKERS, "queued": 0, "servers": 1}
        assert (reply.status, await reply.json()) == (200, up)
        assert reply.headers["X-Lean-Pool-Workers"] == str(WORKERS)  # over HTTPS too
    async with session.get(f"{pool.url}/status", headers=AUTHORIZED) as reply:
        assert (await reply.json())["hits"] == 1


async def test_the_front_door_is_trusted_by_its_name_and_by_its_address(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    certificates: PoolCertificates,
) -> None:
    pool = await start_tls_pool(await start_box("box-a", await start_lean_server()))
    for name in (FRONT_DOOR_NAME, "127.0.0.1"):
        reply = await exchange(
            pool.public_port, certificates.client_context(), name, HEALTH_REQUEST
        )
        assert reply.received.startswith(b"HTTP/1.1 200 OK"), reply.error
    stranger = await exchange(
        pool.public_port, certificates.client_context(), "other.test", HEALTH_REQUEST
    )
    assert stranger.received == b""
    assert "Hostname mismatch" in stranger.error


async def test_a_client_that_does_not_trust_the_pools_authority_is_refused_at_the_front_door(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    tmp_path: Path,
) -> None:
    lean = await start_lean_server()
    pool = await start_tls_pool(await start_box("box-a", lean))
    await server_is(pool, "box-a", "UP")
    another_authority = ThrowawayAuthority.create(tmp_path / "another")
    untrusting = [
        ssl.create_default_context(),  # the system's authorities
        ssl.create_default_context(cafile=str(another_authority.certificate_path)),
    ]

    for context in untrusting:
        connector = aiohttp.TCPConnector(ssl=context)
        async with aiohttp.ClientSession(connector=connector) as stranger:
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await pool.check(stranger, proof("never sent"))

    assert lean.requests == []


async def test_the_front_door_answers_nothing_in_plain_http(
    start_lean_server: StartLeanServer, start_box: StartBox, start_tls_pool: StartTlsPool
) -> None:
    lean = await start_lean_server()
    pool = await start_tls_pool(await start_box("box-a", lean))
    plain_url = pool.url.replace("https://", "http://")

    async with aiohttp.ClientSession() as plain:
        with pytest.raises(aiohttp.ClientError):
            async with plain.post(f"{plain_url}/api/check", json={}, headers=AUTHORIZED):
                pass

    assert lean.requests == []


@pytest.fixture
async def running_agent(tmp_path: Path) -> AsyncIterator[UsageAgent]:
    """A usage agent on loopback whose ``/proc`` files the test writes."""
    write_box(tmp_path, busy_ticks=0, idle_ticks=0, available_bytes=64 * GIBIBYTE)
    agent = UsageAgent(
        dataclasses.replace(
            AgentSettings(),
            host="127.0.0.1",
            port=0,
            stat_path=tmp_path / "stat",
            meminfo_path=tmp_path / "meminfo",
        )
    )
    await agent.start()
    yield agent
    await agent.close()


def write_box(directory: Path, busy_ticks: int, idle_ticks: int, available_bytes: int) -> None:
    """Write the ``/proc`` files of a one-core box."""
    (directory / "stat").write_text(
        f"cpu  {busy_ticks} 0 0 {idle_ticks} 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0 0\n"
    )
    (directory / "meminfo").write_text(f"MemAvailable: {available_bytes // 1024} kB\n")


@pytest.mark.parametrize("port_kind", ["lean", "agent"])
async def test_a_boxs_front_lets_in_the_proxys_client_certificate_and_nothing_else(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    certificates: PoolCertificates,
    running_agent: UsageAgent,
    tmp_path: Path,
    port_kind: str,
) -> None:
    lean = await start_lean_server()
    box = await start_box("box-a", lean, agent_port=running_agent.port)
    port, request = (
        (box.lean_port, HEALTH_REQUEST) if port_kind == "lean" else (box.agent_port, b"")
    )
    pool = certificates.authority
    another_authority = ThrowawayAuthority.create(tmp_path / "another")

    async def present(client_pem: Path | None) -> Exchange:
        context = certificates.client_context(client_pem)
        return await exchange(port, context, "box-a", request)

    # The pool proxy's own certificate is let in: the refusals below are not a dead port.
    answer = (await present(certificates.proxy_client_pem)).received
    if port_kind == "lean":
        assert answer.startswith(b"HTTP/1.1 200 OK")
        assert lean.health_checks == 1
    else:
        assert answer == b"ready up\n"

    # Each caller, and the TLS alert that turns it away. The alert says why: TLS itself
    # refuses a missing certificate, a server certificate presented by a client (whatever
    # its name), and an unknown authority.
    server_in_the_proxys_name, _ = pool.issue(PROXY_CLIENT_NAME, Usage.SERVER, stem="impostor")
    refused_by_tls = {
        "no client certificate": (None, "CERTIFICATE_REQUIRED"),
        "another box's server certificate": (pool.server("box-b"), "UNSUPPORTED_CERTIFICATE"),
        "this box's own server certificate": (box.server_pem, "UNSUPPORTED_CERTIFICATE"),
        "a server certificate in the proxy's name": (
            server_in_the_proxys_name,
            "UNSUPPORTED_CERTIFICATE",
        ),
        "the proxy's name, signed by another authority": (
            another_authority.client(PROXY_CLIENT_NAME),
            "UNKNOWN_CA",
        ),
    }
    for caller, (client_pem, alert) in refused_by_tls.items():
        reply = await present(client_pem)
        assert reply.received == b"", caller
        assert alert in reply.error, f"{caller}: {reply.error}"

    # A real client certificate of the pool's authority, but not the proxy's: TLS accepts it
    # and the front's own rule closes the connection.
    someone_else = await present(pool.client("someone-else"))
    assert someone_else.received == b""
    assert "ALERT" not in someone_else.error

    # Nothing but the proxy's own request ever reached the Lean server.
    assert lean.health_checks == (1 if port_kind == "lean" else 0)
    assert lean.requests == []


async def test_a_box_must_present_a_certificate_for_its_own_name(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    session: aiohttp.ClientSession,
    certificates: PoolCertificates,
    tmp_path: Path,
) -> None:
    """Every box is at 127.0.0.1 here: only the name in its certificate tells them apart."""
    honest, misnamed, foreign, stale = [await start_lean_server() for _ in range(4)]
    authority = certificates.authority
    another_authority = ThrowawayAuthority.create(tmp_path / "another")
    box_a_again, _ = authority.issue("box-a", Usage.SERVER, stem="misnamed")
    expired, _ = authority.issue("box-d", Usage.SERVER, expired=True)
    pool = await start_tls_pool(
        await start_box("box-a", honest),
        # In the list as box-b, but presenting a certificate for box-a: a box that answers at
        # another box's address.
        await start_box("box-b", misnamed, server_pem=box_a_again),
        # The right name, signed by an authority the pool does not know.
        await start_box("box-c", foreign, server_pem=another_authority.server("box-c")),
        # The right name and the right authority, a month too late.
        await start_box("box-d", stale, server_pem=expired),
    )
    refused = ("box-b", "box-c", "box-d")
    await server_is(pool, "box-a", "UP")
    for name in refused:
        await server_is(pool, name, "DOWN")
    # HAProxy's own account of each health check: layer 7 (HTTP) passed for the honest box;
    # the others failed at layer 6, the TLS handshake.
    check_statuses = {
        name: (await pool.server_state("checkers", name))["check_status"]
        for name in ("box-a", *refused)
    }
    assert check_statuses == {"box-a": "L7OK", **dict.fromkeys(refused, "L6RSP")}

    replies = [await pool.check(session, proof(number)) for number in range(4)]

    assert [reply.status for reply in replies] == [200, 200, 200, 200]
    assert len(honest.requests) == 4
    assert misnamed.requests == foreign.requests == stale.requests == []
    assert misnamed.health_checks == foreign.health_checks == stale.health_checks == 0


async def test_haproxy_also_takes_a_certificates_common_name_for_the_servers_name(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    certificates: PoolCertificates,
    tmp_path: Path,
) -> None:
    """Why ``sign-csr`` chooses the common name itself and never takes it from a request.

    These certificates carry an alternative name that is not the server's. HAProxy still
    accepts the one whose common name is the server's name.
    """
    authority = certificates.authority.authority

    def certificate_for(common_name: str, stem: str) -> Path:
        private_key = new_private_key()
        certificate = issue_certificate(
            authority, private_key.public_key(), common_name, subject_names(["other"]), Usage.SERVER
        )
        save_identity(tmp_path, stem, certificate, private_key)
        return tmp_path / f"{stem}.pem"

    pool = await start_tls_pool(
        await start_box(
            "box-a", await start_lean_server(), server_pem=certificate_for("box-a", "by-cn")
        ),
        await start_box(
            "box-b", await start_lean_server(), server_pem=certificate_for("other", "neither")
        ),
    )
    await server_is(pool, "box-b", "DOWN")
    await server_is(pool, "box-a", "UP")


async def test_a_lean_server_that_is_gone_behind_its_front_costs_no_check(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    session: aiohttp.ClientSession,
) -> None:
    """The front still accepts the connection and then has nobody to pass it to. The pool
    proxy sees a connection closed without a reply, and sends the check to another server.
    """
    gone, healthy = await start_lean_server(), await start_lean_server()
    pool = await start_tls_pool(await start_box("box-a", gone), await start_box("box-b", healthy))
    await server_is(pool, "box-a", "UP")
    await server_is(pool, "box-b", "UP")
    await gone.close()

    replies = [await pool.check(session, proof(number)) for number in range(4)]

    assert [reply.status for reply in replies] == [200, 200, 200, 200]
    assert len(healthy.requests) == 4
    await server_is(pool, "box-a", "DOWN")


async def test_the_usage_reading_arrives_through_the_tunnel_and_changes_the_weight(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    running_agent: UsageAgent,
    tmp_path: Path,
) -> None:
    lean_a, lean_b = await start_lean_server(), await start_lean_server()
    pool = await start_tls_pool(
        await start_box("with-agent", lean_a, agent_port=running_agent.port),
        # Nothing listens behind this box's agent port.
        await start_box("without-agent", lean_b),
    )
    agent = running_agent

    async def reaches(status: str, weight: int) -> None:
        async def reached() -> bool:
            state = await pool.server_state("checkers", "with-agent")
            return (state["status"], state["weight"]) == (status, str(weight))

        await eventually(reached)

    # The agent check is pointed at this proxy's loopback tunnels, one per server.
    config = pool.render()
    first_tunnel, second_tunnel = pool.agent_tunnel_port, pool.agent_tunnel_port + 1
    assert f"agent-check agent-addr 127.0.0.1 agent-port {first_tunnel} " in config
    assert f"agent-check agent-addr 127.0.0.1 agent-port {second_tunnel} " in config

    write_box(tmp_path, busy_ticks=500, idle_ticks=500, available_bytes=64 * GIBIBYTE)
    agent.sampler.sample()
    assert agent.sampler.reply() == "ready up 50%\n"
    await reaches("UP", FULL_WEIGHT // 2)

    write_box(tmp_path, busy_ticks=1490, idle_ticks=510, available_bytes=64 * GIBIBYTE)
    agent.sampler.sample()
    assert agent.sampler.reply() == "ready up 1%\n"
    await reaches("UP", FULL_WEIGHT // 100)

    write_box(tmp_path, busy_ticks=1490, idle_ticks=1510, available_bytes=1 * GIBIBYTE)
    agent.sampler.sample()
    assert agent.sampler.reply() == "drain\n"
    await reaches("DRAIN (agent)", FULL_WEIGHT // 100)

    write_box(tmp_path, busy_ticks=1490, idle_ticks=2510, available_bytes=64 * GIBIBYTE)
    agent.sampler.sample()
    assert agent.sampler.reply() == "ready up 100%\n"
    await reaches("UP", FULL_WEIGHT)

    # A server whose agent cannot be reached keeps its configured weight and stays in rotation.
    unreached = await pool.server_state("checkers", "without-agent")
    assert (unreached["status"], unreached["weight"]) == ("UP", str(FULL_WEIGHT))


async def test_a_check_as_slow_as_the_lean_timeout_allows_is_not_cut_by_either_proxy(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    session: aiohttp.ClientSession,
) -> None:
    """Nothing is sent while Lean works: the silence must outlast neither proxy's patience."""
    durations: dict[str, Any] = {
        "lean_timeout_seconds": 1,
        "server_wait_seconds": 1,
        "margin_seconds": 3,
    }
    slowest_check_seconds = 1 + 2 * 1  # the wait for a worker, then header and body

    async def slow_answer(_snippet: dict[str, Any]) -> SnippetReply:
        await asyncio.sleep(slowest_check_seconds)
        return lean_answer()

    lean = await start_lean_server(slow_answer)
    pool = await start_tls_pool(await start_box("box-a", lean, **durations), **durations)
    await server_is(pool, "box-a", "UP")

    reply = await pool.check(session, proof("slow"), no_cache=True)

    assert reply.status == 200
    assert reply.seconds >= slowest_check_seconds
    assert len(lean.requests) == 1  # answered once: neither proxy gave up and tried again


@pytest.mark.skipif(CURL is None, reason="curl is not installed")
async def test_the_front_doors_pin_is_what_curl_accepts(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    start_tls_pool: StartTlsPool,
    certificates: PoolCertificates,
) -> None:
    assert CURL is not None
    pool = await start_tls_pool(await start_box("box-a", await start_lean_server()))
    await server_is(pool, "box-a", "UP")
    front_door = read_certificate(certificates.front_door_pem)

    def curl(pin: str) -> subprocess.CompletedProcess[str]:
        command = [CURL, "-fsSk", "--pinnedpubkey", pin, f"{pool.url}/health"]
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)

    right = await asyncio.to_thread(curl, public_key_pin(front_door))
    wrong = await asyncio.to_thread(
        curl, public_key_pin(certificates.authority.authority.certificate)
    )

    health = f'{{"status":"ok","workers":{WORKERS},"queued":0,"servers":1}}'
    assert (right.returncode, right.stdout) == (0, health)
    assert (wrong.returncode, wrong.stdout) == (90, "")


def admission_options(
    certificates: PoolCertificates, name: str, client_pem: Path | None = None
) -> list[str]:
    """The TLS options of ``leanpool-admit``: the pool's authority, a client certificate (the
    pool proxy's unless another is given) and the name the server must prove.
    """
    return [
        "--tls-ca-file",
        str(certificates.authority_file),
        "--tls-client-pem",
        str(client_pem or certificates.proxy_client_pem),
        "--tls-server-name",
        name,
    ]


async def test_admission_passes_through_a_boxs_front_dialled_by_address_with_the_boxs_name(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    certificates: PoolCertificates,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The server is tested alone, before any pool knows it. Its certificate names it
    ``box-a`` and says nothing of 127.0.0.1, the address it is dialled at.
    """
    lean = await start_lean_server(answering(honest_lean))
    box = await start_box("box-a", lean)
    url = f"https://127.0.0.1:{box.lean_port}"

    exit_status, report = await admit(
        url, cases_directory, key_file, capsys, *admission_options(certificates, "box-a")
    )

    assert exit_status == 0
    assert (report["admitted"], report["server"]) == (True, url)
    assert report["counts"] == {
        "cases": 3,
        "behaved": 3,
        "misbehaved": 0,
        "verify": 1,
        "reject": 2,
        "accepted": 1,
        "rejected": 2,
        "undecided": 0,
    }
    assert len(lean.requests) == 3
    for request in lean.requests:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"


async def test_admission_fails_through_a_boxs_front_when_the_name_is_another_boxs(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    certificates: PoolCertificates,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A box cannot be admitted under a name its certificate does not carry."""
    lean = await start_lean_server(answering(honest_lean))
    box = await start_box("box-a", lean)
    url = f"https://127.0.0.1:{box.lean_port}"

    exit_status, report = await admit(
        url, cases_directory, key_file, capsys, *admission_options(certificates, "box-b")
    )

    assert exit_status == 3
    assert report["admitted"] is False
    assert report["error"].startswith(
        f"TLS with {url}: the server's certificate is not for the name 'box-b' given as "
        "--tls-server-name"
    )
    assert (lean.requests, lean.health_checks) == ([], 0)


async def test_admission_fails_through_a_boxs_front_with_a_client_certificate_not_the_proxys(
    start_lean_server: StartLeanServer,
    start_box: StartBox,
    certificates: PoolCertificates,
    cases_directory: Path,
    key_file: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    lean = await start_lean_server(answering(honest_lean))
    box = await start_box("box-a", lean)
    url = f"https://127.0.0.1:{box.lean_port}"
    another_authority = ThrowawayAuthority.create(tmp_path / "another")
    not_the_proxys = {
        # A client certificate of the pool's own authority, in another name: TLS accepts it
        # and the front's rule closes the connection.
        "the server closed the connection after the TLS handshake without an answer": (
            certificates.authority.client("someone-else")
        ),
        # The box's own certificate: a server certificate, which TLS refuses from a client.
        "refused the client certificate in --tls-client-pem: it is not a client certificate": (
            box.server_pem
        ),
        # The proxy's name, signed by an authority the box does not trust.
        "refused the client certificate in --tls-client-pem: it was not signed by the "
        "authority the server trusts": another_authority.client(PROXY_CLIENT_NAME),
    }

    for reason, client_pem in not_the_proxys.items():
        options = admission_options(certificates, "box-a", client_pem)
        exit_status, report = await admit(url, cases_directory, key_file, capsys, *options)
        assert exit_status == 3, reason
        assert report["admitted"] is False
        assert report["error"].startswith(f"TLS with {url}: the server ")
        assert reason in report["error"]

    # Nothing from any of them reached the Lean server: no check, no key, not even a health
    # request.
    assert (lean.requests, lean.health_checks) == ([], 0)
