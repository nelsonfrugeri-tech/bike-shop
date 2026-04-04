"""Tests for handler tracer propagation (#33) and worktree diff span (#34)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bike_shop.slack.handler import SlackAgentHandler


@pytest.fixture
def mock_config() -> MagicMock:
    """Minimal AgentConfig mock."""
    config = MagicMock()
    config.name = "test-agent"
    config.agent_key = "test-agent"
    config.model_id = "claude-sonnet-4-20250514"
    config.opus_model_id = "claude-opus-4-20250514"
    config.bot_user_id = "U_BOT"
    config.bot_id = "B_BOT"
    config.bot_token = "xoxb-fake"
    config.app_token = "xapp-fake"
    config.system_prompt = "You are a test agent."
    return config


@pytest.fixture
def mock_provider() -> MagicMock:
    """LLMProvider mock that returns a simple response."""
    provider = MagicMock()
    provider.call.return_value = ("test reply", "sess-123")
    return provider


@pytest.fixture
def handler(mock_config: MagicMock, mock_provider: MagicMock) -> SlackAgentHandler:
    """Handler with all external deps mocked."""
    with (
        patch("bike_shop.slack.handler.GitHubAuth"),
        patch("bike_shop.slack.handler.SemanticRouter"),
        patch("bike_shop.slack.handler.MemoryAgent"),
        patch("bike_shop.slack.handler.SessionStore"),
        patch("bike_shop.slack.handler.ModelSwitcher"),
        patch("bike_shop.slack.handler._build_mcp_config", return_value="/tmp/mcp.json"),
        patch("bike_shop.slack.handler._read_project_context", return_value=""),
    ):
        h = SlackAgentHandler(mock_config, mock_provider)
    return h


class TestTracerPropagationBatch:
    """Issue #33: _call_llm_batch must resolve per-project tracer."""

    def test_call_llm_batch_passes_tracer_to_provider(
        self, handler: SlackAgentHandler, mock_provider: MagicMock
    ) -> None:
        """Verify _call_llm_batch resolves tracer and passes it to provider.call()."""
        with (
            patch.object(handler, "_get_workspace", return_value="/tmp/worktree"),
            patch.object(handler, "_get_tracer") as mock_get_tracer,
            patch.object(handler, "_get_memory_agent") as mock_get_mem,
            patch("bike_shop.slack.handler._build_mcp_config", return_value="/tmp/mcp.json"),
        ):
            mock_tracer = MagicMock()
            mock_get_tracer.return_value = mock_tracer
            mock_mem = MagicMock()
            mock_mem.recall.return_value = ""
            mock_get_mem.return_value = mock_mem

            result = handler._call_llm_batch(
                "context", [{"text": "hello"}], "thread-1",
            )

            # Verify provider.call was called with tracer kwarg
            _, kwargs = mock_provider.call.call_args
            assert kwargs["tracer"] is mock_tracer


class TestWorktreeDiffSpan:
    """Issue #34: worktree.diff span after LLM call."""

    @staticmethod
    def _setup_handler_for_process(handler: SlackAgentHandler, mock_tracer: MagicMock) -> None:
        """Configure handler mocks for _process_and_reply calls."""
        handler._router.route.return_value = {
            "agent": None,
            "model": None,
            "model_name": "sonnet",
            "reason": "test",
            "memory": [],
        }
        handler._switcher.is_manual_trigger.return_value = False
        handler._switcher.has_marker.return_value = False

    def test_diff_span_created_when_changes_exist(
        self, handler: SlackAgentHandler, mock_provider: MagicMock
    ) -> None:
        """_process_and_reply adds worktree.diff span when git diff has output."""
        mock_tracer = MagicMock()
        mock_tracer.start_trace.return_value = "trace-1"
        mock_tracer.start_span.return_value = "span-1"
        self._setup_handler_for_process(handler, mock_tracer)

        fake_diff = " src/foo.py | 10 +++++++---\n 1 file changed"

        with (
            patch.object(handler, "_get_tracer", return_value=mock_tracer),
            patch.object(handler, "_get_memory_agent") as mock_get_mem,
            patch.object(handler, "_get_workspace", return_value="/tmp/wt"),
            patch.object(handler, "_call_llm", return_value="reply"),
            patch.object(handler, "_post_reply"),
            patch.object(handler, "_resolve_project", return_value=None),
            patch("bike_shop.slack.handler.subprocess.run") as mock_run,
        ):
            mock_mem = MagicMock()
            mock_get_mem.return_value = mock_mem

            mock_run.return_value = MagicMock(stdout=fake_diff, returncode=0)

            handler._process_and_reply(
                MagicMock(), MagicMock(), "ctx", "question", "thread-1",
                channel="C1", user_name="user",
            )

            # Find the worktree.diff span call
            diff_calls = [
                c for c in mock_tracer.start_span.call_args_list
                if c[0][0] == "worktree.diff"
            ]
            assert len(diff_calls) == 1
            assert diff_calls[0][1]["metadata"]["type"] == "worktree_diff"

    def test_no_diff_span_when_no_changes(
        self, handler: SlackAgentHandler, mock_provider: MagicMock
    ) -> None:
        """No worktree.diff span when git diff output is empty."""
        mock_tracer = MagicMock()
        mock_tracer.start_trace.return_value = "trace-1"
        mock_tracer.start_span.return_value = "span-1"
        self._setup_handler_for_process(handler, mock_tracer)

        with (
            patch.object(handler, "_get_tracer", return_value=mock_tracer),
            patch.object(handler, "_get_memory_agent") as mock_get_mem,
            patch.object(handler, "_get_workspace", return_value="/tmp/wt"),
            patch.object(handler, "_call_llm", return_value="reply"),
            patch.object(handler, "_post_reply"),
            patch.object(handler, "_resolve_project", return_value=None),
            patch("bike_shop.slack.handler.subprocess.run") as mock_run,
        ):
            mock_mem = MagicMock()
            mock_get_mem.return_value = mock_mem

            mock_run.return_value = MagicMock(stdout="", returncode=0)

            handler._process_and_reply(
                MagicMock(), MagicMock(), "ctx", "question", "thread-1",
                channel="C1", user_name="user",
            )

            diff_calls = [
                c for c in mock_tracer.start_span.call_args_list
                if c[0][0] == "worktree.diff"
            ]
            assert len(diff_calls) == 0

    def test_diff_span_failure_does_not_break_flow(
        self, handler: SlackAgentHandler, mock_provider: MagicMock
    ) -> None:
        """If git diff fails, the message flow continues normally."""
        mock_tracer = MagicMock()
        mock_tracer.start_trace.return_value = "trace-1"
        mock_tracer.start_span.return_value = "span-1"
        self._setup_handler_for_process(handler, mock_tracer)

        with (
            patch.object(handler, "_get_tracer", return_value=mock_tracer),
            patch.object(handler, "_get_memory_agent") as mock_get_mem,
            patch.object(handler, "_get_workspace", return_value="/tmp/wt"),
            patch.object(handler, "_call_llm", return_value="reply"),
            patch.object(handler, "_post_reply") as mock_post,
            patch.object(handler, "_resolve_project", return_value=None),
            patch("bike_shop.slack.handler.subprocess.run", side_effect=OSError("git not found")),
        ):
            mock_mem = MagicMock()
            mock_get_mem.return_value = mock_mem

            handler._process_and_reply(
                MagicMock(), MagicMock(), "ctx", "question", "thread-1",
                channel="C1", user_name="user",
            )

            # Reply was still posted despite diff failure
            mock_post.assert_called_once()
