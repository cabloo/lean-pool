"""A stand-in for a Kimina Lean Server. It does NOT run Lean.

The demo's two "Lean servers" are this program, so that the pool can be tried in a minute
instead of after building a Lean and Mathlib image. It speaks the part of Kimina's HTTP
interface lean-pool relies on (``POST /api/check`` and ``GET /health``, the optional Bearer key,
one status for a whole request, diagnostics only when ``debug`` is set). It waits as long as a
check might take and then answers from markers in the code instead of checking it:

    the code contains       the answer is
    ``sorry``               Lean's warning for a proof that assumes a ``sorry``
    ``standin: error``      an error message, as for a wrong proof
    ``standin: timeout``    Kimina's reply to a Lean timeout: HTTP 200 with an ``error``
    ``standin: crash``      HTTP 500, Kimina's reply when a worker crashed
    anything else           accepted, with an info message naming this server

Never use it to judge a proof. It exists to show what the pool does with each kind of answer.

Settings, as environment variables:

    STANDIN_NAME      the name in its answers (default: the host name)
    STANDIN_HOST      the address to listen on (default 0.0.0.0)
    STANDIN_PORT      the port to listen on (default 8000)
    STANDIN_WORKERS   checks it runs at once, like Kimina's LEAN_SERVER_MAX_REPLS (default 4)
    STANDIN_SECONDS   how long every check takes (default 2.0)
    STANDIN_API_KEY   the Bearer key it requires (default: none)
"""

from __future__ import annotations

import asyncio
import hmac
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import web

SORRY_MARKER = "sorry"
ERROR_MARKER = "standin: error"
TIMEOUT_MARKER = "standin: timeout"
CRASH_MARKER = "standin: crash"

_LARGEST_REQUEST_BYTES = 16 * 1024**2
# Lean says where in the file each message and each sorry is; the stand-in always says "the start".
_POSITION = {"line": 1, "column": 0}
_DEFAULT_LEAN_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class StandInSettings:
    """What one stand-in server is called and how it behaves."""

    name: str
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    seconds: float = 2.0
    api_key: str | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> StandInSettings:
        """Read the settings from ``STANDIN_*`` variables."""
        return cls(
            name=environment.get("STANDIN_NAME") or socket.gethostname(),
            host=environment.get("STANDIN_HOST", "0.0.0.0"),
            port=int(environment.get("STANDIN_PORT", "8000")),
            workers=int(environment.get("STANDIN_WORKERS", "4")),
            seconds=float(environment.get("STANDIN_SECONDS", "2.0")),
            api_key=environment.get("STANDIN_API_KEY") or None,
        )


class WorkerCrashedError(Exception):
    """The code asked for a crashed worker: the whole request is answered with HTTP 500."""


_SETTINGS = web.AppKey("settings", StandInSettings)
_WORKERS = web.AppKey("workers", asyncio.Semaphore)


def create_application(settings: StandInSettings) -> web.Application:
    """Build the stand-in server."""
    application = web.Application(client_max_size=_LARGEST_REQUEST_BYTES)
    application[_SETTINGS] = settings
    application[_WORKERS] = asyncio.Semaphore(settings.workers)
    application.router.add_post("/api/check", _handle_check)
    application.router.add_post("/api/check/", _handle_check)
    application.router.add_get("/health", _handle_health)
    return application


def answer_for(code: str, settings: StandInSettings, lean_timeout: int) -> dict[str, Any]:
    """Return one result without its id, as Kimina shapes it, from the markers in ``code``."""
    diagnostics = {"repl_uuid": f"stand-in-{settings.name}"}
    if CRASH_MARKER in code:
        raise WorkerCrashedError
    if TIMEOUT_MARKER in code:
        return {
            "time": float(lean_timeout),
            "error": f"Lean REPL command timed out in {lean_timeout} seconds",
            "diagnostics": diagnostics,
        }
    if ERROR_MARKER in code:
        messages = [_message("error", "unsolved goals (says the stand-in, which ran no Lean)")]
        sorries: list[dict[str, Any]] = []
    elif SORRY_MARKER in code:
        messages = [_message("warning", "declaration uses 'sorry'")]
        sorries = [{"pos": _POSITION, "endPos": _POSITION, "goal": "⊢ the stand-in does not know"}]
    else:
        messages = [_message("info", f"stand-in {settings.name}: accepted without running Lean")]
        sorries = []
    return {
        "time": settings.seconds,
        "response": {"env": 0, "messages": messages, "sorries": sorries},
        "diagnostics": diagnostics,
    }


def _message(severity: str, text: str) -> dict[str, Any]:
    return {"severity": severity, "pos": _POSITION, "endPos": _POSITION, "data": text}


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _handle_check(request: web.Request) -> web.Response:
    settings = request.app[_SETTINGS]
    if not _has_the_key(request, settings.api_key):
        return web.json_response({"detail": "Invalid API key"}, status=401)
    try:
        body = await request.json()
        snippets = [(str(snippet["id"]), str(snippet["code"])) for snippet in body["snippets"]]
        lean_timeout = int(body.get("timeout", _DEFAULT_LEAN_TIMEOUT_SECONDS))
        with_diagnostics = bool(body.get("debug", False))
    except (ValueError, KeyError, TypeError, AttributeError):
        return web.json_response({"detail": "not a check request"}, status=422)
    try:
        results = await asyncio.gather(
            *(_check(request.app, identifier, code, lean_timeout) for identifier, code in snippets)
        )
    except WorkerCrashedError:
        return web.json_response({"detail": "a stand-in worker crashed"}, status=500)
    if not with_diagnostics:
        for result in results:
            if "response" in result:
                del result["diagnostics"]
    return web.json_response({"results": results})


async def _check(
    application: web.Application, identifier: str, code: str, lean_timeout: int
) -> dict[str, Any]:
    """Hold one worker for as long as a check takes, then answer."""
    settings = application[_SETTINGS]
    async with application[_WORKERS]:
        await asyncio.sleep(settings.seconds)
        return {"id": identifier, **answer_for(code, settings, lean_timeout)}


def _has_the_key(request: web.Request, api_key: str | None) -> bool:
    if api_key is None:
        return True
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(token.encode("utf-8"), api_key.encode("utf-8"))


def main() -> None:
    """Serve until stopped."""
    settings = StandInSettings.from_environment(os.environ)
    print(
        f"stand-in Lean server {settings.name!r}: {settings.host}:{settings.port}, "
        f"{settings.workers} workers, {settings.seconds} s per check. It does not run Lean.",
        flush=True,
    )
    web.run_app(
        create_application(settings),
        host=settings.host,
        port=settings.port,
        print=None,
        access_log=None,
    )


if __name__ == "__main__":
    main()
