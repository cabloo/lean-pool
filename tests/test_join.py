"""The join service: its five requests, its refusals, and what it leaves in the spool.

These tests talk to the application in plain HTTP on loopback; that it is served over HTTPS
only, and that curl's pinning works against it, is shown on the real command in
``test_join_command.py``.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.serialization import Encoding

import leanpool.join.app
from leanpool.join import (
    MAXIMUM_REQUEST_BYTES,
    InvalidMessageError,
    JoinSettings,
    Spool,
    SpoolError,
    create_application,
    parse_signing_request,
)
from leanpool.pki import (
    create_authority,
    make_request,
    new_private_key,
    read_certificate,
    read_request,
    sign_request,
    subject_names,
)
from leanpool.pki.files import certificate_pem, private_key_pem

TOKEN = "4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4c"
API_KEY = "pool-key-not-to-be-logged"
SCRIPT = b"#!/bin/sh\necho 'joining the pool'\n"
NOT_FOUND = (404, {"detail": "Not Found"})

JoinClient = TestClient[web.Request, web.Application]
StartJoin = Callable[..., Awaitable[JoinClient]]


def signing_request_text(name: str = "lean-a") -> str:
    request = make_request(new_private_key(), name, subject_names([name]))
    return request.public_bytes(Encoding.PEM).decode("ascii")


CSR = signing_request_text()


def box_request(**changes: Any) -> dict[str, Any]:
    """What a box sends to join: its server's name, ports, workers and signing request."""
    request = {"name": "lean-a", "lean_port": 8000, "agent_port": 18200, "workers": 6, "csr": CSR}
    return {**request, **changes}


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    (directory / "requests").mkdir(parents=True)
    (directory / "responses").mkdir()
    return directory


@pytest.fixture
async def start_join(spool: Path, tmp_path: Path) -> AsyncIterator[StartJoin]:
    """Start join services on request, all over the same spool; all are closed afterwards."""
    clients: list[JoinClient] = []

    async def start(api_key: str | None = API_KEY) -> JoinClient:
        settings = JoinSettings(
            token=TOKEN, script=SCRIPT, spool=spool, api_key=api_key, tls_pem=tmp_path / "unused"
        )
        server = TestServer(create_application(settings))
        # As the command serves it: without the web framework's access log, which would write
        # every request line, token included.
        await server.start_server(access_log=None)
        client = TestClient(server)
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


@pytest.fixture
async def join(start_join: StartJoin) -> JoinClient:
    return await start_join()


async def reply(client: JoinClient, method: str, path: str, **options: Any) -> tuple[int, Any]:
    """Send one request and return its status and its body (JSON, text, or None)."""
    async with client.request(method, path, **options) as response:
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            return response.status, await response.json()
        text = await response.text()
        return response.status, text or None


def spooled(spool: Path) -> dict[str, Any]:
    """Every file under the spool's requests directory, parsed."""
    return {
        path.name: json.loads(path.read_text()) for path in sorted((spool / "requests").iterdir())
    }


def respond(spool: Path, name: str, body: Any) -> None:
    """Write a response file as the pool's operator side should: complete, in one step."""
    temporary = spool / "responses" / f".{name}.tmp"
    temporary.write_text(body if isinstance(body, str) else json.dumps(body))
    os.replace(temporary, spool / "responses" / name)


async def test_the_script_is_served_to_a_caller_with_the_token(join: JoinClient) -> None:
    async with join.get(f"/j/{TOKEN}") as response:
        assert response.status == 200
        assert await response.read() == SCRIPT
        assert response.headers["Content-Type"] == "text/x-shellscript; charset=utf-8"
        assert response.headers["Cache-Control"] == "no-store"


WRONG_TOKEN = "0" * len(TOKEN)
WITHOUT_THE_TOKEN = [
    ("GET", f"/j/{WRONG_TOKEN}"),
    ("POST", f"/j/{WRONG_TOKEN}/csr"),
    ("GET", f"/j/{WRONG_TOKEN}/certificate"),
    ("POST", f"/j/{WRONG_TOKEN}/ready"),
    ("GET", f"/j/{WRONG_TOKEN}/verdict"),
    ("GET", f"/j/{TOKEN[:-1]}"),  # one character short
    ("GET", f"/j/{TOKEN}0"),  # one character long
    ("GET", f"/j/{TOKEN.upper()}"),
    ("GET", "/j/"),
    ("GET", "/j"),
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", f"/{TOKEN}"),
    ("GET", f"/x/{TOKEN}"),
    ("GET", f"/j/x/{TOKEN}"),
    ("GET", f"/j//{TOKEN}"),
    ("GET", f"/j/{TOKEN}%00"),
]


