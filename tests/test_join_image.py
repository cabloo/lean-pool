"""The join service's two image requests: the Lean server image a window ships to a new box.

Plain HTTP on loopback, as in ``test_join.py``. What a box then does with the file (checks its
SHA-256 against the manifest before it loads it) is the box's own; the service decides nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from leanpool.join import IMAGE_CHUNK_BYTES, JoinSettings, create_application, shipped_image
from leanpool.join.image import MAXIMUM_MANIFEST_BYTES

TOKEN = "4f9d2c7a1b8e4d6f9a0c3e5b7d1f2a4c"
WRONG_TOKEN = "0" * len(TOKEN)
SCRIPT = b"#!/bin/sh\necho 'joining the pool'\n"
# A little over four chunks, so that sending it takes several reads.
IMAGE = bytes(range(256)) * (4 * IMAGE_CHUNK_BYTES // 256) + b"the last bytes of the image"

JoinClient = TestClient[web.Request, web.Application]
StartJoin = Callable[..., Awaitable[JoinClient]]


def export(directory: Path, content: bytes = IMAGE, **changes: Any) -> dict[str, Any]:
    """Write what a pool's host exports: the image file and the manifest that names it."""
    digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "tag": "example/kimina-lean-server:lean4.27.0",
        "image_id": "sha256:" + "ab" * 32,
        "layers": "cd" * 32,
        "file": f"image-{digest[:12]}.tar",
        "sha256": digest,
        "bytes": len(content),
        "machine": "x86_64",
        "format": "docker-save",
        "docker": "28.0.4",
        **changes,
    }
    directory.mkdir(exist_ok=True)
    (directory / f"image-{digest[:12]}.tar").write_bytes(content)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    directory = tmp_path / "spool"
    (directory / "requests").mkdir(parents=True)
    (directory / "responses").mkdir()
    return directory


@pytest.fixture
async def start_join(spool: Path, tmp_path: Path) -> AsyncIterator[StartJoin]:
    clients: list[JoinClient] = []

    async def start(image_directory: Path | None) -> JoinClient:
        settings = JoinSettings(
            token=TOKEN,
            script=SCRIPT,
            spool=spool,
            api_key="pool-key",
            tls_pem=tmp_path / "unused",
            image_directory=image_directory,
        )
        server = TestServer(create_application(settings))
        await server.start_server(access_log=None)
        client = TestClient(server)
        await client.start_server()
        clients.append(client)
        return client

    yield start
    for client in clients:
        await client.close()


@pytest.fixture
def images(tmp_path: Path) -> Path:
    return tmp_path / "image"


async def test_the_manifest_is_served_as_the_pools_host_wrote_it(
    start_join: StartJoin, images: Path
) -> None:
    manifest = export(images)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image.json") as response:
        assert response.status == 200
        assert await response.read() == (images / "manifest.json").read_bytes()
        assert await response.json() == manifest
        assert response.headers["Cache-Control"] == "no-store"


async def test_the_image_is_served_whole_and_is_what_the_manifest_names(
    start_join: StartJoin, images: Path
) -> None:
    manifest = export(images)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image") as response:
        assert response.status == 200
        body = await response.read()
    assert body == IMAGE
    assert len(body) == manifest["bytes"] > 4 * IMAGE_CHUNK_BYTES
    assert hashlib.sha256(body).hexdigest() == manifest["sha256"]


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=1000-1999", 1000, 1999),
        ("bytes=0-0", 0, 0),
        # What `curl -C -` sends for a download that stopped: everything from that byte on.
        (f"bytes={IMAGE_CHUNK_BYTES + 17}-", IMAGE_CHUNK_BYTES + 17, len(IMAGE) - 1),
        (f"bytes={len(IMAGE) - 1}-", len(IMAGE) - 1, len(IMAGE) - 1),
    ],
)
async def test_a_range_is_answered_with_exactly_those_bytes(
    start_join: StartJoin, images: Path, header: str, first: int, last: int
) -> None:
    export(images)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image", headers={"Range": header}) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == f"bytes {first}-{last}/{len(IMAGE)}"
        assert await response.read() == IMAGE[first : last + 1]


