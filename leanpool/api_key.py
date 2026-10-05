"""Reading a pool's API key from a file.

A key is never accepted as a command-line argument anywhere in this project: arguments are
visible to every user of the machine in the process list, and they end up in shell history.
"""

from __future__ import annotations

from pathlib import Path


class ApiKeyError(ValueError):
    """The API key is missing, empty or malformed. The message never contains the key."""


def validate_api_key(text: str) -> str:
    """Return the key with surrounding whitespace removed, or refuse it.

    A key is sent as ``Authorization: Bearer <key>``, so it must be a single run of printable
    ASCII with no spaces. Refusing anything else catches the usual accidents (an empty file, a
    file holding two keys, a pasted key with a line break in it) before they become a pool whose
    clients are all rejected.
    """
    key = text.strip()
    if not key:
        raise ApiKeyError("the API key is empty")
    if not key.isascii() or not key.isprintable() or any(character.isspace() for character in key):
        raise ApiKeyError("the API key must be one run of printable ASCII without spaces")
    return key


def read_api_key_file(path: Path) -> str:
    """Read and validate the API key stored in ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ApiKeyError(f"cannot read the API key file {path}: {error}") from error
    try:
        return validate_api_key(text)
    except ApiKeyError as error:
        raise ApiKeyError(f"{path}: {error}") from error
