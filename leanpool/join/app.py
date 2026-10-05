"""The join service's HTTP interface: seven requests under ``/j/<token>``, 404 for everything else.

::

    GET  /j/<token>              200 the join script
    POST /j/<token>/csr          202; JSON {name, lean_port, agent_port, workers, csr}
    GET  /j/<token>/certificate  204 while undecided; 200 {certificate, ca, api_key}; 409 {reason}
    POST /j/<token>/ready        202
    GET  /j/<token>/verdict      204 while admission runs; 200 {admitted, detail}
    GET  /j/<token>/image.json   200 the manifest of the image this window ships; 204 if none
    GET  /j/<token>/image        200 or 206 the image file (``Range`` is honoured); 404 if none

The service executes nothing. It serves the script, writes what a box sends into the spool and
serves what the pool's operator side puts there (``leanpool.join.spool``).

* **The token.** A request whose path does not carry the window's token is answered 404, as is
  a request for anything but the seven above: someone without the token learns nothing, not even
  that a window is open. The comparison takes the same time whatever was sent.
* **One box per window.** The first signing request binds the window to the address it came
  from. From then on every request from another address is answered 409, whatever it asks.
  The two image requests bind nothing: a box may read the manifest and fetch the image, fail,
  and come back, and the window is still good.
* **The image is never read whole.** It is many GB. It is sent from disk in chunks of
  ``IMAGE_CHUNK_BYTES``, and a download that was cut short goes on from where it stopped.
* **Nothing secret is logged.** The token is part of every path, so no request line is ever
  logged: there is no access log, and what is logged names the caller's address and what
  happened, never a path, a body or the API key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from leanpool.join.image import IMAGE_CHUNK_BYTES, shipped_image
from leanpool.join.messages import InvalidMessageError, parse_json, parse_signing_request
from leanpool.join.settings import MAXIMUM_REQUEST_BYTES, JoinSettings
from leanpool.join.spool import Spool, SpoolError, Stored

logger = logging.getLogger(__name__)

_SETTINGS = web.AppKey("settings", JoinSettings)
_SPOOL = web.AppKey("spool", Spool)

_PREFIX = "j"
_NO_STORE = {"Cache-Control": "no-store"}
_ACCEPTED = 202
_NO_CONTENT = 204
_NOT_FOUND = 404
_CONFLICT = 409
_TOO_LARGE = 413
_UNPROCESSABLE = 422
_INTERNAL_ERROR = 500


@dataclass(frozen=True)
class _Window:
    """What a handler needs to know about the window and about who is asking."""

    settings: JoinSettings
    spool: Spool
    caller: str
    is_bound: bool


_Handler = Callable[[web.Request, _Window], Awaitable[web.StreamResponse]]


def create_application(settings: JoinSettings) -> web.Application:
    """Build the join service for one window. It serves whatever the spool holds from then on."""
    application = web.Application(client_max_size=MAXIMUM_REQUEST_BYTES)
    application[_SETTINGS] = settings
    application[_SPOOL] = Spool(settings.spool)
    application.router.add_route("*", "/{anything:.*}", _handle)
    return application


async def _handle(request: web.Request) -> web.StreamResponse:
    """Answer one request: the token first, then the address, then the request itself."""
    settings = request.app[_SETTINGS]
    handler = _route(request, settings.token)
    caller = _caller_address(request)
    if handler is None:
        logger.info("a request without this window's token (or for nothing it serves): 404")
        return _error(_NOT_FOUND, "Not Found")
    try:
        if caller is None:
            raise SpoolError("the caller's address is unknown")
        spool = request.app[_SPOOL]
        bound_to = await asyncio.to_thread(spool.bound_address)
        if bound_to is not None and bound_to != caller:
            logger.warning("refused %s: this window is bound to %s", caller, bound_to)
            return _conflict("this join window is bound to another address")
        return await handler(request, _Window(settings, spool, caller, bound_to is not None))
    except SpoolError as error:
        logger.error("the spool cannot be used: %s", error)
        return _error(_INTERNAL_ERROR, "the join service cannot use its spool")
    except Exception as error:
        # Never left to the web framework's own handler, which logs more than this.
        logger.error("a request from %s failed: %s", caller, type(error).__name__)
        return _error(_INTERNAL_ERROR, "the join service failed")


def _route(request: web.Request, token: str) -> _Handler | None:
    """Return the handler for ``/j/<token>[/<what>]``, or None for anything else."""
    parts = request.path.split("/")
    has_token = _same(parts[2] if len(parts) > 2 else "", token)
    if not has_token or len(parts) > 4 or parts[1] != _PREFIX:
        return None
    what = parts[3] if len(parts) == 4 else None
    return ROUTES.get((request.method, what))


def _same(candidate: str, token: str) -> bool:
    """Compare in constant time. Hashing first makes the lengths equal, so not even the
    token's length shows in how long the comparison takes.
    """
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8", "replace")).digest(),
        hashlib.sha256(token.encode("utf-8")).digest(),
    )


def _caller_address(request: web.Request) -> str | None:
    """The address the connection came from, as the peer's socket address says.

    Never a header: the service is not behind a proxy, and a header is whatever the caller
    wrote. An IPv4 caller seen through an IPv6 socket is given as the IPv4 address it is.
    """
    if request.remote is None:
        return None
    try:
        address = ipaddress.ip_address(request.remote)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


async def _serve_script(_request: web.Request, window: _Window) -> web.Response:
    logger.info("the join script was fetched from %s", window.caller)
    return web.Response(
        body=window.settings.script,
        content_type="text/x-shellscript",
        charset="utf-8",
        headers=_NO_STORE,
    )


async def _receive_signing_request(request: web.Request, window: _Window) -> web.Response:
    """Write the box's signing request to the spool. The first one binds the window."""
    try:
        signing_request = parse_signing_request(parse_json(await request.read()))
    except web.HTTPRequestEntityTooLarge:
        return _too_large()
    except InvalidMessageError as error:
        return _error(_UNPROCESSABLE, str(error))
    record = signing_request.record(window.caller)
    stored = await asyncio.to_thread(window.spool.store_signing_request, record)
    if stored is Stored.DIFFERENT:
        return _conflict("another signing request was already received in this window")
    if stored is Stored.NEW:
        logger.info(
            "a signing request for %r arrived from %s: the window is bound to that address",
            signing_request.name,
            window.caller,
        )
    return _accepted()


