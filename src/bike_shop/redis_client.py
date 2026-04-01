"""Lazy singleton Redis connection with graceful degradation."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_redis = None
_redis_failed = False


def get_redis():
    """Get or create a Redis client. Returns None if unavailable."""
    global _redis, _redis_failed
    if _redis is not None:
        return _redis
    if _redis_failed:
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
        logger.info("Redis connected (%s:%d)", host, port)
        return _redis
    except ImportError:
        logger.warning("redis package not installed — short-term memory disabled")
        _redis_failed = True
        return None
    except Exception as e:
        logger.error("Failed to connect Redis (%s:%d): %s", host, port, e)
        _redis_failed = True
        return None


def reset_redis() -> None:
    """Reset singleton for testing."""
    global _redis, _redis_failed
    _redis = None
    _redis_failed = False
