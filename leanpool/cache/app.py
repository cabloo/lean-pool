"""The cache's HTTP interface: Kimina's ``POST /api/check``, plus ``/status``, ``/health`` and
``/metrics/cache``.
"""

from __future__ import annotations

import asyncio
import enum
import hmac
import json
from collections.abc import AsyncIterator, Sequence

from aiohttp import web

from leanpool.cache.metrics import render_cache_metrics
from leanpool.cache.request import (
    InvalidRequestError,
    Snippet,
    parse_check_request,
    screened_refusal,
    with_timeout,
)
from leanpool.cache.service import CheckService
from leanpool.cache.settings import CacheSettings
from leanpool.cache.store import AsyncResultStore
from leanpool.cache.upstream import UpstreamClient, UpstreamFailure, new_upstream_session
from leanpool.doors import DOOR_HEADER, SCREENED_DOOR
from leanpool.metrics import CACHE_METRICS_PATH, CONTENT_TYPE
from leanpool.screen import screen
from leanpool.signals import PRIORITY_HEADER, is_background

_SETTINGS = web.AppKey("settings", CacheSettings)
_SERVICE = web.AppKey("service", CheckService)

_BEARER_PREFIX = "Bearer "
_UNAUTHORIZED = 401
_TOO_LARGE = 413
_UNPROCESSABLE = 422
_LOOP_DETECTED = 508


def create_application(settings: CacheSettings) -> web.Application:
    """Build the cache service. The store and the upstream session open when it starts."""
    application = web.Application(client_max_size=settings.maximum_request_bytes)
    application[_SETTINGS] = settings
    application.cleanup_ctx.append(_service_lifetime)
    application.router.add_post("/api/check", _handle_check)
    application.router.add_post("/api/check/", _handle_check)
    application.router.add_get("/status", _handle_status)
    application.router.add_get("/health", _handle_health)
    application.router.add_get(CACHE_METRICS_PATH, _handle_metrics)
    return application


async def _service_lifetime(application: web.Application) -> AsyncIterator[None]:
    settings = application[_SETTINGS]
    store = await AsyncResultStore.open(settings.database_path, settings.maximum_bytes)
    session = new_upstream_session(settings.upstream_timeout_seconds)
    application[_SERVICE] = CheckService(
        store=store,
        upstream=UpstreamClient(session, settings.upstream_url, settings.hop_header),
        pin=settings.pin,
        exhaustion_patterns=settings.exhaustion_patterns,
        screened_in_flight=settings.screened_in_flight,
    )
    yield
    await session.close()
    await store.close()


async def _handle_check(request: web.Request) -> web.Response:
    """Serve a check request: each snippet on its own, results in request order.

    Kimina answers a whole request with one status, so this does too: if any snippet could not
    be answered, the first such failure (in request order) is the reply, with the upstream's own
    status and body. The snippets that did get a definitive answer are already stored, so the
    client's retry costs the Lean servers only what was missing.

    A request sent with the screened key must keep to the screened door's fields and limits
    (``screened_refusal``, and a body of at most ``screened_max_request_bytes``, refused before
    more of it is read) and pass the screen (with the pool's ``screened_imports`` beside Mathlib
    and Aesop), which runs off the event loop, so a long file holds
    up no other request; one that names no timeout is given the door's longest. It is then
    served like any other, from the store
    or the servers, and shares stored answers with the pool's own checks (an answer is the
    servers' work, whoever asked). The checks it starts at the servers take places of their own
    (``CheckService``). Each key is taken at its own door alone (``leanpool.doors``): the
    screened key at the screened door, the pool's everywhere else.
    """
    settings = request.app[_SETTINGS]
    sender = _authenticate(request, settings)
    if isinstance(sender, web.Response):
        return sender
    if settings.hop_header in request.headers:
        return _error(_LOOP_DETECTED, "this request has already passed through the cache")
    screened = sender is _Sender.SCREENED
    limit = (
        min(settings.screened_max_request_bytes, settings.maximum_request_bytes)
        if screened
        else None
    )
    try:
        body = await _read_json(request, limit)
        check_request = parse_check_request(body)
    except _RequestTooLargeError as error:
        return _error(_TOO_LARGE, f"a check at the screened door is at most {error.limit} bytes")
    except InvalidRequestError as error:
        return _error(_UNPROCESSABLE, str(error))
    authorization = request.headers.get("Authorization")
    if screened:
        # parse_check_request took only an object, and settings with a screened key have a limit.
        assert isinstance(body, dict)
        assert settings.screened_in_flight is not None
        reason = screened_refusal(
            body,
            check_request,
            max_snippets=settings.screened_in_flight,
            max_timeout=settings.screened_max_timeout_seconds,
        )
        if reason is not None:
            return _error(_UNPROCESSABLE, reason)
        # Off the event loop: the screen is plain Python, and a long file takes it a while.
        finding = await asyncio.to_thread(
            _unscreened, check_request.snippets, settings.screened_imports
        )
        if finding is not None:
            return _error(_UNPROCESSABLE, finding)
        if check_request.timeout is None:
            check_request = with_timeout(check_request, settings.screened_max_timeout_seconds)
        # Forwarded with the pool's own key: no Lean server ever holds the screened one.
        authorization = f"{_BEARER_PREFIX}{settings.api_key}"
    service = request.app[_SERVICE]
    background = is_background(request.headers.get(PRIORITY_HEADER))
    outcomes = await asyncio.gather(
        *(
            service.check(
                snippet, check_request, authorization, background=background, screened=screened
            )
            for snippet in check_request.snippets
        )
    )
    for outcome in outcomes:
        if isinstance(outcome, UpstreamFailure):
            return web.Response(
                status=outcome.status,
                body=outcome.body,
                headers={"Content-Type": outcome.content_type},
            )
    return web.json_response({"results": outcomes})


