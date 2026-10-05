"""An example client: check Lean files through a lean-pool, sized by what the pool says.

    uv run python examples/pool_client.py --url http://127.0.0.1:18100 \
        --api-key-file api-key.txt proofs/*.lean

A pool speaks Kimina's interface, so any Kimina client works against it unchanged. This one
shows what a client can do because it is talking to a pool:

* It sends one file per request and keeps a little more in flight than the pool has workers.
  The pool says how many that is in ``GET /health`` and again in a header of every answer, so
  a Lean server that joins or drops is followed without a restart. What the pool says is
  advice: the client never goes above a ceiling of its own, and keeps its own number when the
  pool says nothing.
* It can mark its work as background (``--background``): such checks wait behind every normal
  check for a worker.
* It tells a verdict from no verdict. A check the pool did not take (HTTP 429, 502, 503 or 504,
  a lost connection) is asked again after a pause. A Lean timeout is reported as "no verdict"
  and is never counted as a failed proof.

It prints one JSON line per file. Copy what is useful: the header names and their readers are in
``leanpool.signals``, and ``leanpool.kimina`` reads one result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import ssl
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import aiohttp

from leanpool.kimina import IndefiniteResultError, read_lean_answer
from leanpool.signals import (
    BACKGROUND_PRIORITY,
    PRIORITY_HEADER,
    PoolCapacity,
    capacity_from_headers,
    capacity_from_health,
)

Outcome = Literal["verified", "rejected", "no verdict"]

# The statuses that mean "the pool did not take this check": ask again, record nothing. 429 is a
# Lean server that had no free worker within its own wait; the others are the proxy's and the
# cache's words for no Lean server having taken the check.
NOT_TAKEN_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class Verdict:
    """What the pool said about one file."""

    name: str
    outcome: Outcome
    cached: bool
    detail: str


@dataclass(frozen=True)
class ClientSettings:
    """How one client talks to a pool.

    ``in_flight`` is the number of requests kept in flight until the pool says its size, and
    whenever it does not. ``ceiling`` is never exceeded, whatever the pool says. ``margin`` is
    how much more than the pool's workers to keep in flight, so that no worker waits for the
    client. ``http_timeout_seconds`` must cover the wait in the pool's queue and the check
    together. Left out, it is what a pool rendered for this Lean timeout with the default waits
    allows a client (``http_wait_seconds``); the pool's generated ``haproxy.cfg`` states the
    numbers of a pool rendered otherwise in its first lines.
    """

    url: str
    api_key: str | None = None
    lean_timeout_seconds: int = 60
    background: bool = False
    in_flight: int = 4
    ceiling: int = 64
    margin: float = 1.25
    attempts: int = 5
    pause_seconds: float = 2.0
    http_timeout_seconds: float | None = None

    @property
    def http_wait_seconds(self) -> float:
        """How long to wait for one answer.

        A pool lets a check wait ``2 x Lean timeout + 30`` seconds in its queue and gives a Lean
        server ``60 + 2 x Lean timeout + 30`` seconds to answer it; its own limit for a client
        is those two and 30 seconds more. Giving up earlier would send again a check the pool
        is still working on.
        """
        if self.http_timeout_seconds is not None:
            return self.http_timeout_seconds
        return 4.0 * self.lean_timeout_seconds + 150.0


class RequestSlots:
    """A limit on the requests in flight that can change while requests are in flight."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._in_flight = 0
        self._changed = asyncio.Condition()

    @property
    def limit(self) -> int:
        """The number of requests that may be in flight now."""
        return self._limit

    async def resize(self, limit: int) -> None:
        """Change the limit. Requests already in flight finish; waiting ones are woken."""
        async with self._changed:
            self._limit = limit
            self._changed.notify_all()

    async def __aenter__(self) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: self._in_flight < self._limit)
            self._in_flight += 1

    async def __aexit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        async with self._changed:
            self._in_flight -= 1
            self._changed.notify_all()


