"""``leanpool-admit``: test one Lean server before it joins a pool.

It talks to the Lean server directly, not through the pool, so the answers are that server's
alone. One JSON report is written to standard output.

A server behind its box's TLS front is reached with the three TLS options, given together: the
pool authority's certificate, the pool proxy's client certificate (the only one a front lets
in) and the name the server's certificate must carry. Without them the server is spoken to in
plain HTTP.

Exit status: 0 when every case behaved, 1 when any case misbehaved, 2 on bad arguments (the
cases directory, the key file and the TLS files included), 3 when the server could not be
reached or the TLS handshake with it failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanpool.admit.cases import CasesError, load_cases
from leanpool.admit.runner import (
    AdmissionSettings,
    ServerUnreachableError,
    run_admission,
    unreachable_report,
)
from leanpool.admit.tls import (
    HTTPS_SCHEME,
    AdmissionTls,
    load_admission_tls,
    validate_https_url,
)
from leanpool.api_key import ApiKeyError, read_api_key_file
from leanpool.environment import SettingsParser, parse_positive_integer

_MISBEHAVED = 1
_UNREACHABLE = 3
_HTTP_SCHEME = "http://"
_TLS_OPTIONS = "--tls-ca-file, --tls-client-pem and --tls-server-name"


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run the admission test, print its report and return the process exit status."""
    parser = _build_parser(os.environ if environment is None else environment)
    options = parser.parse_args(arguments)
    try:
        settings = _read_settings(options)
        cases = load_cases(_required(options.cases, "--cases"))
    except (CasesError, ApiKeyError, ValueError) as error:
        parser.error(str(error))
    try:
        report = asyncio.run(run_admission(settings, cases))
    except ServerUnreachableError as error:
        _print_report(unreachable_report(settings, str(error)))
        return _UNREACHABLE
    _print_report(report.to_json())
    return 0 if report.admitted else _MISBEHAVED


def _build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanpool-admit",
        description="Check that one Lean server verifies what it must and rejects what it must.",
        # No abbreviations: `--api-key` must never be read as short for `--api-key-file`.
        allow_abbrev=False,
    )
    settings = SettingsParser(parser, environment)
    settings.add(
        "--server",
        "LEANPOOL_ADMIT_SERVER",
        str,
        None,
        "the Lean server's URL, e.g. http://host:8000; https://host:8000 with the TLS options",
    )
    settings.add(
        "--api-key-file",
        "LEANPOOL_ADMIT_API_KEY_FILE",
        Path,
        None,
        "a file holding the server's API key; leave out for a server without one",
        optional=True,
    )
    settings.add(
        "--cases",
        "LEANPOOL_ADMIT_CASES",
        Path,
        None,
        "a directory with verify/*.lean (must be accepted) and reject/*.lean (must be rejected)",
    )
    settings.add(
        "--timeout",
        "LEANPOOL_ADMIT_TIMEOUT_SECONDS",
        parse_positive_integer,
        60,
        "the Lean timeout, in seconds, sent with every check",
    )
    settings.add(
        "--concurrency",
        "LEANPOOL_ADMIT_CONCURRENCY",
        parse_positive_integer,
        4,
        "how many checks are in flight at once",
    )
    settings.add(
        "--tls-ca-file",
        "LEANPOOL_ADMIT_TLS_CA_FILE",
        Path,
        None,
        "the pool authority's certificate; only it is trusted then; give the three TLS options "
        "together to test a server behind its box's TLS front, or none of them",
        optional=True,
    )
    settings.add(
        "--tls-client-pem",
        "LEANPOOL_ADMIT_TLS_CLIENT_PEM",
        Path,
        None,
        "the pool proxy's client certificate and key in one file: the only caller a box's TLS "
        "front lets in",
        optional=True,
    )
    settings.add(
        "--tls-server-name",
        "LEANPOOL_ADMIT_TLS_SERVER_NAME",
        str,
        None,
        "the name the server's certificate must carry (its name in the pool's server list), "
        "whatever host or address --server dials",
        optional=True,
    )
    return parser


def _read_settings(options: argparse.Namespace) -> AdmissionSettings:
    server_url: str = _required(options.server, "--server")
    if not server_url.startswith((_HTTP_SCHEME, HTTPS_SCHEME)):
        raise ValueError(f"--server must be an http:// or https:// URL, got {server_url!r}")
    tls = _read_tls(options, server_url)
    api_key = None if options.api_key_file is None else read_api_key_file(options.api_key_file)
    return AdmissionSettings(
        server_url=server_url.rstrip("/"),
        api_key=api_key,
        timeout_seconds=options.timeout,
        concurrency=options.concurrency,
        tls=tls,
    )


def _read_tls(options: argparse.Namespace, server_url: str) -> AdmissionTls | None:
    """Return how to reach the server over TLS, or None when no TLS option was given.

    Some of the three options without the others is refused, and so is a URL whose scheme does
    not say what the options say: a server that was meant to be reached over TLS is never
    spoken to in plain HTTP by an omission, and an https:// URL is never trusted through the
    system's authorities.
    """
    given = {
        "--tls-ca-file": options.tls_ca_file,
        "--tls-client-pem": options.tls_client_pem,
        "--tls-server-name": options.tls_server_name,
    }
    missing = [flag for flag, value in given.items() if value is None]
    is_https = server_url.startswith(HTTPS_SCHEME)
    if len(missing) == len(given):
        if is_https:
            raise ValueError(f"an https:// server needs {_TLS_OPTIONS}")
        return None
    if missing:
        raise ValueError(f"TLS needs {_TLS_OPTIONS} together; missing: {', '.join(missing)}")
    if not is_https:
        raise ValueError(
            f"with the TLS options --server must be an https:// URL, got {server_url!r}"
        )
    validate_https_url(server_url)
    return load_admission_tls(options.tls_ca_file, options.tls_client_pem, options.tls_server_name)


def _required(value: Any, flag: str) -> Any:
    if value is None:
        raise ValueError(f"{flag} is required")
    return value


def _print_report(report: dict[str, Any]) -> None:
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
