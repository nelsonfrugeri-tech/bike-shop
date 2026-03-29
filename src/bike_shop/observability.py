from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import uuid
from base64 import b64encode

logger = logging.getLogger(__name__)


def _get_config() -> tuple[str, str] | None:
    """Returns (host, auth_header) or None if not configured."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

    if not public_key or not secret_key:
        return None

    credentials = b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return host, f"Basic {credentials}"


def _post(path: str, body: dict) -> bool:
    """Send a POST request to Langfuse API. Returns True on success."""
    config = _get_config()
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


class Tracer:
    """Records full LLM call traces to Langfuse via REST API."""

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name
        self._enabled = _get_config() is not None
        if self._enabled:
            logger.info("[%s] Langfuse tracing enabled", agent_name)

    def trace_call(
        self,
        *,
        user_message: str,
        response: str,
        model: str,
        duration_ms: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_results: list[dict] | None = None,
        thinking: list[str] | None = None,
        errors: list[dict] | None = None,
        thread_ts: str | None = None,
        session_id: str | None = None,
        selected_agent: str | None = None,
        router_meta: dict | None = None,
    ) -> None:
        if not self._enabled:
            return

        trace_id = str(uuid.uuid4())
        gen_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        batch = []

        # 1. Trace (container)
        trace_name = f"{self._agent_name}/call"
        if selected_agent:
            trace_name = f"{self._agent_name}/call/{selected_agent}"

        batch.append({
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "timestamp": now,
            "body": {
                "id": trace_id,
                "name": trace_name,
                "userId": self._agent_name,
                "sessionId": session_id or thread_ts,
                "input": user_message,
                "output": response,
                "metadata": {
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
                "tags": ["error"] if errors else [],
            },
        })

        # 2. Generation (LLM call)
        batch.append({
            "id": str(uuid.uuid4()),
            "type": "generation-create",
            "timestamp": now,
            "body": {
                "id": gen_id,
                "traceId": trace_id,
                "name": "claude-cli",
                "model": model,
                "input": user_message,
                "output": response,
                "usage": {
                    "input": input_tokens or 0,
                    "output": output_tokens or 0,
                },
                "metadata": {
                    "duration_ms": round(duration_ms),
                },
                "completionStartTime": now,
            },
        })

        # 3. Thinking spans
        for i, thought in enumerate(thinking or []):
            batch.append({
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "timestamp": now,
                "body": {
                    "id": str(uuid.uuid4()),
                    "traceId": trace_id,
                    "parentObservationId": gen_id,
                    "name": f"thinking-{i+1}",
                    "input": thought,
                    "metadata": {"type": "thinking"},
                },
            })

        # 4. Tool use spans
        for tool in (tools or []):
            tool_span_id = str(uuid.uuid4())
            batch.append({
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "timestamp": now,
                "body": {
                    "id": tool_span_id,
                    "traceId": trace_id,
                    "parentObservationId": gen_id,
                    "name": f"tool/{tool['name']}",
                    "input": tool.get("input", ""),
                    "metadata": {
                        "type": "tool_use",
                        "tool_name": tool["name"],
                        "tool_use_id": tool.get("id", ""),
                    },
                },
            })

            # Find matching result
            for result in (tool_results or []):
                if result.get("tool_use_id") == tool.get("id"):
                    batch.append({
                        "id": str(uuid.uuid4()),
                        "type": "span-update",
                        "timestamp": now,
                        "body": {
                            "id": tool_span_id,
                            "traceId": trace_id,
                            "output": result.get("content", ""),
                            "metadata": {
                                "type": "tool_use",
                                "tool_name": tool["name"],
                                "is_error": result.get("is_error", False),
                            },
                        },
                    })
                    break

        # 5. Error spans
        for i, error in enumerate(errors or []):
            batch.append({
                "id": str(uuid.uuid4()),
                "type": "span-create",
                "timestamp": now,
                "body": {
                    "id": str(uuid.uuid4()),
                    "traceId": trace_id,
                    "parentObservationId": gen_id,
                    "name": f"error-{i+1}",
                    "input": error.get("message", ""),
                    "metadata": {
                        "type": "error",
                        "error_type": error.get("type", "unknown"),
                    },
                    "level": "ERROR",
                },
            })

        ok = _post("/api/public/ingestion", {"batch": batch})
        if ok:
            logger.debug("[%s] Full trace sent to Langfuse (%d events)", self._agent_name, len(batch))
        else:
            logger.warning("[%s] Failed to send trace to Langfuse", self._agent_name)

    def trace_error(self, *, error: str, context: str = "") -> None:
        if not self._enabled:
            return

        trace_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        batch = [
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": now,
                "body": {
                    "id": trace_id,
                    "name": f"{self._agent_name}/error",
                    "userId": self._agent_name,
                    "metadata": {
                        "agent": self._agent_name,
                        "error": error[:500],
                        "context": context[:500],
                    },
                    "tags": ["error"],
                },
            },
        ]

        _post("/api/public/ingestion", {"batch": batch})