async def _handle_status(request: web.Request) -> web.Response:
    settings = request.app[_SETTINGS]
    if _at_screened_door(request):
        return _error(_UNAUTHORIZED, "the screened door takes checks alone")
    refusal = _refuse_unauthenticated(request, settings.api_key)
    if refusal is not None:
        return refusal
    status = await request.app[_SERVICE].status()
    return web.json_response({**status, "maximum_bytes": settings.maximum_bytes})


async def _handle_health(_request: web.Request) -> web.Response:
    """Answer the proxy's health check. Unauthenticated: the proxy carries no key, and the
    reply says nothing but that the process is serving.
    """
    return web.json_response({"status": "ok"})


async def _handle_metrics(request: web.Request) -> web.Response:
    """Answer a Prometheus scrape with the numbers of ``/status``, as text.

    Unauthenticated, unlike ``/status``: a scraper is given no key, and the reply holds counts
    and sizes only. It reads the counters and changes nothing.
    """
    status = await request.app[_SERVICE].status()
    text = render_cache_metrics(status, request.app[_SETTINGS].maximum_bytes)
    return web.Response(body=text.encode("ascii"), headers={"Content-Type": CONTENT_TYPE})


class _Sender(enum.Enum):
    """Which key a check came with: the pool's own, or the screened one."""

    POOL = enum.auto()
    SCREENED = enum.auto()


def _at_screened_door(request: web.Request) -> bool:
    """Whether the proxy says this request came in by the screened door (``leanpool.doors``)."""
    return request.headers.get(DOOR_HEADER) == SCREENED_DOOR


def _authenticate(request: web.Request, settings: CacheSettings) -> _Sender | web.Response:
    """Return which key the request carries, or a 401.

    Each key is taken at its own door alone: the screened key only from a request the screened
    door forwarded, the pool's key only from one it did not. A token that is the other door's key
    is refused like any other. Both keys are compared, each in constant time, whichever matches,
    so the reply's timing says nothing about either key or about which one a token came close to.
    """
    screened_door = _at_screened_door(request)
    if settings.screened_api_key is None:
        refusal = _refuse_unauthenticated(request, settings.api_key)
        if refusal is not None:
            return refusal
        if screened_door:
            return _error(_UNAUTHORIZED, "the screened door takes the screened key alone")
        return _Sender.POOL
    authorization = request.headers.get("Authorization")
    if not authorization:
        return _error(_UNAUTHORIZED, "Missing API key")
    token = authorization.removeprefix(_BEARER_PREFIX).strip().encode("utf-8")
    pool = hmac.compare_digest(token, (settings.api_key or "").encode("utf-8"))
    screened = hmac.compare_digest(token, settings.screened_api_key.encode("utf-8"))
    if pool and not screened_door:
        return _Sender.POOL
    if screened and screened_door:
        return _Sender.SCREENED
    if pool:
        return _error(_UNAUTHORIZED, "the screened door takes the screened key alone")
    if screened:
        return _error(_UNAUTHORIZED, "the screened key is taken at the screened door alone")
    return _error(_UNAUTHORIZED, "Invalid API key")


def _unscreened(snippets: Sequence[Snippet], imports: Sequence[str]) -> str | None:
    """Return why the screen refuses the first snippet it refuses, or None if every one passes.

    One refused snippet refuses the whole request, before any of it is looked up or forwarded.
    ``imports`` are the roots the pool lets a screened check import beside Mathlib and Aesop.
    Run in a thread: it touches nothing but its arguments.
    """
    for snippet in snippets:
        finding = screen(snippet.code, imports)
        if finding is not None:
            return f"refused by the pool's screen: snippet {snippet.identifier!r} holds {finding}"
    return None


def _refuse_unauthenticated(request: web.Request, api_key: str | None) -> web.Response | None:
    """Return a 401 unless the request carries the pool's key (or the pool has none).

    The token is read the way Kimina reads it (an optional ``Bearer`` prefix) and compared in
    constant time, so the comparison's duration reveals nothing about the key.
    """
    if api_key is None:
        return None
    authorization = request.headers.get("Authorization")
    if not authorization:
        return _error(_UNAUTHORIZED, "Missing API key")
    token = authorization.removeprefix(_BEARER_PREFIX).strip()
    if not hmac.compare_digest(token.encode("utf-8"), api_key.encode("utf-8")):
        return _error(_UNAUTHORIZED, "Invalid API key")
    return None


class _RequestTooLargeError(Exception):
    """A body longer than its sender may send; nothing more of it is read."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit)
        self.limit = limit


async def _read_json(request: web.Request, limit: int | None = None) -> object:
    """The body, as JSON. With ``limit``, a body longer than that many bytes is refused as soon
    as it says so (its ``Content-Length``) or its bytes do."""
    try:
        if limit is None:
            body: object = await request.json()
        else:
            body = json.loads(await _read_at_most(request, limit))
    except ValueError:
        raise InvalidRequestError("the body is not valid JSON") from None
    return body


async def _read_at_most(request: web.Request, limit: int) -> bytes:
    if request.content_length is not None and request.content_length > limit:
        raise _RequestTooLargeError(limit)
    chunks, size = [], 0
    async for chunk in request.content.iter_any():
        size += len(chunk)
        if size > limit:
            raise _RequestTooLargeError(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _error(status: int, detail: str) -> web.Response:
    """An error in the shape Kimina's own errors have: ``{"detail": "..."}``."""
    return web.json_response({"detail": detail}, status=status)
