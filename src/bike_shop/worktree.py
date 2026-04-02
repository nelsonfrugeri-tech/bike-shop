"""Git worktree management — isolated workspaces per agent/task.

Worktrees live in {AGENT_WORKSPACE}/.worktrees/{name}/.
Each worktree is a full git checkout on its own branch, sharing
the same .git directory as the main repo.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WORKTREES_DIR = ".worktrees"


def _workspace_root() -> str:
    """Get AGENT_WORKSPACE or raise."""
    ws = os.environ.get("AGENT_WORKSPACE")
    if not ws:
        raise RuntimeError(
            "AGENT_WORKSPACE not set — cannot create worktrees. "
            "Set AGENT_WORKSPACE to the main repo path."
        )
    return ws


def _worktrees_base() -> str:
    """Return the base directory for all worktrees."""
    return os.path.join(_workspace_root(), WORKTREES_DIR)


def create_worktree(
    name: str,
    branch: str | None = None,
    base_branch: str = "main",
) -> str:
    """Create a git worktree and return its absolute path.

    Args:
        name: Worktree directory name (e.g. "elliot-funds-abc123").
        branch: Branch name to create. Defaults to "worktree/{name}".
        base_branch: Branch to base the new worktree from.

    Returns:
        Absolute path to the worktree directory.

    Raises:
        RuntimeError: If worktree creation fails.
    """
    ws = _workspace_root()
    base = _worktrees_base()
    os.makedirs(base, exist_ok=True)

    wt_path = os.path.join(base, name)

    # Already exists — return it
    if os.path.isdir(wt_path):
        logger.info("[worktree] Reusing existing worktree: %s", wt_path)
        return wt_path

    if branch is None:
        branch = f"worktree/{name}"

    try:
        # Fetch latest from remote
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Create worktree with new branch from base
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, wt_path, f"origin/{base_branch}"],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            # Branch might already exist — try without -b
            result = subprocess.run(
                ["git", "worktree", "add", wt_path, branch],
                cwd=ws,
                capture_output=True,
                text=True,
                timeout=30,
            )

        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

        logger.info("[worktree] Created: %s (branch: %s)", wt_path, branch)
        return wt_path

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git worktree add timed out for {name}")


def remove_worktree(name: str) -> bool:
    """Remove a worktree by name. Returns True if removed."""
    ws = _workspace_root()
    wt_path = os.path.join(_worktrees_base(), name)

    if not os.path.exists(wt_path):
        return False

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=15,
        )
        logger.info("[worktree] Removed: %s", wt_path)
        return True
    except Exception as e:
        logger.warning("[worktree] Failed to remove %s: %s", wt_path, e)
        # Fallback: just delete the directory
        try:
            shutil.rmtree(wt_path)
            subprocess.run(["git", "worktree", "prune"], cwd=ws, capture_output=True, timeout=10)
            return True
        except Exception:
            return False


def get_worktree_path(name: str) -> str | None:
    """Get path of an existing worktree, or None."""
    wt_path = os.path.join(_worktrees_base(), name)
    return wt_path if os.path.isdir(wt_path) else None


def ensure_worktree(
    agent_key: str,
    task_id: str | None = None,
    base_branch: str = "main",
) -> str:
    """Ensure a worktree exists for this agent/task and return its path.

    Naming:
        - With task_id: "{agent_key}-{task_id}"
        - Without task_id: "{agent_key}-default"
    """
    suffix = task_id or "default"
    name = f"{agent_key}-{suffix}"
    return create_worktree(name, base_branch=base_branch)


def list_worktrees() -> list[dict[str, str]]:
    """List all managed worktrees with name and path."""
    base = _worktrees_base()
    if not os.path.isdir(base):
        return []

    result = []
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            result.append({"name": entry, "path": full})
    return result


def cleanup_stale_worktrees(max_age_days: int = 7) -> int:
    """Remove worktrees older than max_age_days. Returns count removed."""
    base = _worktrees_base()
    if not os.path.isdir(base):
        return 0

    now = time.time()
    cutoff = now - (max_age_days * 86400)
    removed = 0

    for entry in os.listdir(base):
        full = os.path.join(base, entry)
        if not os.path.isdir(full):
            continue
        try:
            mtime = os.path.getmtime(full)
            if mtime < cutoff:
                if remove_worktree(entry):
                    removed += 1
        except OSError:
            continue

    if removed:
        logger.info("[worktree] Cleaned up %d stale worktrees", removed)
    return removed
