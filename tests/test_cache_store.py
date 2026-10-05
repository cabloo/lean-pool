"""The result store: persistence, the size cap, and least-recently-used eviction."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from leanpool.cache import AsyncResultStore, ResultStore


def key(number: int) -> bytes:
    return number.to_bytes(32, "big")


def payload(size: int, fill: bytes = b"x") -> bytes:
    return fill * size


ENTRY_BYTES = 32 + 68  # a 32-byte key and a 68-byte payload: 100 bytes per entry


def filled_store(path: Path, maximum_entries: int, entries: int) -> ResultStore:
    store = ResultStore(path, maximum_bytes=maximum_entries * ENTRY_BYTES)
    for number in range(entries):
        store.put(key(number), payload(68))
    return store


def test_a_stored_result_is_returned_and_an_unknown_key_is_not(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "cache.sqlite3", maximum_bytes=10_000)
    store.put(key(1), b'{"time":0.5}')
    assert store.get(key(1)) == b'{"time":0.5}'
    assert store.get(key(2)) is None


def test_the_size_counts_keys_and_payloads(tmp_path: Path) -> None:
    store = filled_store(tmp_path / "cache.sqlite3", maximum_entries=10, entries=3)
    statistics = store.statistics()
    assert (statistics.entries, statistics.stored_bytes, statistics.evictions) == (3, 300, 0)
    assert statistics.file_bytes > statistics.stored_bytes


def test_the_store_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    store = filled_store(path, maximum_entries=10, entries=3)
    store.close()
    reopened = ResultStore(path, maximum_bytes=10 * ENTRY_BYTES)
    assert reopened.get(key(2)) == payload(68)
    assert (reopened.statistics().entries, reopened.statistics().stored_bytes) == (3, 300)


def test_eviction_keeps_the_store_under_its_cap(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "cache.sqlite3", maximum_bytes=5 * ENTRY_BYTES)
    for number in range(50):
        store.put(key(number), payload(68))
        assert store.statistics().stored_bytes <= 5 * ENTRY_BYTES
    statistics = store.statistics()
    assert (statistics.entries, statistics.stored_bytes, statistics.evictions) == (5, 500, 45)


def test_the_least_recently_written_entries_are_evicted_first(tmp_path: Path) -> None:
    store = filled_store(tmp_path / "cache.sqlite3", maximum_entries=5, entries=7)
    assert [store.get(key(number)) is not None for number in range(7)] == [
        False,
        False,
        True,
        True,
        True,
        True,
        True,
    ]


def test_reading_an_entry_protects_it_from_eviction(tmp_path: Path) -> None:
    store = filled_store(tmp_path / "cache.sqlite3", maximum_entries=5, entries=5)
    assert store.get(key(0)) is not None  # the oldest entry becomes the most recently used
    store.put(key(5), payload(68))
    store.put(key(6), payload(68))
    assert store.get(key(0)) is not None
    assert store.get(key(1)) is None
    assert store.get(key(2)) is None
    assert store.get(key(3)) is not None


def test_recency_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    store = filled_store(path, maximum_entries=5, entries=5)
    assert store.get(key(0)) is not None
    store.close()
    reopened = ResultStore(path, maximum_bytes=5 * ENTRY_BYTES)
    reopened.put(key(5), payload(68))
    assert reopened.get(key(0)) is not None
    assert reopened.get(key(1)) is None


def test_a_large_entry_evicts_as_many_as_it_needs(tmp_path: Path) -> None:
    store = filled_store(tmp_path / "cache.sqlite3", maximum_entries=5, entries=5)
    store.put(key(9), payload(268))  # 300 bytes: the size of three entries
    statistics = store.statistics()
    assert (statistics.entries, statistics.stored_bytes) == (3, 500)
    assert store.get(key(2)) is None
    assert store.get(key(3)) is not None


def test_an_entry_larger_than_the_cap_is_not_stored_and_evicts_nothing(tmp_path: Path) -> None:
    store = filled_store(tmp_path / "cache.sqlite3", maximum_entries=5, entries=5)
    store.put(key(9), payload(5 * ENTRY_BYTES))
    assert store.get(key(9)) is None
    assert store.statistics().entries == 5


def test_storing_under_an_existing_key_replaces_the_entry(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "cache.sqlite3", maximum_bytes=10_000)
    store.put(key(1), payload(68))
    store.put(key(1), payload(168, b"y"))
    assert store.get(key(1)) == payload(168, b"y")
    statistics = store.statistics()
    assert (statistics.entries, statistics.stored_bytes) == (1, 200)


def test_a_smaller_cap_takes_effect_on_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    filled_store(path, maximum_entries=10, entries=10).close()
    smaller = ResultStore(path, maximum_bytes=3 * ENTRY_BYTES)
    smaller.put(key(99), payload(68))
    assert smaller.statistics().entries == 3
    assert smaller.get(key(99)) is not None


async def test_the_async_store_gives_the_same_answers(tmp_path: Path) -> None:
    store = await AsyncResultStore.open(tmp_path / "cache.sqlite3", maximum_bytes=10_000)
    try:
        await store.put(key(1), b"payload")
        assert await store.get(key(1)) == b"payload"
        assert await store.get(key(2)) is None
        assert (await store.statistics()).entries == 1
    finally:
        await store.close()


async def test_a_slow_disk_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the store's thread is stuck in a slow read, the loop keeps running other work."""
    store_threads: list[int] = []
    original_get = ResultStore.get

    def slow_get(self: ResultStore, wanted: bytes) -> bytes | None:
        store_threads.append(threading.get_ident())
        time.sleep(0.3)
        return original_get(self, wanted)

    monkeypatch.setattr(ResultStore, "get", slow_get)
    store = await AsyncResultStore.open(tmp_path / "cache.sqlite3", maximum_bytes=10_000)
    try:
        pending = asyncio.create_task(store.get(key(1)))
        turns_of_the_loop = 0
        while not pending.done():
            await asyncio.sleep(0.01)
            turns_of_the_loop += 1
        assert await pending is None
    finally:
        await store.close()
    assert turns_of_the_loop >= 5
    assert store_threads == [store_threads[0]]
    assert store_threads[0] != threading.get_ident()
