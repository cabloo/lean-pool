"""The Lean server image a join window ships: its manifest and its file, found and checked.

A pool's host may export the image its own Lean server runs, so that a joining box loads it
instead of building its own. The export is two files in one directory: ``manifest.json`` and the
image file the manifest names. The service serves both as they are and decides nothing about
them; the box checks the file against the manifest before it loads it.

The manifest is written by the pool's host, but it is still read as untrusted text: the file it
names must be a plain ``image-<12 hex>.tar`` directly inside the directory, a regular file of
exactly the recorded size. Anything else ships nothing.
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "manifest.json"
# The image is sent from disk in chunks of this size and never read whole: it is many GB, and
# the service may run in a container with 256 MB.
IMAGE_CHUNK_BYTES = 256 * 1024
MAXIMUM_MANIFEST_BYTES = 16 * 1024

_FILE_PATTERN = re.compile(r"image-[0-9a-f]{12}\.tar")


@dataclass(frozen=True)
class ShippedImage:
    """What a window ships: the manifest as the pool's host wrote it, and the file it names."""

    manifest: bytes
    file: Path


def shipped_image(directory: Path | None) -> ShippedImage | None:
    """Return the image ``directory`` holds, or None when it holds none that can be served.

    None for: no directory, no manifest, a manifest larger than 16 KiB or that is not a JSON
    object, a ``file`` that is not ``image-<12 hex>.tar``, a file that is missing, a symbolic
    link or not a regular file, and a file whose size is not the manifest's ``bytes``.
    """
    if directory is None:
        return None
    try:
        manifest = _read_limited(directory / MANIFEST_NAME)
        record = json.loads(manifest)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    name, size = record.get("file"), record.get("bytes")
    if not isinstance(name, str) or not _FILE_PATTERN.fullmatch(name):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return None
    file = directory / name
    try:
        status = file.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_size != size:
        return None
    return ShippedImage(manifest=manifest, file=file)


def _read_limited(path: Path) -> bytes:
    """Read a small file, refusing one that is larger than a manifest can be."""
    with path.open("rb") as handle:
        content = handle.read(MAXIMUM_MANIFEST_BYTES + 1)
    if len(content) > MAXIMUM_MANIFEST_BYTES:
        raise ValueError(f"{path.name} is larger than {MAXIMUM_MANIFEST_BYTES} bytes")
    return content
