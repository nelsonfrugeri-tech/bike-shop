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
from bike_shop.observability import Tracer

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
        self._tracer = Tracer(f"memory-{agent_key}")

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
        trace_id: str | None = None,
        parent_span_id: str | None = None,
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
            ("LONG-TERM — Agent memory", self._uid_agent, 5, "agent"),
            ("LONG-TERM — Project memory", self._uid_project, 5, "project"),
            ("LONG-TERM — Team memory", self._uid_team, 3, "team"),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for label, uid, lim, scope_name in scopes:
                futures[pool.submit(mem0.search, query, user_id=uid, limit=lim)] = (label, scope_name)
            for future in as_completed(futures):
                label, scope_name = futures[future]
                # Create a span for each mem0 search
                search_span_id: str | None = None
                if trace_id:
                    search_span_id = self._tracer.start_span(
                        "mem0.search", trace_id=trace_id, parent_id=parent_span_id,
                        metadata={"scope": scope_name},
                    )
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
                    if search_span_id and trace_id:
                        self._tracer.end_span(
                            search_span_id, trace_id=trace_id,
                            output=f"{len(memories)} memories",
                        )
                except Exception as e:
                    logger.warning("[memory-agent] Failed to recall %s: %s", label, e)
                    if search_span_id and trace_id:
                        self._tracer.end_span(
                            search_span_id, trace_id=trace_id,
                            output=str(e), level="ERROR",
                        )

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
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
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

        with ThreadPoolExecutor(max_workers=min(len(memory_requests) * 3, 12)) as pool:
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
        # Over-fetch when filtering by type to compensate for client-side filtering
        fetch_limit = limit * 3 if types else limit
        results = mem0.search(query, user_id=user_id, limit=fetch_limit)
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
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        observe_span_id: str | None = None,
    ) -> None:
        """Fire-and-forget: extract memories in a background thread.

        The Slack response is already sent before this runs, so there's no
        user-facing latency. The extraction subprocess (Haiku ~2-5s) runs
        without blocking the handler's daemon thread.

        When *observe_span_id* is provided, this method is responsible for
        closing that span when the background work finishes, ensuring
        sub-spans (extraction.haiku, mem0.store) end before the parent.
        """
        if not self._mem0_enabled:
            # Close observe span immediately if nothing to do
            if observe_span_id and trace_id:
                self._tracer.end_span(observe_span_id, trace_id=trace_id,
                                      output="mem0 disabled")
            return

        thread = threading.Thread(
            target=self._observe_sync,
            args=(agent_name, user_message, agent_response),
            kwargs={
                "trace_id": trace_id,
                "parent_span_id": parent_span_id,
                "observe_span_id": observe_span_id,
            },
            daemon=True,
        )
        thread.start()

    def _observe_sync(
        self,
        agent_name: str,
        user_message: str,
        agent_response: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        observe_span_id: str | None = None,
    ) -> None:
        """Synchronous extraction + storage. Runs in background thread."""
        try:
            mem0 = get_mem0()
            if not mem0:
                return

            # Extraction generation span
            extraction_span: str | None = None
            if trace_id:
                extraction_span = self._tracer.start_generation(
                    "extraction.haiku",
                    trace_id=trace_id,
                    model="claude-haiku-4-5-20251001",
                    input=user_message[:300],
                    parent_id=parent_span_id,
                )

            memories = extract_memories(
                agent_name, user_message, agent_response, self._project_id,
            )

            if extraction_span and trace_id:
                self._tracer.end_generation(
                    extraction_span, trace_id=trace_id,
                    output=f"{len(memories)} memories extracted",
                )

            # Store span
            store_span: str | None = None
            if trace_id and memories:
                store_span = self._tracer.start_span(
                    "mem0.store", trace_id=trace_id, parent_id=parent_span_id,
                    metadata={"count": len(memories)},
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

            if store_span and trace_id:
                self._tracer.end_span(
                    store_span, trace_id=trace_id,
                    output=f"{len(memories)} stored",
                )

            if memories:
                logger.debug(
                    "[memory-agent] Stored %d extracted memories from %s",
                    len(memories), agent_name,
                )
        except Exception as e:
            logger.warning("[memory-agent] Background extraction failed for %s: %s", agent_name, e)
        finally:
            # Close the parent observe span from the handler — must happen
            # after all sub-spans (extraction.haiku, mem0.store) are closed.
            if observe_span_id and trace_id:
                self._tracer.end_span(
                    observe_span_id, trace_id=trace_id,
                    metadata={"async": True},
                )
                self._tracer.flush()
