"""``leanpool-pki``: a pool's certificate authority and the certificates it signs.

A private key is only ever written to a file of mode 0600. No command prints one, takes one as
an argument or reads one from the environment.

Exit status: 0 on success, 1 when the command was refused (no existing file is changed), 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from leanpool.environment import SettingsParser, parse_positive_integer
from leanpool.pki.authority import (
    AUTHORITY_LIFETIME,
    CERTIFICATE_LIFETIME,
    DEFAULT_AUTHORITY_NAME,
    Usage,
    create_authority,
    issue_certificate,
    new_private_key,
)
from leanpool.pki.files import (
    AUTHORITY_CERTIFICATE_NAME,
    AUTHORITY_KEY_NAME,
    PUBLIC_MODE,
    certificate_pem,
    ensure_directory,
    identity_paths,
    load_authority,
    read_certificate,
    read_request,
    refuse_existing,
    request_paths,
    save_authority,
    save_identity,
    save_request,
    write_file,
)
from leanpool.pki.names import PkiError, SubjectNames, parse_common_name, subject_names
from leanpool.pki.report import days_left, public_key_pin
from leanpool.pki.signing import make_request, sign_request

_REFUSED = 1
_FILE_STEM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

if TYPE_CHECKING:
    # Subscriptable only for the type checker; at run time it is used in annotations alone.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def main(
    arguments: Sequence[str] | None = None, environment: Mapping[str, str] | None = None
) -> int:
    """Run one subcommand and return the process exit status."""
    parser = build_parser(os.environ if environment is None else environment)
    options = parser.parse_args(arguments)
    if getattr(options, "ca_dir", "") is None:
        parser.error("--ca-dir (or LEANPOOL_PKI_CA_DIR) is required")
    try:
        _COMMANDS[options.command](options)
    except (PkiError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return _REFUSED
    return 0


def build_parser(environment: Mapping[str, str]) -> argparse.ArgumentParser:
    """Build the parser for the seven subcommands."""
    parser = argparse.ArgumentParser(
        prog="leanpool-pki",
        description="Make a lean-pool's certificate authority and the certificates it signs.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _add_init_command(commands, environment)
    _add_issue_command(commands, environment, "issue-server", "a server certificate", names=True)
    _add_issue_command(commands, environment, "issue-client", "a client certificate", names=False)
    _add_csr_command(commands)
    _add_sign_csr_command(commands, environment)
    _add_certificate_command(commands, "pin", "print the certificate's public-key pin")
    _add_certificate_command(commands, "expiry", "print the days the certificate has left")
    return parser


def _add_authority_option(parser: argparse.ArgumentParser, environment: Mapping[str, str]) -> None:
    SettingsParser(parser, environment).add(
        "--ca-dir",
        "LEANPOOL_PKI_CA_DIR",
        Path,
        None,
        "the directory holding the authority's ca.crt and ca.key",
    )


def _add_days_option(parser: argparse.ArgumentParser, lifetime: timedelta) -> None:
    parser.add_argument(
        "--days",
        type=_positive_days,
        default=lifetime.days,
        metavar="DAYS",
        help=f"how long the certificate is valid (default {lifetime.days})",
    )


def _add_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out-dir", type=Path, required=True, metavar="DIR", help="where the files are written"
    )
    parser.add_argument(
        "--file-stem",
        default=None,
        metavar="STEM",
        help="the files' name without its ending (default: the name)",
    )
    _add_replace_option(parser)


def _add_replace_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--replace",
        action="store_true",
        help="overwrite existing files; without it an existing file is a refusal",
    )


def _add_name_options(parser: argparse.ArgumentParser, *, names: bool) -> None:
    parser.add_argument(
        "--name",
        required=True,
        metavar="NAME",
        help="the DNS name the certificate is issued to (its common name)",
    )
    if not names:
        return
    parser.add_argument(
        "--dns",
        action="append",
        default=[],
        metavar="NAME",
        help="a further DNS name the certificate is valid for; repeatable",
    )
    parser.add_argument(
        "--ip",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="an IP address the certificate is valid for; repeatable",
    )


def _add_init_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser(
        "init",
        help="make a new authority; an existing one is never overwritten",
        allow_abbrev=False,
    )
    _add_authority_option(parser, environment)
    parser.add_argument(
        "--name",
        default=DEFAULT_AUTHORITY_NAME,
        metavar="TEXT",
        help=f"the authority's name (default {DEFAULT_AUTHORITY_NAME!r})",
    )
    _add_days_option(parser, AUTHORITY_LIFETIME)


def _add_issue_command(
    commands: Subparsers, environment: Mapping[str, str], command: str, what: str, *, names: bool
) -> None:
    parser = commands.add_parser(
        command, help=f"make a key and {what} signed by the authority", allow_abbrev=False
    )
    _add_authority_option(parser, environment)
    _add_name_options(parser, names=names)
    _add_output_options(parser)
    _add_days_option(parser, CERTIFICATE_LIFETIME)


def _add_csr_command(commands: Subparsers) -> None:
    parser = commands.add_parser(
        "csr", help="make a key and a signing request for it", allow_abbrev=False
    )
    _add_name_options(parser, names=True)
    _add_output_options(parser)


def _add_sign_csr_command(commands: Subparsers, environment: Mapping[str, str]) -> None:
    parser = commands.add_parser(
        "sign-csr",
        help="sign a request as a server certificate for exactly the allowed names",
        allow_abbrev=False,
    )
    _add_authority_option(parser, environment)
    parser.add_argument(
        "--csr", type=Path, required=True, metavar="FILE", help="the signing request"
    )
    parser.add_argument(
        "--out", type=Path, required=True, metavar="FILE", help="where the certificate is written"
    )
    parser.add_argument(
        "--allow-dns",
        action="append",
        default=[],
        metavar="NAME",
        help="a DNS name the certificate is issued for; repeatable, at least one; the first "
        "is its common name",
    )
    parser.add_argument(
        "--allow-ip",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="an IP address the certificate is issued for; repeatable",
    )
    _add_replace_option(parser)
    _add_days_option(parser, CERTIFICATE_LIFETIME)


def _add_certificate_command(commands: Subparsers, command: str, description: str) -> None:
    parser = commands.add_parser(command, help=description, allow_abbrev=False)
    parser.add_argument(
        "certificate", type=Path, metavar="CERTIFICATE", help="a PEM certificate file"
    )


def _positive_days(text: str) -> int:
    try:
        return parse_positive_integer(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _init(options: argparse.Namespace) -> None:
    authority = create_authority(options.name, timedelta(days=options.days))
    _report(save_authority(options.ca_dir, authority))


def _issue_server(options: argparse.Namespace) -> None:
    name = parse_common_name(options.name)
    _issue(options, name, subject_names([name, *options.dns], options.ip), Usage.SERVER)


def _issue_client(options: argparse.Namespace) -> None:
    name = parse_common_name(options.name)
    _issue(options, name, subject_names([name]), Usage.CLIENT)


def _issue(options: argparse.Namespace, name: str, names: SubjectNames, usage: Usage) -> None:
    """Make a key here and a certificate for it. Nothing is made if a target file exists."""
    stem = _file_stem(options, name)
    _refuse_the_authoritys_own_files(options.ca_dir, identity_paths(options.out_dir, stem))
    refuse_existing(identity_paths(options.out_dir, stem), replace=options.replace)
    authority = load_authority(options.ca_dir)
    private_key = new_private_key()
    certificate = issue_certificate(
        authority, private_key.public_key(), name, names, usage, timedelta(days=options.days)
    )
    ensure_directory(options.out_dir)
    _report(save_identity(options.out_dir, stem, certificate, private_key, replace=options.replace))


def _csr(options: argparse.Namespace) -> None:
    name = parse_common_name(options.name)
    stem = _file_stem(options, name)
    refuse_existing(request_paths(options.out_dir, stem), replace=options.replace)
    private_key = new_private_key()
    request = make_request(private_key, name, subject_names([name, *options.dns], options.ip))
    ensure_directory(options.out_dir)
    _report(save_request(options.out_dir, stem, request, private_key, replace=options.replace))


def _sign_csr(options: argparse.Namespace) -> None:
    allowed = subject_names(options.allow_dns, options.allow_ip)
    _refuse_the_authoritys_own_files(options.ca_dir, [options.out])
    refuse_existing([options.out], replace=options.replace)
    certificate = sign_request(
        load_authority(options.ca_dir),
        read_request(options.csr),
        allowed,
        timedelta(days=options.days),
    )
    write_file(options.out, certificate_pem(certificate), PUBLIC_MODE, replace=options.replace)
    _report([options.out])


def _refuse_the_authoritys_own_files(authority_directory: Path, targets: Sequence[Path]) -> None:
    """Refuse to write where the authority's certificate or key is, ``--replace`` or not."""
    own = {
        (authority_directory / name).resolve()
        for name in (AUTHORITY_CERTIFICATE_NAME, AUTHORITY_KEY_NAME)
    }
    for target in targets:
        if target.resolve() in own:
            raise PkiError(f"{target} is the authority's own file; it is never overwritten")


def _pin(options: argparse.Namespace) -> None:
    print(public_key_pin(read_certificate(options.certificate)))


def _expiry(options: argparse.Namespace) -> None:
    print(days_left(read_certificate(options.certificate)))


def _file_stem(options: argparse.Namespace, name: str) -> str:
    stem: str = name if options.file_stem is None else options.file_stem
    if not _FILE_STEM_PATTERN.fullmatch(stem):
        raise PkiError(
            f"file stem {stem!r} must be letters, digits, '.', '_' and '-', starting with a "
            "letter or digit"
        )
    return stem


def _report(paths: Sequence[Path]) -> None:
    """Print what was written, one path per line. Paths only: never a file's content."""
    for path in paths:
        print(path)


_COMMANDS: dict[str, Callable[[argparse.Namespace], None]] = {
    "init": _init,
    "issue-server": _issue_server,
    "issue-client": _issue_client,
    "csr": _csr,
    "sign-csr": _sign_csr,
    "pin": _pin,
    "expiry": _expiry,
}
