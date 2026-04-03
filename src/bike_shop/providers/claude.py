"""Claude CLI provider with optional real-time streaming and hierarchical tracing."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from bike_shop.config import AgentConfig
from bike_shop.observability import Tracer
from bike_shop.providers import LLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeout tiers based on prompt size (characters)
# ~4 chars ~ 1 token (English text average)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Shared event parsing — used by both batch and streaming modes
# ---------------------------------------------------------------------------


@dataclass
class _ParseState:
    """Mutable accumulator for parsed stream-json events."""

    response: str = ""
    session_id: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    thinking_count: int = 0

    def to_usage_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "tools": self.tools,
            "tool_results": self.tool_results,
            "thinking": self.thinking,
            "errors": self.errors,
        }


# Callback type for real-time span creation during streaming.
# (event_type, block_or_event_dict, state) -> None
SpanCallback = Callable[[str, dict[str, Any], _ParseState], None]


def _handle_event(event: dict[str, Any], state: _ParseState,
                  on_span: SpanCallback | None = None) -> None:
    """Process a single stream-json event, updating *state* in place.

    When *on_span* is provided (streaming mode), it is called for tool_use,
    thinking, tool_result and error events so the caller can create real-time
    Langfuse spans.  In batch mode *on_span* is ``None`` and no spans are
    created.
    """
    event_type = event.get("type", "")

    # System events
    if event_type == "system" and event.get("session_id"):
        state.session_id = event["session_id"]

    # Assistant messages
    if event_type == "assistant":
        content = event.get("message", {}).get("content", [])
        for block in content:
            block_type = block.get("type", "")

            if block_type == "text":
                state.response = block.get("text", "").strip()

            elif block_type == "tool_use":
                tool_id = block.get("id", "")
                tool_name = block.get("name", "unknown")
                tool_input = json.dumps(block.get("input", {}))[:1000]

                state.tools.append({
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                })

                if on_span:
                    on_span("tool_use", block, state)

            elif block_type == "thinking":
                state.thinking_count += 1
                thought = block.get("thinking", "")[:500]
                state.thinking.append(thought)

                if on_span:
                    on_span("thinking", block, state)

        # Usage from assistant message
        usage = event.get("message", {}).get("usage", {})
        if usage:
            state.input_tokens += usage.get("input_tokens", 0)
            state.output_tokens += usage.get("output_tokens", 0)
            state.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
            state.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

    # Tool results
    if event_type == "result" and event.get("subtype") == "tool_result":
        tool_use_id = event.get("tool_use_id", "")
        content = json.dumps(event.get("content", ""))[:1000]
        is_error = event.get("is_error", False)

        state.tool_results.append({
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        })

        if on_span:
            on_span("tool_result", event, state)

    # Error events
    if event_type == "error":
        error_entry = {
            "message": event.get("error", {}).get("message", str(event)),
            "type": event.get("error", {}).get("type", "unknown"),
        }
        state.errors.append(error_entry)

        if on_span:
            on_span("error", event, state)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ClaudeProvider(LLMProvider):
    """Calls Claude via the claude CLI with optional real-time tracing."""

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
        trace_id: str | None = None,
        parent_span_id: str | None = None,
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

        # Fix #9: read STREAM_ENABLED at call time, not import time
        stream_enabled = os.environ.get("LANGFUSE_STREAM_ENABLED", "true").lower() == "true"

        if stream_enabled and tracer.enabled:
            return self._call_streaming(
                cmd, env, config, model_id, tracer,
                user_message=user_message or prompt[-500:],
                agent=agent,
                router_meta=router_meta,
                start_time=start_time,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                workspace=workspace,
                timeout=timeout,
                prompt=prompt,
            )
        else:
            return self._call_batch(
                cmd, env, config, model_id, tracer, prompt,
                user_message=user_message or prompt[-500:],
                agent=agent,
                router_meta=router_meta,
                start_time=start_time,
                workspace=workspace,
                timeout=timeout,
            )

    def _call_batch(
        self,
        cmd: list[str],
        env: dict[str, str],
        config: AgentConfig,
        model_id: str,
        tracer: Tracer,
        prompt: str,
        *,
        user_message: str,
        agent: str | None,
        router_meta: dict | None,
        start_time: float,
        workspace: str,
        timeout: int,
    ) -> tuple[str, str | None]:
        """Original subprocess.run batch mode — fallback."""
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

            response, new_session_id, usage = _parse_response(result.stdout)

            tracer.trace_call(
                user_message=user_message,
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
        env: dict[str, str],
        grace_period: int = 5,
    ) -> subprocess.CompletedProcess[str]:
        """Run subprocess with graceful shutdown on timeout.

        On timeout: SIGTERM -> wait grace_period -> SIGKILL.
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

    def _call_streaming(
        self,
        cmd: list[str],
        env: dict[str, str],
        config: AgentConfig,
        model_id: str,
        tracer: Tracer,
        *,
        user_message: str,
        agent: str | None,
        router_meta: dict | None,
        start_time: float,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        workspace: str,
        timeout: int,
        prompt: str,
    ) -> tuple[str, str | None]:
        """Streaming mode with Popen — creates spans in real-time."""
        # Create trace and LLM call span
        if not trace_id:
            trace_name = f"{config.name}/call"
            if agent:
                trace_name = f"{config.name}/call/{agent}"
            trace_id = tracer.start_trace(
                trace_name,
                input=user_message,
                metadata={
                    "agent": config.name,
                    "selected_agent": agent,
                    "router_model": (router_meta or {}).get("model_name"),
                    "router_reason": (router_meta or {}).get("reason"),
                    "model": model_id,
                },
            )

        gen_id = tracer.start_generation(
            "claude-cli",
            trace_id=trace_id,
            model=model_id,
            input=user_message,
            parent_id=parent_span_id,
        )

        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workspace,  # Fix #2: use workspace param, not env var
                env=env,
            )

            response, new_session_id, usage = _parse_stream(
                proc, tracer, trace_id, gen_id,
            )

            duration_ms = (time.time() - start_time) * 1000

            # End generation
            tracer.end_generation(
                gen_id,
                trace_id=trace_id,
                output=response,
                usage={
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                },
                metadata={"duration_ms": round(duration_ms)},
            )

            # Update trace with output
            tracer.update_trace(
                trace_id,
                output=response,
                metadata={
                    "duration_ms": round(duration_ms),
                    "tool_count": len(usage.get("tools", [])),
                    "has_thinking": bool(usage.get("thinking")),
                    "has_errors": bool(usage.get("errors")),
                },
                tags=["error"] if usage.get("errors") else [],
            )

            # Error spans
            for i, error in enumerate(usage.get("errors", [])):
                span_id = tracer.start_span(
                    f"error-{i + 1}",
                    trace_id=trace_id,
                    parent_id=gen_id,
                    input=error.get("message", ""),
                    metadata={"type": "error", "error_type": error.get("type", "unknown")},
                )
                tracer.end_span(span_id, trace_id=trace_id, level="ERROR")

            tracer.flush()

            if proc.returncode != 0 and proc.returncode is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                logger.error("Claude CLI failed (rc=%d): %s", proc.returncode, stderr.strip())

            return response, new_session_id

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error("Claude CLI streaming error: %s", e)

            tracer.end_generation(
                gen_id,
                trace_id=trace_id,
                output=str(e),
                metadata={"error": True, "duration_ms": round(duration_ms)},
            )
            tracer.update_trace(trace_id, tags=["error"])
            tracer.flush()

            # Kill process if still running
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

            return "(error)", None


