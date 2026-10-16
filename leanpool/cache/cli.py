"""``leanpool-cache``: run the cache service.

The API key is read from the environment or from a file, never from a flag: a command line is
visible to every user of the machine.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from aiohttp import web

from leanpool.api_key import ApiKeyError, read_api_key_file, validate_api_key
from leanpool.cache.app import create_application
from leanpool.cache.policy import DEFAULT_EXHAUSTION_PATTERNS
from leanpool.cache.settings import CacheSettings
from leanpool.environment import (
    SettingsParser,
    environment_default,
    parse_port,
    parse_positive_integer,
    parse_positive_number,
)
from leanpool.screen import IMPORT_ROOT

logger = logging.getLogger(__name__)

_API_KEY_VARIABLE = "LEANPOOL_CACHE_API_KEY"
_SCREENED_API_KEY_VARIABLE = "LEANPOOL_CACHE_SCREENED_API_KEY"
_PATTERNS_VARIABLE = "LEANPOOL_CACHE_EXHAUSTION_PATTERNS"
_PATTERN_SEPARATOR = ","
_IMPORTS_VARIABLE = "LEANPOOL_CACHE_SCREENED_IMPORTS"


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run the cache service until it is stopped and return the process exit status."""
    settings = parse_settings(arguments, os.environ if environment is None else environment)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info(
        "lean-pool cache for pin %r on %s:%d, forwarding misses to %s, store %s (cap %d bytes), %s",
        settings.pin,
        settings.host,
        settings.port,
        settings.upstream_url,
        settings.database_path,
        settings.maximum_bytes,
        "with a screened key" if settings.screened_api_key else "no screened key",
    )
    # No access log: a pool serves thousands of checks a minute, and /status has the counts.
    web.run_app(
        create_application(settings),
        host=settings.host,
        port=settings.port,
        access_log=None,
        print=None,
    )
    return 0


def parse_settings(
    arguments: Sequence[str] | None, environment: Mapping[str, str]
) -> CacheSettings:
    """Read the cache's settings from flags and environment variables."""
    parser = _build_parser(environment)
    options = parser.parse_args(arguments)
    if options.pin is None:
        parser.error("--pin (or LEANPOOL_CACHE_PIN) is required")
    try:
        api_key = _resolve_api_key(options, environment)
        screened_api_key = _resolve_screened_api_key(options, environment, api_key)
        patterns = _resolve_patterns(options, environment)
        imports = _resolve_imports(options, environment)
    except ValueError as error:
        parser.error(str(error))
    if screened_api_key is not None and options.screened_in_flight is None:
        parser.error(
            "a screened key needs --screened-in-flight (or LEANPOOL_CACHE_SCREENED_IN_FLIGHT): "
            "how many of its checks may be at the servers at once"
        )
    return CacheSettings(
        pin=options.pin,
        api_key=api_key,
        screened_api_key=screened_api_key,
        screened_in_flight=options.screened_in_flight,
        screened_max_timeout_seconds=options.screened_max_timeout,
        screened_max_request_bytes=options.screened_max_request_bytes,
        screened_imports=imports,
        database_path=options.database,
        host=options.host,
        port=options.port,
        upstream_url=options.upstream_url,
        upstream_timeout_seconds=options.upstream_timeout,
        maximum_bytes=options.max_bytes,
        maximum_request_bytes=options.max_request_bytes,
        exhaustion_patterns=patterns,
        hop_header=options.hop_header,
    )


