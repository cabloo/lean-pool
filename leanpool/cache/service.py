"""The cache's rule for one check: answer from the store, join a check in flight, or forward."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from leanpool.cache.keys import check_key
from leanpool.cache.policy import is_storable
from leanpool.cache.request import CheckRequest, Snippet
from leanpool.cache.store import AsyncResultStore
from leanpool.cache.upstream import UpstreamClient, UpstreamFailure

logger = logging.getLogger(__name__)

CACHED_FIELD = "cached"


@dataclass
class CheckCounters:
    """How each check since start was served. Every check counts in exactly one of the four.

    * ``hits``: answered from the store.
    * ``misses``: not in the store, forwarded to the Lean servers.
    * ``coalesced``: identical to a check already in flight; waited for that one's answer.
    * ``bypasses``: sent with ``no_cache`` (or an option the key does not cover); forwarded
      without looking in the store or writing to it.
    """

    hits: int = 0
    misses: int = 0
    coalesced: int = 0
    bypasses: int = 0


@dataclass(frozen=True)
class _SharedAnswer:
    """A check's result without its attempt id, as shared by everyone waiting for it."""

    fields: dict[str, Any]
    from_store: bool


@dataclass(frozen=True)
class _Flight:
    """One check on its way to the Lean servers, and the priority it was sent with."""

    task: asyncio.Task[_SharedAnswer | UpstreamFailure]
    background: bool


