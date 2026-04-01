from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from bike_shop.config import MODEL_MAP
from bike_shop.memory_schema import scopes_description, types_description
from bike_shop.observability import Tracer

logger = logging.getLogger(__name__)

ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-sonnet-4-20250514")

_ROUTER_PROMPT_TEMPLATE = """You are a semantic router. Analyze the user message and the thread context to decide:
1. Which specialized agent should handle this task
2. Which model (complexity level) should power it
3. Whether long-term memory should be consulted

Available agents:
{agent_list}
- none: simple questions, confirmations, short answers, status checks

Model selection rules:
- opus: deep thinking, complex architecture, difficult debugging, multi-step reasoning, long feature development, deep research
- sonnet: standard coding, reviews, implementation, moderate tasks
- haiku: ONLY for truly standalone simple questions with no ongoing task context

IMPORTANT: Consider the thread context. If there is an ongoing complex task (implementation, architecture, review),
maintain the appropriate model even if the current message is simple (e.g. "how's it going?" in an implementation
thread should keep sonnet/opus, not downgrade to haiku).

Memory lookup: If the message references or requires knowledge about past decisions, team preferences, procedures,
facts, or outcomes from OTHER conversations, request a memory lookup. Each lookup specifies:
- query: what to search for (concise, specific)
- scopes: which scopes to search — {scopes}
- types: which memory types to filter — {types}

Return an EMPTY "memory" array if the message is self-contained and doesn't need cross-thread context.

Respond ONLY with valid JSON, nothing else:
{{"agent": "name_or_none", "model": "opus|sonnet|haiku", "reason": "brief explanation", "memory": [{{"query": "search text", "scopes": ["project"], "types": ["decision"]}}]}}
{thread_context}
User message:
"""


