"""Tests for worktree management."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class TestWorktreeHelpers:
    """Tests for worktree helper functions."""

    def test_workspace_root_raises_without_env(self) -> None:
        from bike_shop.worktree import _workspace_root
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError, match="AGENT_WORKSPACE not set"):
                _workspace_root()

    def test_workspace_root_returns_env_value(self) -> None:
        from bike_shop.worktree import _workspace_root
        with patch.dict(os.environ, {"AGENT_WORKSPACE": "/tmp/test-repo"}):
            assert _workspace_root() == "/tmp/test-repo"

    def test_worktrees_base_path(self) -> None:
        from bike_shop.worktree import _worktrees_base
        with patch.dict(os.environ, {"AGENT_WORKSPACE": "/tmp/test-repo"}):
            assert _worktrees_base() == "/tmp/test-repo/.worktrees"


class TestEnsureWorktree:
    """Tests for ensure_worktree naming."""

    @patch("bike_shop.worktree.create_worktree")
    def test_default_name(self, mock_create: MagicMock) -> None:
        mock_create.return_value = "/tmp/wt/elliot-default"

        from bike_shop.worktree import ensure_worktree
        with patch.dict(os.environ, {"AGENT_WORKSPACE": "/tmp/test-repo"}):
            path = ensure_worktree("elliot")

        mock_create.assert_called_once_with("elliot-default", base_branch="main")
        assert path == "/tmp/wt/elliot-default"

    @patch("bike_shop.worktree.create_worktree")
    def test_with_task_id(self, mock_create: MagicMock) -> None:
        mock_create.return_value = "/tmp/wt/elliot-funds-abc"

        from bike_shop.worktree import ensure_worktree
        with patch.dict(os.environ, {"AGENT_WORKSPACE": "/tmp/test-repo"}):
            path = ensure_worktree("elliot", task_id="funds-abc")

        mock_create.assert_called_once_with("elliot-funds-abc", base_branch="main")


class TestListWorktrees:
    """Tests for list_worktrees."""

    @patch("bike_shop.worktree._worktrees_base")
    def test_returns_empty_when_no_dir(self, mock_base: MagicMock) -> None:
        mock_base.return_value = "/tmp/nonexistent"

        from bike_shop.worktree import list_worktrees
        assert list_worktrees() == []


class TestGetWorktreePath:
    """Tests for get_worktree_path."""

    def test_returns_none_when_not_exists(self) -> None:
        from bike_shop.worktree import get_worktree_path
        with patch.dict(os.environ, {"AGENT_WORKSPACE": "/tmp/test-repo"}):
            assert get_worktree_path("nonexistent") is None
