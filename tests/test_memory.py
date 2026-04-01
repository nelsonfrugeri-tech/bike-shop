"""Tests for two-tier memory: ShortTermMemory, MemoryAgent, extraction."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ShortTermMemory
# ---------------------------------------------------------------------------


class TestShortTermMemory:
    """Tests for Redis-backed short-term memory."""

    def _make_stm(self):
        from bike_shop.short_term import ShortTermMemory
        return ShortTermMemory()

    def _mock_redis(self):
        """Create a mock Redis client with pipeline support."""
        mock_r = MagicMock()
        mock_pipe = MagicMock()
        mock_r.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [1, True, True]
        return mock_r

    @patch("bike_shop.short_term.get_redis")
    def test_push_writes_to_thread_key(self, mock_get_redis: MagicMock) -> None:
        mock_r = self._mock_redis()
        mock_get_redis.return_value = mock_r

        stm = self._make_stm()
        entry = {"role": "user", "author": "Alice", "content": "hello", "ts": "123"}

        result = stm.push("mr-robot", "bike-shop", "C123", "T456", entry)

        assert result is True
        mock_r.pipeline.assert_called_once()
        pipe = mock_r.pipeline.return_value
        pipe.lpush.assert_called_once()
        key = pipe.lpush.call_args[0][0]
        assert key == "bike-shop:mr-robot:bike-shop:C123:T456"

    @patch("bike_shop.short_term.get_redis")
    def test_push_returns_false_when_redis_unavailable(self, mock_get_redis: MagicMock) -> None:
        mock_get_redis.return_value = None
        stm = self._make_stm()
        result = stm.push("mr-robot", "bike-shop", "C123", "T456", {"role": "user"})
        assert result is False

    @patch("bike_shop.short_term.get_redis")
    def test_push_recent_writes_to_recent_key(self, mock_get_redis: MagicMock) -> None:
        mock_r = self._mock_redis()
        mock_get_redis.return_value = mock_r

        stm = self._make_stm()
        entry = {"role": "agent", "author": "Mr. Robot", "content": "done"}

        result = stm.push_recent("mr-robot", "bike-shop", entry)

        assert result is True
        pipe = mock_r.pipeline.return_value
        key = pipe.lpush.call_args[0][0]
        assert key == "bike-shop:mr-robot:bike-shop:recent"

    @patch("bike_shop.short_term.get_redis")
    def test_get_thread_returns_parsed_entries(self, mock_get_redis: MagicMock) -> None:
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        entries = [
            json.dumps({"role": "user", "content": "msg2"}),
            json.dumps({"role": "agent", "content": "msg1"}),
        ]
        mock_r.lrange.return_value = entries

        stm = self._make_stm()
        result = stm.get_thread("mr-robot", "bike-shop", "C123", "T456")

        assert len(result) == 2
        assert result[0]["content"] == "msg2"
        assert result[1]["role"] == "agent"

    @patch("bike_shop.short_term.get_redis")
    def test_get_thread_returns_empty_when_redis_down(self, mock_get_redis: MagicMock) -> None:
        mock_get_redis.return_value = None
        stm = self._make_stm()
        assert stm.get_thread("mr-robot", "bike-shop", "C123", "T456") == []

    @patch("bike_shop.short_term.get_redis")
    def test_get_recent_returns_parsed_entries(self, mock_get_redis: MagicMock) -> None:
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        mock_r.lrange.return_value = [json.dumps({"role": "user", "content": "hi"})]

        stm = self._make_stm()
        result = stm.get_recent("elliot", "bike-shop")

        assert len(result) == 1
        assert result[0]["content"] == "hi"

    @patch("bike_shop.short_term.get_redis")
    def test_mark_summarized_and_check(self, mock_get_redis: MagicMock) -> None:
        mock_r = MagicMock()
        mock_get_redis.return_value = mock_r
        mock_r.hget.return_value = "1"

        stm = self._make_stm()
        stm.mark_summarized("bike-shop:mr-robot:bike-shop:C1:T1")

        mock_r.hset.assert_called_once_with(
            "meta:bike-shop:mr-robot:bike-shop:C1:T1", "summarized", "1",
        )

        assert stm.is_summarized("bike-shop:mr-robot:bike-shop:C1:T1") is True


# ---------------------------------------------------------------------------
# MemoryAgent — scope mapping
# ---------------------------------------------------------------------------


class TestMemoryAgentScopes:
    """Tests that MemoryAgent maps scopes to correct Mem0 user_ids."""

    @patch("bike_shop.memory_agent._get_mem0")
    def test_scope_user_ids(self, mock_mem0: MagicMock) -> None:
        mock_mem0.return_value = MagicMock()

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        assert ma._uid_team == "team"
        assert ma._uid_project == "team:bike-shop"
        assert ma._uid_agent == "mr-robot:bike-shop"

    @patch("bike_shop.memory_agent._get_mem0")
    def test_scope_to_user_id_mapping(self, mock_mem0: MagicMock) -> None:
        mock_mem0.return_value = MagicMock()

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="elliot", project_id="bike-shop")

        assert ma._scope_to_user_id("team") == "team"
        assert ma._scope_to_user_id("project") == "team:bike-shop"
        assert ma._scope_to_user_id("agent") == "elliot:bike-shop"


# ---------------------------------------------------------------------------
# MemoryAgent — recall assembles 3 scopes
# ---------------------------------------------------------------------------


class TestMemoryAgentRecall:
    """Tests that recall queries all scopes and assembles output."""

    @patch("bike_shop.memory_agent._get_mem0")
    @patch("bike_shop.short_term.get_redis")
    def test_recall_assembles_all_scopes(
        self, mock_redis: MagicMock, mock_get_mem0: MagicMock,
    ) -> None:
        # Mock Redis
        mock_r = MagicMock()
        mock_redis.return_value = mock_r
        mock_r.lrange.return_value = [
            json.dumps({"role": "user", "author": "Alice", "content": "test message"}),
        ]

        # Mock Mem0
        mock_mem0 = MagicMock()
        mock_get_mem0.return_value = mock_mem0
        mock_mem0.search.return_value = {
            "results": [{"memory": "some remembered fact"}],
        }

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("test query", channel="C123", thread_ts="T456")

        assert "PROJECT MEMORY" in result
        assert "SHORT-TERM" in result
        assert "LONG-TERM" in result

        # Mem0 should be called 3 times (agent, project, team)
        assert mock_mem0.search.call_count == 3

    @patch("bike_shop.memory_agent._get_mem0")
    @patch("bike_shop.short_term.get_redis")
    def test_recall_returns_empty_when_nothing_found(
        self, mock_redis: MagicMock, mock_get_mem0: MagicMock,
    ) -> None:
        mock_redis.return_value = MagicMock()
        mock_redis.return_value.lrange.return_value = []

        mock_mem0 = MagicMock()
        mock_get_mem0.return_value = mock_mem0
        mock_mem0.search.return_value = {"results": []}

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("anything")
        assert result == ""

    @patch("bike_shop.memory_agent._get_mem0")
    @patch("bike_shop.short_term.get_redis")
    def test_recall_graceful_when_mem0_down(
        self, mock_redis: MagicMock, mock_get_mem0: MagicMock,
    ) -> None:
        mock_redis.return_value = None
        mock_get_mem0.return_value = None

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("anything")
        assert result == ""


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class TestExtraction:
    """Tests for selective memory extraction."""

    @patch("bike_shop.extraction.subprocess.run")
    def test_extraction_returns_empty_for_trivial(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="[]")

        from bike_shop.extraction import extract_memories
        result = extract_memories("Mr. Robot", "hi", "hello!", "bike-shop")

        assert result == []

    @patch("bike_shop.extraction.subprocess.run")
    def test_extraction_returns_structured_for_decisions(self, mock_run: MagicMock) -> None:
        response_json = json.dumps([
            {"type": "decision", "scope": "project", "content": "We chose Redis for caching"},
            {"type": "preference", "scope": "team", "content": "Team prefers TDD approach"},
        ])
        mock_run.return_value = MagicMock(stdout=response_json)

        from bike_shop.extraction import extract_memories
        result = extract_memories("Mr. Robot", "What should we use?", "Let's use Redis...", "bike-shop")

        assert len(result) == 2
        assert result[0]["type"] == "decision"
        assert result[0]["scope"] == "project"
        assert result[1]["scope"] == "team"

    @patch("bike_shop.extraction.subprocess.run")
    def test_extraction_filters_invalid_entries(self, mock_run: MagicMock) -> None:
        response_json = json.dumps([
            {"type": "decision", "scope": "project", "content": "Valid memory"},
            {"type": "invalid_type", "scope": "project", "content": "Bad type"},
            {"type": "decision", "scope": "invalid_scope", "content": "Bad scope"},
            {"type": "decision", "scope": "project", "content": "ab"},  # too short
        ])
        mock_run.return_value = MagicMock(stdout=response_json)

        from bike_shop.extraction import extract_memories
        result = extract_memories("Mr. Robot", "msg", "response", "bike-shop")

        assert len(result) == 1
        assert result[0]["content"] == "Valid memory"

    @patch("bike_shop.extraction.subprocess.run")
    def test_extraction_handles_timeout(self, mock_run: MagicMock) -> None:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 15)

        from bike_shop.extraction import extract_memories
        result = extract_memories("Mr. Robot", "msg", "response", "bike-shop")

        assert result == []

    @patch("bike_shop.extraction.subprocess.run")
    def test_extraction_handles_markdown_code_blocks(self, mock_run: MagicMock) -> None:
        raw = '```json\n[{"type": "fact", "scope": "agent", "content": "Uses Python 3.12"}]\n```'
        mock_run.return_value = MagicMock(stdout=raw)

        from bike_shop.extraction import extract_memories
        result = extract_memories("Elliot", "info", "Python 3.12 is used", "bike-shop")

        assert len(result) == 1
        assert result[0]["type"] == "fact"
        assert result[0]["scope"] == "agent"