def _parse_stream(
    proc: subprocess.Popen[str],
    tracer: Tracer,
    trace_id: str,
    gen_id: str,
) -> tuple[str, str | None, dict[str, Any]]:
    """Parse streaming JSON from Popen, creating spans in real-time.

    Returns (response_text, session_id, usage_dict).
    """
    state = _ParseState()

    # Map tool_use_id -> span_id for matching results
    tool_span_map: dict[str, str] = {}

    def _on_span(kind: str, data: dict[str, Any], st: _ParseState) -> None:
        """Create real-time Langfuse spans during streaming."""
        if kind == "tool_use":
            tool_id = data.get("id", "")
            tool_name = data.get("name", "unknown")
            tool_input = json.dumps(data.get("input", {}))[:1000]
            span_id = tracer.start_span(
                f"tool.{tool_name}",
                trace_id=trace_id,
                parent_id=gen_id,
                input=tool_input,
                metadata={"type": "tool_use", "tool_name": tool_name},
            )
            tool_span_map[tool_id] = span_id

        elif kind == "thinking":
            thought = data.get("thinking", "")[:500]
            span_id = tracer.start_span(
                f"thinking.{st.thinking_count}",
                trace_id=trace_id,
                parent_id=gen_id,
                input=thought,
                metadata={"type": "thinking"},
            )
            tracer.end_span(span_id, trace_id=trace_id)

        elif kind == "tool_result":
            tool_use_id = data.get("tool_use_id", "")
            content = json.dumps(data.get("content", ""))[:1000]
            is_error = data.get("is_error", False)
            if tool_use_id in tool_span_map:
                tracer.end_span(
                    tool_span_map[tool_use_id],
                    trace_id=trace_id,
                    output=content,
                    metadata={"is_error": is_error},
                    level="ERROR" if is_error else None,
                )

    # Fix #8: guard clause instead of assert
    if proc.stdout is None:
        logger.error("proc.stdout is None — cannot parse streaming output")
        return "...", None, state.to_usage_dict()

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except (ValueError, KeyError):
            continue

        _handle_event(event, state, on_span=_on_span)

    # Wait for process to finish
    proc.wait()

    # End any tool spans that never got a result
    for tool_id, span_id in tool_span_map.items():
        matched = any(r.get("tool_use_id") == tool_id for r in state.tool_results)
        if not matched:
            tracer.end_span(span_id, trace_id=trace_id)

    if not state.response:
        state.response = "..."

    return state.response, state.session_id, state.to_usage_dict()


def _parse_response(stdout: str) -> tuple[str, str | None, dict[str, Any]]:
    """Parse ALL stream-json events from completed output.

    Returns (response_text, session_id, full_usage).
    Kept for batch mode fallback.
    """
    state = _ParseState()

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, KeyError):
            continue

        _handle_event(event, state)

    if not state.response:
        state.response = "..."

    return state.response, state.session_id, state.to_usage_dict()