class PoolClient:
    """Checks Lean files through one pool."""

    def __init__(self, session: aiohttp.ClientSession, settings: ClientSettings) -> None:
        self._session = session
        self._settings = settings
        self._url = settings.url.rstrip("/")
        self._slots = RequestSlots(min(settings.in_flight, settings.ceiling))
        self._headers = {}
        if settings.api_key is not None:
            self._headers["Authorization"] = f"Bearer {settings.api_key}"
        if settings.background:
            self._headers[PRIORITY_HEADER] = BACKGROUND_PRIORITY

    @property
    def in_flight_limit(self) -> int:
        """How many requests this client keeps in flight at the moment."""
        return self._slots.limit

    async def check_all(self, files: Mapping[str, str]) -> list[Verdict]:
        """Check every file (name to Lean code) and return the verdicts in the files' order."""
        await self._ask_the_pool_its_size()
        return list(
            await asyncio.gather(*(self._check(name, code) for name, code in files.items()))
        )

    async def _ask_the_pool_its_size(self) -> None:
        try:
            async with self._session.get(f"{self._url}/health") as response:
                await self._follow(capacity_from_health(await response.json()))
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return  # the first check will say what is wrong; until then, the configured number

    async def _follow(self, capacity: PoolCapacity | None) -> None:
        """Keep ``margin`` times the pool's workers in flight, within the ceiling."""
        if capacity is None or capacity.workers == 0:
            return
        wanted = min(self._settings.ceiling, math.ceil(capacity.workers * self._settings.margin))
        if wanted != self._slots.limit:
            await self._slots.resize(wanted)

    async def _check(self, name: str, code: str) -> Verdict:
        body = {
            "snippets": [{"id": name, "code": code}],
            "timeout": self._settings.lean_timeout_seconds,
        }
        detail = "the pool was not asked"
        async with self._slots:
            for attempt in range(self._settings.attempts):
                if attempt:
                    await asyncio.sleep(self._settings.pause_seconds)
                try:
                    async with self._session.post(
                        f"{self._url}/api/check", json=body, headers=self._headers
                    ) as response:
                        await self._follow(capacity_from_headers(response.headers))
                        if response.status in NOT_TAKEN_STATUSES:
                            detail = f"the pool answered HTTP {response.status}"
                            continue
                        if response.status != 200:
                            text = (await response.text())[:200]
                            return Verdict(
                                name, "no verdict", False, f"HTTP {response.status}: {text}"
                            )
                        result = (await response.json())["results"][0]
                except (aiohttp.ClientError, TimeoutError) as error:
                    detail = f"the pool could not be reached: {error!r}"
                    continue
                except (ValueError, LookupError, TypeError):
                    # A 200 that is not a check's reply says nothing about the proof, and
                    # asking again would get the same: this file has no verdict, the rest go on.
                    return Verdict(name, "no verdict", False, "the reply was not a check's result")
                return read_verdict(name, result)
        return Verdict(name, "no verdict", False, f"{detail}, {self._settings.attempts} times")


def read_verdict(name: str, result: Mapping[str, Any]) -> Verdict:
    """Read one result: Lean accepted the file, Lean rejected it, or it gave no verdict."""
    cached = result.get("cached") is True
    try:
        answer = read_lean_answer(result)
    except IndefiniteResultError as error:
        return Verdict(name, "no verdict", cached, str(error))
    if answer.has_errors:
        first_error = next(m.text for m in answer.messages if m.severity == "error")
        return Verdict(name, "rejected", cached, first_error.splitlines()[0][:200])
    if answer.uses_sorry:
        return Verdict(name, "rejected", cached, "the proof uses sorry")
    return Verdict(name, "verified", cached, "")


def new_session(settings: ClientSettings, ca_file: Path | None = None) -> aiohttp.ClientSession:
    """Create the HTTP session: no connection cap of its own, and the pool's CA if it has one."""
    context = ssl.create_default_context(cafile=ca_file) if ca_file is not None else None
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0, ssl=context if context is not None else True),
        timeout=aiohttp.ClientTimeout(total=settings.http_wait_seconds),
    )


def _parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Lean files through a lean-pool.")
    parser.add_argument("files", nargs="+", type=Path, help="the Lean files to check")
    parser.add_argument("--url", required=True, help="the pool's address, e.g. http://host:18100")
    parser.add_argument("--api-key-file", type=Path, help="a file holding the pool's API key")
    parser.add_argument("--ca-file", type=Path, help="the pool authority's certificate (TLS)")
    parser.add_argument("--lean-timeout", type=int, default=60, help="seconds Lean may take")
    parser.add_argument("--background", action="store_true", help="wait behind normal checks")
    parser.add_argument("--ceiling", type=int, default=64, help="never more requests in flight")
    return parser.parse_args(arguments)


async def _run(options: argparse.Namespace) -> int:
    api_key = options.api_key_file.read_text().strip() if options.api_key_file else None
    settings = ClientSettings(
        url=options.url,
        api_key=api_key,
        lean_timeout_seconds=options.lean_timeout,
        background=options.background,
        ceiling=options.ceiling,
    )
    files = {str(path): path.read_text(encoding="utf-8") for path in options.files}
    async with new_session(settings, options.ca_file) as session:
        verdicts = await PoolClient(session, settings).check_all(files)
    for verdict in verdicts:
        print(json.dumps(asdict(verdict)))
    return 0 if all(verdict.outcome != "no verdict" for verdict in verdicts) else 1


def main(arguments: Sequence[str] | None = None) -> int:
    """Check the files named on the command line; exit 1 if any got no verdict."""
    return asyncio.run(_run(_parse_arguments(sys.argv[1:] if arguments is None else arguments)))


if __name__ == "__main__":
    raise SystemExit(main())
