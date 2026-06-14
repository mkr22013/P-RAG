"""
infrastructure/cache.py
────────────────────────────────────────────────────────────────────────────
Redis cache client for JSON index chunks.

Caches downloaded index files in Redis so subsequent queries
don't need to hit Azure Blob Storage.

Cache invalidation is event-driven — when the indexer re-indexes a document
it sends a Service Bus message which triggers a cache delete.
No TTL needed — keys live until explicitly invalidated.

Environment variables:
    REDIS_CONNECTION_STRING — Redis connection string, e.g.:
                            rediss://:password@host:6380/0
                            (note: rediss:// for SSL, redis:// for non-SSL)

Local dev fallback:
    When REDIS_CONNECTION_STRING is not set, uses a simple in-process
    dict as cache. Behaves identically but is not shared across instances.
    Zero impact on local development.

Redis key format:
    index:{year}:{plan_category}:{group_number}:{variant}
    e.g. index:2026:medical:1000016:retiree
"""

import os
from config import settings
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

REDIS_CONNECTION_STRING = settings.REDIS_CONNECTION_STRING

# ── Local dev fallback — in-process dict ─────────────────────────────────────
_local_cache: dict = {}

# ── Redis client (lazy init) ──────────────────────────────────────────────────
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.from_url(
            REDIS_CONNECTION_STRING,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        logger.info("[cache] Redis client initialised.")
    return _redis_client


# ── Public API ─────────────────────────────────────────────────────────────────


def make_redis_key(
    year: str, plan_category: str, group_number: str, variant: str
) -> str:
    """
    Builds a consistent Redis key from plan attributes.
    Centralised here so indexer and server always use the same format.
    """
    parts = [
        "index",
        year.strip(),
        plan_category.strip().lower(),
        group_number.strip(),
        (variant or "standard").strip().lower(),
    ]
    return ":".join(parts)


async def get_index(redis_key: str) -> Optional[list]:
    """
    Retrieves cached index chunks for the given key.
    Returns list of chunk dicts, or None on cache miss.
    """
    if not REDIS_CONNECTION_STRING:
        # Local dev — in-process dict
        data = _local_cache.get(redis_key)
        if data:
            logger.debug("[cache] Local hit: %s", redis_key)
            return json.loads(data)
        return None

    try:
        r = _get_redis()
        data = r.get(redis_key)
        if data:
            logger.info("[cache] Redis hit: %s", redis_key)
            return json.loads(data)
        logger.info("[cache] Redis miss: %s", redis_key)
        return None
    except Exception as exc:
        logger.error("[cache] get_index failed for %s: %s", redis_key, exc)
        return None


async def set_index(redis_key: str, chunks: list) -> bool:
    """
    Stores index chunks in cache.
    No TTL — keys live until explicitly invalidated by the indexer.

    Returns True on success, False on failure.
    """
    try:
        data = json.dumps(chunks, ensure_ascii=False)
    except Exception as exc:
        logger.error("[cache] JSON serialisation failed: %s", exc)
        return False

    if not REDIS_CONNECTION_STRING:
        # Local dev — in-process dict
        _local_cache[redis_key] = data
        logger.debug("[cache] Local set: %s (%d chunks)", redis_key, len(chunks))
        return True

    try:
        r = _get_redis()
        r.set(redis_key, data)
        logger.info("[cache] Redis set: %s (%d chunks)", redis_key, len(chunks))
        return True
    except Exception as exc:
        logger.error("[cache] set_index failed for %s: %s", redis_key, exc)
        return False


async def invalidate_index(redis_key: str) -> bool:
    """
    Deletes a cached index entry.
    Called by the cache invalidation Azure Function after re-indexing.

    Returns True if key was deleted, False if key didn't exist or on error.
    """
    if not REDIS_CONNECTION_STRING:
        existed = redis_key in _local_cache
        _local_cache.pop(redis_key, None)
        logger.debug("[cache] Local invalidated: %s", redis_key)
        return existed

    try:
        r = _get_redis()
        deleted = r.delete(redis_key)
        if deleted:
            logger.info("[cache] Redis invalidated: %s", redis_key)
        else:
            logger.info("[cache] Redis key not found (already gone): %s", redis_key)
        return bool(deleted)
    except Exception as exc:
        logger.error("[cache] invalidate_index failed for %s: %s", redis_key, exc)
        return False


async def get_cache_stats() -> dict:
    """
    Returns basic cache statistics for monitoring/health checks.
    """
    if not REDIS_CONNECTION_STRING:
        return {
            "mode": "local",
            "entries": len(_local_cache),
            "keys": list(_local_cache.keys()),
        }

    try:
        r = _get_redis()
        info = r.info("memory")
        keys = r.keys("index:*")
        return {
            "mode": "redis",
            "entries": len(keys),
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "keys": keys,
        }
    except Exception as exc:
        logger.error("[cache] get_cache_stats failed: %s", exc)
        return {"mode": "redis", "error": str(exc)}