@pytest.mark.parametrize(("method", "path"), WITHOUT_THE_TOKEN)
async def test_a_request_without_the_token_is_answered_404_whatever_it_asks(
    join: JoinClient, spool: Path, method: str, path: str
) -> None:
    assert await reply(join, method, path, json=box_request()) == NOT_FOUND
    assert spooled(spool) == {}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/j/{TOKEN}"),
        ("HEAD", f"/j/{TOKEN}"),
        ("GET", f"/j/{TOKEN}/"),
        ("GET", f"/j/{TOKEN}/csr"),
        ("PUT", f"/j/{TOKEN}/csr"),
        ("POST", f"/j/{TOKEN}/certificate"),
        ("GET", f"/j/{TOKEN}/ready"),
        ("POST", f"/j/{TOKEN}/verdict"),
        ("DELETE", f"/j/{TOKEN}/verdict"),
        ("GET", f"/j/{TOKEN}/status"),
        ("GET", f"/j/{TOKEN}/certificate/again"),
        ("GET", f"/j/{TOKEN}/certificate/"),
    ],
)
async def test_anything_but_the_five_requests_is_answered_404(
    join: JoinClient, spool: Path, method: str, path: str
) -> None:
    async with join.request(method, path, json=box_request()) as response:
        assert response.status == 404
    assert spooled(spool) == {}


async def test_a_wrong_token_and_a_wrong_path_get_the_same_answer(join: JoinClient) -> None:
    """Someone without the token cannot tell a closed window from a mistyped path."""
    answers = []
    for path in (f"/j/{WRONG_TOKEN}", f"/j/{WRONG_TOKEN}/certificate", "/elsewhere"):
        async with join.get(path) as response:
            headers = {name: value for name, value in response.headers.items() if name != "Date"}
            answers.append((response.status, headers, await response.read()))
    assert answers[0] == answers[1] == answers[2]


async def test_the_token_is_compared_in_constant_time(
    join: JoinClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Digests of equal length go to ``hmac.compare_digest``, whatever was sent."""
    compared: list[tuple[int, int]] = []
    real_compare = hmac.compare_digest

    def recording(left: bytes, right: bytes) -> bool:
        compared.append((len(left), len(right)))
        return real_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", recording)
    for candidate in ("", "x", WRONG_TOKEN, TOKEN, TOKEN * 20):
        await reply(join, "GET", f"/j/{candidate}")
    await reply(join, "GET", "/")
    assert compared == [(32, 32)] * 6


async def test_a_signing_request_is_written_to_the_spool_with_the_callers_address(
    join: JoinClient, spool: Path
) -> None:
    assert await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request()) == (
        202,
        {"status": "received"},
    )
    assert spooled(spool) == {"csr.json": {**box_request(), "address": "127.0.0.1"}}
    written = spool / "requests" / "csr.json"
    assert stat.S_IMODE(written.stat().st_mode) == 0o644
    assert sorted(os.listdir(spool / "requests")) == ["csr.json"]  # no temporary file is left


async def test_the_address_is_the_connections_and_never_a_headers(
    join: JoinClient, spool: Path
) -> None:
    headers = {"X-Forwarded-For": "192.0.2.9", "Forwarded": "for=192.0.2.9"}
    status, _body = await reply(
        join, "POST", f"/j/{TOKEN}/csr", json=box_request(), headers=headers
    )
    assert status == 202
    assert spooled(spool)["csr.json"]["address"] == "127.0.0.1"


async def test_the_same_signing_request_again_is_accepted_and_changes_nothing(
    join: JoinClient, spool: Path
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    before = (spool / "requests" / "csr.json").read_bytes()
    assert (await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request()))[0] == 202
    assert (spool / "requests" / "csr.json").read_bytes() == before


@pytest.mark.parametrize(
    "change", [{"name": "lean-b"}, {"workers": 7}, {"csr": signing_request_text()}]
)
async def test_a_different_signing_request_in_the_same_window_is_refused(
    join: JoinClient, spool: Path, change: dict[str, Any]
) -> None:
    """One box per window: what the pool's operator side has seen never changes under it."""
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    before = (spool / "requests" / "csr.json").read_bytes()
    assert await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request(**change)) == (
        409,
        {"reason": "another signing request was already received in this window"},
    )
    assert (spool / "requests" / "csr.json").read_bytes() == before


