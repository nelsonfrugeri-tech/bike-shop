"""Two-tier memory agent — Redis (short-term) + Mem0/Qdrant (long-term).

Short-term: per-agent, per-project, per-thread conversation buffers in Redis (24h TTL).
Long-term: three scopes — team (global), project (shared), agent (private) in Mem0.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from bike_shop.extraction import extract_memories
from bike_shop.short_term import ShortTermMemory

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
    """Two-tier memory: Redis short-term + Mem0 long-term with scoped access.

    Scopes (Mem0 user_id patterns):
        Team:    "team"                    — leader preferences, global procedures
        Project: "team:{project_id}"       — project-specific decisions
        Agent:   "{agent_key}:{project_id}" — agent's own implementation decisions
    """

    def __init__(self, agent_key: str, project_id: str = "bike-shop") -> None:
        self._agent_key = agent_key
        self._project_id = project_id
        self._short_term = ShortTermMemory()

        # Mem0 scoped user_ids
        self._uid_team = "team"
        self._uid_project = f"team:{project_id}"
        self._uid_agent = f"{agent_key}:{project_id}"

        self._mem0_enabled = False
        mem0 = _get_mem0()
        if mem0:
            self._mem0_enabled = True
            logger.info(
                "MemoryAgent enabled for agent='%s' project='%s'",
                agent_key, project_id,
            )

    def _scope_to_user_id(self, scope: str) -> str:
        """Map extraction scope to Mem0 user_id."""
        if scope == "team":
            return self._uid_team
        if scope == "project":
            return self._uid_project
        return self._uid_agent

    def recall(
        self,
        query: str,
        channel: str = "",
        thread_ts: str = "",
    ) -> str:
        """Search both tiers for relevant context.

        Performs 5 lookups:
            Mem0: agent memory (limit=5), project memory (limit=5), team memory (limit=3)
            Redis: thread buffer (limit=20), recent activity (limit=10)

        Returns formatted string with sections, or "" if nothing found.
        """
        sections: list[str] = []

        # --- Redis short-term ---
        if channel and thread_ts:
            thread_msgs = self._short_term.get_thread(
                self._agent_key, self._project_id, channel, thread_ts,
            )
            if thread_msgs:
                lines = []
                for m in reversed(thread_msgs):  # oldest first
                    role = m.get("role", "?")
                    author = m.get("author", "")
                    content = m.get("content", "")
                    prefix = f"{author} ({role})" if author else role
                    lines.append(f"  - {prefix}: {content[:200]}")
                sections.append("SHORT-TERM — Thread:\n" + "\n".join(lines))

        recent_msgs = self._short_term.get_recent(self._agent_key, self._project_id)
        if recent_msgs:
            lines = []
            for m in reversed(recent_msgs):  # oldest first
                content = m.get("content", "")
                author = m.get("author", "")
                lines.append(f"  - {author}: {content[:150]}")
            sections.append("SHORT-TERM — Recent activity:\n" + "\n".join(lines))

        # --- Mem0 long-term ---
        if self._mem0_enabled:
            mem0 = _get_mem0()
            if mem0:
                for label, uid, limit in [
                    ("LONG-TERM — Agent memory", self._uid_agent, 5),
                    ("LONG-TERM — Project memory", self._uid_project, 5),
                    ("LONG-TERM — Team memory", self._uid_team, 3),
                ]:
                    try:
                        results = mem0.search(query, user_id=uid, limit=limit)
                        memories = []
                        if results and results.get("results"):
                            for r in results["results"]:
                                text = r.get("memory", "")
                                if text:
                                    memories.append(f"  - {text}")
                        if memories:
                            sections.append(f"{label}:\n" + "\n".join(memories))
                    except Exception as e:
                        logger.warning("[memory-agent] Failed to recall %s: %s", label, e)

        if not sections:
            return ""

        return (
            "\n\n--- PROJECT MEMORY ---\n"
            + "\n\n".join(sections)
            + "\n--- END MEMORY ---\n"
        )

    def push_user_message(
        self,
        user_name: str,
        message: str,
        channel: str,
        thread_ts: str,
    ) -> None:
        """Push user message to Redis short-term BEFORE LLM call."""
        entry: dict[str, Any] = {
            "role": "user",
            "author": user_name,
            "content": message,
            "ts": thread_ts,
        }
        self._short_term.push(
            self._agent_key, self._project_id, channel, thread_ts, entry,
        )
        self._short_term.push_recent(self._agent_key, self._project_id, entry)

    def observe(
        self,
        agent_name: str,
        user_message: str,
        agent_response: str,
        channel: str = "",
        thread_ts: str = "",
        route_decision: dict[str, Any] | None = None,
    ) -> None:
        """Observe a message exchange: push to Redis + selective extraction to Mem0.

        Args:
            agent_name: Display name of the agent.
            user_message: The user's message.
            agent_response: The agent's response.
            channel: Slack channel ID.
            thread_ts: Slack thread timestamp.
            route_decision: Router decision dict (agent, model, model_name, reason).
        """
        # 1. Push agent response to Redis short-term
        route = route_decision or {}
        entry: dict[str, Any] = {
            "role": "agent",
            "author": agent_name,
            "content": agent_response[:500],
            "ts": thread_ts,
            "route": {
                "agent": route.get("agent", ""),
                "model": route.get("model", ""),
                "model_name": route.get("model_name", ""),
                "reason": route.get("reason", ""),
            },
        }

        if channel and thread_ts:
            self._short_term.push(
                self._agent_key, self._project_id, channel, thread_ts, entry,
            )
        self._short_term.push_recent(self._agent_key, self._project_id, entry)

        # 2. Selective extraction to Mem0
        if not self._mem0_enabled:
            return

        mem0 = _get_mem0()
        if not mem0:
            return

        try:
            memories = extract_memories(
                agent_name, user_message, agent_response, self._project_id,
            )

            for m in memories:
                uid = self._scope_to_user_id(m["scope"])
                mem0.add(
                    m["content"],
                    user_id=uid,
                    metadata={
                        "agent": agent_name,
                        "type": m["type"],
                        "scope": m["scope"],
                    },
                )

            if memories:
                logger.debug(
                    "[memory-agent] Stored %d extracted memories from %s",
                    len(memories), agent_name,
                )
        except Exception as e:
            logger.warning("[memory-agent] Failed to store observations: %s", e)
