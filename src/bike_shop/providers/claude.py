from __future__ import annotations

import json
import logging
import os
import subprocess

from bike_shop.config import AgentConfig
from bike_shop.providers import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeProvider(LLMProvider):
    """Calls Claude via the claude CLI."""

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
        model_id = model_override or config.model_id

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
            if result.returncode != 0:
                logger.error("Claude CLI failed (rc=%d): %s", result.returncode, result.stderr.strip())

            return self._parse_response(result.stdout)

        except Exception as e:
            logger.error("Claude CLI error: %s", e)
            return "(error)", None

    def _parse_response(self, stdout: str) -> tuple[str, str | None]:
        """Parse stream-json output. Returns (response_text, session_id)."""
        response = ""
        new_session_id = None

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
            except (ValueError, KeyError):
                continue

        if not response:
            response = "..."

        return response, new_session_id
