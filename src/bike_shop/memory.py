from __future__ import annotations


class MemoryStore:
    """Minimal wrapper — memory is now handled by MemoryAgent (Mem0).

    This class exists for backward compatibility with the handler.
    """

    def __init__(self, agent_key: str) -> None:
        self._agent_key = agent_key

    def get_recent_context(self) -> str:
        """No longer used — context comes from MemoryAgent.recall()."""
        return ""

    def record_message(self, user: str, text: str) -> int:
        """No longer used — observations go through MemoryAgent.observe()."""
        return 0

    def needs_summary(self) -> bool:
        """No longer needed — Mem0 handles extraction automatically."""
        return False

    def build_instruction(self) -> str:
        return ""
