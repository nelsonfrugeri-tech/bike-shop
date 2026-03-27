from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEEP_THINK_MARKER = "[DEEP_THINK]"
MAX_ESCALATIONS_PER_THREAD = 2

DEEP_THINK_TRIGGERS = frozenset({
    "pensem profundamente",
    "pensem com calma",
    "analisem com calma",
    "analisem profundamente",
    "think deeply",
    "analyze carefully",
})


class ModelSwitcher:
    """Manages model escalation (Sonnet ↔ Opus) per thread."""

    def __init__(self) -> None:
        self._escalations: dict[str, int] = {}

    def is_manual_trigger(self, text: str) -> bool:
        """Check if the message contains a project-lead trigger for Opus."""
        lower = text.lower()
        return any(trigger in lower for trigger in DEEP_THINK_TRIGGERS)

    def should_escalate(self, thread_ts: str) -> bool:
        """Check if escalation is still allowed for this thread."""
        return self._escalations.get(thread_ts, 0) < MAX_ESCALATIONS_PER_THREAD

    def record_escalation(self, thread_ts: str) -> int:
        """Record an escalation and return the new count."""
        count = self._escalations.get(thread_ts, 0) + 1
        self._escalations[thread_ts] = count
        logger.info("Opus escalation %d/%d for thread %s", count, MAX_ESCALATIONS_PER_THREAD, thread_ts)
        return count

    def strip_marker(self, text: str) -> str:
        """Remove the [DEEP_THINK] marker from response text."""
        return text.replace(DEEP_THINK_MARKER, "").strip()

    def has_marker(self, text: str) -> bool:
        """Check if the response contains the [DEEP_THINK] marker."""
        return DEEP_THINK_MARKER in text
