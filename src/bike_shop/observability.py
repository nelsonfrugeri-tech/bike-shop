"""Hierarchical observability — real-time tracing via Langfuse REST API.

Supports nested spans and generations with parent/child relationships,
micro-batch flushing, and backwards-compatible trace_call()/trace_error().
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from base64 import b64encode
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FLUSH_INTERVAL_MS = int(os.environ.get("LANGFUSE_FLUSH_INTERVAL_MS", "500"))
TRACE_DETAIL = os.environ.get("LANGFUSE_TRACE_DETAIL", "full")  # full|basic|off


class TraceDetail(Enum):
    FULL = "full"
    BASIC = "basic"
    OFF = "off"


def _parse_detail() -> TraceDetail:
    try:
        return TraceDetail(TRACE_DETAIL.lower())
    except ValueError:
        return TraceDetail.FULL


def _get_config() -> tuple[str, str] | None:
    """Returns (host, auth_header) or None if not configured."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

    if not public_key or not secret_key:
        return None

    credentials = b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return host, f"Basic {credentials}"


def _post(path: str, body: dict[str, Any],
          config_override: tuple[str, str] | None = None) -> bool:
    """Send a POST request to Langfuse API. Returns True on success."""
    config = config_override or _get_config()
    if not config:
        return False

    host, auth = config
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{host}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200 or resp.status == 207
    except Exception as e:
        logger.debug("Langfuse request failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _ensure_json_object(value: Any) -> dict[str, Any] | list[Any]:
    """Ensure value is a JSON object (dict/list), not a plain string.

    Langfuse REST API expects input/output as JSON objects.
    Plain strings are silently dropped, resulting in null.
    """
    if isinstance(value, (dict, list)):
        return value
    return {"value": value}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Batch buffer with micro-batch flushing
# ---------------------------------------------------------------------------


class _BatchBuffer:
    """Thread-safe event buffer that auto-flushes on interval."""

    def __init__(self, flush_interval_ms: int = FLUSH_INTERVAL_MS,
                 config_override: tuple[str, str] | None = None) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._flush_interval = flush_interval_ms / 1000.0
        self._timer: threading.Timer | None = None
        self._started = False
        self._config_override = config_override

    def add(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)
            if not self._started:
                self._started = True
                self._schedule_flush()

    def add_many(self, events: list[dict[str, Any]]) -> None:
        with self._lock:
            self._events.extend(events)
            if not self._started:
                self._started = True
                self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._flush_interval, self._do_flush)
        self._timer.daemon = True
        self._timer.start()

    def _do_flush(self) -> None:
        with self._lock:
            events = self._events[:]
            self._events.clear()
            self._started = False

        if events:
            ok = _post("/api/public/ingestion", {"batch": events},
                       config_override=self._config_override)
            if ok:
                logger.debug("Langfuse batch flushed (%d events)", len(events))
            else:
                logger.warning("Langfuse batch flush failed (%d events)", len(events))

    def flush(self) -> None:
        """Force immediate flush."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._do_flush()


# Singleton buffer
_buffer = _BatchBuffer()

import atexit
atexit.register(_buffer.flush)


# ---------------------------------------------------------------------------
# Tracer — hierarchical span management
# ---------------------------------------------------------------------------


class Tracer:
    """Hierarchical tracer with real-time span management via Langfuse REST API.

    Supports:
    - Nested traces -> spans -> generations
    - Micro-batch flushing (configurable interval)
    - Backwards-compatible trace_call() / trace_error()
    - Graceful degradation when Langfuse is unavailable
    """

    def __init__(
        self,
        agent_name: str,
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._custom_config: tuple[str, str] | None = None

        if langfuse_public_key and langfuse_secret_key:
            host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
            credentials = b64encode(
                f"{langfuse_public_key}:{langfuse_secret_key}".encode()
            ).decode()
            self._custom_config = (host, f"Basic {credentials}")

        config = self._custom_config or _get_config()
        self._enabled = config is not None and _parse_detail() != TraceDetail.OFF

        # Per-project buffer when custom Langfuse keys are provided,
        # otherwise use the shared global buffer
        if self._custom_config:
            self._buffer = _BatchBuffer(config_override=self._custom_config)
        else:
            self._buffer = _buffer

        if self._enabled:
            logger.info("[%s] Langfuse tracing enabled (detail=%s)", agent_name, TRACE_DETAIL)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Core API — hierarchical span management
    # ------------------------------------------------------------------

    def start_trace(
        self,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
        input: str | None = None,
    ) -> str:
        """Start a new trace. Returns trace_id."""
        if not self._enabled:
            return _uuid()

        trace_id = _uuid()
        now = _now_iso()

        self._buffer.add({
            "id": _uuid(),
            "type": "trace-create",
            "timestamp": now,
            "body": {
                "id": trace_id,
                "name": name,
                "userId": user_id or self._agent_name,
                "sessionId": session_id,
                "input": _ensure_json_object(input) if input is not None else None,
                "metadata": metadata or {},
                "tags": tags or [],
            },
        })

        return trace_id

    def update_trace(
        self,
        trace_id: str,
        *,
        output: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update an existing trace with output/metadata."""
        if not self._enabled:
            return

        body: dict[str, Any] = {"id": trace_id}
        if output is not None:
            body["output"] = _ensure_json_object(output)
        if metadata is not None:
            body["metadata"] = metadata
        if tags is not None:
            body["tags"] = tags

        self._buffer.add({
            "id": _uuid(),
            "type": "trace-create",
            "timestamp": _now_iso(),
            "body": body,
        })

    def start_span(
        self,
        name: str,
        *,
        trace_id: str,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        input: Any = None,
    ) -> str:
        """Start a span. Returns span_id."""
        if not self._enabled:
            return _uuid()

        span_id = _uuid()
        now = _now_iso()

        body: dict[str, Any] = {
            "id": span_id,
            "traceId": trace_id,
            "name": name,
            "startTime": now,
            "metadata": metadata or {},
        }
        if parent_id:
            body["parentObservationId"] = parent_id
        if input is not None:
            body["input"] = _ensure_json_object(input)

        self._buffer.add({
            "id": _uuid(),
            "type": "span-create",
            "timestamp": now,
            "body": body,
        })

        return span_id

    def end_span(
        self,
        span_id: str,
        *,
        trace_id: str,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
    ) -> None:
        """End a span with output and optional metadata."""
        if not self._enabled:
            return

        now = _now_iso()
        body: dict[str, Any] = {
            "id": span_id,
            "traceId": trace_id,
            "endTime": now,
        }
        if output is not None:
            body["output"] = _ensure_json_object(output)
        if metadata:
            body["metadata"] = metadata
        if level:
            body["level"] = level

        self._buffer.add({
            "id": _uuid(),
            "type": "span-update",
            "timestamp": now,
            "body": body,
        })

    def start_generation(
        self,
        name: str,
        *,
        trace_id: str,
        model: str,
        input: Any = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Start a generation (LLM call). Returns generation_id."""
        if not self._enabled:
            return _uuid()

        gen_id = _uuid()
        now = _now_iso()

        body: dict[str, Any] = {
            "id": gen_id,
            "traceId": trace_id,
            "name": name,
            "model": model,
            "startTime": now,
            "metadata": metadata or {},
        }
        if parent_id:
            body["parentObservationId"] = parent_id
        if input is not None:
            body["input"] = _ensure_json_object(input)

        self._buffer.add({
            "id": _uuid(),
            "type": "generation-create",
            "timestamp": now,
            "body": body,
        })

        return gen_id

    def end_generation(
        self,
        gen_id: str,
        *,
        trace_id: str,
        output: Any = None,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """End a generation with output, usage, and optional metadata."""
        if not self._enabled:
            return

        now = _now_iso()
        body: dict[str, Any] = {
            "id": gen_id,
            "traceId": trace_id,
            "endTime": now,
            "completionStartTime": now,
        }
        if output is not None:
            body["output"] = _ensure_json_object(output)
        if usage:
            body["usage"] = usage
        if metadata:
            body["metadata"] = metadata

        self._buffer.add({
            "id": _uuid(),
            "type": "generation-update",
            "timestamp": now,
            "body": body,
        })

    def flush(self) -> None:
        """Force flush all pending events."""
        self._buffer.flush()

    # ------------------------------------------------------------------
    # Backwards-compatible API
    # ------------------------------------------------------------------

    def trace_call(
        self,
        *,
        user_message: str,
        response: str,
        model: str,
        duration_ms: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_results: list[dict[str, Any]] | None = None,
        thinking: list[str] | None = None,
        errors: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        session_id: str | None = None,
        selected_agent: str | None = None,
        router_meta: dict[str, Any] | None = None,
    ) -> None:
        """Backwards-compatible: sends a complete trace with all spans at once."""
        if not self._enabled:
            return

        trace_name = f"{self._agent_name}/call"
        if selected_agent:
            trace_name = f"{self._agent_name}/call/{selected_agent}"

        trace_id = self.start_trace(
            trace_name,
            session_id=session_id or thread_ts,
            input=user_message,
            metadata={
                "agent": self._agent_name,
                "selected_agent": selected_agent,
                "router_model": (router_meta or {}).get("model_name"),
                "router_reason": (router_meta or {}).get("reason"),
                "model": model,
                "duration_ms": round(duration_ms),
                "thread_ts": thread_ts,
                "tool_count": len(tools or []),
                "has_thinking": bool(thinking),
                "has_errors": bool(errors),
            },
            tags=["error"] if errors else [],
        )

        self.update_trace(trace_id, output=response)

        # Generation
        gen_id = self.start_generation(
            "claude-cli",
            trace_id=trace_id,
            model=model,
            input=user_message,
        )
        self.end_generation(
            gen_id,
            trace_id=trace_id,
            output=response,
            usage={
                "input": input_tokens or 0,
                "output": output_tokens or 0,
            },
            metadata={"duration_ms": round(duration_ms)},
        )

        # Thinking spans
        if _parse_detail() == TraceDetail.FULL:
            for i, thought in enumerate(thinking or []):
                span_id = self.start_span(
                    f"thinking-{i + 1}",
                    trace_id=trace_id,
                    parent_id=gen_id,
                    input=thought,
                    metadata={"type": "thinking"},
                )
                self.end_span(span_id, trace_id=trace_id)

            # Tool spans
            for tool in tools or []:
                tool_span_id = self.start_span(
                    f"tool/{tool['name']}",
                    trace_id=trace_id,
                    parent_id=gen_id,
                    input=tool.get("input", ""),
                    metadata={
                        "type": "tool_use",
                        "tool_name": tool["name"],
                        "tool_use_id": tool.get("id", ""),
                    },
                )

                # Find matching result
                for result in tool_results or []:
                    if result.get("tool_use_id") == tool.get("id"):
                        self.end_span(
                            tool_span_id,
                            trace_id=trace_id,
                            output=result.get("content", ""),
                            metadata={
                                "type": "tool_use",
                                "tool_name": tool["name"],
                                "is_error": result.get("is_error", False),
                            },
                        )
                        break
                else:
                    self.end_span(tool_span_id, trace_id=trace_id)

            # Error spans
            for i, error in enumerate(errors or []):
                span_id = self.start_span(
                    f"error-{i + 1}",
                    trace_id=trace_id,
                    parent_id=gen_id,
                    input=error.get("message", ""),
                    metadata={
                        "type": "error",
                        "error_type": error.get("type", "unknown"),
                    },
                )
                self.end_span(span_id, trace_id=trace_id, level="ERROR")

        # Flush immediately for backwards compat (callers expect sync send)
        self.flush()

    def trace_error(self, *, error: str, context: str = "") -> None:
        """Backwards-compatible: sends a standalone error trace."""
        if not self._enabled:
            return

        trace_id = self.start_trace(
            f"{self._agent_name}/error",
            metadata={
                "agent": self._agent_name,
                "error": error[:500],
                "context": context[:500],
            },
            tags=["error"],
        )

        self.flush()
