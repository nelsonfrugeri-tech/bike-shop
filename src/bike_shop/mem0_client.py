"""Lazy singleton Mem0 client with graceful degradation."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded Mem0 client
_mem0 = None


def get_mem0():
    """Get or create a Mem0 client. Returns None if not configured."""
    global _mem0
    if _mem0 is not None:
        return _mem0

    qdrant_host = os.environ.get("QDRANT_HOST", "localhost")
    qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    try:
        from mem0 import Memory

        config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": qdrant_host,
                    "port": qdrant_port,
                    "collection_name": "bike-shop-memory",
                    "embedding_model_dims": 768,
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": "nomic-embed-text",
                    "ollama_base_url": ollama_url,
                },
            },
        }

        # Use Anthropic for memory extraction if key available
        if anthropic_key:
            config["llm"] = {
                "provider": "anthropic",
                "config": {
                    "model": "claude-haiku-4-5-20251001",
                    "api_key": anthropic_key,
                },
            }

        _mem0 = Memory.from_config(config)
        logger.info("Mem0 connected (qdrant=%s:%d, ollama=%s)", qdrant_host, qdrant_port, ollama_url)
        return _mem0
    except ImportError:
        logger.warning("mem0ai not installed — memory agent disabled")
        return None
    except Exception as e:
        logger.error("Failed to connect Mem0: %s", e)
        return None


def reset_mem0() -> None:
    """Reset singleton for testing."""
    global _mem0
    _mem0 = None