EVERY_REQUEST = [
    ("GET", ""),
    ("POST", "/csr"),
    ("GET", "/certificate"),
    ("POST", "/ready"),
    ("GET", "/verdict"),
]
BOUND_ELSEWHERE = (409, {"reason": "this join window is bound to another address"})


@pytest.mark.parametrize(("method", "what"), EVERY_REQUEST)
async def test_once_bound_every_request_from_another_address_is_answered_409(
    join: JoinClient, spool: Path, method: str, what: str
) -> None:
    """The window here was bound by a box at 192.0.2.9; this caller is at 127.0.0.1."""
    elsewhere = {**box_request(), "address": "192.0.2.9"}
    (spool / "requests" / "csr.json").write_text(json.dumps(elsewhere))
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    respond(spool, "verdict.json", {"admitted": True, "detail": "38 of 38"})

    assert await reply(join, method, f"/j/{TOKEN}{what}", json=box_request()) == BOUND_ELSEWHERE

    assert spooled(spool) == {"csr.json": elsewhere}


async def test_the_first_signing_request_binds_the_window_to_its_address(
    join: JoinClient, spool: Path
) -> None:
    """Two callers on two addresses of this machine: the first is the window's box."""
    try:
        connector = aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))
        second_caller = aiohttp.ClientSession(connector=connector)
        url = f"http://127.0.0.1:{join.port}/j/{TOKEN}"
        async with second_caller, second_caller.get(url) as response:
            assert response.status == 200  # before anyone is bound, the script is anyone's
    except OSError:
        pytest.skip("this machine has no second loopback address to call from")

    assert (await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request()))[0] == 202
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})

    connector = aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))
    async with aiohttp.ClientSession(connector=connector) as second_caller:
        for method, what in EVERY_REQUEST:
            async with second_caller.request(method, f"{url}{what}", json=box_request()) as answer:
                assert (answer.status, await answer.json()) == BOUND_ELSEWHERE
                assert API_KEY not in await answer.text()
    assert spooled(spool)["csr.json"]["address"] == "127.0.0.1"
    assert (await reply(join, "GET", f"/j/{TOKEN}/certificate"))[0] == 200


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "exactly name, lean_port, agent_port, workers, csr"),
        ([box_request()], "exactly name, lean_port"),
        ("lean-a", "exactly name, lean_port"),
        ({key: value for key, value in box_request().items() if key != "csr"}, "exactly name"),
        (box_request(address="192.0.2.9"), "exactly name"),
        (box_request(host="192.0.2.9"), "exactly name"),
        (box_request(name=7), "name must be a string"),
        (box_request(name=""), "name"),
        (box_request(name="lean a"), "name"),
        (box_request(name="lean_a"), "it must be a DNS name"),
        (box_request(name="lean-a\n    server evil 192.0.2.1:1"), "name"),
        (box_request(name="a" * 64), "name"),
        (box_request(lean_port="8000"), "lean_port must be a whole number"),
        (box_request(lean_port=8000.0), "lean_port must be a whole number"),
        (box_request(lean_port=True), "lean_port must be a whole number"),
        (box_request(lean_port=0), "server port 0 is outside 1-65535"),
        (box_request(agent_port=65536), "agent port 65536 is outside 1-65535"),
        (box_request(agent_port=None), "agent_port must be a whole number"),
        (box_request(agent_port=8000), "lean_port and agent_port must differ"),
        (box_request(workers=0), "workers must be between 1 and 256"),
        (box_request(workers=257), "workers must be between 1 and 256"),
        (box_request(workers="6"), "workers must be a whole number"),
        (box_request(csr=None), "csr must be a PEM signing request"),
        (box_request(csr=""), "csr must be one PEM signing request"),
        (box_request(csr="not a request"), "csr must be one PEM signing request"),
        (box_request(csr=CSR + CSR), "csr must be one PEM signing request"),
        (box_request(csr=CSR.replace("REQUEST", "REQUÊTE")), "csr must be a PEM signing request"),
    ],
)
async def test_a_malformed_signing_request_is_refused_and_nothing_is_written(
    join: JoinClient, spool: Path, body: Any, reason: str
) -> None:
    status, answer = await reply(join, "POST", f"/j/{TOKEN}/csr", json=body)
    assert status == 422
    assert reason in answer["detail"]
    assert spooled(spool) == {}
    with pytest.raises(InvalidMessageError):
        parse_signing_request(body)


