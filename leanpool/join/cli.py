"""``leanpool-join``: run the join service for one window, over HTTPS only.

The token and the API key are read from a file or from the environment, never from a flag: a
command line is visible to every user of the machine. Neither is ever logged.

Exit status: 0 after a requested stop, 1 when the service could not start (an unusable spool,
certificate or port), 2 on a usage error.
"""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from aiohttp import web

from leanpool.api_key import ApiKeyError, read_api_key_file, validate_api_key
from leanpool.environment import SettingsParser, parse_port
from leanpool.join.app import create_application
from leanpool.join.settings import JoinSettings, TokenError, validate_token
from leanpool.join.spool import Spool, SpoolError

logger = logging.getLogger(__name__)

_TOKEN_VARIABLE = "LEANPOOL_JOIN_TOKEN"
_API_KEY_VARIABLE = "LEANPOOL_JOIN_API_KEY"
_COULD_NOT_START = 1


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run the join service until it is stopped and return the process exit status."""
    settings = parse_settings(arguments, os.environ if environment is None else environment)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        Spool(settings.spool).prepare()
        tls_context = build_tls_context(settings.tls_pem)
        logger.info(
            "lean-pool join service on https://%s:%d, spool %s",
            settings.host,
            settings.port,
            settings.spool,
        )
        # No access log: every path holds the window's token.
        web.run_app(
            create_application(settings),
            host=settings.host,
            port=settings.port,
            ssl_context=tls_context,
            access_log=None,
            print=None,
        )
    except (SpoolError, OSError) as error:
        print(f"leanpool-join: {error}", file=sys.stderr)
        return _COULD_NOT_START
    return 0


def build_tls_context(pem: Path) -> ssl.SSLContext:
    """Return the context the service serves with: TLS 1.3 or later, the certificate in ``pem``.

    ``pem`` holds the certificate followed by its key, the same file HAProxy's ``crt`` takes.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(pem)
    except ssl.SSLError:
        # Not the library's own message: it may quote the file it could not parse.
        raise OSError(f"{pem} does not hold a certificate followed by its private key") from None
    except OSError as error:
        raise OSError(
            f"cannot read the certificate file {pem}: {error.strerror or error}"
        ) from None
    return context


def parse_settings(arguments: Sequence[str] | None, environment: Mapping[str, str]) -> JoinSettings:
    """Read the join service's settings from flags and environment variables."""
    parser = _build_parser(environment)
    options = parser.parse_args(arguments)
    for flag, value in (
        ("--script (or LEANPOOL_JOIN_SCRIPT)", options.script),
        ("--tls-pem (or LEANPOOL_JOIN_TLS_PEM)", options.tls_pem),
        ("--spool (or LEANPOOL_JOIN_SPOOL)", options.spool),
    ):
        if value is None:
            parser.error(f"{flag} is required")
    try:
        token = _resolve_token(options, environment)
        api_key = _resolve_api_key(options, environment)
        script = _read_script(options.script)
    except ValueError as error:
        parser.error(str(error))
    return JoinSettings(
        token=token,
        script=script,
        spool=options.spool,
        api_key=api_key,
        tls_pem=options.tls_pem,
        host=options.host,
        port=options.port,
        image_directory=options.image_directory,
    )


def _build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leanpool-join",
        description="Serve one join window: hand a new Lean server box its script, take its "
        "signing request, and pass on the pool's answers.",
        # No abbreviations: `--token` must never be read as short for `--token-file`.
        allow_abbrev=False,
    )
    settings = SettingsParser(parser, environment)
    settings.add(
        "--script", "LEANPOOL_JOIN_SCRIPT", Path, None, "the join script to serve; no secret in it"
    )
    settings.add(
        "--token-file",
        "LEANPOOL_JOIN_TOKEN_FILE",
        Path,
        None,
        f"a file holding the window's token; this or the token itself in {_TOKEN_VARIABLE} is "
        "required",
        optional=True,
    )
    settings.add(
        "--tls-pem",
        "LEANPOOL_JOIN_TLS_PEM",
        Path,
        None,
        "the certificate and key to serve with, in one file: the pool's front door's",
    )
    settings.add(
        "--spool",
        "LEANPOOL_JOIN_SPOOL",
        Path,
        None,
        "the directory shared with the pool's operator side: requests/ is written, responses/ "
        "is read",
    )
    settings.add(
        "--api-key-file",
        "LEANPOOL_JOIN_API_KEY_FILE",
        Path,
        None,
        f"a file holding the pool's API key; this or the key itself in {_API_KEY_VARIABLE} is "
        "required unless --pool-without-key",
        optional=True,
    )
    settings.add_switch(
        "--pool-without-key",
        "LEANPOOL_JOIN_POOL_WITHOUT_KEY",
        False,
        "the pool's Lean servers have no API key: a box is handed none",
    )
    settings.add(
        "--image-directory",
        "LEANPOOL_JOIN_IMAGE_DIRECTORY",
        Path,
        None,
        "a directory holding the Lean server image this window ships (manifest.json and the "
        "image file it names); without it a joining box builds its own image",
        optional=True,
    )
    settings.add("--host", "LEANPOOL_JOIN_HOST", str, "0.0.0.0", "the address to listen on")
    settings.add("--port", "LEANPOOL_JOIN_PORT", parse_port, 18110, "the port to listen on")
    return parser


def _resolve_token(options: argparse.Namespace, environment: Mapping[str, str]) -> str:
    token_from_environment = environment.get(_TOKEN_VARIABLE, "")
    if options.token_file is not None and token_from_environment:
        raise TokenError(f"give either --token-file or {_TOKEN_VARIABLE}, not both")
    if options.token_file is not None:
        try:
            text = options.token_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise TokenError(f"cannot read the token file {options.token_file}: {error}") from error
        try:
            return validate_token(text)
        except TokenError as error:
            raise TokenError(f"{options.token_file}: {error}") from error
    if token_from_environment:
        return validate_token(token_from_environment)
    raise TokenError(f"no token: set --token-file or {_TOKEN_VARIABLE}")


def _resolve_api_key(options: argparse.Namespace, environment: Mapping[str, str]) -> str | None:
    """Return the pool's key, or None when a pool without one was asked for explicitly."""
    key_from_environment = environment.get(_API_KEY_VARIABLE, "")
    if options.api_key_file is not None and key_from_environment:
        raise ApiKeyError(f"give either --api-key-file or {_API_KEY_VARIABLE}, not both")
    if options.api_key_file is not None:
        return read_api_key_file(options.api_key_file)
    if key_from_environment:
        return validate_api_key(key_from_environment)
    if options.pool_without_key:
        return None
    raise ApiKeyError(
        f"no API key: set --api-key-file or {_API_KEY_VARIABLE}, "
        "or pass --pool-without-key for a pool whose Lean servers have none"
    )


def _read_script(path: Path) -> bytes:
    try:
        script = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read the join script {path}: {error}") from error
    if not script.strip():
        raise ValueError(f"the join script {path} is empty")
    return script
