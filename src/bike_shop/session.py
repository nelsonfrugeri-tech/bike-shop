from __future__ import annotations

import json
import os
import time

SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".cache", "bike-shop", "sessions")
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

    def get(self, thread_ts: str, project_id: str | None = None) -> str | None:
        sessions = self._load()
        entry = sessions.get(thread_ts)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > SESSION_TTL:
            sessions.pop(thread_ts, None)
            self._save(sessions)
            return None
        # Don't resume sessions from a different project
        stored_project = entry.get("project_id")
        if project_id and stored_project and stored_project != project_id:
            return None
        return entry.get("session_id")

    def get_project_id(self, thread_ts: str) -> str | None:
        """Return the project_id stored for this thread, or None."""
        sessions = self._load()
        entry = sessions.get(thread_ts)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > SESSION_TTL:
            return None
        return entry.get("project_id")

    def store(self, thread_ts: str, session_id: str, project_id: str | None = None) -> None:
        sessions = self._load()
        data: dict[str, object] = {"session_id": session_id, "ts": time.time()}
        if project_id:
            data["project_id"] = project_id
        sessions[thread_ts] = data
        self._save(sessions)
