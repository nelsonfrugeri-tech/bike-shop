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
    """Records LLM call traces to Langfuse via REST API."""

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
        tools_used: list[str] | None = None,
        thread_ts: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if not self._enabled:
            return

        trace_id = str(uuid.uuid4())
        gen_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        batch = [
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": now,
                "body": {
                    "id": trace_id,
                    "name": f"{self._agent_name}/call",
                    "userId": self._agent_name,
                    "sessionId": session_id or thread_ts,
                    "input": user_message,
                    "output": response,
                    "metadata": {
                        "agent": self._agent_name,
                        "model": model,
                        "thread_ts": thread_ts,
                        "tools_used": tools_used or [],
                    },
                },
            },
            {
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
                        "tools_used": tools_used or [],
                    },
                    "completionStartTime": now,
                },
            },
        ]

        ok = _post("/api/public/ingestion", {"batch": batch})
        if ok:
            logger.debug("[%s] Trace sent to Langfuse", self._agent_name)
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
