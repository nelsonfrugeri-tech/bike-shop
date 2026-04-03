"""Tests for worktree management."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest


class TestWorkspaceRoot:
    """Tests for _workspace_root()."""

    def test_raises_when_not_set(self) -> None:
        from bike_shop.worktree import _workspace_root

        env = {k: v for k, v in os.environ.items() if k != "AGENT_WORKSPACE"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="AGENT_WORKSPACE not set"):
                _workspace_root()

    def test_raises_when_directory_does_not_exist(self, tmp_path) -> None:
        from bike_shop.worktree import _workspace_root

        nonexistent = str(tmp_path / "ghost")
        with patch.dict(os.environ, {"AGENT_WORKSPACE": nonexistent}):
            with pytest.raises(RuntimeError, match="does not exist"):
                _workspace_root()

    def test_returns_value_when_directory_exists(self, tmp_path) -> None:
        from bike_shop.worktree import _workspace_root

        with patch.dict(os.environ, {"AGENT_WORKSPACE": str(tmp_path)}):
            assert _workspace_root() == str(tmp_path)


class TestWorktreesBase:
    """Tests for _worktrees_base() — must use AGENT_WORKTREE_DIR."""

    def test_raises_when_not_set(self) -> None:
        from bike_shop.worktree import _worktrees_base

        env = {k: v for k, v in os.environ.items() if k != "AGENT_WORKTREE_DIR"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="AGENT_WORKTREE_DIR not set"):
                _worktrees_base()

    def test_returns_agent_worktree_dir(self, tmp_path) -> None:
        from bike_shop.worktree import _worktrees_base

        wt_dir = str(tmp_path / "worktrees")
        with patch.dict(os.environ, {"AGENT_WORKTREE_DIR": wt_dir}):
            assert _worktrees_base() == wt_dir

    def test_does_not_use_agent_workspace(self, tmp_path) -> None:
        """AGENT_WORKTREE_DIR is independent of AGENT_WORKSPACE."""
        from bike_shop.worktree import _worktrees_base

        wt_dir = str(tmp_path / "worktrees")
        workspace = str(tmp_path / "repo")
        os.makedirs(workspace)
        with patch.dict(
            os.environ,
            {"AGENT_WORKTREE_DIR": wt_dir, "AGENT_WORKSPACE": workspace},
        ):
            result = _worktrees_base()
        assert result == wt_dir
        # Must NOT embed AGENT_WORKSPACE path
        assert workspace not in result


class TestEnsureWorktree:
    """Tests for ensure_worktree naming and error propagation."""

    @patch("bike_shop.worktree.create_worktree")
    def test_default_name(self, mock_create: MagicMock, tmp_path) -> None:
        mock_create.return_value = str(tmp_path / "elliot-default")

        from bike_shop.worktree import ensure_worktree

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(tmp_path / "wt")},
        ):
            path = ensure_worktree("elliot")

        mock_create.assert_called_once_with("elliot-default", base_branch="main")
        assert path == str(tmp_path / "elliot-default")

    @patch("bike_shop.worktree.create_worktree")
    def test_with_task_id(self, mock_create: MagicMock, tmp_path) -> None:
        mock_create.return_value = str(tmp_path / "elliot-funds-abc")

        from bike_shop.worktree import ensure_worktree

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(tmp_path / "wt")},
        ):
            ensure_worktree("elliot", task_id="funds-abc")

        mock_create.assert_called_once_with("elliot-funds-abc", base_branch="main")

    def test_raises_when_worktree_dir_not_set(self, tmp_path) -> None:
        """ensure_worktree must raise (not return None) when env is missing."""
        from bike_shop.worktree import ensure_worktree

        env = {k: v for k, v in os.environ.items() if k != "AGENT_WORKTREE_DIR"}
        env["AGENT_WORKSPACE"] = str(tmp_path)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="AGENT_WORKTREE_DIR not set"):
                ensure_worktree("elliot")


class TestGetWorktreePath:
    """Tests for get_worktree_path."""

    def test_returns_none_when_not_exists(self, tmp_path) -> None:
        from bike_shop.worktree import get_worktree_path

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(tmp_path / "wt")},
        ):
            assert get_worktree_path("nonexistent") is None

    def test_returns_path_when_exists(self, tmp_path) -> None:
        from bike_shop.worktree import get_worktree_path

        wt_dir = tmp_path / "wt"
        agent_dir = wt_dir / "elliot-default"
        agent_dir.mkdir(parents=True)

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(wt_dir)},
        ):
            result = get_worktree_path("elliot-default")

        assert result == str(agent_dir)


class TestListWorktrees:
    """Tests for list_worktrees."""

    def test_returns_empty_when_dir_missing(self, tmp_path) -> None:
        from bike_shop.worktree import list_worktrees

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(tmp_path / "ghost")},
        ):
            assert list_worktrees() == []

    def test_lists_existing_worktrees(self, tmp_path) -> None:
        from bike_shop.worktree import list_worktrees

        wt_dir = tmp_path / "wt"
        (wt_dir / "elliot-default").mkdir(parents=True)
        (wt_dir / "mr-robot-default").mkdir(parents=True)

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(wt_dir)},
        ):
            result = list_worktrees()

        names = [r["name"] for r in result]
        assert "elliot-default" in names
        assert "mr-robot-default" in names


class TestCleanupStaleWorktrees:
    """Tests for cleanup_stale_worktrees."""

    @patch("bike_shop.worktree.remove_worktree")
    def test_removes_old_worktrees(self, mock_remove: MagicMock, tmp_path) -> None:
        from bike_shop.worktree import cleanup_stale_worktrees

        wt_dir = tmp_path / "wt"
        old_dir = wt_dir / "elliot-old"
        old_dir.mkdir(parents=True)

        # Set mtime to 10 days ago
        old_time = time.time() - (10 * 86400)
        os.utime(str(old_dir), (old_time, old_time))

        mock_remove.return_value = True

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(wt_dir)},
        ):
            removed = cleanup_stale_worktrees(max_age_days=7)

        assert removed == 1
        mock_remove.assert_called_once_with("elliot-old")

    @patch("bike_shop.worktree.remove_worktree")
    def test_keeps_recent_worktrees(self, mock_remove: MagicMock, tmp_path) -> None:
        from bike_shop.worktree import cleanup_stale_worktrees

        wt_dir = tmp_path / "wt"
        recent_dir = wt_dir / "elliot-recent"
        recent_dir.mkdir(parents=True)
        # mtime is now — recent

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(wt_dir)},
        ):
            removed = cleanup_stale_worktrees(max_age_days=7)

        assert removed == 0
        mock_remove.assert_not_called()

    def test_returns_zero_when_dir_missing(self, tmp_path) -> None:
        from bike_shop.worktree import cleanup_stale_worktrees

        with patch.dict(
            os.environ,
            {"AGENT_WORKSPACE": str(tmp_path), "AGENT_WORKTREE_DIR": str(tmp_path / "ghost")},
        ):
            assert cleanup_stale_worktrees() == 0


class TestGetWorkspaceNoFallback:
    """Tests that SlackAgentHandler._get_workspace propagates errors (no fallback)."""

    def test_propagates_runtime_error(self) -> None:
        """_get_workspace must raise RuntimeError, not return None, on failure."""
        from unittest.mock import MagicMock, patch

        # Import here to avoid triggering slack_bolt at module level in test env
        # We patch the class minimally
        from bike_shop.slack.handler import SlackAgentHandler

        config = MagicMock()
        config.agent_key = "elliot"
        provider = MagicMock()

        handler = SlackAgentHandler.__new__(SlackAgentHandler)
        handler._config = config

        with patch(
            "bike_shop.slack.handler.ensure_worktree",
            side_effect=RuntimeError("AGENT_WORKTREE_DIR not set"),
        ):
            with pytest.raises(RuntimeError, match="AGENT_WORKTREE_DIR not set"):
                handler._get_workspace()
