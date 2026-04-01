"""Short-term memory backed by Redis — per-agent, per-project, per-thread buffers."""

from __future__ import annotations

import json
import logging
from typing import Any

from bike_shop.redis_client import get_redis

logger = logging.getLogger(__name__)

# Defaults
THREAD_LIMIT = 20
RECENT_LIMIT = 10
TTL_SECONDS = 86400  # 24 hours


class ShortTermMemory:
    """Redis-backed short-term conversation memory.

    Key format invariant:
        bike-shop:{agent}:{project}:{channel}:{thread_ts}
        - agent: lowercase, hyphenated (e.g., "mr-robot")
        - project: lowercase, hyphenated (e.g., "market-analysis")
        - channel: Slack channel ID (alphanumeric, e.g., "C0APD9JNE10")
        - thread_ts: Slack timestamp (digits and dot, e.g., "1774992784.411319")
        None of these fields contain ":" so split(":") is safe.

    Key patterns:
        Thread buffer: bike-shop:{agent}:{project}:{channel}:{thread_ts}
        Recent index:  bike-shop:{agent}:{project}:recent
    """

    @staticmethod
    def _thread_key(agent: str, project: str, channel: str, thread_ts: str) -> str:
        return f"bike-shop:{agent}:{project}:{channel}:{thread_ts}"

    @staticmethod
    def _recent_key(agent: str, project: str) -> str:
        return f"bike-shop:{agent}:{project}:recent"

    @staticmethod
    def _meta_key(key: str) -> str:
        return f"meta:{key}"

    def push(
        self,
        agent: str,
        project: str,
        channel: str,
        thread_ts: str,
        entry: dict[str, Any],
    ) -> bool:
        """Push entry to thread buffer. Returns True on success."""
        r = get_redis()
        if r is None:
            return False

        key = self._thread_key(agent, project, channel, thread_ts)
        try:
            pipe = r.pipeline()
            pipe.lpush(key, json.dumps(entry))
            pipe.ltrim(key, 0, THREAD_LIMIT - 1)
            pipe.expire(key, TTL_SECONDS)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("[short-term] Failed to push to %s: %s", key, e)
            return False

    def push_recent(
        self,
        agent: str,
        project: str,
        entry: dict[str, Any],
    ) -> bool:
        """Push entry to recent activity index. Returns True on success."""
        r = get_redis()
        if r is None:
            return False

        key = self._recent_key(agent, project)
        try:
            pipe = r.pipeline()
            pipe.lpush(key, json.dumps(entry))
            pipe.ltrim(key, 0, RECENT_LIMIT - 1)
            pipe.expire(key, TTL_SECONDS)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("[short-term] Failed to push recent to %s: %s", key, e)
            return False

    def get_thread(
        self,
        agent: str,
        project: str,
        channel: str,
        thread_ts: str,
        limit: int = THREAD_LIMIT,
    ) -> list[dict[str, Any]]:
        """Get thread buffer entries (newest first)."""
        r = get_redis()
        if r is None:
            return []

        key = self._thread_key(agent, project, channel, thread_ts)
        try:
            raw = r.lrange(key, 0, limit - 1)
            return [json.loads(item) for item in raw]
        except Exception as e:
            logger.warning("[short-term] Failed to get thread %s: %s", key, e)
            return []

    def get_recent(
        self,
        agent: str,
        project: str,
        limit: int = RECENT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Get recent activity entries (newest first)."""
        r = get_redis()
        if r is None:
            return []

        key = self._recent_key(agent, project)
        try:
            raw = r.lrange(key, 0, limit - 1)
            return [json.loads(item) for item in raw]
        except Exception as e:
            logger.warning("[short-term] Failed to get recent %s: %s", key, e)
            return []

    def scan_expiring(
        self,
        ttl_threshold_seconds: int = 7200,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        """Find thread keys with TTL < threshold. Returns list of (key, messages)."""
        r = get_redis()
        if r is None:
            return []

        results: list[tuple[str, list[dict[str, Any]]]] = []
        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match="bike-shop:*:*:*:*", count=100)
                for key in keys:
                    # Skip meta keys and recent keys
                    if key.startswith("meta:") or key.endswith(":recent"):
                        continue
                    ttl = r.ttl(key)
                    if 0 < ttl < ttl_threshold_seconds:
                        raw = r.lrange(key, 0, -1)
                        messages = [json.loads(item) for item in raw]
                        if messages:
                            results.append((key, messages))
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("[short-term] Failed to scan expiring keys: %s", e)

        return results

    def mark_summarized(self, key: str) -> bool:
        """Mark a key as already summarized."""
        r = get_redis()
        if r is None:
            return False

        try:
            meta = self._meta_key(key)
            r.hset(meta, "summarized", "1")
            r.expire(meta, TTL_SECONDS)
            return True
        except Exception as e:
            logger.warning("[short-term] Failed to mark summarized %s: %s", key, e)
            return False

    def is_summarized(self, key: str) -> bool:
        """Check if a key has been summarized."""
        r = get_redis()
        if r is None:
            return False

        try:
            meta = self._meta_key(key)
            return r.hget(meta, "summarized") == "1"
        except Exception:
            return False
