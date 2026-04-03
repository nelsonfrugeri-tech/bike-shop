"""Tests for message accumulator."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


class TestMessageAccumulator:
    """Tests for MessageAccumulator batching behavior."""

    def test_single_message_flushes_after_window(self) -> None:
        from bike_shop.accumulator import MessageAccumulator

        callback = MagicMock()

        with patch("bike_shop.accumulator.BUFFER_WINDOW", 0.1):
            acc = MessageAccumulator(flush_callback=callback)
            acc.add("elliot", "T123", {"text": "hello", "user_name": "alice"})

            time.sleep(0.3)

        callback.assert_called_once()
        key, messages = callback.call_args[0]
        assert key == "elliot:T123"
        assert len(messages) == 1
        assert messages[0]["text"] == "hello"

    def test_multiple_messages_batch_together(self) -> None:
        from bike_shop.accumulator import MessageAccumulator

        callback = MagicMock()

        with patch("bike_shop.accumulator.BUFFER_WINDOW", 0.2):
            acc = MessageAccumulator(flush_callback=callback)
            acc.add("elliot", "T123", {"text": "task 1"})
            time.sleep(0.05)
            acc.add("elliot", "T123", {"text": "task 2"})
            time.sleep(0.05)
            acc.add("elliot", "T123", {"text": "task 3"})

            time.sleep(0.4)

        callback.assert_called_once()
        key, messages = callback.call_args[0]
        assert len(messages) == 3

    def test_different_threads_flush_separately(self) -> None:
        from bike_shop.accumulator import MessageAccumulator

        callback = MagicMock()

        with patch("bike_shop.accumulator.BUFFER_WINDOW", 0.1):
            acc = MessageAccumulator(flush_callback=callback)
            acc.add("elliot", "T100", {"text": "task A"})
            acc.add("elliot", "T200", {"text": "task B"})

            time.sleep(0.3)

        assert callback.call_count == 2

    def test_max_batch_size_flushes_immediately(self) -> None:
        import bike_shop.accumulator as acc_mod
        from bike_shop.accumulator import MessageAccumulator

        callback = MagicMock()

        old_max = acc_mod.MAX_BATCH_SIZE
        old_window = acc_mod.BUFFER_WINDOW
        try:
            acc_mod.MAX_BATCH_SIZE = 3
            acc_mod.BUFFER_WINDOW = 10.0
            acc = MessageAccumulator(flush_callback=callback)
            acc.add("elliot", "T123", {"text": "1"})
            acc.add("elliot", "T123", {"text": "2"})
            acc.add("elliot", "T123", {"text": "3"})

            time.sleep(0.1)
        finally:
            acc_mod.MAX_BATCH_SIZE = old_max
            acc_mod.BUFFER_WINDOW = old_window

        callback.assert_called_once()
        _, messages = callback.call_args[0]
        assert len(messages) == 3

    def test_pending_count(self) -> None:
        import bike_shop.accumulator as acc_mod
        from bike_shop.accumulator import MessageAccumulator

        old_window = acc_mod.BUFFER_WINDOW
        try:
            acc_mod.BUFFER_WINDOW = 10.0
            acc = MessageAccumulator(flush_callback=MagicMock())
            assert acc.pending_count() == 0

            acc.add("elliot", "T1", {"text": "a"})
            assert acc.pending_count() == 1

            acc.add("elliot", "T2", {"text": "b"})
            assert acc.pending_count() == 2

            acc.cancel_all()
            assert acc.pending_count() == 0
        finally:
            acc_mod.BUFFER_WINDOW = old_window

    def test_cancel_all_stops_timers(self) -> None:
        from bike_shop.accumulator import MessageAccumulator

        callback = MagicMock()

        with patch("bike_shop.accumulator.BUFFER_WINDOW", 0.1):
            acc = MessageAccumulator(flush_callback=callback)
            acc.add("elliot", "T123", {"text": "hello"})
            acc.cancel_all()

            time.sleep(0.3)

        callback.assert_not_called()
