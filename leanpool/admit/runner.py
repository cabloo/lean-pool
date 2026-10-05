"""Running the admission cases against one Lean server and reporting what it did."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp

from leanpool.admit.cases import AdmissionCase, Expectation
from leanpool.admit.tls import AdmissionTls, TlsHandshakeError, probe
from leanpool.admit.verdict import (
    Observation,
    Verdict,
    behaved,
    expected_observation,
    judge_result,
    undecided,
)

_HEALTH_TIMEOUT_SECONDS = 10.0
# A check may wait for a free worker, then import its header, then run its body, and Kimina
# allows the header and the body the full Lean timeout each. The margin covers the wait.
_HTTP_MARGIN_SECONDS = 120.0
_OK = 200
_BODY_EXCERPT_LENGTH = 200


class ServerUnreachableError(Exception):
    """The Lean server did not answer its health check, so nothing could be tested.

    That includes a TLS handshake that failed: the message then says which side refused which
    certificate.
    """


@dataclass(frozen=True)
class AdmissionSettings:
    """Which server to test and how hard.

    ``timeout_seconds`` is the Lean timeout sent with every check. ``concurrency`` is how many
    checks are in flight at once; the reported checks per second are measured at that setting.
    ``tls`` is given for a server behind its box's TLS front (``leanpool.admit.tls``); without
    it the server is spoken to in plain HTTP.
    """

    server_url: str
    api_key: str | None
    timeout_seconds: int
    concurrency: int
    tls: AdmissionTls | None = None

    @property
    def server_name(self) -> str | None:
        """The name the server's certificate must carry; None without TLS."""
        return None if self.tls is None else self.tls.server_name


@dataclass(frozen=True)
class CaseOutcome:
    """What the server did with one case.

    ``seconds`` is the round trip as the client saw it (queueing on the server included);
    ``lean_seconds`` is the time Lean itself reported, when the server gave one.
    """

    case: str
    expected: Observation
    observed: Observation
    behaved: bool
    seconds: float
    lean_seconds: float | None
    detail: str


@dataclass(frozen=True)
class AdmissionReport:
    """The outcome of every case and the totals an operator decides on."""

    settings: AdmissionSettings
    elapsed_seconds: float
    outcomes: tuple[CaseOutcome, ...]

    @property
    def admitted(self) -> bool:
        """Whether every case behaved. One misbehaving case is enough to refuse the server."""
        return bool(self.outcomes) and all(outcome.behaved for outcome in self.outcomes)

    def to_json(self) -> dict[str, Any]:
        """Return the report as a JSON-serialisable object. The API key is never part of it."""
        return {
            "server": self.settings.server_url,
            "admitted": self.admitted,
            "timeout_seconds": self.settings.timeout_seconds,
            "concurrency": self.settings.concurrency,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "checks_per_second": _rate(len(self.outcomes), self.elapsed_seconds),
            "counts": self._counts(),
            "cases": [_outcome_to_json(outcome) for outcome in self.outcomes],
        }

    def _counts(self) -> dict[str, int]:
        observed = [outcome.observed for outcome in self.outcomes]
        expected = [outcome.expected for outcome in self.outcomes]
        return {
            "cases": len(self.outcomes),
            "behaved": sum(outcome.behaved for outcome in self.outcomes),
            "misbehaved": sum(not outcome.behaved for outcome in self.outcomes),
            "verify": expected.count(expected_observation(Expectation.VERIFY)),
            "reject": expected.count(expected_observation(Expectation.REJECT)),
            "accepted": observed.count(Observation.ACCEPTED),
            "rejected": observed.count(Observation.REJECTED),
            "undecided": observed.count(Observation.UNDECIDED),
        }


def unreachable_report(settings: AdmissionSettings, reason: str) -> dict[str, Any]:
    """Return the report for a server that could not be tested at all."""
    return {"server": settings.server_url, "admitted": False, "error": reason}


async def run_admission(
    settings: AdmissionSettings, cases: Sequence[AdmissionCase]
) -> AdmissionReport:
    """Send every case to the server, ``concurrency`` at a time, and judge each answer.

    Each case is sent once, as a plain single-snippet Kimina request, and is never retried: a
    server that needs a retry to give a definitive answer has not yet shown it gives one.
    Raises ``ServerUnreachableError`` if the server does not answer its health check.

    With TLS, a connection without the API key comes first (``probe``): the key is sent only
    once the server has proved its name and has answered a request made with our certificate.
    """
    await _require_tls(settings)
    http_timeout = aiohttp.ClientTimeout(total=2 * settings.timeout_seconds + _HTTP_MARGIN_SECONDS)
    async with aiohttp.ClientSession(
        timeout=http_timeout, headers=_headers(settings), connector=_connector(settings)
    ) as session:
        await _require_reachable(session, settings)
        slots = asyncio.Semaphore(settings.concurrency)
        started = time.perf_counter()
        outcomes = await asyncio.gather(
            *(_run_case(session, settings, slots, case) for case in cases)
        )
        elapsed_seconds = time.perf_counter() - started
    return AdmissionReport(settings, elapsed_seconds, tuple(outcomes))


