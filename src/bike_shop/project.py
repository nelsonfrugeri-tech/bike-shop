"""Multi-project support — registry, config, and channel-to-project resolution.

Projects are defined in projects.yaml. Each project maps Slack channels to
a ProjectConfig that carries repo paths, memory collection, observability
keys, and MCP config overrides.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Immutable configuration for a single managed project."""

    project_id: str
    display_name: str
    repo_path: str
    worktree_dir: str
    github_repo: str
    mem0_collection: str
    langfuse_tags: list[str]
    langfuse_public_key: str
    langfuse_secret_key: str
    mcp_config: str | None
    slack_channels: list[str]


class ProjectRegistry:
    """Loads projects.yaml and provides lookup by channel or project id."""

    def __init__(self, config_path: str) -> None:
        self._projects: dict[str, ProjectConfig] = {}
        self._channel_map: dict[str, str] = {}
        self._default_id: str = ""
        self._load(config_path)

    def _load(self, config_path: str) -> None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Project config not found: {config_path}")

        with open(path) as f:
            raw = yaml.safe_load(f)

        self._default_id = raw.get("default_project", "")
        projects_raw: dict[str, Any] = raw.get("projects", {})

        for pid, pdata in projects_raw.items():
            config = ProjectConfig(
                project_id=pid,
                display_name=pdata.get("display_name", pid),
                repo_path=os.path.expandvars(pdata.get("repo_path", "")),
                worktree_dir=os.path.expandvars(pdata.get("worktree_dir", "")),
                github_repo=pdata.get("github_repo", ""),
                mem0_collection=pdata.get("mem0_collection", f"{pid}-memory"),
                langfuse_tags=pdata.get("langfuse_tags", []),
                langfuse_public_key=os.path.expandvars(pdata.get("langfuse_public_key", "")),
                langfuse_secret_key=os.path.expandvars(pdata.get("langfuse_secret_key", "")),
                mcp_config=pdata.get("mcp_config"),
                slack_channels=pdata.get("slack_channels", []),
            )
            self._projects[pid] = config

            for channel_id in config.slack_channels:
                self._channel_map[channel_id] = pid

        if self._default_id and self._default_id not in self._projects:
            raise ValueError(
                f"default_project '{self._default_id}' not found in projects"
            )

        logger.info(
            "ProjectRegistry loaded: %d projects, %d channel mappings, default=%s",
            len(self._projects),
            len(self._channel_map),
            self._default_id,
        )

    def get_by_channel(self, channel_id: str) -> ProjectConfig | None:
        """Lookup project by Slack channel ID."""
        pid = self._channel_map.get(channel_id)
        if pid:
            return self._projects[pid]
        return None

    def get_by_id(self, project_id: str) -> ProjectConfig | None:
        """Lookup project by project ID."""
        return self._projects.get(project_id)

    def get_default(self) -> ProjectConfig:
        """Return the default project. Raises if none configured."""
        if not self._default_id:
            raise ValueError("No default_project configured in projects.yaml")
        return self._projects[self._default_id]

    def all_projects(self) -> list[ProjectConfig]:
        """Return all registered projects."""
        return list(self._projects.values())


class ProjectResolver:
    """Resolves a Slack channel/thread to a ProjectConfig.

    Resolution order:
    1. Channel mapping (channel_id in project's slack_channels)
    2. Thread inheritance (session store has project_id for this thread)
    3. Default project fallback
    """

    def __init__(
        self,
        registry: ProjectRegistry,
        session_store: Any = None,
    ) -> None:
        self._registry = registry
        self._session_store = session_store

    def resolve(
        self,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> ProjectConfig:
        """Resolve channel/thread to a ProjectConfig."""
        # 1. Channel mapping
        project = self._registry.get_by_channel(channel_id)
        if project:
            return project

        # 2. Thread inheritance
        if thread_ts and self._session_store is not None:
            pid = self._session_store.get_project_id(thread_ts)
            if pid:
                project = self._registry.get_by_id(pid)
                if project:
                    return project

        # 3. Default fallback
        return self._registry.get_default()
