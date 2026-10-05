"""The persistent result store: SQLite, bounded, least recently used out first.

``ResultStore`` is plain synchronous code, tested as such. ``AsyncResultStore`` runs it on one
dedicated thread so that no disk access ever happens on the service's event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, TypeVarTuple

Result = TypeVar("Result")
Arguments = TypeVarTuple("Arguments")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    key BLOB PRIMARY KEY,
    payload BLOB NOT NULL,
    size_bytes INTEGER NOT NULL,
    last_used INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS results_by_recency ON results (last_used, size_bytes);
"""
# Answered from the recency index alone, so opening a large store does not read every payload.
_TOTALS_QUERY = """
SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), COALESCE(MAX(last_used), 0) FROM results
"""
_WRITE_AHEAD_LOG_SUFFIX = "-wal"


@dataclass(frozen=True)
class StoreStatistics:
    """The store's size.

    ``stored_bytes`` is what the size cap counts: the keys and payloads of the entries.
    ``file_bytes`` is what the database occupies on disk, which is larger (SQLite's pages and
    indexes, and its write-ahead log).
    """

    entries: int
    stored_bytes: int
    file_bytes: int
    evictions: int


class ResultStore:
    """Results by key in one SQLite file, evicting the least recently used above a size cap.

    Recency is a counter, not a clock: each read or write stamps the entry with the next
    number. That gives a strict order even for entries touched within the same millisecond, and
    a wall clock that jumps cannot reorder the cache.

    Durability is tuned for a cache: write-ahead logging with ``synchronous=NORMAL`` cannot
    corrupt the file, and what a power cut can lose (the last few writes) costs only a recheck.

    Not thread-safe: use it from one thread, as ``AsyncResultStore`` does.
    """

    def __init__(self, path: Path, maximum_bytes: int) -> None:
        self._path = path
        self._maximum_bytes = maximum_bytes
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        with self._connection:
            self._connection.executescript(_SCHEMA)
        entries, stored_bytes, latest_use = self._connection.execute(_TOTALS_QUERY).fetchone()
        self._entries: int = entries
        self._stored_bytes: int = stored_bytes
        self._latest_use: int = latest_use
        self._evictions = 0

    def get(self, key: bytes) -> bytes | None:
        """Return the payload stored under ``key`` and mark it as just used, or None."""
        row = self._connection.execute(
            "SELECT payload FROM results WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        with self._connection:
            self._connection.execute(
                "UPDATE results SET last_used = ? WHERE key = ?", (self._next_use(), key)
            )
        payload: bytes = row[0]
        return payload

    def put(self, key: bytes, payload: bytes) -> None:
        """Store ``payload`` under ``key``, then evict the least recently used entries until the
        store is within its cap.

        An entry larger than the whole cap is not stored: it could only be kept by evicting
        everything else, itself included on the next write.
        """
        size_bytes = len(key) + len(payload)
        if size_bytes > self._maximum_bytes:
            return
        with self._connection:
            replaced_bytes = self._size_of(key)
            self._connection.execute(
                "INSERT OR REPLACE INTO results (key, payload, size_bytes, last_used) "
                "VALUES (?, ?, ?, ?)",
                (key, payload, size_bytes, self._next_use()),
            )
            stored_bytes = self._stored_bytes + size_bytes - (replaced_bytes or 0)
            evicted_entries, evicted_bytes = self._evict_down_to_cap(stored_bytes)
        self._entries += (1 if replaced_bytes is None else 0) - evicted_entries
        self._stored_bytes = stored_bytes - evicted_bytes
        self._evictions += evicted_entries

    def statistics(self) -> StoreStatistics:
        """Return the store's current size."""
        return StoreStatistics(
            entries=self._entries,
            stored_bytes=self._stored_bytes,
            file_bytes=self._file_bytes(),
            evictions=self._evictions,
        )

    def close(self) -> None:
        """Close the database."""
        self._connection.close()

    def _next_use(self) -> int:
        self._latest_use += 1
        return self._latest_use

    def _size_of(self, key: bytes) -> int | None:
        row = self._connection.execute(
            "SELECT size_bytes FROM results WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else int(row[0])

    def _evict_down_to_cap(self, stored_bytes: int) -> tuple[int, int]:
        """Delete least recently used entries until ``stored_bytes`` fits the cap.

        Returns how many entries and bytes were removed. Runs inside the caller's transaction,
        so a failed write leaves neither a half-evicted store nor wrong totals.
        """
        evicted_entries = evicted_bytes = 0
        while stored_bytes - evicted_bytes > self._maximum_bytes:
            row = self._connection.execute(
                "SELECT rowid, size_bytes FROM results ORDER BY last_used LIMIT 1"
            ).fetchone()
            if row is None:
                break
            row_id, size_bytes = row
            self._connection.execute("DELETE FROM results WHERE rowid = ?", (row_id,))
            evicted_entries += 1
            evicted_bytes += size_bytes
        return evicted_entries, evicted_bytes

    def _file_bytes(self) -> int:
        write_ahead_log = self._path.with_name(self._path.name + _WRITE_AHEAD_LOG_SUFFIX)
        return sum(file.stat().st_size for file in (self._path, write_ahead_log) if file.exists())


class AsyncResultStore:
    """A ``ResultStore`` whose every call runs on one dedicated thread.

    One thread, not a pool: SQLite allows one writer at a time anyway, a single thread needs no
    locking around the store's counters, and the event loop is never blocked by the disk.
    """

    def __init__(self, store: ResultStore, executor: ThreadPoolExecutor) -> None:
        self._store = store
        self._executor = executor

    @classmethod
    async def open(cls, path: Path, maximum_bytes: int) -> AsyncResultStore:
        """Open (or create) the database at ``path`` on the store's own thread."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="leanpool-store")
        loop = asyncio.get_running_loop()
        try:
            store = await loop.run_in_executor(executor, ResultStore, path, maximum_bytes)
        except BaseException:
            executor.shutdown(wait=False)
            raise
        return cls(store, executor)

    async def get(self, key: bytes) -> bytes | None:
        """Return the payload stored under ``key`` and mark it as just used, or None."""
        return await self._run(self._store.get, key)

    async def put(self, key: bytes, payload: bytes) -> None:
        """Store ``payload`` under ``key`` and evict down to the cap."""
        await self._run(self._store.put, key, payload)

    async def statistics(self) -> StoreStatistics:
        """Return the store's current size."""
        return await self._run(self._store.statistics)

    async def close(self) -> None:
        """Close the database and release the thread."""
        await self._run(self._store.close)
        self._executor.shutdown(wait=False)

    async def _run(
        self, function: Callable[[*Arguments], Result], *arguments: *Arguments
    ) -> Result:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function, *arguments)
