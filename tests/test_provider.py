"""Tests for ClaudeProvider — batch and streaming modes."""

from __future__ import annotations

import json
import os
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

    def test_user_event_tool_results_parsed(self) -> None:
        """Tool results in user events (actual Claude CLI format) are parsed."""
        events = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                        {"type": "text", "text": "Done"},
                    ],
                    "usage": {"input_tokens": 50, "output_tokens": 30},
                },
            }),
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": "file1.txt\nfile2.txt",
                            "is_error": False,
                        }
                    ],
                },
            }),
        ]
        stdout = "\n".join(events)

        response, _, usage = _parse_response(stdout)

        assert response == "Done"
        assert len(usage["tools"]) == 1
        assert len(usage["tool_results"]) == 1
        assert usage["tool_results"][0]["tool_use_id"] == "tu1"
        assert "file1.txt" in usage["tool_results"][0]["content"]

    def test_user_event_tool_result_with_list_content(self) -> None:
        """Tool results with list content are JSON-serialized."""
        events = [
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu2",
                            "content": [{"type": "text", "text": "output"}],
                            "is_error": False,
                        }
                    ],
                },
            }),
        ]
        stdout = "\n".join(events)

        _, _, usage = _parse_response(stdout)

        assert len(usage["tool_results"]) == 1
        assert usage["tool_results"][0]["tool_use_id"] == "tu2"
        assert "output" in usage["tool_results"][0]["content"]

    def test_user_event_tool_result_error(self) -> None:
        """Tool results with is_error=True are captured."""
        events = [
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu3",
                            "content": "command not found",
                            "is_error": True,
                        }
                    ],
                },
            }),
        ]
        stdout = "\n".join(events)

        _, _, usage = _parse_response(stdout)

        assert len(usage["tool_results"]) == 1
        assert usage["tool_results"][0]["is_error"] is True

    def test_both_user_and_legacy_tool_results(self) -> None:
        """Both user-event and legacy result-event formats are parsed."""
        events = [
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu1", "content": "from user", "is_error": False},
                    ],
                },
            }),
            json.dumps({
                "type": "result",
                "subtype": "tool_result",
                "tool_use_id": "tu2",
                "content": "from legacy",
                "is_error": False,
            }),
        ]
        stdout = "\n".join(events)

        _, _, usage = _parse_response(stdout)

        assert len(usage["tool_results"]) == 2
        ids = {r["tool_use_id"] for r in usage["tool_results"]}
        assert ids == {"tu1", "tu2"}

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

    def test_creates_tool_spans_from_user_events(self) -> None:
        """Tool results in user events populate tool span output."""
        tracer = MagicMock()
        tracer.start_span.return_value = "tool-span-id"

        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"cmd": "ls"}},
                        {"type": "text", "text": "Done"},
                    ],
                    "usage": {},
                },
            }),
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": "file.txt",
                            "is_error": False,
                        }
                    ],
                },
            }),
        ]
        proc = self._make_proc(lines)

        response, _, usage = _parse_stream(proc, tracer, "t1", "g1")

        assert response == "Done"
        assert len(usage["tool_results"]) == 1
        # end_span called with output for the tool
        tracer.end_span.assert_called()
        end_call_kwargs = tracer.end_span.call_args
        assert end_call_kwargs is not None

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

    @patch.dict(os.environ, {"LANGFUSE_STREAM_ENABLED": "false"})
    @patch("bike_shop.providers.claude._run_with_idle_watchdog")
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
        response, _ = provider.call(config, "hello", workspace="/tmp/test-worktree")

        mock_run.assert_called_once()
        assert response == "ok"


# ---------------------------------------------------------------------------
# Idle-based watchdog tests
# ---------------------------------------------------------------------------

import signal
import sys
import textwrap
import time as time_mod

from bike_shop.providers.claude import (
    _IdleTimeoutError,
    _run_with_idle_watchdog,
)

# Short timeouts for test speed
IDLE = 2
MAX = 10
GRACE = 1


def _python(script: str) -> list[str]:
    """Build a command that runs an inline Python script."""
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


class TestHealthyProcess:
    """Process that emits output regularly should NOT be killed."""

    def test_completes_normally(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            for i in range(5):
                print(f"line {i}", flush=True)
                time.sleep(0.3)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        assert "line 0" in result.stdout
        assert "line 4" in result.stdout

    def test_fast_process_completes(self, tmp_path: object) -> None:
        cmd = _python('print("done", flush=True)')
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        assert "done" in result.stdout


class TestStuckProcess:
    """Process that stops producing output should be killed after idle timeout."""

    def test_killed_after_idle(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            print("start", flush=True)
            time.sleep(60)  # hang forever
        """)
        start = time_mod.time()
        with pytest.raises(_IdleTimeoutError, match="idle"):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=MAX,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        elapsed = time_mod.time() - start
        assert elapsed < IDLE + GRACE + 3


class TestAbsoluteTimeout:
    """Process emitting output forever should be killed at max absolute timeout."""

    def test_killed_at_absolute_timeout(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            while True:
                print("alive", flush=True)
                time.sleep(0.5)
        """)
        short_max = 3
        start = time_mod.time()
        with pytest.raises(_IdleTimeoutError, match="absolute safety timeout"):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=short_max,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        elapsed = time_mod.time() - start
        assert elapsed < short_max + GRACE + 3


class TestOutputCollection:
    """Verify stdout is fully collected from incremental reads."""

    def test_all_lines_collected(self, tmp_path: object) -> None:
        n = 50
        cmd = _python(f"""\
            for i in range({n}):
                print(f"line {{i}}", flush=True)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        lines = [l for l in result.stdout.strip().splitlines() if l.startswith("line")]
        assert len(lines) == n

    def test_stderr_collected(self, tmp_path: object) -> None:
        cmd = _python("""\
            import sys
            print("out", flush=True)
            print("err", file=sys.stderr, flush=True)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert "out" in result.stdout
        assert "err" in result.stderr


class TestGracefulKill:
    """Verify SIGTERM is sent before SIGKILL."""

    def test_sigterm_received(self, tmp_path: object) -> None:
        """Process that traps SIGTERM writes a marker file; verify the file exists."""
        marker = tmp_path / "sigterm_received"
        cmd = _python(f"""\
            import signal, sys, time, pathlib

            def handler(sig, frame):
                pathlib.Path("{marker}").write_text("yes")
                sys.exit(0)

            signal.signal(signal.SIGTERM, handler)
            print("start", flush=True)
            time.sleep(60)
        """)
        with pytest.raises(_IdleTimeoutError):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=MAX,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        assert marker.exists(), "SIGTERM handler was never called"
        assert marker.read_text() == "yes"


class TestProcessCrash:
    """Process that crashes immediately should return without timeout."""

    def test_crash_returns_nonzero(self, tmp_path: object) -> None:
        cmd = _python("raise SystemExit(42)")
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 42
