from __future__ import annotations

import json
import logging
import os
import subprocess
import time

from bike_shop.config import MODEL_MAP
from bike_shop.observability import Tracer

logger = logging.getLogger(__name__)

ROUTER_MODEL = "claude-haiku-4-5-20251001"

ROUTER_PROMPT = """You are a semantic router. Analyze the user message and decide:
1. Which specialized agent should handle this task
2. Which model (complexity level) should power it

Available agents:
- architect: architecture design, system design, trade-offs, diagrams
- review-py: code review, PR review, reviewing code quality
- debater: comparing approaches, discussing trade-offs deeply
- explorer: exploring existing codebase, understanding code
- dev-py: heavy Python implementation, complex coding
- tech-pm: business analysis, user stories, product decisions, backlog
- builder: infrastructure setup, docker, dependencies, environment
- none: simple questions, confirmations, short answers, status checks

Model selection rules:
- opus: deep thinking, complex architecture, difficult debugging, multi-step reasoning, long feature development, deep research
- sonnet: standard coding, reviews, implementation, moderate tasks
- haiku: confirmations, simple questions, status checks, short lookups

Respond ONLY with valid JSON, nothing else:
{"agent": "name_or_none", "model": "opus|sonnet|haiku", "reason": "brief explanation"}

User message:
"""


class SemanticRouter:
    """Classifies messages and selects the right agent + model."""

    def __init__(self) -> None:
        self._tracer = Tracer("semantic-router")

    def route(self, message: str) -> dict:
        """Classify a message. Returns {"agent": str|None, "model": str, "reason": str}."""
        start = time.time()

        try:
            result = subprocess.run(
                [
                    "claude", "-p", ROUTER_PROMPT + message,
                    "--model", ROUTER_MODEL,
                    "--dangerously-skip-permissions",
                    "--output-format", "text",
                    "--max-turns", "1",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
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

            model = decision.get("model", "sonnet")
            if model not in MODEL_MAP:
                model = "sonnet"

            reason = decision.get("reason", "")
            model_id = MODEL_MAP[model]

            logger.info(
                "[router] agent=%s model=%s reason=%s (%.0fms)",
                agent or "direct", model, reason, duration_ms,
            )

            # Trace to Langfuse
            self._tracer.trace_call(
                user_message=message[:300],
                response=json.dumps({"agent": agent, "model": model, "reason": reason}),
                model=ROUTER_MODEL,
                duration_ms=duration_ms,
                input_tokens=None,
                output_tokens=None,
                tools=[],
                tool_results=[],
                thinking=[],
                errors=[],
            )

            return {"agent": agent, "model": model_id, "model_name": model, "reason": reason}

        except (json.JSONDecodeError, subprocess.TimeoutExpired, Exception) as e:
            duration_ms = (time.time() - start) * 1000
            logger.warning("[router] Failed to classify (%.0fms): %s — defaulting to sonnet", duration_ms, e)

            self._tracer.trace_error(error=str(e), context=message[:300])

            return {"agent": None, "model": MODEL_MAP["sonnet"], "model_name": "sonnet", "reason": "router_fallback"}