async def test_a_certificate_or_a_private_key_is_not_a_signing_request(
    join: JoinClient, spool: Path
) -> None:
    authority = create_authority()
    for text in (certificate_pem(authority.certificate), private_key_pem(authority.private_key)):
        body = box_request(csr=text.decode("ascii"))
        assert (await reply(join, "POST", f"/j/{TOKEN}/csr", json=body))[0] == 422
    assert spooled(spool) == {}


@pytest.mark.parametrize("body", [b"", b"{", b"\xff\xfe", b"[" * 10_000])
async def test_a_body_that_is_not_json_is_refused(
    join: JoinClient, spool: Path, body: bytes
) -> None:
    status, answer = await reply(join, "POST", f"/j/{TOKEN}/csr", data=body)
    assert (status, answer) == (422, {"detail": "the body is not valid JSON"})
    assert spooled(spool) == {}


def padded_request(size: int) -> bytes:
    """A valid request of exactly ``size`` bytes: JSON may have spaces after it."""
    body = json.dumps(box_request()).encode()
    return body + b" " * (size - len(body))


async def test_a_body_of_16_kib_is_accepted_and_one_byte_more_is_not(
    join: JoinClient, spool: Path
) -> None:
    assert MAXIMUM_REQUEST_BYTES == 16 * 1024
    too_large = padded_request(MAXIMUM_REQUEST_BYTES + 1)
    status, answer = await reply(join, "POST", f"/j/{TOKEN}/csr", data=too_large)
    assert (status, answer) == (413, {"detail": "a request body may be at most 16384 bytes"})
    assert spooled(spool) == {}

    largest = padded_request(MAXIMUM_REQUEST_BYTES)
    assert (await reply(join, "POST", f"/j/{TOKEN}/csr", data=largest))[0] == 202
    assert (await reply(join, "POST", f"/j/{TOKEN}/ready", data=too_large))[0] == 413
    assert sorted(spooled(spool)) == ["csr.json"]


async def test_there_is_no_certificate_while_the_pool_has_not_decided(
    join: JoinClient, spool: Path
) -> None:
    assert await reply(join, "GET", f"/j/{TOKEN}/certificate") == (204, None)
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    assert await reply(join, "GET", f"/j/{TOKEN}/certificate") == (204, None)
    # A file that is still being written is not an answer yet.
    respond(spool, "certificate.json", '{"certificate": "-----BEGIN CERTIF')
    assert await reply(join, "GET", f"/j/{TOKEN}/certificate") == (204, None)


