"""Forwarding one check to the Lean servers (through the proxy's loopback listener)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from leanpool.loop_guard import HOP_HEADER_VALUE
from leanpool.signals import BACKGROUND_PRIORITY, PRIORITY_HEADER

_CHECK_PATH = "/api/check"
_JSON_CONTENT_TYPE = "application/json"
_BAD_GATEWAY = 502
_GATEWAY_TIMEOUT = 504
_OK = 200


@dataclass(frozen=True)
class UpstreamAnswer:
    """The single result of a check the Lean servers answered with HTTP 200."""

    result: dict[str, Any]


@dataclass(frozen=True)
class UpstreamFailure:
    """A check that got no result: what to send the client instead.

    For an HTTP error from upstream this is the upstream's own status and body, unchanged, so a
    client's retry rules (429, 5xx) work through the cache exactly as they do against a Lean
    server. When there was no HTTP reply at all it is a 502 or 504 written here.
    """

    status: int
    body: bytes
    content_type: str = _JSON_CONTENT_TYPE


def new_upstream_session(timeout_seconds: float) -> aiohttp.ClientSession:
    """Create the HTTP session used to reach the Lean servers.

    The connection limit is switched off (aiohttp's default is 100). The proxy in front of the
    Lean servers is where checks queue, one slot per worker; a limit here would be a second,
    hidden queue that caps the pool below its real capacity as soon as it grows.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0),
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
    )


class UpstreamClient:
    """Sends single-snippet checks upstream and reads their one result."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, hop_header: str) -> None:
        self._session = session
        self._url = base_url.rstrip("/") + _CHECK_PATH
        self._hop_header = hop_header
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        """How many checks are waiting for the Lean servers right now."""
        return self._in_flight

    async def check(
        self, body: Mapping[str, Any], authorization: str | None, *, background: bool = False
    ) -> UpstreamAnswer | UpstreamFailure:
        """Send a check with exactly one snippet and return its result or the failure.

        The caller's ``Authorization`` header is passed on unchanged (each Lean server checks the
        key itself), and the loop-guard header marks the request as already past the cache. A
        background check says so again on this hop: the proxy's queue for a worker is in front of
        the Lean servers, behind the cache.
        """
        headers = {self._hop_header: HOP_HEADER_VALUE}
        if authorization is not None:
            headers["Authorization"] = authorization
        if background:
            headers[PRIORITY_HEADER] = BACKGROUND_PRIORITY
        self._in_flight += 1
        try:
            async with self._session.post(self._url, json=body, headers=headers) as response:
                status = response.status
                payload = await response.read()
                content_type = response.headers.get("Content-Type", _JSON_CONTENT_TYPE)
        except TimeoutError:
            return _failure(_GATEWAY_TIMEOUT, "the Lean servers did not answer in time")
        except aiohttp.ClientError as error:
            return _failure(_BAD_GATEWAY, f"could not reach the Lean servers: {error}")
        finally:
            self._in_flight -= 1
        if status != _OK:
            return UpstreamFailure(status=status, body=payload, content_type=content_type)
        return _read_single_result(payload, body["snippets"][0]["id"])


def _read_single_result(
    payload: bytes, expected_identifier: str
) -> UpstreamAnswer | UpstreamFailure:
    """Read the one result a single-snippet check must return, under the id that was sent."""
    try:
        results = json.loads(payload)["results"]
        (result,) = results
        identifier = result["id"]
    except (ValueError, KeyError, TypeError):
        return _failure(_BAD_GATEWAY, "the Lean servers' reply is not a single check result")
    if identifier != expected_identifier:
        return _failure(_BAD_GATEWAY, "the Lean servers' reply is for a different snippet")
    return UpstreamAnswer(result=result)


def _failure(status: int, detail: str) -> UpstreamFailure:
    return UpstreamFailure(status=status, body=json.dumps({"detail": detail}).encode("utf-8"))
