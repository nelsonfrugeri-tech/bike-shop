from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded Mem0 client
_mem0 = None


def _get_mem0():
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


class MemoryAgent:
    """Observes all messages and extracts facts into shared Mem0 memory."""

    def __init__(self, project_id: str = "bike-shop") -> None:
        self._project_id = project_id
        self._enabled = False

        mem0 = _get_mem0()
        if mem0:
            self._enabled = True
            logger.info("MemoryAgent enabled for project '%s'", project_id)

    def observe(self, agent_name: str, user_message: str, agent_response: str) -> None:
        """Observe a message exchange and extract important facts."""
        if not self._enabled:
            return

        mem0 = _get_mem0()
        if not mem0:
            return

        try:
            # Save the full exchange — Mem0 extracts facts automatically
            exchange = f"User: {user_message}\nAgent ({agent_name}): {agent_response}"
            mem0.add(
                exchange,
                user_id=self._project_id,
                metadata={
                    "agent": agent_name,
                    "type": "conversation",
                },
            )
            logger.debug("[memory-agent] Observed exchange from %s", agent_name)
        except Exception as e:
            logger.warning("[memory-agent] Failed to save observation: %s", e)

    def recall(self, query: str, limit: int = 10) -> str:
        """Search shared memory for relevant context."""
        if not self._enabled:
            return ""

        mem0 = _get_mem0()
        if not mem0:
            return ""

        try:
            results = mem0.search(query, user_id=self._project_id, limit=limit)

            if not results or not results.get("results"):
                return ""

            memories = []
            for r in results["results"]:
                memory_text = r.get("memory", "")
                if memory_text:
                    memories.append(f"- {memory_text}")

            if not memories:
                return ""

            return (
                "\n\n--- SHARED PROJECT MEMORY ---\n"
                + "\n".join(memories)
                + "\n--- END MEMORY ---\n"
            )
        except Exception as e:
            logger.warning("[memory-agent] Failed to recall: %s", e)
            return ""
