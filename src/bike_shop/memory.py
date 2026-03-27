from __future__ import annotations


class MemoryStore:
    """Instructs agents to use the memory-keeper MCP for persistent memory."""

    def __init__(self, agent_key: str) -> None:
        self._agent_key = agent_key

    @property
    def path(self) -> str:
        return ""

    def ensure(self) -> str:
        return ""

    def exists(self) -> bool:
        return False

    def build_instruction(self) -> str:
        channel = self._agent_key
        return (
            "\n\nMEMORY:\n"
            "You have persistent memory via the memory-keeper MCP tool.\n"
            f"Your channel is '{channel}'. Use it for all memory operations.\n"
            "- At the START of every conversation, call context_get to restore your memory.\n"
            "- When the project lead makes a decision, save it immediately with context_save.\n"
            "- When you learn something important, save it.\n"
            "- Use category 'decision' for decisions, 'task' for tasks, 'note' for context.\n"
            "This memory persists across sessions — you will remember everything.\n"
        )