def _build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanpool-cache",
        description="Serve repeated Lean checks from a store; forward the rest to the pool.",
        # No abbreviations: `--api-key` must never be read as short for `--api-key-file`.
        allow_abbrev=False,
    )
    defaults = CacheSettings(pin="", api_key=None, database_path=Path("leanpool-cache.sqlite3"))
    settings = SettingsParser(parser, environment)
    settings.add(
        "--pin",
        "LEANPOOL_CACHE_PIN",
        str,
        None,
        "a string naming the pool's Lean and Mathlib versions; part of every cache key, so "
        "change it whenever the Lean servers' image changes",
    )
    settings.add(
        "--api-key-file",
        "LEANPOOL_CACHE_API_KEY_FILE",
        Path,
        None,
        f"a file holding the pool's API key (or put the key itself in {_API_KEY_VARIABLE}); "
        "required unless --allow-unauthenticated",
    )
    settings.add(
        "--screened-api-key-file",
        "LEANPOOL_CACHE_SCREENED_API_KEY_FILE",
        Path,
        None,
        "a file holding a second key, for senders trusted less than the pool's own (or put the "
        f"key itself in {_SCREENED_API_KEY_VARIABLE}): their checks must pass the screen and are "
        "forwarded with the pool's key; none by default",
    )
    settings.add(
        "--screened-in-flight",
        "LEANPOOL_CACHE_SCREENED_IN_FLIGHT",
        parse_positive_integer,
        None,
        "with a screened key, required: the most checks its senders started that are at the "
        "servers at once (as many again may wait), and the most snippets in one of their requests",
    )
    settings.add(
        "--screened-max-timeout",
        "LEANPOOL_CACHE_SCREENED_MAX_TIMEOUT_SECONDS",
        parse_positive_integer,
        defaults.screened_max_timeout_seconds,
        "the longest Lean timeout, in seconds, a check sent with the screened key may ask for, "
        "and the one it is given when it names none",
    )
    settings.add(
        "--screened-max-request-bytes",
        "LEANPOOL_CACHE_SCREENED_MAX_REQUEST_BYTES",
        parse_positive_integer,
        defaults.screened_max_request_bytes,
        "the largest request body accepted with the screened key (never more than "
        "--max-request-bytes)",
    )
    settings.add_switch(
        "--allow-unauthenticated",
        "LEANPOOL_CACHE_ALLOW_UNAUTHENTICATED",
        False,
        "serve without checking a key, for a pool whose Lean servers have none",
    )
    settings.add(
        "--database",
        "LEANPOOL_CACHE_DATABASE",
        Path,
        defaults.database_path,
        "the SQLite file holding the stored results",
    )
    settings.add(
        "--max-bytes",
        "LEANPOOL_CACHE_MAX_BYTES",
        parse_positive_integer,
        defaults.maximum_bytes,
        "the size cap in bytes; the least recently used results are evicted above it",
    )
    settings.add("--host", "LEANPOOL_CACHE_HOST", str, defaults.host, "the address to listen on")
    settings.add(
        "--port", "LEANPOOL_CACHE_PORT", parse_port, defaults.port, "the port to listen on"
    )
    settings.add(
        "--upstream-url",
        "LEANPOOL_CACHE_UPSTREAM_URL",
        str,
        defaults.upstream_url,
        "where a miss is forwarded: the proxy's loopback listener in front of the Lean servers",
    )
    settings.add(
        "--upstream-timeout",
        "LEANPOOL_CACHE_UPSTREAM_TIMEOUT_SECONDS",
        parse_positive_number,
        defaults.upstream_timeout_seconds,
        "seconds to wait for the Lean servers; must cover the proxy's queue wait plus the "
        "slowest check (the generated haproxy.cfg states the minimum)",
    )
    settings.add(
        "--max-request-bytes",
        "LEANPOOL_CACHE_MAX_REQUEST_BYTES",
        parse_positive_integer,
        defaults.maximum_request_bytes,
        "the largest request body accepted",
    )
    settings.add(
        "--hop-header",
        "LEANPOOL_HOP_HEADER",
        str,
        defaults.hop_header,
        "the loop-guard header; must match the proxy's",
    )
    parser.add_argument(
        "--screened-import",
        action="append",
        metavar="ROOT",
        help="with a screened key, a check may also import ROOT and its modules (a module name's "
        "first part, such as FormalConjectures), beside Mathlib and Aesop; repeatable "
        f"[env {_IMPORTS_VARIABLE}, comma-separated; default none]",
    )
    parser.add_argument(
        "--exhaustion-pattern",
        action="append",
        metavar="TEXT",
        help="a result with a message containing TEXT (any case) is never stored; repeatable "
        f"[env {_PATTERNS_VARIABLE}, comma-separated; "
        f"default {_PATTERN_SEPARATOR.join(DEFAULT_EXHAUSTION_PATTERNS)}]",
    )
    return parser