def _headers(settings: AdmissionSettings) -> dict[str, str]:
    return {} if settings.api_key is None else {"Authorization": f"Bearer {settings.api_key}"}


def _connector(settings: AdmissionSettings) -> aiohttp.TCPConnector | None:
    """With TLS, every connection trusts the pool's authority only and presents our certificate."""
    return None if settings.tls is None else aiohttp.TCPConnector(ssl=settings.tls.context)


async def _require_tls(settings: AdmissionSettings) -> None:
    """With TLS, refuse to go on unless the server proves its name and accepts our certificate."""
    if settings.tls is None:
        return
    try:
        await probe(settings.server_url, settings.tls, _HEALTH_TIMEOUT_SECONDS)
    except TlsHandshakeError as error:
        raise ServerUnreachableError(f"TLS with {settings.server_url}: {error}") from error
    except (OSError, TimeoutError) as error:
        raise ServerUnreachableError(f"{settings.server_url} did not answer: {error!r}") from error


async def _require_reachable(session: aiohttp.ClientSession, settings: AdmissionSettings) -> None:
    health_url = f"{settings.server_url}/health"
    timeout = aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT_SECONDS)
    try:
        async with session.get(
            health_url, timeout=timeout, server_hostname=settings.server_name
        ) as response:
            status = response.status
    except (aiohttp.ClientError, TimeoutError) as error:
        raise ServerUnreachableError(f"{health_url} did not answer: {error!r}") from error
    if status != _OK:
        raise ServerUnreachableError(f"{health_url} answered HTTP {status}")


async def _run_case(
    session: aiohttp.ClientSession,
    settings: AdmissionSettings,
    slots: asyncio.Semaphore,
    case: AdmissionCase,
) -> CaseOutcome:
    async with slots:
        started = time.perf_counter()
        verdict, lean_seconds = await _check(session, settings, case)
        seconds = time.perf_counter() - started
    return CaseOutcome(
        case=case.name,
        expected=expected_observation(case.expectation),
        observed=verdict.observation,
        behaved=behaved(case.expectation, verdict.observation),
        seconds=seconds,
        lean_seconds=lean_seconds,
        detail=verdict.detail,
    )


async def _check(
    session: aiohttp.ClientSession, settings: AdmissionSettings, case: AdmissionCase
) -> tuple[Verdict, float | None]:
    """Send one case and return its verdict and the time Lean reported for it."""
    body = {
        "snippets": [{"id": case.name, "code": case.code}],
        "timeout": settings.timeout_seconds,
    }
    try:
        async with session.post(
            f"{settings.server_url}/api/check", json=body, server_hostname=settings.server_name
        ) as response:
            if response.status != _OK:
                excerpt = (await response.text())[:_BODY_EXCERPT_LENGTH]
                return undecided(f"HTTP {response.status}: {excerpt}"), None
            reply = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        return undecided(f"no usable reply: {error!r}"), None
    return _judge_reply(reply, case.name)


def _judge_reply(reply: object, case_name: str) -> tuple[Verdict, float | None]:
    result = _single_result(reply)
    if result is None:
        return undecided("the reply is not a single check result"), None
    if result.get("id") != case_name:
        return undecided(f"the reply is for a different snippet ({result.get('id')!r})"), None
    lean_seconds = result.get("time")
    return judge_result(result), lean_seconds if isinstance(lean_seconds, int | float) else None


def _single_result(reply: object) -> dict[str, Any] | None:
    """Return the one result of a single-snippet reply, or None if the reply is not one."""
    results = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return None
    result: dict[str, Any] = results[0]
    return result


def _rate(count: int, elapsed_seconds: float) -> float | None:
    return round(count / elapsed_seconds, 3) if elapsed_seconds > 0 else None


def _outcome_to_json(outcome: CaseOutcome) -> dict[str, Any]:
    return {
        "case": outcome.case,
        "expected": outcome.expected.value,
        "observed": outcome.observed.value,
        "behaved": outcome.behaved,
        "seconds": round(outcome.seconds, 3),
        "lean_seconds": outcome.lean_seconds,
        "detail": outcome.detail,
    }
