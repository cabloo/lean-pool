"""The admission test through TLS: a Lean server tested alone behind a front that wants the
pool proxy's client certificate, and every way that can be refused.

The servers here are in-process, on loopback, with throwaway certificates made by the project's
own certificate code. The same against a real HAProxy front is in ``test_tls_live.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from admit_support import admit, answering, honest_lean
from support import API_KEY, StartLeanServer
from tls_support import ThrowawayAuthority, tls_server

import leanpool.admit.runner
from leanpool.admit import AdmissionTlsError, load_admission_tls
from leanpool.admit.cli import main
from leanpool.admit.tls import describe_client_certificate, describe_server_certificate
from leanpool.pki import Usage

Captured = pytest.CaptureFixture[str]
PROXY = "lean-pool-proxy"
TLS_FLAGS = ("--tls-ca-file", "--tls-client-pem", "--tls-server-name")


@pytest.fixture
def pool(tmp_path: Path) -> ThrowawayAuthority:
    """The pool's authority, which signs the box's certificate and the proxy's."""
    return ThrowawayAuthority.create(tmp_path / "pool")


@pytest.fixture
def other(tmp_path: Path) -> ThrowawayAuthority:
    """An authority the pool knows nothing of."""
    return ThrowawayAuthority.create(tmp_path / "other")


