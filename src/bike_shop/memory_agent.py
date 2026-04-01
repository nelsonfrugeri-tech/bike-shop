"""Memory agent — Mem0/Qdrant long-term memory for cross-thread context.

Two recall modes:
  1. Full recall — on new threads (no session_id), queries all 3 scopes
  2. Router recall — on any thread, router requests specific memory lookups
     filtered by scope + type based on semantic analysis of the message
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from bike_shop.extraction import extract_memories
from bike_shop.mem0_client import get_mem0

logger = logging.getLogger(__name__)


class MemoryAgent:
    """Long-term memory via Mem0 with scoped access.

    Scopes (Mem0 user_id patterns):
        Team:    "team"                    — leader preferences, global procedures
        Project: "team:{project_id}"       — project-specific decisions
        Agent:   "{agent_key}:{project_id}" — agent's own implementation decisions
    """

    def __init__(self, agent_key: str, project_id: str = "bike-shop") -> None:
        self._agent_key = agent_key
        self._project_id = project_id

        # Mem0 scoped user_ids
        self._uid_team = "team"
        self._uid_project = f"team:{project_id}"
        self._uid_agent = f"{agent_key}:{project_id}"

        self._mem0_enabled = False
        mem0 = get_mem0()
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
        *,
        has_session: bool = False,
    ) -> str:
        """Full recall — all 3 scopes, only on new threads.

        When --resume is active, the CLI already has full thread history,
        so this returns "" unless the router also requests specific memories.
        """
        if has_session:
            return ""

        if not self._mem0_enabled:
            return ""

        mem0 = get_mem0()
        if not mem0:
            return ""

        sections: list[str] = []
        scopes = [
            ("LONG-TERM — Agent memory", self._uid_agent, 5),
            ("LONG-TERM — Project memory", self._uid_project, 5),
            ("LONG-TERM — Team memory", self._uid_team, 3),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for label, uid, lim in scopes:
                futures[pool.submit(mem0.search, query, user_id=uid, limit=lim)] = label
            for future in as_completed(futures):
                label = futures[future]
                try:
                    results = future.result(timeout=5)
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
            "\n\n--- PROJECT MEMORY (from previous threads) ---\n"
            + "\n\n".join(sections)
            + "\n--- END MEMORY ---\n"
        )

    def recall_filtered(
        self,
        memory_requests: list[dict[str, Any]],
    ) -> str:
        """Router-driven recall — searches Mem0 filtered by scope + type.

        Args:
            memory_requests: list of dicts from router, each with:
                - query: str — what to search for
                - scopes: list[str] — which scopes to search (team, project, agent)
                - types: list[str] — which memory types to filter (decision, procedure, etc.)

        Returns formatted string with results, or "" if nothing found.
        """
        if not self._mem0_enabled or not memory_requests:
            return ""

        mem0 = get_mem0()
        if not mem0:
            return ""

        sections: list[str] = []

        with ThreadPoolExecutor(max_workers=len(memory_requests) * 3) as pool:
            futures: dict[Any, str] = {}

            for req in memory_requests:
                query = req.get("query", "")
                scopes = req.get("scopes", ["project"])
                types = req.get("types", [])

                if not query:
                    continue

                for scope in scopes:
                    uid = self._scope_to_user_id(scope)
                    label = f"Memory ({scope}): {query}"
                    futures[pool.submit(
                        self._search_filtered, mem0, query, uid, types,
                    )] = label

            for future in as_completed(futures):
                label = futures[future]
                try:
                    memories = future.result(timeout=5)
                    if memories:
                        sections.append(f"{label}:\n" + "\n".join(f"  - {m}" for m in memories))
                except Exception as e:
                    logger.warning("[memory-agent] Filtered recall failed for %s: %s", label, e)

        if not sections:
            return ""

        return (
            "\n\n--- RELEVANT MEMORY (router-requested) ---\n"
            + "\n\n".join(sections)
            + "\n--- END MEMORY ---\n"
        )

    @staticmethod
    def _search_filtered(
        mem0: Any,
        query: str,
        user_id: str,
        types: list[str],
        limit: int = 5,
    ) -> list[str]:
        """Search Mem0 and filter results by memory type metadata."""
        results = mem0.search(query, user_id=user_id, limit=limit)
        memories = []

        if not results:
            return memories

        for r in results.get("results", []) if isinstance(results, dict) else results:
            if not isinstance(r, dict):
                continue
            text = r.get("memory", "")
            if not text:
                continue

            # Filter by type if specified
            if types:
                metadata = r.get("metadata", {}) or {}
                mem_type = metadata.get("type", "")
                if mem_type not in types:
                    continue

            memories.append(text)

        return memories

    def observe(
        self,
        agent_name: str,
        user_message: str,
        agent_response: str,
        channel: str = "",
        thread_ts: str = "",
        route_decision: dict[str, Any] | None = None,
        user_name: str = "",
    ) -> None:
        """Fire-and-forget: extract memories in a background thread.

        The Slack response is already sent before this runs, so there's no
        user-facing latency. The extraction subprocess (Haiku ~2-5s) runs
        without blocking the handler's daemon thread.
        """
        if not self._mem0_enabled:
            return

        thread = threading.Thread(
            target=self._observe_sync,
            args=(agent_name, user_message, agent_response),
            daemon=True,
        )
        thread.start()

    def _observe_sync(
        self,
        agent_name: str,
        user_message: str,
        agent_response: str,
    ) -> None:
        """Synchronous extraction + storage. Runs in background thread."""
        mem0 = get_mem0()
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
