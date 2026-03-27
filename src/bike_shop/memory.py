from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

MEMORY_BASE = os.path.expanduser("~/.claude/workspace/bike-shop/memory")
SUMMARY_INTERVAL = 10


class MemoryStore:
    """Manages persistent memory per agent using local JSON storage.

    Tracks messages, auto-summarizes every SUMMARY_INTERVAL messages,
    and injects recent context into prompts automatically.
    """

    def __init__(self, agent_key: str) -> None:
        self._agent_key = agent_key
        self._dir = os.path.join(MEMORY_BASE, agent_key)
        self._messages_path = os.path.join(self._dir, "messages.json")
        self._decisions_path = os.path.join(self._dir, "decisions.json")
        self._summaries_path = os.path.join(self._dir, "summaries.json")
        os.makedirs(self._dir, exist_ok=True)

    def _load_json(self, path: str) -> list:
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                return json.load(f)
        except (ValueError, OSError):
            return []

    def _save_json(self, path: str, data: list) -> None:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def record_message(self, user: str, text: str) -> int:
        """Record an incoming message. Returns total message count."""
        messages = self._load_json(self._messages_path)
        messages.append({
            "user": user,
            "text": text[:500],  # truncate to save space
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        # Keep last 50 messages max
        if len(messages) > 50:
            messages = messages[-50:]
        self._save_json(self._messages_path, messages)
        return len(messages)

    def record_summary(self, summary: str) -> None:
        """Save a conversation summary."""
        summaries = self._load_json(self._summaries_path)
        summaries.append({
            "summary": summary,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        # Keep last 10 summaries
        if len(summaries) > 10:
            summaries = summaries[-10:]
        self._save_json(self._summaries_path, summaries)

    def save_decision(self, decision: str) -> None:
        """Save a project decision."""
        decisions = self._load_json(self._decisions_path)
        decisions.append({
            "decision": decision,
            "ts": time.strftime("%Y-%m-%d %H:%M"),
        })
        self._save_json(self._decisions_path, decisions)

    def needs_summary(self) -> bool:
        """Check if it's time to auto-summarize (every N messages)."""
        messages = self._load_json(self._messages_path)
        return len(messages) > 0 and len(messages) % SUMMARY_INTERVAL == 0

    def get_recent_context(self) -> str:
        """Build context string from recent messages, summaries, and decisions."""
        parts = []

        # Last summary
        summaries = self._load_json(self._summaries_path)
        if summaries:
            latest = summaries[-1]
            parts.append(f"LAST SUMMARY ({latest['ts']}):\n{latest['summary']}")

        # Decisions
        decisions = self._load_json(self._decisions_path)
        if decisions:
            dec_lines = [f"- {d['decision']}" for d in decisions[-10:]]
            parts.append("DECISIONS:\n" + "\n".join(dec_lines))

        # Recent messages (last 10)
        messages = self._load_json(self._messages_path)
        if messages:
            recent = messages[-10:]
            msg_lines = [f"[{m['ts']}] {m['user']}: {m['text']}" for m in recent]
            parts.append("RECENT MESSAGES:\n" + "\n".join(msg_lines))

        if not parts:
            return ""

        return "\n\n--- YOUR MEMORY (read this before responding) ---\n" + "\n\n".join(parts) + "\n--- END MEMORY ---\n"

    def build_instruction(self) -> str:
        """Minimal prompt instruction — the heavy lifting is done in code."""
        return (
            "\n\nYou have persistent memory managed automatically. "
            "Your recent context is injected above. "
            "If you notice you are repeating yourself or confirming something "
            "already confirmed — STOP. Say nothing. You are in a loop.\n"
        )
