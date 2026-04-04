"""Lazy Mem0 client pool with graceful degradation.

Supports multiple collections (one per project) via a dict of singletons.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded Mem0 clients keyed by collection name
_mem0_clients: dict[str, object] = {}


def get_mem0(collection_name: str = "bike-shop-memory") -> object | None:
    """Get or create a Mem0 client for the given collection. Returns None if not configured."""
    if collection_name in _mem0_clients:
        return _mem0_clients[collection_name]

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
                    "collection_name": collection_name,
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

        client = Memory.from_config(config)
        _mem0_clients[collection_name] = client
        logger.info(
            "Mem0 connected (collection=%s, qdrant=%s:%d, ollama=%s)",
            collection_name, qdrant_host, qdrant_port, ollama_url,
        )
        return client
    except ImportError:
        logger.warning("mem0ai not installed — memory agent disabled")
        return None
    except Exception as e:
        logger.error("Failed to connect Mem0 (collection=%s): %s", collection_name, e)
        return None


def reset_mem0() -> None:
    """Reset all singletons for testing."""
    _mem0_clients.clear()
