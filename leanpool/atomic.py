"""Writing a file so that it is complete or absent, never half-written. Standard library only."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write_atomically(path: Path, data: bytes, mode: int, *, replace: bool = False) -> None:
    """Put ``data`` at ``path`` in one step, with ``mode``.

    The data is written to a temporary file beside the target, which has ``mode`` before it has
    any content. Without ``replace`` the temporary file is then linked to the target's name,
    which fails with ``FileExistsError`` if the name is taken: two writers racing for one name
    cannot both win, and neither can leave a half-written file. With ``replace`` it is renamed
    over the target. A reader sees the old content, or none, or all of the new content.
    """
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            os.fchmod(temporary_file.fileno(), mode)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if replace:
            os.replace(temporary_name, path)
        else:
            os.link(temporary_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