async def test_a_download_cut_short_is_completed_by_a_second_request(
    start_join: StartJoin, images: Path
) -> None:
    """The first request is closed after a part of the body; the second asks for the rest."""
    manifest = export(images)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image") as response:
        received = await response.content.readexactly(IMAGE_CHUNK_BYTES + 5)
    rest_from = {"Range": f"bytes={len(received)}-"}
    async with join.get(f"/j/{TOKEN}/image", headers=rest_from) as response:
        assert response.status == 206
        received += await response.read()
    assert hashlib.sha256(received).hexdigest() == manifest["sha256"]


async def test_a_range_past_the_end_is_refused(start_join: StartJoin, images: Path) -> None:
    export(images)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image", headers={"Range": f"bytes={len(IMAGE)}-"}) as response:
        assert response.status == 416


async def test_a_window_that_ships_no_image_says_so(start_join: StartJoin, images: Path) -> None:
    for directory in (None, images):  # no directory given; a directory with nothing in it
        images.mkdir(exist_ok=True)
        join = await start_join(directory)
        async with join.get(f"/j/{TOKEN}/image.json") as response:
            assert response.status == 204
            assert await response.read() == b""
        async with join.get(f"/j/{TOKEN}/image") as response:
            assert response.status == 404
            assert await response.json() == {"detail": "this join window ships no image"}


@pytest.mark.parametrize("what", ["/image.json", "/image"])
async def test_without_the_token_the_image_requests_are_answered_404_like_everything_else(
    start_join: StartJoin, images: Path, what: str
) -> None:
    export(images)
    join = await start_join(images)
    for path in (f"/j/{WRONG_TOKEN}{what}", f"/j/{TOKEN[:-1]}{what}", f"/j/{TOKEN}{what}/more"):
        async with join.get(path) as response:
            assert response.status == 404
            assert await response.json() == {"detail": "Not Found"}
    async with join.post(f"/j/{TOKEN}{what}") as response:  # nothing is ever written through them
        assert response.status == 404


async def test_the_image_requests_bind_nothing_and_a_bound_window_refuses_another_address(
    start_join: StartJoin, images: Path, spool: Path
) -> None:
    export(images)
    join = await start_join(images)
    for what in ("/image.json", "/image", "/image.json"):
        async with join.get(f"/j/{TOKEN}{what}") as response:
            assert response.status == 200
            await response.read()
    assert list((spool / "requests").iterdir()) == []  # the window is as it was: nobody's
    # Bound to a box at another address, the window gives this caller nothing, the image included.
    bound = {"name": "lean-a", "lean_port": 8000, "agent_port": 18200, "workers": 6, "csr": "x"}
    (spool / "requests" / "csr.json").write_text(json.dumps({**bound, "address": "192.0.2.9"}))
    for what in ("/image.json", "/image"):
        async with join.get(f"/j/{TOKEN}{what}") as response:
            assert response.status == 409
            assert await response.json() == {
                "reason": "this join window is bound to another address"
            }


