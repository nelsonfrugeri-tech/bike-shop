"""Lazy singleton Redis connection with graceful degradation and temporal retry."""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

_redis = None
_redis_failed_at: float | None = None
_RETRY_AFTER = 60  # seconds — retry connection after this cooldown


def get_redis():
    """Get or create a Redis client. Returns None if unavailable.

    Uses temporal backoff: after a failed connection attempt, waits
    _RETRY_AFTER seconds before trying again (avoids hammering a down server).
    If an existing connection breaks, it is discarded and retried on cooldown.
    """
    global _redis, _redis_failed_at

    # Fast path: existing healthy connection
    if _redis is not None:
        try:
            _redis.ping()
            return _redis
        except Exception:
            _redis = None
            _redis_failed_at = time.monotonic()
            return None

    # Cooldown: don't retry too soon after a failure
    if _redis_failed_at is not None and (time.monotonic() - _redis_failed_at < _RETRY_AFTER):
        return None

    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))

    try:
        import redis

        client = redis.Redis(
            host=host,
            port=port,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        client.ping()
        _redis = client
        _redis_failed_at = None
        logger.info("Redis connected (%s:%d)", host, port)
        return _redis
    except ImportError:
        logger.warning("redis package not installed — short-term memory disabled")
        _redis_failed_at = time.monotonic()
        return None
    except Exception as e:
        logger.error("Failed to connect Redis (%s:%d): %s", host, port, e)
        _redis_failed_at = time.monotonic()
        return None


def reset_redis() -> None:
    """Reset singleton for testing."""
    global _redis, _redis_failed_at
    _redis = None
    _redis_failed_at = None
