from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time

from bike_shop.config import AgentConfig
from bike_shop.observability import Tracer
from bike_shop.providers import LLMProvider

logger = logging.getLogger(__name__)

# Timeout tiers based on prompt size (characters)
# ~4 chars ≈ 1 token (English text average)
TIMEOUT_SMALL = int(os.environ.get("CLAUDE_TIMEOUT_SMALL", "180"))     # 3 min — < 8k tokens
TIMEOUT_MEDIUM = int(os.environ.get("CLAUDE_TIMEOUT_MEDIUM", "300"))   # 5 min — 8k-32k tokens
TIMEOUT_LARGE = int(os.environ.get("CLAUDE_TIMEOUT_LARGE", "600"))     # 10 min — > 32k tokens

CONTEXT_MEDIUM_THRESHOLD = 32_000   # ~8k tokens
CONTEXT_LARGE_THRESHOLD = 128_000   # ~32k tokens


def _select_timeout(prompt: str) -> int:
    """Select timeout tier based on prompt size."""
    size = len(prompt)
    if size >= CONTEXT_LARGE_THRESHOLD:
        return TIMEOUT_LARGE
    if size >= CONTEXT_MEDIUM_THRESHOLD:
        return TIMEOUT_MEDIUM
    return TIMEOUT_SMALL


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
        agent: str | None = None,
        session_id: str | None = None,
        memory_file: str | None = None,
        mcp_config: str | None = None,
        github_token: str | None = None,
        router_meta: dict | None = None,
        workspace: str | None = None,
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

        if agent:
            cmd.extend(["--agent", agent])

        if mcp_config:
            cmd.extend(["--mcp-config", mcp_config])

        if session_id:
            cmd.extend(["--resume", session_id])

        if memory_file and os.path.exists(memory_file):
            cmd.extend(["--append-system-prompt-file", memory_file])

        env = os.environ.copy()
        if github_token:
            env["GH_TOKEN"] = github_token

        timeout = _select_timeout(prompt)
        logger.debug("[%s] Calling Claude CLI (model=%s, timeout=%ds, prompt=%d chars)...",
                     config.name, model_id, timeout, len(prompt))
        start_time = time.time()

        if not workspace:
            raise RuntimeError(
                "workspace must be set to an isolated worktree path. "
                "Ensure AGENT_WORKTREE_DIR is configured and ensure_worktree() "
                "was called before invoking the provider."
            )

        try:
            result = self._run_with_graceful_timeout(
                cmd, timeout=timeout,
                cwd=workspace,
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
                tools=usage.get("tools"),
                tool_results=usage.get("tool_results"),
                thinking=usage.get("thinking"),
                errors=usage.get("errors"),
                session_id=new_session_id,
                selected_agent=agent,
                router_meta=router_meta,
            )

            return response, new_session_id

        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000
            logger.warning("[%s] Claude CLI timed out after %ds (%.0fms)",
                          config.name, timeout, duration_ms)
            tracer.trace_error(
                error=f"Timeout after {timeout}s (prompt={len(prompt)} chars)",
                context=prompt[-500:],
            )
            return f"(timeout after {timeout // 60}min — task was too long, try breaking it into smaller steps)", None

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error("Claude CLI error: %s", e)
            tracer.trace_error(error=str(e), context=prompt[-500:])
            return "(error)", None

    @staticmethod
    def _run_with_graceful_timeout(
        cmd: list[str],
        timeout: int,
        cwd: str,
        env: dict,
        grace_period: int = 5,
    ) -> subprocess.CompletedProcess:
        """Run subprocess with graceful shutdown on timeout.

        On timeout: SIGTERM → wait grace_period → SIGKILL.
        This kills child processes (uvicorn, servers) cleanly.
        """
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own process group for clean kill
        )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            # Graceful: SIGTERM the entire process group
            pgid = os.getpgid(proc.pid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass

            # Wait for grace period
            try:
                stdout, stderr = proc.communicate(timeout=grace_period)
                logger.info("[claude] Process terminated gracefully after SIGTERM")
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                # Force kill the entire process group
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.kill()
                proc.wait()
                logger.warning("[claude] Process force-killed after SIGKILL")
                raise subprocess.TimeoutExpired(cmd, timeout)

    def _parse_response(self, stdout: str) -> tuple[str, str | None, dict]:
        """Parse ALL stream-json events. Returns (response_text, session_id, full_usage)."""
        response = ""
        new_session_id = None
        tools = []          # each tool_use with name, input, id
        tool_results = []   # each tool_result with output
        thinking = []       # thinking blocks
        errors = []         # error events
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_creation_tokens = 0

        for line in stdout.splitlines():
            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                # System events (session id)
                if event_type == "system" and event.get("session_id"):
                    new_session_id = event["session_id"]

                # Assistant messages
                if event_type == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        block_type = block.get("type", "")

                        if block_type == "text":
                            response = block.get("text", "").strip()

                        elif block_type == "tool_use":
                            tools.append({
                                "id": block.get("id", ""),
                                "name": block.get("name", "unknown"),
                                "input": json.dumps(block.get("input", {}))[:1000],
                            })

                        elif block_type == "thinking":
                            thinking.append(block.get("thinking", "")[:500])

                    # Usage from assistant message
                    usage = event.get("message", {}).get("usage", {})
                    if usage:
                        input_tokens += usage.get("input_tokens", 0)
                        output_tokens += usage.get("output_tokens", 0)
                        cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                        cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

                # Tool results
                if event_type == "result" and event.get("subtype") == "tool_result":
                    tool_results.append({
                        "tool_use_id": event.get("tool_use_id", ""),
                        "content": json.dumps(event.get("content", ""))[:1000],
                        "is_error": event.get("is_error", False),
                    })

                # Error events
                if event_type == "error":
                    errors.append({
                        "message": event.get("error", {}).get("message", str(event)),
                        "type": event.get("error", {}).get("type", "unknown"),
                    })

            except (ValueError, KeyError):
                continue

        if not response:
            response = "..."

        return response, new_session_id, {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "tools": tools,
            "tool_results": tool_results,
            "thinking": thinking,
            "errors": errors,
        }
