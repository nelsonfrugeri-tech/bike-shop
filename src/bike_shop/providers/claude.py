from __future__ import annotations

import json
import logging
import os
import subprocess
import time

from bike_shop.config import AgentConfig
from bike_shop.observability import Tracer
from bike_shop.providers import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeProvider(LLMProvider):
    """Calls Claude via the claude CLI."""

    def __init__(self) -> None:
        self._tracers: dict[str, Tracer] = {}

    def _get_tracer(self, agent_name: str) -> Tracer:
        if agent_name not in self._tracers:
            self._tracers[agent_name] = Tracer(agent_name)
        return self._tracers[agent_name]

    def call(
        self,
        config: AgentConfig,
        prompt: str,
        *,
        user_message: str = "",
        model_override: str | None = None,
        session_id: str | None = None,
        memory_file: str | None = None,
        mcp_config: str | None = None,
        github_token: str | None = None,
    ) -> tuple[str, str | None]:
        model_id = model_override or config.model_id
        tracer = self._get_tracer(config.name)

        cmd = [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model_id,
        ]

        if mcp_config:
            cmd.extend(["--mcp-config", mcp_config])

        if session_id:
            cmd.extend(["--resume", session_id])

        if memory_file and os.path.exists(memory_file):
            cmd.extend(["--append-system-prompt-file", memory_file])

        env = os.environ.copy()
        if github_token:
            env["GH_TOKEN"] = github_token

        logger.debug("[%s] Calling Claude CLI (model=%s)...", config.name, model_id)
        start_time = time.time()

        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=None,
                cwd=os.environ.get("AGENT_WORKSPACE", os.path.expanduser("~")),
                env=env,
            )
            duration_ms = (time.time() - start_time) * 1000

            if result.returncode != 0:
                logger.error("Claude CLI failed (rc=%d): %s", result.returncode, result.stderr.strip())
                tracer.trace_error(error=result.stderr.strip()[:500], context=prompt[-500:])

            response, new_session_id, usage = self._parse_response(result.stdout)

            tracer.trace_call(
                user_message=user_message or prompt[-500:],
                response=response,
                model=model_id,
                duration_ms=duration_ms,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                tools_used=usage.get("tools_used"),
                session_id=new_session_id,
            )

            return response, new_session_id

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error("Claude CLI error: %s", e)
            tracer.trace_error(error=str(e), context=prompt[-500:])
            return "(error)", None

    def _parse_response(self, stdout: str) -> tuple[str, str | None, dict]:
        """Parse stream-json output. Returns (response_text, session_id, usage)."""
        response = ""
        new_session_id = None
        tools_used = []
        input_tokens = None
        output_tokens = None

        for line in stdout.splitlines():
            try:
                event = json.loads(line)

                if event.get("type") == "system" and event.get("session_id"):
                    new_session_id = event["session_id"]

                if event.get("type") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    texts = [c["text"] for c in content if c.get("type") == "text"]
                    if texts:
                        response = "\n".join(texts).strip()

                    # Extract tool uses
                    for c in content:
                        if c.get("type") == "tool_use":
                            tools_used.append(c.get("name", "unknown"))

                    # Extract usage stats
                    usage = event.get("message", {}).get("usage", {})
                    if usage:
                        input_tokens = usage.get("input_tokens", input_tokens)
                        output_tokens = usage.get("output_tokens", output_tokens)

            except (ValueError, KeyError):
                continue

        if not response:
            response = "..."

        return response, new_session_id, {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tools_used": tools_used,
        }
