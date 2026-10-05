"""The shared result cache: answers a repeated check from its store, forwards the rest."""

from leanpool.cache.app import create_application
from leanpool.cache.keys import check_key
from leanpool.cache.policy import DEFAULT_EXHAUSTION_PATTERNS, is_storable
from leanpool.cache.settings import CacheSettings
from leanpool.cache.store import AsyncResultStore, ResultStore, StoreStatistics

__all__ = [
    "DEFAULT_EXHAUSTION_PATTERNS",
    "AsyncResultStore",
    "CacheSettings",
    "ResultStore",
    "StoreStatistics",
    "check_key",
    "create_application",
    "is_storable",
]