def broken_exports() -> dict[str, Callable[[Path], None]]:
    """Ways a directory can hold something that must not be served."""

    def outside(directory: Path) -> None:
        (directory.parent / "secret.tar").write_bytes(IMAGE)
        export(directory, file="../secret.tar")

    def another_name(directory: Path) -> None:
        (directory / "api-key").write_bytes(IMAGE)
        export(directory, file="api-key")

    def absolute(directory: Path) -> None:
        export(directory, file=str(directory / "manifest.json"))

    def a_link(directory: Path) -> None:
        manifest = export(directory)
        (directory.parent / "elsewhere").write_bytes(IMAGE)
        (directory / manifest["file"]).unlink()
        (directory / manifest["file"]).symlink_to(directory.parent / "elsewhere")

    def another_size(directory: Path) -> None:
        export(directory, bytes=len(IMAGE) - 1)

    def no_file(directory: Path) -> None:
        manifest = export(directory)
        (directory / manifest["file"]).unlink()

    def not_json(directory: Path) -> None:
        export(directory)
        (directory / "manifest.json").write_text("{not json")

    def not_an_object(directory: Path) -> None:
        export(directory)
        (directory / "manifest.json").write_text(json.dumps(["image-0123456789ab.tar"]))

    def too_large(directory: Path) -> None:
        export(directory, padding="x" * MAXIMUM_MANIFEST_BYTES)

    def size_is_text(directory: Path) -> None:
        export(directory, bytes=str(len(IMAGE)))

    return {function.__name__: function for function in (
        outside, another_name, absolute, a_link, another_size, no_file, not_json, not_an_object,
        too_large, size_is_text,
    )}  # fmt: skip


@pytest.mark.parametrize("case", sorted(broken_exports()))
async def test_an_export_that_is_not_one_plain_image_file_of_the_recorded_size_ships_nothing(
    start_join: StartJoin, images: Path, case: str
) -> None:
    images.mkdir()
    broken_exports()[case](images)
    assert shipped_image(images) is None
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image.json") as response:
        assert response.status == 204
    async with join.get(f"/j/{TOKEN}/image") as response:
        assert response.status == 404
        assert IMAGE[:64] not in await response.read()


def test_a_good_export_is_found_and_nothing_is_read_but_the_manifest(images: Path) -> None:
    manifest = export(images)
    shipped = shipped_image(images)
    assert shipped is not None
    assert shipped.file == images / manifest["file"]
    assert json.loads(shipped.manifest) == manifest
    assert shipped_image(None) is None
    assert shipped_image(images / "not-there") is None


async def test_the_image_is_sent_in_bounded_chunks_and_never_read_whole(
    start_join: StartJoin, images: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service may run in 256 MB and the image is many GB: it is handed to the web
    framework's file response with a chunk size, and nothing here reads the file.
    """
    export(images)
    handed: list[dict[str, Any]] = []
    file_response = web.FileResponse

    def recording(path: Path, **options: Any) -> web.FileResponse:
        handed.append({"path": path, **options})
        return file_response(path, **options)

    monkeypatch.setattr("aiohttp.web.FileResponse", recording)
    read_whole: list[Path] = []
    read_bytes = Path.read_bytes

    def noted_read(path: Path) -> bytes:
        read_whole.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", noted_read)
    join = await start_join(images)
    async with join.get(f"/j/{TOKEN}/image") as response:
        assert await response.read() == IMAGE
    assert [call["chunk_size"] for call in handed] == [IMAGE_CHUNK_BYTES]
    assert IMAGE_CHUNK_BYTES == 256 * 1024
    # (The spool's small files are read that way; the image, never.)
    assert [path for path in read_whole if path.parent == images] == []


async def test_what_is_logged_about_an_image_names_the_caller_and_never_the_token(
    start_join: StartJoin, images: Path, caplog: pytest.LogCaptureFixture
) -> None:
    export(images)
    join = await start_join(images)
    with caplog.at_level(logging.INFO):
        for headers in ({}, {"Range": "bytes=10-"}):
            async with join.get(f"/j/{TOKEN}/image", headers=headers) as response:
                await response.read()
        async with join.get(f"/j/{TOKEN}/image.json") as response:
            await response.read()
    logged = [record.getMessage() for record in caplog.records]
    assert "the image is being sent to 127.0.0.1" in logged
    assert "the image is being sent to 127.0.0.1 (a part of it: a download goes on)" in logged
    assert "the image's manifest was fetched from 127.0.0.1" in logged
    assert TOKEN not in caplog.text
