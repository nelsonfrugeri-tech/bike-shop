"""Tests for ClaudeProvider — batch and streaming modes."""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from bike_shop.providers.claude import ClaudeProvider, _parse_response, _parse_stream


# ---------------------------------------------------------------------------
# _parse_response (batch mode parser)
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Tests for batch-mode response parser."""

    def test_empty_output(self) -> None:
        response, session_id, usage = _parse_response("")
        assert response == "..."
        assert session_id is None

    def test_text_response(self) -> None:
        events = [
            json.dumps({"type": "system", "session_id": "sess-123"}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello world"}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            }),
        ]
        stdout = "\n".join(events)

        response, session_id, usage = _parse_response(stdout)

        assert response == "Hello world"
        assert session_id == "sess-123"
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50

    def test_tool_use_and_results(self) -> None:
        events = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"cmd": "ls"}},
                        {"type": "text", "text": "Done"},
                    ],
                    "usage": {"input_tokens": 50, "output_tokens": 30},
                },
            }),
            json.dumps({
                "type": "result",
                "subtype": "tool_result",
                "tool_use_id": "tu1",
                "content": "file.txt",
                "is_error": False,
            }),
        ]
        stdout = "\n".join(events)

        response, _, usage = _parse_response(stdout)

        assert response == "Done"
        assert len(usage["tools"]) == 1
        assert usage["tools"][0]["name"] == "Bash"
        assert len(usage["tool_results"]) == 1
        assert usage["tool_results"][0]["tool_use_id"] == "tu1"

    def test_thinking_blocks(self) -> None:
        events = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "Let me think..."},
                        {"type": "text", "text": "Answer"},
                    ],
                    "usage": {},
                },
            }),
        ]
        stdout = "\n".join(events)

        response, _, usage = _parse_response(stdout)

        assert response == "Answer"
        assert len(usage["thinking"]) == 1
        assert "think" in usage["thinking"][0]

    def test_error_events(self) -> None:
        events = [
            json.dumps({
                "type": "error",
                "error": {"message": "rate limited", "type": "rate_limit"},
            }),
        ]
        stdout = "\n".join(events)

        _, _, usage = _parse_response(stdout)

        assert len(usage["errors"]) == 1
        assert usage["errors"][0]["type"] == "rate_limit"

    def test_malformed_json_lines_ignored(self) -> None:
        events = [
            "not json at all",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {}}}),
            "{broken json",
        ]
        stdout = "\n".join(events)

        response, _, _ = _parse_response(stdout)
        assert response == "ok"


# ---------------------------------------------------------------------------
# _parse_stream (streaming mode parser)
# ---------------------------------------------------------------------------


class TestParseStream:
    """Tests for streaming-mode parser with real-time span creation."""

    def _make_proc(self, lines: list[str], returncode: int = 0) -> MagicMock:
        """Create a mock Popen with stdout lines."""
        proc = MagicMock()
        proc.stdout = StringIO("\n".join(lines) + "\n")
        proc.stderr = StringIO("")
        proc.wait.return_value = returncode
        proc.returncode = returncode
        return proc

    def test_parses_text_response(self) -> None:
        tracer = MagicMock()
        tracer.start_span.return_value = "span-id"
        tracer.start_generation.return_value = "gen-id"

        lines = [
            json.dumps({"type": "system", "session_id": "sess-1"}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }),
        ]
        proc = self._make_proc(lines)

        response, session_id, usage = _parse_stream(proc, tracer, "t1", "g1")

        assert response == "Hello"
        assert session_id == "sess-1"
        assert usage["input_tokens"] == 10

    def test_creates_tool_spans(self) -> None:
        tracer = MagicMock()
        tracer.start_span.return_value = "tool-span-id"

        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Write", "input": {"path": "/tmp/x"}},
                        {"type": "text", "text": "Done"},
                    ],
                    "usage": {},
                },
            }),
            json.dumps({
                "type": "result",
                "subtype": "tool_result",
                "tool_use_id": "tu1",
                "content": "ok",
                "is_error": False,
            }),
        ]
        proc = self._make_proc(lines)

        response, _, usage = _parse_stream(proc, tracer, "t1", "g1")

        assert response == "Done"
        # Tool span created and ended
        tracer.start_span.assert_called()
        tracer.end_span.assert_called()

    def test_creates_thinking_spans(self) -> None:
        tracer = MagicMock()
        tracer.start_span.return_value = "think-span-id"

        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "reasoning..."},
                        {"type": "text", "text": "Answer"},
                    ],
                    "usage": {},
                },
            }),
        ]
        proc = self._make_proc(lines)

        response, _, _ = _parse_stream(proc, tracer, "t1", "g1")

        assert response == "Answer"
        # Thinking span created with name "thinking.1"
        call_args = tracer.start_span.call_args_list
        assert any("thinking" in str(c) for c in call_args)

    def test_empty_output_returns_ellipsis(self) -> None:
        tracer = MagicMock()
        proc = self._make_proc([])

        response, _, _ = _parse_stream(proc, tracer, "t1", "g1")
        assert response == "..."


# ---------------------------------------------------------------------------
# ClaudeProvider — mode selection
# ---------------------------------------------------------------------------


class TestClaudeProviderModeSelection:
    """Tests that the provider selects batch vs streaming correctly."""

    @patch("bike_shop.providers.claude.STREAM_ENABLED", False)
    @patch("bike_shop.providers.claude.subprocess.run")
    @patch("bike_shop.observability._get_config", return_value=None)
    def test_batch_mode_when_streaming_disabled(
        self, _cfg: MagicMock, mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {}}}),
            stderr="",
        )

        from bike_shop.config import AgentConfig
        config = AgentConfig(
            name="Test", role="test", bot_token="x", app_token="x",
            system_prompt="test", agent_key="test",
        )

        provider = ClaudeProvider()
        response, _ = provider.call(config, "hello")

        mock_run.assert_called_once()
        assert response == "ok"
