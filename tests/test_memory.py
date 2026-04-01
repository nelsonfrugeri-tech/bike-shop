"""Tests for memory: MemoryAgent scopes, recall, extraction."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# MemoryAgent — scope mapping
# ---------------------------------------------------------------------------


class TestMemoryAgentScopes:
    """Tests that MemoryAgent maps scopes to correct Mem0 user_ids."""

    @patch("bike_shop.memory_agent.get_mem0")
    def test_scope_user_ids(self, mock_mem0: MagicMock) -> None:
        mock_mem0.return_value = MagicMock()

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        assert ma._uid_team == "team"
        assert ma._uid_project == "team:bike-shop"
        assert ma._uid_agent == "mr-robot:bike-shop"

    @patch("bike_shop.memory_agent.get_mem0")
    def test_scope_to_user_id_mapping(self, mock_mem0: MagicMock) -> None:
        mock_mem0.return_value = MagicMock()

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="elliot", project_id="bike-shop")

        assert ma._scope_to_user_id("team") == "team"
        assert ma._scope_to_user_id("project") == "team:bike-shop"
        assert ma._scope_to_user_id("agent") == "elliot:bike-shop"


# ---------------------------------------------------------------------------
# MemoryAgent — recall (Mem0 only, no Redis)
# ---------------------------------------------------------------------------


class TestMemoryAgentRecall:
    """Tests that recall queries Mem0 scopes only on new threads."""

    @patch("bike_shop.memory_agent.get_mem0")
    def test_recall_skips_when_session_exists(self, mock_get_mem0: MagicMock) -> None:
        mock_mem0 = MagicMock()
        mock_get_mem0.return_value = mock_mem0

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("test query", has_session=True)

        assert result == ""
        mock_mem0.search.assert_not_called()

    @patch("bike_shop.memory_agent.get_mem0")
    def test_recall_queries_mem0_on_new_thread(self, mock_get_mem0: MagicMock) -> None:
        mock_mem0 = MagicMock()
        mock_get_mem0.return_value = mock_mem0
        mock_mem0.search.return_value = {
            "results": [{"memory": "some remembered fact"}],
        }

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("test query", has_session=False)

        assert "PROJECT MEMORY" in result
        assert "LONG-TERM" in result
        assert "some remembered fact" in result
        # Mem0 should be called 3 times (agent, project, team)
        assert mock_mem0.search.call_count == 3

    @patch("bike_shop.memory_agent.get_mem0")
    def test_recall_returns_empty_when_nothing_found(self, mock_get_mem0: MagicMock) -> None:
        mock_mem0 = MagicMock()
        mock_get_mem0.return_value = mock_mem0
        mock_mem0.search.return_value = {"results": []}

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("anything", has_session=False)
        assert result == ""

    @patch("bike_shop.memory_agent.get_mem0")
    def test_recall_graceful_when_mem0_down(self, mock_get_mem0: MagicMock) -> None:
        mock_get_mem0.return_value = None

        from bike_shop.memory_agent import MemoryAgent
        ma = MemoryAgent(agent_key="mr-robot", project_id="bike-shop")

        result = ma.recall("anything", has_session=False)
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
            {"type": "decision", "scope": "project", "content": "We chose Qdrant for vector storage"},
            {"type": "preference", "scope": "team", "content": "Team prefers TDD approach"},
        ])
        mock_run.return_value = MagicMock(stdout=response_json)

        from bike_shop.extraction import extract_memories
        result = extract_memories("Mr. Robot", "What should we use?", "Let's use Qdrant...", "bike-shop")

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
