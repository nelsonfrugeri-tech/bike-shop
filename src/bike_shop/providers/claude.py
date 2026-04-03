from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time

from bike_shop.config import AgentConfig
from bike_shop.observability import Tracer
from bike_shop.providers import LLMProvider

logger = logging.getLogger(__name__)

# Idle-based watchdog: kill process only when it stops producing output
IDLE_TIMEOUT = int(os.environ.get("CLAUDE_IDLE_TIMEOUT", "300"))  # 5 min default

# Absolute safety net — kill no matter what after this duration
MAX_ABSOLUTE_TIMEOUT = int(os.environ.get("CLAUDE_MAX_TIMEOUT", "1800"))  # 30 min

GRACE_PERIOD = 5  # seconds between SIGTERM and SIGKILL


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

        logger.debug(
            "[%s] Calling Claude CLI (model=%s, idle_timeout=%ds, prompt=%d chars)...",
            config.name, model_id, IDLE_TIMEOUT, len(prompt),
        )
        start_time = time.time()

        if not workspace:
            raise RuntimeError(
                "workspace must be set to an isolated worktree path. "
                "Ensure AGENT_WORKTREE_DIR is configured and ensure_worktree() "
                "was called before invoking the provider."
            )

        try:
            result = _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE_TIMEOUT,
                max_timeout=MAX_ABSOLUTE_TIMEOUT,
                grace_period=GRACE_PERIOD,
                cwd=workspace,
                env=env,
                agent_name=config.name,
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

        except _IdleTimeoutError as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.warning(
                "[%s] Claude CLI idle timeout after %ds (%.0fms): %s",
                config.name, e.idle_seconds, duration_ms, e,
            )
            tracer.trace_error(
                error=f"Idle timeout after {e.idle_seconds}s (prompt={len(prompt)} chars)",
                context=prompt[-500:],
            )
            return (
                f"(timeout after {e.idle_seconds // 60}min "
                f"-- agent was idle, task may be stuck)",
                None,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error("Claude CLI error: %s", e)
            tracer.trace_error(error=str(e), context=prompt[-500:])
            return "(error)", None

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


class _IdleTimeoutError(Exception):
    """Raised when the process has been idle (no stdout) for too long."""

    def __init__(self, idle_seconds: int, reason: str = "idle") -> None:
        self.idle_seconds = idle_seconds
        self.reason = reason
        super().__init__(f"Process {reason} for {idle_seconds}s")


def _graceful_kill(proc: subprocess.Popen, grace_period: int = 5) -> None:
    """SIGTERM to process group, wait grace, then SIGKILL."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        proc.wait(timeout=grace_period)
        logger.info("[claude] Process terminated gracefully after SIGTERM")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        logger.warning("[claude] Process force-killed after SIGKILL")


def _run_with_idle_watchdog(
    cmd: list[str],
    *,
    idle_timeout: int,
    max_timeout: int,
    grace_period: int = 5,
    cwd: str,
    env: dict,
    agent_name: str = "claude",
) -> subprocess.CompletedProcess:
    """Run subprocess with idle-based watchdog.

    Monitors stdout line by line. If no output is produced for
    ``idle_timeout`` seconds, the process is killed gracefully.
    An absolute ``max_timeout`` acts as a safety net.

    Returns:
        CompletedProcess with collected stdout/stderr.

    Raises:
        _IdleTimeoutError: when idle or absolute timeout triggers.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )

    stdout_lines: list[str] = []
    last_activity = time.monotonic()
    start_time = time.monotonic()
    stop_event = threading.Event()
    kill_reason: str | None = None

    stderr_lines: list[str] = []

    def _reader() -> None:
        """Read stdout line by line, update last_activity timestamp.

        Note: iteration is line-buffered. If the process emits partial
        lines without trailing newline, last_activity won't update
        until the line completes or the pipe closes.
        """
        nonlocal last_activity
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                stdout_lines.append(line)
                last_activity = time.monotonic()
                if stop_event.is_set():
                    break
        except ValueError:
            # stdout closed
            pass

    def _stderr_reader() -> None:
        """Read stderr line by line into stderr_lines.

        stderr activity does NOT update last_activity — only stdout
        matters for idle detection.
        """
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
                if stop_event.is_set():
                    break
        except ValueError:
            # stderr closed
            pass

    def _watchdog() -> None:
        """Check idle time and absolute timeout periodically."""
        nonlocal kill_reason
        while not stop_event.wait(timeout=1.0):
            now = time.monotonic()
            idle_elapsed = now - last_activity
            total_elapsed = now - start_time

            if total_elapsed >= max_timeout:
                kill_reason = "absolute_timeout"
                logger.warning(
                    "[%s] Process hit absolute timeout (%ds) -- killing",
                    agent_name, max_timeout,
                )
                _graceful_kill(proc, grace_period)
                stop_event.set()
                return

            if idle_elapsed >= idle_timeout:
                kill_reason = "idle"
                logger.warning(
                    "[%s] Process idle for %ds -- killing",
                    agent_name, int(idle_elapsed),
                )
                _graceful_kill(proc, grace_period)
                stop_event.set()
                return

    reader_thread = threading.Thread(target=_reader, daemon=True)
    stderr_reader_thread = threading.Thread(target=_stderr_reader, daemon=True)
    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)

    reader_thread.start()
    stderr_reader_thread.start()
    watchdog_thread.start()

    proc.wait()
    stop_event.set()
    reader_thread.join(timeout=5)
    stderr_reader_thread.join(timeout=5)
    watchdog_thread.join(timeout=5)

    stderr = "".join(stderr_lines)
    stdout = "".join(stdout_lines)

    if kill_reason == "idle":
        raise _IdleTimeoutError(idle_timeout, reason="idle")
    if kill_reason == "absolute_timeout":
        raise _IdleTimeoutError(max_timeout, reason="hit absolute safety timeout")

    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