async def test_a_signed_certificate_is_handed_over_with_the_authoritys_and_the_pools_key(
    join: JoinClient, spool: Path
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    async with join.get(f"/j/{TOKEN}/certificate") as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {
            "certificate": "CERTIFICATE",
            "ca": "AUTHORITY",
            "api_key": API_KEY,
        }


async def test_a_pool_without_a_key_hands_over_none(start_join: StartJoin, spool: Path) -> None:
    join = await start_join(api_key=None)
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    status, body = await reply(join, "GET", f"/j/{TOKEN}/certificate")
    assert (status, body["api_key"]) == (200, None)


async def test_a_refused_signing_request_is_answered_409_with_the_reason(
    join: JoinClient, spool: Path
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", {"reason": "the name lean-a is already in the pool"})
    assert await reply(join, "GET", f"/j/{TOKEN}/certificate") == (
        409,
        {"reason": "the name lean-a is already in the pool"},
    )


async def test_an_answer_left_over_in_the_spool_is_given_to_nobody_before_a_signing_request(
    join: JoinClient, spool: Path
) -> None:
    """The pool's key goes to the box the window is bound to, and a window starts unbound."""
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    respond(spool, "verdict.json", {"admitted": True, "detail": "from an earlier window"})
    assert await reply(join, "GET", f"/j/{TOKEN}/certificate") == (204, None)
    assert await reply(join, "GET", f"/j/{TOKEN}/verdict") == (204, None)


@pytest.mark.parametrize(
    "answer",
    [
        {"certificate": "CERTIFICATE"},
        {"ca": "AUTHORITY"},
        {"certificate": "", "ca": "AUTHORITY"},
        {"certificate": 7, "ca": "AUTHORITY"},
        {"reason": ""},
        {},
        ["CERTIFICATE", "AUTHORITY"],
        None,
    ],
)
async def test_an_answer_of_the_wrong_shape_is_an_error_and_hands_over_nothing(
    join: JoinClient, spool: Path, caplog: pytest.LogCaptureFixture, answer: Any
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", answer)
    status, body = await reply(join, "GET", f"/j/{TOKEN}/certificate")
    assert (status, body) == (500, {"detail": "the join service cannot use its spool"})
    assert "certificate.json in the spool is not usable" in caplog.text


async def test_ready_is_refused_until_a_certificate_has_been_issued(
    join: JoinClient, spool: Path
) -> None:
    assert await reply(join, "POST", f"/j/{TOKEN}/ready") == (
        409,
        {"reason": "no signing request has been received in this window"},
    )
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    not_issued = (409, {"reason": "no certificate has been issued in this window"})
    assert await reply(join, "POST", f"/j/{TOKEN}/ready") == not_issued
    respond(spool, "certificate.json", {"reason": "refused"})
    assert await reply(join, "POST", f"/j/{TOKEN}/ready") == not_issued
    assert sorted(spooled(spool)) == ["csr.json"]


async def test_ready_is_written_to_the_spool_once(join: JoinClient, spool: Path) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})

    assert await reply(join, "POST", f"/j/{TOKEN}/ready") == (202, {"status": "received"})
    assert spooled(spool)["ready.json"] == {"address": "127.0.0.1"}
    before = (spool / "requests" / "ready.json").stat().st_mtime_ns
    # Again, with a body this time: it is accepted, and nothing in it is used.
    assert (await reply(join, "POST", f"/j/{TOKEN}/ready", json={"name": "other"}))[0] == 202
    assert (spool / "requests" / "ready.json").stat().st_mtime_ns == before
    assert sorted(os.listdir(spool / "requests")) == ["csr.json", "ready.json"]


@pytest.mark.parametrize(
    "verdict",
    [
        {"admitted": True, "detail": "38 of 38 cases behaved"},
        {"admitted": False, "detail": "reject/false_claim.lean was verified"},
    ],
)
async def test_the_verdict_is_handed_over_when_there_is_one(
    join: JoinClient, spool: Path, verdict: dict[str, Any]
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    await reply(join, "POST", f"/j/{TOKEN}/ready")
    assert await reply(join, "GET", f"/j/{TOKEN}/verdict") == (204, None)
    respond(spool, "verdict.json", '{"admitted": tr')
    assert await reply(join, "GET", f"/j/{TOKEN}/verdict") == (204, None)

    respond(spool, "verdict.json", {**verdict, "seconds": 41.5})

    assert await reply(join, "GET", f"/j/{TOKEN}/verdict") == (200, verdict)


@pytest.mark.parametrize(
    "verdict", [{"admitted": "yes", "detail": ""}, {"admitted": True}, {"detail": "ok"}, [], None]
)
async def test_a_verdict_of_the_wrong_shape_is_an_error(
    join: JoinClient, spool: Path, verdict: Any
) -> None:
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    respond(spool, "verdict.json", verdict)
    assert (await reply(join, "GET", f"/j/{TOKEN}/verdict"))[0] == 500


async def test_a_restarted_service_carries_on_from_what_is_in_the_spool(
    start_join: StartJoin, spool: Path
) -> None:
    first = await start_join()
    await reply(first, "POST", f"/j/{TOKEN}/csr", json=box_request())
    await first.close()
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})

    second = await start_join()

    assert (await reply(second, "GET", f"/j/{TOKEN}/certificate"))[0] == 200
    assert (await reply(second, "POST", f"/j/{TOKEN}/csr", json=box_request()))[0] == 202
    other = box_request(name="lean-b")
    assert (await reply(second, "POST", f"/j/{TOKEN}/csr", json=other))[0] == 409


