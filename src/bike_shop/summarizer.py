"""TTL summarization — summarizes expiring Redis conversations into Mem0.

Run as: python -m bike_shop.summarizer
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from bike_shop.memory_agent import _get_mem0
from bike_shop.short_term import ShortTermMemory

logger = logging.getLogger(__name__)

SUMMARIZE_MODEL = "claude-haiku-4-5-20251001"

_SUMMARIZE_PROMPT = """Summarize this conversation thread concisely. Focus on:
- Decisions made
- Key outcomes
- Important facts learned
- Action items or follow-ups

Keep it under 200 words. Be factual, not verbose.

Conversation:
{conversation}
"""


class Summarizer:
    """Scans Redis for expiring keys and summarizes them into Mem0."""

    def __init__(self, ttl_threshold: int = 7200) -> None:
        self._stm = ShortTermMemory()
        self._ttl_threshold = ttl_threshold

    def run(self) -> int:
        """Run one pass of summarization. Returns count of summarized threads."""
        expiring = self._stm.scan_expiring(self._ttl_threshold)
        if not expiring:
            logger.info("[summarizer] No expiring keys found")
            return 0

        mem0 = _get_mem0()
        count = 0

        for key, messages in expiring:
            # Skip already summarized
            if self._stm.is_summarized(key):
                continue

            # Parse key: bike-shop:{agent}:{project}:{channel}:{thread_ts}
            parts = key.split(":")
            if len(parts) < 5:
                continue

            agent = parts[1]
            project = parts[2]
            channel = parts[3]
            thread_ts = parts[4]

            # Build conversation text
            lines = []
            participants = set()
            for m in reversed(messages):  # oldest first
                author = m.get("author", "unknown")
                content = m.get("content", "")
                role = m.get("role", "?")
                participants.add(author)
                lines.append(f"{author} ({role}): {content}")

            conversation_text = "\n".join(lines)

            # Summarize via Haiku
            summary = self._call_summarize(conversation_text)
            if not summary:
                continue

            # Store in Mem0 if available
            if mem0:
                uid = f"{agent}:{project}"
                try:
                    mem0.add(
                        summary,
                        user_id=uid,
                        metadata={
                            "type": "summary",
                            "channel": channel,
                            "thread_ts": thread_ts,
                            "participants": list(participants),
                            "message_count": len(messages),
                            "date": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                except Exception as e:
                    logger.warning("[summarizer] Failed to store summary for %s: %s", key, e)
                    continue

            self._stm.mark_summarized(key)
            count += 1
            logger.info("[summarizer] Summarized %s (%d messages)", key, len(messages))

        logger.info("[summarizer] Summarized %d threads", count)
        return count

    @staticmethod
    def _call_summarize(conversation_text: str) -> str:
        """Call Haiku to summarize a conversation."""
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation_text[:4000])

        try:
            result = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--model", SUMMARIZE_MODEL,
                    "--dangerously-skip-permissions",
                    "--output-format", "text",
                    "--max-turns", "1",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                cwd=os.environ.get("AGENT_WORKSPACE", os.path.expanduser("~")),
            )
            return result.stdout.strip()
        except Exception as e:
            logger.warning("[summarizer] Haiku call failed: %s", e)
            return ""


def main() -> None:
    """Entry point for cron: python -m bike_shop.summarizer"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from dotenv import load_dotenv
    load_dotenv()

    summarizer = Summarizer()
    count = summarizer.run()
    logger.info("Summarization complete: %d threads processed", count)


if __name__ == "__main__":
    main()
