"""Reading a client's ``POST /api/check`` body.

The cache accepts what a Kimina Lean Server accepts. It validates only the fields it has to
interpret (``snippets``, ``timeout``, ``debug`` and its own ``no_cache``) and forwards every
other field untouched, so an option a newer Kimina adds keeps working through the cache.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

NO_CACHE_FIELD = "no_cache"
_INTERPRETED_FIELDS = frozenset({"snippets", NO_CACHE_FIELD})


class InvalidRequestError(ValueError):
    """The request body is not a check the cache can serve. The message is sent to the client."""


@dataclass(frozen=True)
class Snippet:
    """One Lean file to check, under the id its caller chose."""

    identifier: str
    code: str


@dataclass(frozen=True)
class CheckRequest:
    """A validated check request.

    ``timeout`` is None when the client left it to the server's default. ``forwarded_fields`` is
    the body without ``snippets`` and ``no_cache``: what every upstream request made on behalf
    of this one starts from.
    """

    snippets: tuple[Snippet, ...]
    timeout: int | None
    debug: bool
    bypass_cache: bool
    forwarded_fields: Mapping[str, Any]


def parse_check_request(body: object) -> CheckRequest:
    """Validate a decoded JSON body and return the request it describes."""
    if not isinstance(body, dict):
        raise InvalidRequestError("the body must be a JSON object")
    no_cache = _read_boolean(body, NO_CACHE_FIELD)
    return CheckRequest(
        snippets=_read_snippets(body.get("snippets")),
        timeout=_read_timeout(body.get("timeout")),
        debug=_read_boolean(body, "debug"),
        bypass_cache=no_cache or body.get("infotree") is not None,
        forwarded_fields={
            name: value for name, value in body.items() if name not in _INTERPRETED_FIELDS
        },
    )


def _read_snippets(raw_snippets: object) -> tuple[Snippet, ...]:
    """Read the snippets, keeping Kimina's own rules: at least one, and unique ids."""
    if not isinstance(raw_snippets, list) or not raw_snippets:
        raise InvalidRequestError("`snippets` must be a non-empty list")
    snippets = tuple(_read_snippet(raw_snippet) for raw_snippet in raw_snippets)
    if len({snippet.identifier for snippet in snippets}) != len(snippets):
        raise InvalidRequestError("`snippets` must have unique ids")
    return snippets


def _read_snippet(raw_snippet: object) -> Snippet:
    if not isinstance(raw_snippet, dict):
        raise InvalidRequestError("each snippet must be an object with `id` and `code`")
    identifier, code = raw_snippet.get("id"), raw_snippet.get("code")
    if not isinstance(identifier, str) or not isinstance(code, str):
        raise InvalidRequestError("each snippet needs a string `id` and a string `code`")
    return Snippet(identifier=identifier, code=code)


def _read_timeout(raw_timeout: object) -> int | None:
    """Read the Lean timeout in whole seconds.

    The timeout is part of the cache key, so ``60`` and ``60.0`` must be one value: a whole
    float is accepted and read as its integer. Kimina's timeout is an integer of at least 0;
    anything else is refused here exactly as the server would refuse it.
    """
    if raw_timeout is None:
        return None
    refusal = InvalidRequestError("`timeout` must be a whole number of seconds, 0 or more")
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int | float):
        raise refusal
    if not math.isfinite(raw_timeout) or raw_timeout < 0 or raw_timeout != int(raw_timeout):
        raise refusal
    return int(raw_timeout)


def _read_boolean(body: Mapping[str, Any], field: str) -> bool:
    value = body.get(field, False)
    if not isinstance(value, bool):
        raise InvalidRequestError(f"`{field}` must be true or false")
    return value