def front_context(server_pem: Path, trusted: ThrowawayAuthority) -> ssl.SSLContext:
    """What a box's TLS front does at the TLS level: TLS 1.3, and a client certificate that
    ``trusted`` signed is required.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(server_pem))
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(trusted.certificate_path))
    return context


def tls_options(authority: ThrowawayAuthority, client_pem: Path, name: str) -> list[str]:
    values = (str(authority.certificate_path), str(client_pem), name)
    return [word for option in zip(TLS_FLAGS, values, strict=True) for word in option]


async def test_a_server_behind_tls_is_admitted_when_dialled_by_address(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """The certificate names the server, not its address: nothing in it says 127.0.0.1."""
    lean = await start_lean_server(
        answering(honest_lean), ssl_context=front_context(pool.server("lean-a"), pool)
    )
    url = f"https://127.0.0.1:{lean.port}"
    options = tls_options(pool, pool.client(PROXY), "lean-a")

    exit_status, report = await admit(url, cases_directory, key_file, capsys, *options)

    assert exit_status == 0
    assert (report["admitted"], report["server"]) == (True, url)
    assert report["counts"]["behaved"] == 3
    assert len(lean.requests) == 3
    for request in lean.requests:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in json.dumps(report)


async def test_the_name_is_checked_whatever_host_is_dialled(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    lean = await start_lean_server(
        answering(honest_lean), ssl_context=front_context(pool.server("lean-a"), pool)
    )
    # Dialled by a host name the certificate does not carry, and named in another letter case.
    options = tls_options(pool, pool.client(PROXY), "Lean-A")
    url = f"https://localhost:{lean.port}"
    exit_status, _report = await admit(url, cases_directory, key_file, capsys, *options)
    assert exit_status == 0


async def refused(
    lean_port: int,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
    options: list[str],
) -> str:
    """Run an admission that must fail before any check, and return why it says it did."""
    url = f"https://127.0.0.1:{lean_port}"
    started = time.monotonic()
    exit_status, report = await admit(url, cases_directory, key_file, capsys, *options)
    assert exit_status == 3
    assert report == {"server": url, "admitted": False, "error": report["error"]}
    assert time.monotonic() - started < 30  # it neither hangs nor tries again and again
    assert report["error"].startswith(f"TLS with {url}: ")
    error: str = report["error"]
    return error


async def test_a_server_with_another_boxs_name_fails_admission_and_says_so(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    lean = await start_lean_server(ssl_context=front_context(pool.server("lean-a"), pool))
    options = tls_options(pool, pool.client(PROXY), "lean-b")

    error = await refused(lean.port, cases_directory, key_file, capsys, options)

    assert "the server's certificate is not for the name 'lean-b' given as" in error
    assert "--tls-server-name" in error
    assert (lean.requests, lean.health_checks) == ([], 0)


async def test_a_server_of_another_authority_fails_admission_and_says_so(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    other: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """The right name, signed by an authority that is not the one in ``--tls-ca-file``."""
    lean = await start_lean_server(ssl_context=front_context(other.server("lean-a"), pool))
    options = tls_options(pool, pool.client(PROXY), "lean-a")

    error = await refused(lean.port, cases_directory, key_file, capsys, options)

    assert "was not signed by the authority in --tls-ca-file" in error
    assert (lean.requests, lean.health_checks) == ([], 0)


async def test_a_server_with_an_expired_certificate_fails_admission_and_says_so(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    expired, _certificate = pool.issue("lean-a", Usage.SERVER, expired=True)
    lean = await start_lean_server(ssl_context=front_context(expired, pool))
    options = tls_options(pool, pool.client(PROXY), "lean-a")

    error = await refused(lean.port, cases_directory, key_file, capsys, options)

    assert "the server's certificate has expired" in error
    assert (lean.requests, lean.health_checks) == ([], 0)


async def test_a_client_certificate_the_server_refuses_fails_admission_and_says_why(
    pool: ThrowawayAuthority,
    other: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """The server here answers a certificate it refuses with a TLS alert, as HAProxy does.
    (An asyncio server just closes, which the next test covers.)
    """
    expired, _certificate = pool.issue("expired-proxy", Usage.CLIENT, expired=True)
    presented = {
        # A box's certificate is a server certificate: TLS refuses it from a client.
        "it is not a client certificate (TLS alert: unsupported certificate)": pool.server(
            "lean-b"
        ),
        "it was not signed by the authority the server trusts (TLS alert: unknown ca)": (
            other.client(PROXY)
        ),
        "it has expired (TLS alert: certificate expired)": expired,
    }
    box = pool.server("lean-a")
    with tls_server(box, client_authority_file=pool.certificate_path) as (port, failures):
        for reason, client_pem in presented.items():
            options = tls_options(pool, client_pem, "lean-a")
            error = await refused(port, cases_directory, key_file, capsys, options)
            assert error.endswith(
                f"the server refused the client certificate in --tls-client-pem: {reason}"
            )
    assert len(failures) == len(presented)  # one connection each: nothing was tried again


Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
StartFront = Callable[[Handler, ssl.SSLContext | None], Awaitable[int]]


@pytest.fixture
async def start_front() -> AsyncIterator[StartFront]:
    """Start loopback servers that answer each connection with ``handler``; all are closed."""
    servers: list[asyncio.Server] = []

    async def start(handler: Handler, context: ssl.SSLContext | None) -> int:
        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=context)
        servers.append(server)
        port: int = server.sockets[0].getsockname()[1]
        return port

    yield start
    for server in servers:
        server.close()


async def test_the_api_key_is_not_sent_to_a_front_that_closes_without_answering(
    start_front: StartFront,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """A box's front accepts a client certificate of the pool's authority at the TLS level
    and then closes on any that is not the proxy's. The TLS handshake has succeeded by then,
    as far as the client can tell: only the missing answer says otherwise.
    """
    received: list[bytes] = []

    async def take_one_request_and_close(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(TimeoutError, OSError):
            received.append(await asyncio.wait_for(reader.read(65536), 5.0))
        writer.close()

    port = await start_front(take_one_request_and_close, front_context(pool.server("lean-a"), pool))
    options = tls_options(pool, pool.client("someone-else"), "lean-a")

    error = await refused(port, cases_directory, key_file, capsys, options)

    assert "closed the connection after the TLS handshake without an answer" in error
    assert "a client certificate that is not the pool proxy's (--tls-client-pem)" in error
    assert len(received) == 1  # one connection, and no second try
    assert received[0].startswith(b"GET /health HTTP/1.1\r\n")
    assert b"Authorization" not in received[0]
    assert API_KEY.encode() not in received[0]


async def test_a_port_that_does_not_speak_tls_1_3_fails_admission_and_says_so(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    options = tls_options(pool, pool.client(PROXY), "lean-a")
    plain = await start_lean_server()  # a Lean server published without its front
    old_context = front_context(pool.server("lean-a"), pool)
    old_context.minimum_version = ssl.TLSVersion.TLSv1_2
    old_context.maximum_version = ssl.TLSVersion.TLSv1_2
    old = await start_lean_server(ssl_context=old_context)

    for lean in (plain, old):
        error = await refused(lean.port, cases_directory, key_file, capsys, options)
        # Either OpenSSL's word for what came back, or a connection closed mid-handshake.
        assert re.search(
            r"(the TLS handshake failed \(\w+\)|the connection was closed during the TLS "
            r"handshake); is this the port of a TLS front\?$",
            error,
        )
        assert (lean.requests, lean.health_checks) == ([], 0)


async def test_a_server_that_stays_silent_fails_admission_instead_of_hanging(
    start_front: StartFront,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silent before the handshake, and silent after it: both end after a fixed wait."""
    monkeypatch.setattr(leanpool.admit.runner, "_HEALTH_TIMEOUT_SECONDS", 0.5)
    connections: list[asyncio.StreamWriter] = []

    async def say_nothing(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(writer)
        with contextlib.suppress(OSError):
            await reader.read()  # until the caller gives up and closes
        writer.close()

    options = tls_options(pool, pool.client(PROXY), "lean-a")
    ports = [
        await start_front(say_nothing, None),
        await start_front(say_nothing, front_context(pool.server("lean-a"), pool)),
    ]
    for port in ports:
        url = f"https://127.0.0.1:{port}"
        started = time.monotonic()
        exit_status, report = await admit(url, cases_directory, key_file, capsys, *options)
        assert exit_status == 3
        assert report["error"].startswith(f"{url} did not answer: TimeoutError")
        assert time.monotonic() - started < 4.0
    assert len(connections) == 2  # one try each


async def test_a_server_that_is_not_there_exits_with_status_three(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    lean = await start_lean_server(ssl_context=front_context(pool.server("lean-a"), pool))
    url = f"https://127.0.0.1:{lean.port}"
    await lean.close()
    options = tls_options(pool, pool.client(PROXY), "lean-a")
    exit_status, report = await admit(url, cases_directory, key_file, capsys, *options)
    assert exit_status == 3
    assert report["error"].startswith(f"{url} did not answer: ConnectionRefusedError")


def usage_error(arguments: list[str], environment: dict[str, str], capsys: Captured) -> str:
    with pytest.raises(SystemExit) as exit_information:
        main(arguments, environment)
    assert exit_information.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


@pytest.mark.parametrize(
    "given",
    [
        ["--tls-ca-file"],
        ["--tls-client-pem"],
        ["--tls-server-name"],
        ["--tls-ca-file", "--tls-client-pem"],
        ["--tls-ca-file", "--tls-server-name"],
        ["--tls-client-pem", "--tls-server-name"],
    ],
)
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_some_tls_options_without_the_others_are_a_usage_error(
    pool: ThrowawayAuthority,
    cases_directory: Path,
    capsys: Captured,
    given: list[str],
    scheme: str,
) -> None:
    every = dict(zip(TLS_FLAGS, tls_options(pool, pool.client(PROXY), "lean-a")[1::2], strict=True))
    options = [word for flag in given for word in (flag, every[flag])]
    arguments = ["--server", f"{scheme}://127.0.0.1:8000", "--cases", str(cases_directory)]
    error = usage_error([*arguments, *options], {}, capsys)
    missing = ", ".join(flag for flag in TLS_FLAGS if flag not in given)
    assert (
        "TLS needs --tls-ca-file, --tls-client-pem and --tls-server-name together; "
        f"missing: {missing}"
    ) in error


async def test_an_https_server_without_the_tls_options_is_refused_before_any_request(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """Never trusted through the system's authorities, and never reached without a name."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(pool.server("lean-a", addresses=("127.0.0.1",))))
    lean = await start_lean_server(ssl_context=context)
    arguments = ["--server", f"https://127.0.0.1:{lean.port}", "--cases", str(cases_directory)]
    arguments += ["--api-key-file", str(key_file)]

    error = await asyncio.to_thread(usage_error, arguments, {}, capsys)

    assert "an https:// server needs --tls-ca-file, --tls-client-pem and --tls-server-name" in error
    assert (lean.requests, lean.health_checks) == ([], 0)


async def test_an_http_server_with_the_tls_options_is_refused_before_any_request(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    """Options that ask for TLS never end in a check, or a key, sent in plain HTTP."""
    lean = await start_lean_server(answering(honest_lean))
    arguments = ["--server", lean.url, "--cases", str(cases_directory)]
    arguments += ["--api-key-file", str(key_file), *tls_options(pool, pool.client(PROXY), "lean-a")]

    error = await asyncio.to_thread(usage_error, arguments, {}, capsys)

    assert f"with the TLS options --server must be an https:// URL, got {lean.url!r}" in error
    assert (lean.requests, lean.health_checks) == ([], 0)


def test_unusable_tls_files_and_names_are_usage_errors_that_quote_no_file(
    pool: ThrowawayAuthority, cases_directory: Path, tmp_path: Path, capsys: Captured
) -> None:
    proxy = pool.client(PROXY)
    authority_file = pool.certificate_path
    key_only = pool.directory / f"{PROXY}.key"
    certificate_only = pool.directory / f"{PROXY}.crt"
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.write_text("")
    unusable = [
        ((missing, proxy, "lean-a"), f"cannot read --tls-ca-file {missing}"),
        ((empty, proxy, "lean-a"), f"--tls-ca-file {empty} does not hold a PEM certificate"),
        ((key_only, proxy, "lean-a"), f"--tls-ca-file {key_only} does not hold a PEM certificate"),
        ((authority_file, missing, "lean-a"), f"cannot read --tls-client-pem {missing}"),
        (
            (authority_file, certificate_only, "lean-a"),
            f"--tls-client-pem {certificate_only} does not hold a certificate followed by its",
        ),
        (
            (authority_file, key_only, "lean-a"),
            f"--tls-client-pem {key_only} does not hold a certificate followed by its",
        ),
        ((authority_file, proxy, "127.0.0.1"), "--tls-server-name '127.0.0.1' must be a DNS name"),
        ((authority_file, proxy, "lean_a"), "--tls-server-name 'lean_a' must be a DNS name"),
        ((authority_file, proxy, "*.example"), "--tls-server-name '*.example' must be a DNS name"),
    ]
    secret_line = key_only.read_text().splitlines()[1]
    arguments = ["--server", "https://127.0.0.1:8000", "--cases", str(cases_directory)]
    for values, reason in unusable:
        options = [word for pair in zip(TLS_FLAGS, map(str, values), strict=True) for word in pair]
        error = usage_error([*arguments, *options], {}, capsys)
        assert reason in " ".join(error.split())
        assert secret_line not in error


@pytest.mark.parametrize(
    "url", ["https://", "https://:8000", "https://lean-a:0", "https://lean-a:port", "https://léan"]
)
def test_an_https_url_without_a_usable_host_and_port_is_a_usage_error(
    pool: ThrowawayAuthority, cases_directory: Path, capsys: Captured, url: str
) -> None:
    options = tls_options(pool, pool.client(PROXY), "lean-a")
    error = usage_error(["--server", url, "--cases", str(cases_directory), *options], {}, capsys)
    assert "must be https://HOST:PORT" in error


async def test_the_tls_options_can_come_from_the_environment(
    start_lean_server: StartLeanServer,
    pool: ThrowawayAuthority,
    cases_directory: Path,
    key_file: Path,
    capsys: Captured,
) -> None:
    lean = await start_lean_server(
        answering(honest_lean), ssl_context=front_context(pool.server("lean-a"), pool)
    )
    environment = {
        "LEANPOOL_ADMIT_SERVER": f"https://127.0.0.1:{lean.port}",
        "LEANPOOL_ADMIT_CASES": str(cases_directory),
        "LEANPOOL_ADMIT_API_KEY_FILE": str(key_file),
        "LEANPOOL_ADMIT_TLS_CA_FILE": str(pool.certificate_path),
        "LEANPOOL_ADMIT_TLS_CLIENT_PEM": str(pool.client(PROXY)),
        "LEANPOOL_ADMIT_TLS_SERVER_NAME": "lean-b",
    }
    capsys.readouterr()
    assert await asyncio.to_thread(main, [], environment) == 3  # the name from the environment
    assert "is not for the name 'lean-b'" in json.loads(capsys.readouterr().out)["error"]
    # A flag wins over its variable.
    assert await asyncio.to_thread(main, ["--tls-server-name", "lean-a"], environment) == 0
    assert json.loads(capsys.readouterr().out)["admitted"] is True


def test_only_the_given_authority_is_trusted_and_nothing_below_tls_1_3_is_spoken(
    pool: ThrowawayAuthority,
) -> None:
    tls = load_admission_tls(pool.certificate_path, pool.client(PROXY), "Lean-A")
    assert tls.server_name == "lean-a"
    assert len(tls.context.get_ca_certs()) == 1  # the pool's; none of the system's
    assert tls.context.verify_mode == ssl.CERT_REQUIRED
    assert tls.context.check_hostname
    assert tls.context.minimum_version == ssl.TLSVersion.TLSv1_3
    with pytest.raises(AdmissionTlsError, match="must be a DNS name"):
        load_admission_tls(pool.certificate_path, pool.client(PROXY), "192.0.2.7")


def verification_error(code: int, message: str) -> ssl.SSLCertVerificationError:
    """What Python's ``ssl`` raises when a server's certificate does not verify."""
    error = ssl.SSLCertVerificationError(1, f"certificate verify failed: {message}")
    error.verify_code, error.verify_message = code, message
    return error


@pytest.mark.parametrize(
    ("code", "openssl_says", "message"),
    [
        (62, "Hostname mismatch", "is not for the name 'lean-a' given as --tls-server-name"),
        (20, "unable to get local issuer certificate", "was not signed by the authority in"),
        (19, "self-signed certificate in certificate chain", "was not signed by the authority in"),
        (18, "self-signed certificate", "was not signed by the authority in --tls-ca-file"),
        (7, "certificate signature failure", "was not signed by the authority in --tls-ca-file"),
        (10, "certificate has expired", "has expired"),
        (9, "certificate is not yet valid", "is not valid yet"),
        (26, "unsuitable certificate purpose", "was refused"),
    ],
)
def test_a_refused_server_certificate_is_described_by_what_is_wrong_with_it(
    code: int, openssl_says: str, message: str
) -> None:
    described = describe_server_certificate(verification_error(code, openssl_says), "lean-a")
    assert described.startswith(f"the server's certificate {message}")
    assert described.endswith(f"(OpenSSL: {openssl_says})")  # and OpenSSL's own words


@pytest.mark.parametrize(
    ("reason", "meaning", "alert"),
    [
        ("TLSV13_ALERT_CERTIFICATE_REQUIRED", "the server received no client certificate", ""),
        ("SSLV3_ALERT_UNSUPPORTED_CERTIFICATE", "it is not a client certificate", ""),
        ("TLSV1_ALERT_UNKNOWN_CA", "it was not signed by the authority the server trusts", ""),
        ("SSLV3_ALERT_CERTIFICATE_EXPIRED", "it has expired", "certificate expired"),
        ("SSLV3_ALERT_BAD_CERTIFICATE", "the server could not use it", "bad certificate"),
        ("SSLV3_ALERT_CERTIFICATE_UNKNOWN", "the server did not accept it", ""),
        ("TLSV1_ALERT_ACCESS_DENIED", "the server denied it access", "access denied"),
    ],
)
def test_a_refused_client_certificate_is_described_by_the_alert_the_server_sent(
    reason: str, meaning: str, alert: str
) -> None:
    error = ssl.SSLError(1, f"[SSL: {reason}] an alert")
    error.reason = reason
    described = describe_client_certificate(error)
    assert described.startswith(
        f"the server refused the client certificate in --tls-client-pem: {meaning} (TLS alert: "
    )
    assert alert in described


def test_any_other_tls_failure_after_the_handshake_is_reported_in_openssls_word() -> None:
    error = ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] bad record")
    error.reason = "DECRYPTION_FAILED_OR_BAD_RECORD_MAC"
    assert describe_client_certificate(error) == (
        "the server ended the TLS connection without an answer "
        "(DECRYPTION_FAILED_OR_BAD_RECORD_MAC)"
    )
