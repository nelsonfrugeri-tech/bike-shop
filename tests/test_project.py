"""Tests for multi-project support: ProjectConfig, ProjectRegistry, ProjectResolver."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class TestProjectConfig:
    """Tests for ProjectConfig dataclass."""

    def test_frozen_immutable(self) -> None:
        from bike_shop.project import ProjectConfig

        config = ProjectConfig(
            project_id="test",
            display_name="Test",
            repo_path="/tmp/test",
            worktree_dir="/tmp/test-wt",
            github_repo="org/test",
            mem0_collection="test-memory",
            langfuse_tags=["test"],
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            mcp_config=None,
            slack_channels=["C123"],
        )
        with pytest.raises(AttributeError):
            config.project_id = "changed"  # type: ignore[misc]


class TestProjectRegistry:
    """Tests for ProjectRegistry loading and lookups."""

    def test_raises_when_file_not_found(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        with pytest.raises(FileNotFoundError, match="not found"):
            ProjectRegistry(str(tmp_path / "nonexistent.yaml"))

    def test_loads_valid_config(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: alpha\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: /tmp/alpha\n"
            "    worktree_dir: /tmp/alpha-wt\n"
            "    github_repo: org/alpha\n"
            "    mem0_collection: alpha-mem\n"
            "    langfuse_tags: [alpha]\n"
            "    langfuse_public_key: pk\n"
            "    langfuse_secret_key: sk\n"
            "    mcp_config: null\n"
            "    slack_channels: [C111, C222]\n"
        )
        registry = ProjectRegistry(str(yaml_path))
        assert len(registry.all_projects()) == 1
        assert registry.get_by_id("alpha") is not None
        assert registry.get_by_id("alpha").display_name == "Alpha"

    def test_channel_mapping(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: alpha\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: /tmp/a\n"
            "    worktree_dir: /tmp/a-wt\n"
            "    github_repo: org/a\n"
            "    mem0_collection: a-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: [C111]\n"
            "  beta:\n"
            "    display_name: Beta\n"
            "    repo_path: /tmp/b\n"
            "    worktree_dir: /tmp/b-wt\n"
            "    github_repo: org/b\n"
            "    mem0_collection: b-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: [C222]\n"
        )
        registry = ProjectRegistry(str(yaml_path))
        assert registry.get_by_channel("C111").project_id == "alpha"
        assert registry.get_by_channel("C222").project_id == "beta"
        assert registry.get_by_channel("C999") is None

    def test_get_default(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: alpha\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: /tmp/a\n"
            "    worktree_dir: /tmp/a-wt\n"
            "    github_repo: org/a\n"
            "    mem0_collection: a-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: []\n"
        )
        registry = ProjectRegistry(str(yaml_path))
        assert registry.get_default().project_id == "alpha"

    def test_invalid_default_raises(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: nonexistent\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: /tmp/a\n"
            "    worktree_dir: /tmp/a-wt\n"
            "    github_repo: org/a\n"
            "    mem0_collection: a-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: []\n"
        )
        with pytest.raises(ValueError, match="not found"):
            ProjectRegistry(str(yaml_path))

    def test_env_var_expansion(self, tmp_path) -> None:
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: alpha\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: ${TEST_REPO_PATH_XYZ}\n"
            "    worktree_dir: ${TEST_WT_PATH_XYZ}\n"
            "    github_repo: org/a\n"
            "    mem0_collection: a-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: []\n"
        )
        with patch.dict(os.environ, {
            "TEST_REPO_PATH_XYZ": "/resolved/repo",
            "TEST_WT_PATH_XYZ": "/resolved/wt",
        }):
            registry = ProjectRegistry(str(yaml_path))
            project = registry.get_by_id("alpha")
            assert project.repo_path == "/resolved/repo"
            assert project.worktree_dir == "/resolved/wt"


class TestProjectResolver:
    """Tests for ProjectResolver resolution order."""

    def _make_registry(self, tmp_path) -> "ProjectRegistry":
        from bike_shop.project import ProjectRegistry

        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default_project: alpha\n"
            "projects:\n"
            "  alpha:\n"
            "    display_name: Alpha\n"
            "    repo_path: /tmp/a\n"
            "    worktree_dir: /tmp/a-wt\n"
            "    github_repo: org/a\n"
            "    mem0_collection: a-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: [C111]\n"
            "  beta:\n"
            "    display_name: Beta\n"
            "    repo_path: /tmp/b\n"
            "    worktree_dir: /tmp/b-wt\n"
            "    github_repo: org/b\n"
            "    mem0_collection: b-mem\n"
            "    langfuse_tags: []\n"
            "    langfuse_public_key: ''\n"
            "    langfuse_secret_key: ''\n"
            "    mcp_config: null\n"
            "    slack_channels: [C222]\n"
        )
        return ProjectRegistry(str(yaml_path))

    def test_resolves_by_channel(self, tmp_path) -> None:
        from bike_shop.project import ProjectResolver

        registry = self._make_registry(tmp_path)
        resolver = ProjectResolver(registry)
        assert resolver.resolve("C222").project_id == "beta"

    def test_falls_back_to_default(self, tmp_path) -> None:
        from bike_shop.project import ProjectResolver

        registry = self._make_registry(tmp_path)
        resolver = ProjectResolver(registry)
        assert resolver.resolve("C999").project_id == "alpha"

    def test_thread_inheritance(self, tmp_path) -> None:
        from bike_shop.project import ProjectResolver

        registry = self._make_registry(tmp_path)
        session_store = MagicMock()
        session_store.get_project_id.return_value = "beta"

        resolver = ProjectResolver(registry, session_store=session_store)
        # Unknown channel, but thread has project_id stored
        result = resolver.resolve("C999", thread_ts="1234.5678")
        assert result.project_id == "beta"

    def test_channel_takes_priority_over_thread(self, tmp_path) -> None:
        from bike_shop.project import ProjectResolver

        registry = self._make_registry(tmp_path)
        session_store = MagicMock()
        session_store.get_project_id.return_value = "beta"

        resolver = ProjectResolver(registry, session_store=session_store)
        # Channel maps to alpha, thread says beta — channel wins
        result = resolver.resolve("C111", thread_ts="1234.5678")
        assert result.project_id == "alpha"


class TestSessionStoreProjectId:
    """Tests for project_id in SessionStore."""

    def test_store_and_get_project_id(self, tmp_path) -> None:
        from bike_shop.session import SessionStore

        with patch("bike_shop.session.SESSIONS_DIR", str(tmp_path)):
            store = SessionStore("test-agent")
            store.store("t1", "session-1", project_id="market-analysis")
            assert store.get_project_id("t1") == "market-analysis"

    def test_get_project_id_returns_none_when_missing(self, tmp_path) -> None:
        from bike_shop.session import SessionStore

        with patch("bike_shop.session.SESSIONS_DIR", str(tmp_path)):
            store = SessionStore("test-agent")
            assert store.get_project_id("nonexistent") is None

    def test_backwards_compat_no_project_id(self, tmp_path) -> None:
        from bike_shop.session import SessionStore

        with patch("bike_shop.session.SESSIONS_DIR", str(tmp_path)):
            store = SessionStore("test-agent")
            store.store("t1", "session-1")  # No project_id
            assert store.get_project_id("t1") is None
            assert store.get("t1") == "session-1"


class TestMem0ClientMultiCollection:
    """Tests that get_mem0 supports multiple collections."""

    def test_different_collections_create_different_clients(self) -> None:
        from bike_shop.mem0_client import get_mem0, reset_mem0

        reset_mem0()
        mock_memory = MagicMock()
        mock_memory.from_config.return_value = MagicMock()

        with patch.dict("sys.modules", {"mem0": MagicMock(Memory=mock_memory)}):
            client_a = get_mem0("collection-a")
            client_b = get_mem0("collection-b")
            client_a_again = get_mem0("collection-a")

            assert client_a is client_a_again  # Same singleton
            assert mock_memory.from_config.call_count == 2  # Only 2 created

        reset_mem0()

    def test_default_collection_name(self) -> None:
        from bike_shop.mem0_client import get_mem0, reset_mem0

        reset_mem0()
        mock_memory = MagicMock()
        mock_memory.from_config.return_value = MagicMock()

        with patch.dict("sys.modules", {"mem0": MagicMock(Memory=mock_memory)}):
            get_mem0()  # Default collection name

            call_config = mock_memory.from_config.call_args[0][0]
            assert call_config["vector_store"]["config"]["collection_name"] == "bike-shop-memory"

        reset_mem0()


class TestWorktreeProjectOverrides:
    """Tests that worktree functions accept repo_path/worktree_dir overrides."""

    def test_workspace_root_uses_explicit_path(self, tmp_path) -> None:
        from bike_shop.worktree import _workspace_root

        explicit = str(tmp_path)
        result = _workspace_root(repo_path=explicit)
        assert result == explicit

    def test_workspace_root_falls_back_to_env(self, tmp_path) -> None:
        from bike_shop.worktree import _workspace_root

        with patch.dict(os.environ, {"AGENT_WORKSPACE": str(tmp_path)}):
            result = _workspace_root()
            assert result == str(tmp_path)

    def test_worktrees_base_uses_explicit_path(self) -> None:
        from bike_shop.worktree import _worktrees_base

        result = _worktrees_base(worktree_dir="/explicit/path")
        assert result == "/explicit/path"

    def test_worktrees_base_falls_back_to_env(self) -> None:
        from bike_shop.worktree import _worktrees_base

        with patch.dict(os.environ, {"AGENT_WORKTREE_DIR": "/env/path"}):
            result = _worktrees_base()
            assert result == "/env/path"

    @patch("bike_shop.worktree.create_worktree")
    def test_ensure_worktree_passes_overrides(self, mock_create, tmp_path) -> None:
        from bike_shop.worktree import ensure_worktree

        mock_create.return_value = str(tmp_path / "wt")
        ensure_worktree(
            "elliot",
            repo_path="/custom/repo",
            worktree_dir="/custom/wt",
        )
        mock_create.assert_called_once_with(
            "elliot-default",
            base_branch="main",
            repo_path="/custom/repo",
            worktree_dir="/custom/wt",
        )