async def test_a_whole_join_with_a_real_signing_request_and_a_real_authority(
    join: JoinClient, spool: Path, tmp_path: Path
) -> None:
    """The box's side and the pool's operator side, with the service between them."""
    authority = create_authority()
    box_key = new_private_key()
    made = make_request(box_key, "lean-a", subject_names(["lean-a"]))
    body = box_request(csr=made.public_bytes(Encoding.PEM).decode("ascii"))

    assert (await reply(join, "GET", f"/j/{TOKEN}"))[0] == 200
    assert (await reply(join, "POST", f"/j/{TOKEN}/csr", json=body))[0] == 202

    # The operator side: sign what the spool holds, for the name it validated and no other.
    request_file = tmp_path / "received.csr"
    request_file.write_text(spooled(spool)["csr.json"]["csr"])
    signed = sign_request(authority, read_request(request_file), subject_names(["lean-a"]))
    answer = {
        "certificate": certificate_pem(signed).decode("ascii"),
        "ca": certificate_pem(authority.certificate).decode("ascii"),
    }
    respond(spool, "certificate.json", answer)

    status, handed = await reply(join, "GET", f"/j/{TOKEN}/certificate")
    assert (status, handed) == (200, {**answer, "api_key": API_KEY})
    certificate_file = tmp_path / "lean-a.crt"
    certificate_file.write_text(handed["certificate"])
    assert read_certificate(certificate_file).public_key() == box_key.public_key()

    assert (await reply(join, "POST", f"/j/{TOKEN}/ready"))[0] == 202
    respond(spool, "verdict.json", {"admitted": True, "detail": "admitted with 6 workers"})
    assert await reply(join, "GET", f"/j/{TOKEN}/verdict") == (
        200,
        {"admitted": True, "detail": "admitted with 6 workers"},
    )
    assert sorted(spooled(spool)) == ["csr.json", "ready.json"]


async def test_neither_the_token_nor_the_api_key_is_ever_logged(
    join: JoinClient, spool: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await reply(join, "GET", f"/j/{TOKEN}")
    await reply(join, "GET", f"/j/{WRONG_TOKEN}")
    await reply(join, "POST", f"/j/{TOKEN}/csr", data=b"not json")
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request())
    await reply(join, "POST", f"/j/{TOKEN}/csr", json=box_request(name="lean-b"))
    respond(spool, "certificate.json", ["wrong shape"])
    await reply(join, "GET", f"/j/{TOKEN}/certificate")
    respond(spool, "certificate.json", {"certificate": "CERTIFICATE", "ca": "AUTHORITY"})
    await reply(join, "GET", f"/j/{TOKEN}/certificate")
    await reply(join, "POST", f"/j/{TOKEN}/ready")
    respond(spool, "verdict.json", {"admitted": True, "detail": "ok"})
    await reply(join, "GET", f"/j/{TOKEN}/verdict")
    (spool / "requests" / "csr.json").write_text("damaged")
    await reply(join, "GET", f"/j/{TOKEN}/verdict")

    assert len(caplog.records) >= 8  # the service does log what happened
    assert "the certificate was handed to 127.0.0.1" in caplog.text
    for record in caplog.records:
        logged = record.getMessage() + str(record.args) + str(record.exc_info)
        assert TOKEN not in logged
        assert API_KEY not in logged
        assert "/j/" not in logged  # no path at all


async def test_an_unexpected_failure_is_answered_500_without_a_traceback_in_the_log(
    join: JoinClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_self: Spool) -> None:
        raise RuntimeError(f"something went wrong at /j/{TOKEN}")

    monkeypatch.setattr(Spool, "bound_address", fail)
    assert await reply(join, "GET", f"/j/{TOKEN}") == (
        500,
        {"detail": "the join service failed"},
    )
    assert "a request from 127.0.0.1 failed: RuntimeError" in caplog.text
    assert TOKEN not in caplog.text


def test_the_service_executes_nothing() -> None:
    """It moves files and answers requests: nothing in it can start a process."""
    package = Path(leanpool.join.app.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text()
        for forbidden in ("subprocess", "os.system", "os.exec", "os.spawn", "os.popen", "eval("):
            assert forbidden not in text, f"{source.name} mentions {forbidden}"
        assert "exec(" not in text.replace("executes", "")


def test_the_spool_creates_its_requests_directory_and_refuses_one_it_cannot_write(
    tmp_path: Path,
) -> None:
    Spool(tmp_path / "fresh").prepare()
    assert (tmp_path / "fresh" / "requests").is_dir()
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    (locked / "requests").mkdir(parents=True)
    (locked / "requests").chmod(0o500)
    with pytest.raises(SpoolError, match="cannot write requests into"):
        Spool(locked).prepare()
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the spool should be")
    with pytest.raises(SpoolError, match="cannot create"):
        Spool(blocked).prepare()


def test_a_damaged_request_file_is_an_error_not_an_unbound_window(spool: Path) -> None:
    for damage in ("not json", "[]", '{"name": "lean-a"}', '{"address": 7}'):
        (spool / "requests" / "csr.json").write_text(damage)
        with pytest.raises(SpoolError):
            Spool(spool).bound_address()
