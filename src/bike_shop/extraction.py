"""Selective memory extraction — classifies what's worth storing long-term."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-haiku-4-5-20251001"

_EXTRACTION_PROMPT = """Analyze this conversation exchange and extract ONLY information worth remembering long-term.

Extract facts, decisions, preferences, procedures, or outcomes. Skip greetings, confirmations, small talk, and routine status updates.

For each extracted memory, classify:
- type: decision | fact | preference | procedure | outcome
- scope: team (global team preferences/procedures) | project (project-specific decisions) | agent (agent's own implementation details)
- content: the memory text (concise, self-contained)

Respond ONLY with valid JSON array. Return [] if nothing worth extracting.

Example:
[
  {{"type": "decision", "scope": "project", "content": "We chose Redis for short-term memory because of sub-ms latency"}},
  {{"type": "preference", "scope": "team", "content": "Team prefers test-first development approach"}}
]

Agent: {agent_name}
Project: {project_id}

User message:
{user_message}

Agent response:
{agent_response}
"""


def extract_memories(
    agent_name: str,
    user_message: str,
    agent_response: str,
    project_id: str = "bike-shop",
) -> list[dict[str, Any]]:
    """Extract structured memories from a conversation exchange.

    Returns list of dicts with keys: type, scope, content.
    Returns [] if nothing worth extracting or on failure.
    """
    prompt = _EXTRACTION_PROMPT.format(
        agent_name=agent_name,
        project_id=project_id,
        user_message=user_message[:2000],
        agent_response=agent_response[:3000],
    )

    try:
        # Pass prompt via stdin (-p -) to avoid shell injection via CLI args.
        # --dangerously-skip-permissions is required because Claude CLI
        # demands it for non-interactive subprocess calls (no TTY).
        result = subprocess.run(
            [
                "claude", "-p", "-",
                "--model", EXTRACTION_MODEL,
                "--dangerously-skip-permissions",
                "--output-format", "text",
                "--max-turns", "1",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=os.environ.get("AGENT_WORKSPACE", os.path.expanduser("~")),
        )

        raw = result.stdout.strip()

        # Handle markdown code blocks
        if "```" in raw:
            raw = raw.split("```json")[-1].split("```")[0].strip()
            if not raw:
                raw = result.stdout.strip().split("```")[-2].strip()

        memories = json.loads(raw)

        if not isinstance(memories, list):
            return []

        # Validate structure
        valid = []
        valid_types = {"decision", "fact", "preference", "procedure", "outcome"}
        valid_scopes = {"team", "project", "agent"}

        for m in memories:
            if (
                isinstance(m, dict)
                and m.get("type") in valid_types
                and m.get("scope") in valid_scopes
                and isinstance(m.get("content"), str)
                and len(m["content"]) > 5
            ):
                valid.append(m)

        if valid:
            logger.debug(
                "[extraction] Extracted %d memories from %s exchange",
                len(valid), agent_name,
            )

        return valid

    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        logger.debug("[extraction] Parse/timeout error: %s", e)
        return []
    except Exception as e:
        logger.warning("[extraction] Failed: %s", e)
        return []
