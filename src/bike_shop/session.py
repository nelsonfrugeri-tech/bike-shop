from __future__ import annotations

import json
import os
import tempfile
import time

SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "bike-shop")
SESSION_TTL = 86400  # 24h


class SessionStore:
    """Tracks Claude CLI sessions per agent, keyed by Slack thread_ts."""

    def __init__(self, agent_key: str) -> None:
        self._agent_key = agent_key
        self._path = os.path.join(SESSIONS_DIR, f"sessions-{agent_key}.json")
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path) as f:
                return json.load(f)
        except (ValueError, OSError):
            return {}

    def _save(self, sessions: dict) -> None:
        with open(self._path, "w") as f:
            json.dump(sessions, f)

    def get(self, thread_ts: str) -> str | None:
        sessions = self._load()
        entry = sessions.get(thread_ts)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > SESSION_TTL:
            sessions.pop(thread_ts, None)
            self._save(sessions)
            return None
        return entry.get("session_id")

    def store(self, thread_ts: str, session_id: str) -> None:
        sessions = self._load()
        sessions[thread_ts] = {"session_id": session_id, "ts": time.time()}
        self._save(sessions)
