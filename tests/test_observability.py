"""Tests for hierarchical observability tracer."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bike_shop.observability import Tracer, _BatchBuffer, _now_iso, _uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """Tests for utility functions."""

    def test_now_iso_format(self) -> None:
        ts = _now_iso()
        assert ts.endswith("Z")
        assert "T" in ts

    def test_uuid_is_unique(self) -> None:
        a = _uuid()
        b = _uuid()
        assert a != b
        assert len(a) == 36  # UUID4 format


# ---------------------------------------------------------------------------
# BatchBuffer
# ---------------------------------------------------------------------------


class TestBatchBuffer:
    """Tests for the micro-batch buffer."""

    @patch("bike_shop.observability._post")
    def test_flush_sends_events(self, mock_post: MagicMock) -> None:
        mock_post.return_value = True
        buf = _BatchBuffer(flush_interval_ms=60000)  # long interval so it doesn't auto-fire

        buf.add({"id": "1", "type": "trace-create", "timestamp": "now", "body": {"id": "t1"}})
        buf.add({"id": "2", "type": "span-create", "timestamp": "now", "body": {"id": "s1"}})
        buf.flush()

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/api/public/ingestion"
        batch = call_args[0][1]["batch"]
        assert len(batch) == 2

    @patch("bike_shop.observability._post")
    def test_flush_empty_buffer_no_call(self, mock_post: MagicMock) -> None:
        buf = _BatchBuffer(flush_interval_ms=60000)
        buf.flush()
        mock_post.assert_not_called()

    @patch("bike_shop.observability._post")
    def test_add_many(self, mock_post: MagicMock) -> None:
        mock_post.return_value = True
        buf = _BatchBuffer(flush_interval_ms=60000)

        events = [
            {"id": str(i), "type": "span-create", "timestamp": "now", "body": {"id": f"s{i}"}}
            for i in range(5)
        ]
        buf.add_many(events)
        buf.flush()

        batch = mock_post.call_args[0][1]["batch"]
        assert len(batch) == 5


# ---------------------------------------------------------------------------
# Tracer — disabled when no config
# ---------------------------------------------------------------------------


class TestTracerDisabled:
    """Tests that tracer is a no-op when Langfuse is not configured."""

    @patch("bike_shop.observability._get_config", return_value=None)
    def test_disabled_tracer_returns_uuids(self, _: MagicMock) -> None:
        tracer = Tracer("test-agent")
        assert not tracer.enabled

        trace_id = tracer.start_trace("test")
        assert len(trace_id) == 36

        span_id = tracer.start_span("span", trace_id=trace_id)
        assert len(span_id) == 36

        gen_id = tracer.start_generation("gen", trace_id=trace_id, model="test")
        assert len(gen_id) == 36

        # Should not raise
        tracer.end_span(span_id, trace_id=trace_id)
        tracer.end_generation(gen_id, trace_id=trace_id)
        tracer.update_trace(trace_id, output="done")
        tracer.flush()

    @patch("bike_shop.observability._get_config", return_value=None)
    def test_trace_call_noop_when_disabled(self, _: MagicMock) -> None:
        tracer = Tracer("test-agent")
        # Should not raise
        tracer.trace_call(
            user_message="hello",
            response="hi",
            model="test",
            duration_ms=100,
        )

    @patch("bike_shop.observability._get_config", return_value=None)
    def test_trace_error_noop_when_disabled(self, _: MagicMock) -> None:
        tracer = Tracer("test-agent")
        tracer.trace_error(error="something failed")


# ---------------------------------------------------------------------------
# Tracer — enabled
# ---------------------------------------------------------------------------


class TestTracerEnabled:
    """Tests for enabled tracer sending events to buffer."""

    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_start_trace_adds_event(self, _cfg: MagicMock, mock_buffer: MagicMock) -> None:
        tracer = Tracer("test-agent")
        trace_id = tracer.start_trace("my-trace", input="hello")

        assert len(trace_id) == 36
        mock_buffer.add.assert_called_once()
        event = mock_buffer.add.call_args[0][0]
        assert event["type"] == "trace-create"
        assert event["body"]["name"] == "my-trace"
        assert event["body"]["input"] == "hello"

    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_start_end_span(self, _cfg: MagicMock, mock_buffer: MagicMock) -> None:
        tracer = Tracer("test-agent")
        trace_id = "trace-123"

        span_id = tracer.start_span("my-span", trace_id=trace_id, parent_id="parent-1")
        tracer.end_span(span_id, trace_id=trace_id, output="done")

        assert mock_buffer.add.call_count == 2
        create_event = mock_buffer.add.call_args_list[0][0][0]
        update_event = mock_buffer.add.call_args_list[1][0][0]

        assert create_event["type"] == "span-create"
        assert create_event["body"]["parentObservationId"] == "parent-1"
        assert update_event["type"] == "span-update"
        assert update_event["body"]["output"] == "done"

    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_start_end_generation(self, _cfg: MagicMock, mock_buffer: MagicMock) -> None:
        tracer = Tracer("test-agent")
        trace_id = "trace-123"

        gen_id = tracer.start_generation(
            "llm-call", trace_id=trace_id, model="claude-sonnet",
            input="prompt",
        )
        tracer.end_generation(
            gen_id, trace_id=trace_id,
            output="response", usage={"input": 100, "output": 50},
        )

        assert mock_buffer.add.call_count == 2
        create_event = mock_buffer.add.call_args_list[0][0][0]
        update_event = mock_buffer.add.call_args_list[1][0][0]

        assert create_event["type"] == "generation-create"
        assert create_event["body"]["model"] == "claude-sonnet"
        assert update_event["type"] == "generation-update"
        assert update_event["body"]["usage"] == {"input": 100, "output": 50}


# ---------------------------------------------------------------------------
# Tracer — backwards-compatible trace_call
# ---------------------------------------------------------------------------


class TestTracerBackwardsCompat:
    """Tests that trace_call still works as before."""

    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_trace_call_creates_hierarchy(self, _cfg: MagicMock, mock_buffer: MagicMock) -> None:
        tracer = Tracer("mr-robot")

        tracer.trace_call(
            user_message="hello",
            response="hi there",
            model="claude-sonnet-4",
            duration_ms=1500,
            input_tokens=100,
            output_tokens=50,
            tools=[{"id": "t1", "name": "Bash", "input": "ls"}],
            tool_results=[{"tool_use_id": "t1", "content": "file.txt", "is_error": False}],
            thinking=["thinking about it"],
            errors=[],
            selected_agent="dev-py",
        )

        # Should have: trace-create, trace-update, gen-create, gen-update,
        # thinking span create+update, tool span create+update
        assert mock_buffer.add.call_count >= 6
        mock_buffer.flush.assert_called_once()

    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_trace_error_creates_trace(self, _cfg: MagicMock, mock_buffer: MagicMock) -> None:
        tracer = Tracer("mr-robot")
        tracer.trace_error(error="something broke", context="during testing")

        mock_buffer.add.assert_called_once()
        event = mock_buffer.add.call_args[0][0]
        assert event["type"] == "trace-create"
        assert "error" in event["body"]["tags"]
        mock_buffer.flush.assert_called_once()


# ---------------------------------------------------------------------------
# Tracer — detail levels
# ---------------------------------------------------------------------------


class TestTracerDetailLevels:
    """Tests that detail levels control span granularity."""

    @patch("bike_shop.observability.TRACE_DETAIL", "basic")
    @patch("bike_shop.observability._buffer")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_basic_detail_skips_thinking_and_tool_spans(
        self, _cfg: MagicMock, mock_buffer: MagicMock,
    ) -> None:
        tracer = Tracer("mr-robot")

        tracer.trace_call(
            user_message="hello",
            response="hi",
            model="claude-sonnet-4",
            duration_ms=100,
            thinking=["deep thought"],
            tools=[{"id": "t1", "name": "Bash", "input": "ls"}],
            tool_results=[{"tool_use_id": "t1", "content": "ok", "is_error": False}],
        )

        # In basic mode: trace-create, trace-update, gen-create, gen-update only
        # No thinking or tool spans
        event_types = [call[0][0]["type"] for call in mock_buffer.add.call_args_list]
        assert "span-create" not in event_types

    @patch("bike_shop.observability.TRACE_DETAIL", "off")
    @patch("bike_shop.observability._get_config", return_value=("http://localhost:3000", "Basic abc"))
    def test_off_detail_disables_tracing(self, _cfg: MagicMock) -> None:
        tracer = Tracer("mr-robot")
        assert not tracer.enabled
