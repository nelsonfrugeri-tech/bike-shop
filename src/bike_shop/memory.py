from __future__ import annotations

import os

MEMORY_BASE = os.path.expanduser("~/.claude/workspace/bike-shop/memory")


class MemoryStore:
    """Manages persistent memory files for each agent."""

    def __init__(self, agent_key: str) -> None:
        self._agent_key = agent_key
        self._dir = os.path.join(MEMORY_BASE, agent_key)
        self._path = os.path.join(self._dir, "MEMORY.md")
        os.makedirs(self._dir, exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def ensure(self) -> str:
        """Create memory file if it doesn't exist. Returns path."""
        if not os.path.exists(self._path):
            with open(self._path, "w") as f:
                f.write(f"# {self._agent_key} - Memory\n\n")
        return self._path

    def exists(self) -> bool:
        return os.path.exists(self._path)

    def build_instruction(self) -> str:
        return (
            f"\n\nYou have persistent memory at {self._path}. "
            "When you learn something important (decisions, patterns, user preferences), "
            f"save it by appending to that file using Bash: echo '- ...' >> {self._path}. "
            "Consult your memory file at the start of complex tasks."
            "\n\n--- RESILIENCE RULES ---\n"
            "You are an autonomous agent. External tools (Slack, Notion, GitHub) may timeout or fail.\n"
            "BEFORE starting any long operation or multi-step task:\n"
            "1. Save your current plan and progress to your MEMORY.md file\n"
            "2. Break work into small checkpoints — save after each one\n"
            "3. If a tool call fails or times out, DO NOT stop. Save what you have, note the failure in memory, and continue with the next step\n"
            "4. When resuming work, ALWAYS read your MEMORY.md first to recover context\n"
            "5. After completing a task, save a summary of what was done and any pending items\n"
            "Format for memory entries: `## [YYYY-MM-DD HH:MM] Topic` followed by bullet points.\n"
            "This ensures zero gap in memory — you can always pick up where you left off."
        )