def _resolve_api_key(options: argparse.Namespace, environment: Mapping[str, str]) -> str | None:
    """Return the pool's key, or None when running without one was asked for explicitly.

    Without a key the cache would answer anyone who can reach it, so having none is never the
    silent default: either a key is configured or ``--allow-unauthenticated`` says so.
    """
    key_from_environment = environment.get(_API_KEY_VARIABLE, "")
    if options.api_key_file is not None and key_from_environment:
        raise ApiKeyError(f"give either --api-key-file or {_API_KEY_VARIABLE}, not both")
    if options.api_key_file is not None:
        return read_api_key_file(options.api_key_file)
    if key_from_environment:
        return validate_api_key(key_from_environment)
    if options.allow_unauthenticated:
        return None
    raise ApiKeyError(
        f"no API key: set --api-key-file or {_API_KEY_VARIABLE}, "
        "or pass --allow-unauthenticated for a pool without a key"
    )


def _resolve_screened_api_key(
    options: argparse.Namespace, environment: Mapping[str, str], api_key: str | None
) -> str | None:
    """Return the screened key, or None when there is none (the default).

    It needs the pool's own key: without one every sender is let in unscreened, so a second key
    would screen nobody. And it must differ from it, or the screen could never tell them apart.
    """
    key_from_environment = environment.get(_SCREENED_API_KEY_VARIABLE, "")
    if options.screened_api_key_file is not None and key_from_environment:
        raise ApiKeyError(
            f"give either --screened-api-key-file or {_SCREENED_API_KEY_VARIABLE}, not both"
        )
    if options.screened_api_key_file is not None:
        screened = read_api_key_file(options.screened_api_key_file)
    elif key_from_environment:
        screened = validate_api_key(key_from_environment)
    else:
        return None
    if api_key is None:
        raise ApiKeyError("a screened key needs the pool's own key: the cache has none")
    if screened == api_key:
        raise ApiKeyError("the screened key must differ from the pool's own key")
    return screened


def _resolve_patterns(
    options: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[str, ...]:
    if options.exhaustion_pattern:
        return tuple(options.exhaustion_pattern)
    return environment_default(
        environment, _PATTERNS_VARIABLE, _parse_patterns, DEFAULT_EXHAUSTION_PATTERNS
    )


def _resolve_imports(
    options: argparse.Namespace, environment: Mapping[str, str]
) -> tuple[str, ...]:
    """The roots a screened check may import beside Mathlib and Aesop: the flags', else the
    variable's (comma-separated), else none."""
    if options.screened_import:
        roots = tuple(options.screened_import)
    else:
        roots = tuple(
            root.strip()
            for root in environment.get(_IMPORTS_VARIABLE, "").split(_PATTERN_SEPARATOR)
            if root.strip()
        )
    for root in roots:
        if not IMPORT_ROOT.fullmatch(root):
            raise ValueError(
                f"--screened-import: {root!r} is not the first part of a Lean module's name "
                "(such as FormalConjectures)"
            )
    return roots


def _parse_patterns(text: str) -> tuple[str, ...]:
    patterns = tuple(
        pattern.strip() for pattern in text.split(_PATTERN_SEPARATOR) if pattern.strip()
    )
    if not patterns:
        raise ValueError("no pattern given")
    return patterns
