"""The cache's HTTP interface: Kimina's ``POST /api/check``, plus ``/status`` and ``/health``."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator

from aiohttp import web

from leanpool.cache.request import InvalidRequestError, parse_check_request
from leanpool.cache.service import CheckService
from leanpool.cache.settings import CacheSettings
from leanpool.cache.store import AsyncResultStore
from leanpool.cache.upstream import UpstreamClient, UpstreamFailure, new_upstream_session
from leanpool.signals import PRIORITY_HEADER, is_background

_SETTINGS = web.AppKey("settings", CacheSettings)
_SERVICE = web.AppKey("service", CheckService)

_BEARER_PREFIX = "Bearer "
_UNAUTHORIZED = 401
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
    """
    settings = request.app[_SETTINGS]
    refusal = _refuse_unauthenticated(request, settings.api_key)
    if refusal is not None:
        return refusal
    if settings.hop_header in request.headers:
        return _error(_LOOP_DETECTED, "this request has already passed through the cache")
    try:
        check_request = parse_check_request(await _read_json(request))
    except InvalidRequestError as error:
        return _error(_UNPROCESSABLE, str(error))
    service = request.app[_SERVICE]
    authorization = request.headers.get("Authorization")
    background = is_background(request.headers.get(PRIORITY_HEADER))
    outcomes = await asyncio.gather(
        *(
            service.check(snippet, check_request, authorization, background=background)
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


async def _read_json(request: web.Request) -> object:
    try:
        body: object = await request.json()
    except ValueError:
        raise InvalidRequestError("the body is not valid JSON") from None
    return body


def _error(status: int, detail: str) -> web.Response:
    """An error in the shape Kimina's own errors have: ``{"detail": "..."}``."""
    return web.json_response({"detail": detail}, status=status)
