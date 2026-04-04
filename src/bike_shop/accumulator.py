"""Message accumulator — buffers rapid-fire Slack messages into batches.

When a user sends multiple messages in quick succession (e.g. 3 tasks),
the accumulator collects them within a configurable window and flushes
them as a single batch for consolidated processing.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

BUFFER_WINDOW = float(os.environ.get("MSG_BUFFER_WINDOW", "3.0"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "10"))
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "3"))


class MessageAccumulator:
    """Buffers messages per agent+thread, flushes after window expires.

    Usage:
        acc = MessageAccumulator(flush_callback=handle_batch)
        acc.add(agent_key, thread_ts, message_dict)
        # After BUFFER_WINDOW seconds of silence, handle_batch is called
        # with (key, [messages])
    """

    def __init__(self, flush_callback: Callable[[str, list[dict[str, Any]]], None]) -> None:
        self._callback = flush_callback
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def add(
        self,
        agent_key: str,
        thread_ts: str,
        message: dict[str, Any],
    ) -> None:
        """Add message to buffer. Starts/resets flush timer."""
        key = f"{agent_key}:{thread_ts}"
        flush_now = False

        with self._lock:
            self._buffers.setdefault(key, []).append(message)
            buf_size = len(self._buffers[key])

            # Cancel existing timer — new message resets the window
            if key in self._timers:
                self._timers[key].cancel()
                del self._timers[key]

            # Check if batch is full
            if buf_size >= MAX_BATCH_SIZE:
                flush_now = True
            else:
                # Start new timer
                timer = threading.Timer(BUFFER_WINDOW, self._flush, args=[key])
                timer.daemon = True
                timer.start()
                self._timers[key] = timer

                logger.debug(
                    "[accumulator] Buffered msg %d for %s (window=%.1fs)",
                    buf_size, key, BUFFER_WINDOW,
                )

        # Flush outside the lock to avoid deadlock
        if flush_now:
            logger.info(
                "[accumulator] Batch full (%d msgs) for %s — flushing immediately",
                buf_size, key,
            )
            self._flush(key)

    def _flush(self, key: str) -> None:
        """Timer expired or batch full — flush all buffered messages."""
        with self._lock:
            messages = self._buffers.pop(key, [])
            self._timers.pop(key, None)

        if not messages:
            return

        logger.info(
            "[accumulator] Flushing %d messages for %s",
            len(messages), key,
        )

        try:
            self._callback(key, messages)
        except Exception as e:
            logger.error("[accumulator] Flush callback failed for %s: %s", key, e)

    def pending_count(self) -> int:
        """Number of keys with pending messages."""
        with self._lock:
            return len(self._buffers)

    def cancel_all(self) -> None:
        """Cancel all pending timers. Used during shutdown."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._buffers.clear()