class CheckService:
    """Serves single checks: one snippet of one request at a time.

    It never changes what a caller measures: a repeat of a stored check gets the answer Lean
    gave the first time, under the caller's own id, and everything that is not a definitive
    answer goes to the Lean servers every time.
    """

    def __init__(
        self,
        store: AsyncResultStore,
        upstream: UpstreamClient,
        pin: str,
        exhaustion_patterns: Sequence[str],
    ) -> None:
        self._store = store
        self._upstream = upstream
        self._pin = pin
        self._exhaustion_patterns = tuple(exhaustion_patterns)
        self._flights: dict[bytes, _Flight] = {}
        self.counters = CheckCounters()

    async def check(
        self,
        snippet: Snippet,
        request: CheckRequest,
        authorization: str | None,
        *,
        background: bool = False,
    ) -> dict[str, Any] | UpstreamFailure:
        """Return the result for ``snippet`` under its own id, or the upstream failure.

        ``background`` is the caller's priority: a background check waits behind every normal
        check for a Lean worker, and is forwarded saying so.
        """
        if request.bypass_cache:
            self.counters.bypasses += 1
            return await self._forward_untouched(snippet, request, authorization, background)
        outcome = await self._shared_answer(snippet, request, authorization, background)
        if isinstance(outcome, UpstreamFailure):
            return outcome
        return _result_for_caller(snippet.identifier, outcome, include_diagnostics=request.debug)

    async def status(self) -> dict[str, int]:
        """Return the counters and the store's size, for ``GET /status``."""
        statistics = await self._store.statistics()
        return {
            "hits": self.counters.hits,
            "misses": self.counters.misses,
            "coalesced": self.counters.coalesced,
            "bypasses": self.counters.bypasses,
            "in_flight": self._upstream.in_flight,
            "stored_entries": statistics.entries,
            "stored_bytes": statistics.stored_bytes,
            "file_bytes": statistics.file_bytes,
            "evictions": statistics.evictions,
        }

    async def _shared_answer(
        self,
        snippet: Snippet,
        request: CheckRequest,
        authorization: str | None,
        background: bool,
    ) -> _SharedAnswer | UpstreamFailure:
        """Wait for the one answer to this check, starting the work only if nobody has.

        Single flight: identical checks that arrive while one is in flight wait for its answer
        instead of each occupying a Lean worker. The work runs as its own task and callers
        wait on it shielded, so a caller that disconnects does not cancel the answer the
        others are waiting for (nor the write to the store).

        Across the two priorities: a normal check never waits on a background check's flight,
        which may sit behind every normal check in the proxy's queue. It starts its own, later
        callers join that one, and the background caller keeps the flight it started. A
        background check that meets a normal flight joins it.
        """
        key = check_key(self._pin, snippet.code, request.timeout)
        flight = self._flights.get(key)
        if flight is not None and flight.background and not background:
            flight = None
        if flight is None:
            task = asyncio.create_task(
                self._answer(key, snippet, request, authorization, background)
            )
            flight = _Flight(task=task, background=background)
            self._flights[key] = flight
            task.add_done_callback(functools.partial(self._forget, key, flight))
        else:
            self.counters.coalesced += 1
        return await asyncio.shield(flight.task)

    def _forget(self, key: bytes, flight: _Flight, _finished: object = None) -> None:
        """Take a finished flight off the list, unless a newer flight has its place."""
        if self._flights.get(key) is flight:
            del self._flights[key]

    async def _answer(
        self,
        key: bytes,
        snippet: Snippet,
        request: CheckRequest,
        authorization: str | None,
        background: bool,
    ) -> _SharedAnswer | UpstreamFailure:
        stored_fields = await self._lookup(key)
        if stored_fields is not None:
            self.counters.hits += 1
            return _SharedAnswer(fields=stored_fields, from_store=True)
        self.counters.misses += 1
        # Diagnostics are always requested, so the stored answer can serve a later caller who
        # asks for them; a caller who did not ask has them removed from its own copy.
        reply = await self._upstream.check(
            _upstream_body(snippet, request, debug=True), authorization, background=background
        )
        if isinstance(reply, UpstreamFailure):
            return reply
        fields = {name: value for name, value in reply.result.items() if name != "id"}
        if is_storable(reply.result, self._exhaustion_patterns):
            await self._remember(key, fields)
        return _SharedAnswer(fields=fields, from_store=False)

    async def _forward_untouched(
        self,
        snippet: Snippet,
        request: CheckRequest,
        authorization: str | None,
        background: bool,
    ) -> dict[str, Any] | UpstreamFailure:
        reply = await self._upstream.check(
            _upstream_body(snippet, request), authorization, background=background
        )
        return reply if isinstance(reply, UpstreamFailure) else reply.result

    async def _lookup(self, key: bytes) -> dict[str, Any] | None:
        """Read the store; a store that fails is a miss, never a failed check."""
        try:
            payload = await self._store.get(key)
        except sqlite3.Error:
            logger.exception("the result store could not be read; treating the check as a miss")
            return None
        if payload is None:
            return None
        fields: dict[str, Any] = json.loads(payload)
        return fields

    async def _remember(self, key: bytes, fields: dict[str, Any]) -> None:
        """Write to the store; a store that fails loses the entry, never the answer."""
        try:
            await self._store.put(key, json.dumps(fields, separators=(",", ":")).encode("utf-8"))
        except sqlite3.Error:
            logger.exception("the result store could not be written; the answer is not cached")


def _upstream_body(
    snippet: Snippet, request: CheckRequest, *, debug: bool | None = None
) -> dict[str, Any]:
    """Build the single-snippet request sent upstream for one snippet of ``request``.

    Everything the client sent is kept except ``snippets`` (replaced by this one) and
    ``no_cache`` (ours, not Kimina's).
    """
    body = dict(request.forwarded_fields)
    body["snippets"] = [{"id": snippet.identifier, "code": snippet.code}]
    if debug is not None:
        body["debug"] = debug
    return body


def _result_for_caller(
    identifier: str, answer: _SharedAnswer, *, include_diagnostics: bool
) -> dict[str, Any]:
    """Shape a shared answer the way Kimina would have answered this caller.

    Kimina omits ``diagnostics`` from a result that carries a ``response`` unless the request
    asked for ``debug``; a result that carries an ``error`` keeps them either way.
    """
    result = {"id": identifier, **answer.fields}
    if not include_diagnostics and "response" in result:
        result.pop("diagnostics", None)
    if answer.from_store:
        result[CACHED_FIELD] = True
    return result
