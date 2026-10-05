"""Reading the server list file and replacing it atomically."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from leanpool.haproxy.servers import ServerListError

_NEW_FILE_MODE = 0o644


def read_server_file(path: Path, *, missing_is_empty: bool = False) -> str:
    """Return the file's text. A missing file is an empty list only when the caller allows it.

    Adding the first server to a pool creates the file; every other operation on a file that
    does not exist is a mistyped path, and treating it as an empty pool would hide that.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_is_empty:
            return ""
        raise ServerListError(f"the server list {path} does not exist") from None
    except (OSError, UnicodeDecodeError) as error:
        raise ServerListError(f"cannot read the server list {path}: {error}") from error


def edit_server_file(path: Path, edit: Callable[[str], str], *, may_create: bool = False) -> None:
    """Replace the file with ``edit(its text)``, or leave it untouched if ``edit`` refuses.

    ``edit`` validates before anything is written, so a refused change leaves the file
    byte-identical. An accepted one is written to a temporary file beside the original and
    renamed over it: a reader (the proxy's reload) sees the old list or the new one, never a
    half-written file, even if this process dies part-way.
    """
    new_text = edit(read_server_file(path, missing_is_empty=may_create))
    write_file_atomically(path, new_text)


def write_file_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that the file is replaced in one step, keeping its mode."""
    mode = _existing_mode(path)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _existing_mode(path: Path) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return _NEW_FILE_MODE
