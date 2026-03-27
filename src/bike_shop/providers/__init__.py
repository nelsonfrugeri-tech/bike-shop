from __future__ import annotations

from abc import ABC, abstractmethod

from bike_shop.config import AgentConfig


class LLMProvider(ABC):
    """Abstract base for LLM providers (Claude, Codex, etc.)."""

    @abstractmethod
    def call(
        self,
        config: AgentConfig,
        prompt: str,
        *,
        model_override: str | None = None,
        session_id: str | None = None,
        memory_file: str | None = None,
        mcp_config: str | None = None,
        github_token: str | None = None,
    ) -> tuple[str, str | None]:
        """Send prompt to the LLM. Returns (response_text, session_id)."""
        ...
