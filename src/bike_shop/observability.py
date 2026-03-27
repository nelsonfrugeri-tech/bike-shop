from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# Lazy-loaded Langfuse client
_langfuse = None


def _get_langfuse():
    """Get or create a Langfuse client. Returns None if not configured."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

    if not public_key or not secret_key:
        logger.debug("Langfuse not configured — observability disabled")
        return None

    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("Langfuse connected at %s", host)
        return _langfuse
    except ImportError:
        logger.warning("langfuse package not installed — pip install langfuse")
        return None
    except Exception as e:
        logger.error("Failed to connect to Langfuse: %s", e)
        return None


class Tracer:
    """Records LLM call traces to Langfuse."""

    def __init__(self, agent_name: str) -> None:
        self._agent_name = agent_name

    def trace_call(
        self,
        *,
        prompt: str,
        response: str,
        model: str,
        duration_ms: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        tools_used: list[str] | None = None,
        thread_ts: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Record a single LLM call to Langfuse."""
        langfuse = _get_langfuse()
        if not langfuse:
            return

        try:
            trace = langfuse.trace(
                name=f"{self._agent_name}/call",
                user_id=self._agent_name,
                session_id=session_id or thread_ts,
                metadata={
                    "agent": self._agent_name,
                    "model": model,
                    "thread_ts": thread_ts,
                    "tools_used": tools_used or [],
                },
            )

            trace.generation(
                name="claude-cli",
                model=model,
                input=prompt[-2000:],  # truncate to save space
                output=response[-2000:],
                usage={
                    "input": input_tokens,
                    "output": output_tokens,
                },
                metadata={
                    "duration_ms": duration_ms,
                    "tools_used": tools_used or [],
                },
            )

            langfuse.flush()
        except Exception as e:
            logger.warning("[%s] Failed to send trace to Langfuse: %s", self._agent_name, e)

    def trace_error(self, *, error: str, context: str = "") -> None:
        """Record an error to Langfuse."""
        langfuse = _get_langfuse()
        if not langfuse:
            return

        try:
            langfuse.trace(
                name=f"{self._agent_name}/error",
                user_id=self._agent_name,
                metadata={
                    "agent": self._agent_name,
                    "error": error,
                    "context": context[:500],
                },
                level="ERROR",
            )
            langfuse.flush()
        except Exception as e:
            logger.warning("[%s] Failed to send error trace: %s", self._agent_name, e)