def _parse_frontmatter(filepath: str) -> tuple[str, str] | None:
    """Extract name and first sentence of description from expert frontmatter.

    Returns (name, short_description) or None if parsing fails.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    # Match YAML frontmatter between --- delimiters
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return None

    fm = fm_match.group(1)

    # Extract name
    name_match = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    if not name_match:
        return None
    name = name_match.group(1).strip().strip('"').strip("'")

    # Extract description (may be multi-line folded with >)
    desc_match = re.search(
        r"^description:\s*>?\s*\n((?:\s{2,}.+\n?)+)", fm, re.MULTILINE
    )
    if desc_match:
        desc_lines = desc_match.group(1).strip().splitlines()
        full_desc = " ".join(line.strip() for line in desc_lines)
    else:
        # Single-line description
        desc_match = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        if not desc_match:
            return None
        full_desc = desc_match.group(1).strip().strip('"').strip("'")

    # First sentence: split on ". " (period + space) to avoid truncating on
    # abbreviations like "e.g.", version numbers like "v2.0", or "Dr."
    parts = re.split(r"\.\s", full_desc, maxsplit=1)
    first_sentence = parts[0].strip().rstrip(".")

    # Validate name format: lowercase letters, digits, hyphens
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        logger.warning("[router] Invalid expert name format: '%s' — skipping", name)
        return None

    return name, first_sentence


class SemanticRouter:
    """Classifies messages and selects the right expert + model."""

    EXPERTS_DIR = os.getenv(
        "EXPERTS_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "agents", "experts"),
    )

    def __init__(self, experts_dir: str | None = None) -> None:
        self._tracer = Tracer("semantic-router")
        self._experts_dir = experts_dir or self.EXPERTS_DIR
        self._experts = self._discover_experts()
        self._validated_experts: set[str] = set(self._experts.keys())

    def _discover_experts(self) -> dict[str, str]:
        """Scan experts directory for .md files and parse frontmatter.

        Returns dict mapping expert name to short description.
        """
        agents_dir = self._experts_dir
        pattern = os.path.join(agents_dir, "*.md")
        experts: dict[str, str] = {}

        resolved_dir = Path(agents_dir).resolve()
        for filepath in sorted(glob.glob(pattern)):
            if not Path(filepath).resolve().is_relative_to(resolved_dir):
                logger.warning("[router] Skipping symlink outside experts dir: %s", filepath)
                continue
            parsed = _parse_frontmatter(filepath)
            if parsed:
                name, short_desc = parsed
                experts[name] = short_desc
                logger.info("[router] Discovered expert: %s — %s", name, short_desc)
            else:
                logger.warning(
                    "[router] Could not parse frontmatter from %s", filepath,
                )

        if not experts:
            logger.warning(
                "[router] No expert files found in %s — routing will use direct mode",
                agents_dir,
            )
        return experts

    def _build_prompt(self, experts: dict[str, str], thread_context: str = "") -> str:
        """Build the router prompt dynamically from discovered experts."""
        agent_lines = "\n".join(
            f"- {name}: {desc}" for name, desc in sorted(experts.items())
        )
        ctx = ""
        if thread_context:
            ctx = f"\nThread context (recent messages):\n{thread_context}\n\n"
        return _ROUTER_PROMPT_TEMPLATE.format(
            agent_list=agent_lines,
            thread_context=ctx,
            scopes=scopes_description(),
            types=types_description(),
        )

    def route(self, message: str, thread_context: str = "") -> dict:
        """Classify a message. Returns {"agent": str|None, "model": str, "reason": str}."""
        start = time.time()
        prompt = self._build_prompt(self._experts, thread_context)

        try:
            # Pass prompt via stdin to avoid CLI arg size limits and shell escaping
            result = subprocess.run(
                [
                    "claude", "-p", "-",
                    "--model", ROUTER_MODEL,
                    "--dangerously-skip-permissions",
                    "--output-format", "text",
                    "--max-turns", "1",
                ],
                input=prompt + message,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("AGENT_WORKSPACE", os.path.expanduser("~")),
            )

            duration_ms = (time.time() - start) * 1000
            raw = result.stdout.strip()

            # Parse JSON from response
            # Handle cases where response has markdown code blocks
            if "```" in raw:
                raw = raw.split("```json")[-1].split("```")[0].strip()
                if not raw:
                    raw = result.stdout.strip().split("```")[-2].strip()

            decision = json.loads(raw)

            # Normalize
            agent = decision.get("agent", "none")
            if agent == "none" or agent == "null" or not agent:
                agent = None
            elif agent not in self._validated_experts:
                logger.warning("[router] Expert '%s' not found on disk — falling back to direct mode", agent)
                agent = None

            model = decision.get("model", "sonnet")
            if model not in MODEL_MAP:
                model = "sonnet"

            reason = decision.get("reason", "")
            memory = decision.get("memory", [])
            model_id = MODEL_MAP[model]

            # Validate memory requests
            if not isinstance(memory, list):
                memory = []
            memory = [
                m for m in memory
                if isinstance(m, dict) and m.get("query")
            ]

            logger.info(
                "[router] agent=%s model=%s memory_lookups=%d reason=%s (%.0fms)",
                agent or "direct", model, len(memory), reason, duration_ms,
            )

            # Trace to Langfuse
            self._tracer.trace_call(
                user_message=message[:300],
                response=json.dumps({"agent": agent, "model": model, "reason": reason, "memory": memory}),
                model=ROUTER_MODEL,
                duration_ms=duration_ms,
                input_tokens=None,
                output_tokens=None,
                tools=[],
                tool_results=[],
                thinking=[],
                errors=[],
            )

            return {"agent": agent, "model": model_id, "model_name": model, "reason": reason, "memory": memory}

        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
            duration_ms = (time.time() - start) * 1000
            logger.warning("[router] Failed to classify (%.0fms): %s — defaulting to sonnet", duration_ms, e)

            self._tracer.trace_error(error=str(e), context=message[:300])

            return {"agent": None, "model": MODEL_MAP["sonnet"], "model_name": "sonnet", "reason": "router_fallback", "memory": []}