async def _serve_certificate(_request: web.Request, window: _Window) -> web.Response:
    """Hand the box its certificate, the authority's and the pool's key, once it is signed."""
    answer = await asyncio.to_thread(window.spool.certificate_answer) if window.is_bound else None
    if answer is None:
        return web.Response(status=_NO_CONTENT)
    if answer.reason is not None:
        logger.info("the refusal of its signing request was handed to %s", window.caller)
        return _conflict(answer.reason)
    logger.info("the certificate was handed to %s", window.caller)
    return web.json_response(
        {
            "certificate": answer.certificate,
            "ca": answer.authority,
            "api_key": window.settings.api_key,
        },
        headers=_NO_STORE,
    )


async def _receive_ready(request: web.Request, window: _Window) -> web.Response:
    """Write that the box is ready for its admission test. Its body, if any, is not used."""
    try:
        await request.read()
    except web.HTTPRequestEntityTooLarge:
        return _too_large()
    if not window.is_bound:
        return _conflict("no signing request has been received in this window")
    answer = await asyncio.to_thread(window.spool.certificate_answer)
    if answer is None or answer.certificate is None:
        return _conflict("no certificate has been issued in this window")
    stored = await asyncio.to_thread(window.spool.store_ready, {"address": window.caller})
    if stored is Stored.DIFFERENT:
        return _conflict("this window already holds another ready notice")
    if stored is Stored.NEW:
        logger.info("%s is ready for its admission test", window.caller)
    return _accepted()


async def _serve_verdict(_request: web.Request, window: _Window) -> web.Response:
    verdict = await asyncio.to_thread(window.spool.verdict) if window.is_bound else None
    if verdict is None:
        return web.Response(status=_NO_CONTENT)
    logger.info("the verdict (admitted: %s) was handed to %s", verdict.admitted, window.caller)
    return web.json_response(
        {"admitted": verdict.admitted, "detail": verdict.detail}, headers=_NO_STORE
    )


async def _serve_image_manifest(_request: web.Request, window: _Window) -> web.Response:
    """Hand over the manifest of the image this window ships, as the pool's host wrote it."""
    shipped = await asyncio.to_thread(shipped_image, window.settings.image_directory)
    if shipped is None:
        return web.Response(status=_NO_CONTENT)
    logger.info("the image's manifest was fetched from %s", window.caller)
    return web.Response(body=shipped.manifest, content_type="application/json", headers=_NO_STORE)


async def _serve_image(request: web.Request, window: _Window) -> web.StreamResponse:
    """Send the image file from disk, in chunks, from where a ``Range`` says to start."""
    shipped = await asyncio.to_thread(shipped_image, window.settings.image_directory)
    if shipped is None:
        return _error(_NOT_FOUND, "this join window ships no image")
    resumed = " (a part of it: a download goes on)" if "Range" in request.headers else ""
    logger.info("the image is being sent to %s%s", window.caller, resumed)
    return web.FileResponse(shipped.file, chunk_size=IMAGE_CHUNK_BYTES, headers=_NO_STORE)


# Everything the service answers, by method and by what follows the token (None: nothing).
ROUTES: dict[tuple[str, str | None], _Handler] = {
    ("GET", None): _serve_script,
    ("POST", "csr"): _receive_signing_request,
    ("GET", "certificate"): _serve_certificate,
    ("POST", "ready"): _receive_ready,
    ("GET", "verdict"): _serve_verdict,
    ("GET", "image.json"): _serve_image_manifest,
    ("GET", "image"): _serve_image,
}


def _accepted() -> web.Response:
    return web.json_response({"status": "received"}, status=_ACCEPTED)


def _conflict(reason: str) -> web.Response:
    return web.json_response({"reason": reason}, status=_CONFLICT, headers=_NO_STORE)


def _too_large() -> web.Response:
    return _error(_TOO_LARGE, f"a request body may be at most {MAXIMUM_REQUEST_BYTES} bytes")


def _error(status: int, detail: str) -> web.Response:
    return web.json_response({"detail": detail}, status=status)
