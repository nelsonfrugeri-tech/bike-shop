"""Git worktree management — isolated workspaces per agent/task.

Worktrees live in {AGENT_WORKTREE_DIR}/{name}/.
Each worktree is a full git checkout on its own branch, sharing
the same .git directory as the main repo.

Environment variables:
    AGENT_WORKSPACE: Path to the main git repository (read-only reference).
                     Must exist and be a valid git repo. Mandatory.
    AGENT_WORKTREE_DIR: Directory where worktrees are created. Separate from
                        AGENT_WORKSPACE so worktrees can live outside the main
                        repo. Mandatory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

def _workspace_root(repo_path: str | None = None) -> str:
    """Get workspace root from explicit param or AGENT_WORKSPACE env var.

    Args:
        repo_path: Explicit repo path (from ProjectConfig). Falls back to
                   AGENT_WORKSPACE env var if None.

    AGENT_WORKSPACE must point to the main git repository.
    It is used as the cwd for git commands but never written to directly.
    """
    ws = repo_path or os.environ.get("AGENT_WORKSPACE")
    if not ws:
        raise RuntimeError(
            "AGENT_WORKSPACE not set — cannot create worktrees. "
            "Set AGENT_WORKSPACE to the main repo path."
        )
    if not os.path.isdir(ws):
        raise RuntimeError(
            f"AGENT_WORKSPACE directory does not exist: {ws}"
        )
    return ws


def _worktrees_base(worktree_dir: str | None = None) -> str:
    """Return the base directory for all worktrees.

    Args:
        worktree_dir: Explicit worktree dir (from ProjectConfig). Falls back
                      to AGENT_WORKTREE_DIR env var if None.

    This directory is separate from AGENT_WORKSPACE so worktrees can live
    outside the main repository tree.

    Raises:
        RuntimeError: If neither param nor env var is set.
    """
    wt_dir = worktree_dir or os.environ.get("AGENT_WORKTREE_DIR")
    if not wt_dir:
        raise RuntimeError(
            "AGENT_WORKTREE_DIR not set — cannot create worktrees. "
            "Set AGENT_WORKTREE_DIR to the directory where worktrees should live."
        )
    return wt_dir


def _detect_default_branch(repo_path: str) -> str:
    """Detect the default branch of a git repo (main, master, etc.)."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # refs/remotes/origin/main → main
            return result.stdout.strip().rsplit("/", 1)[-1]
    except Exception:
        pass

    # Fallback: check if main or master exists
    for branch in ("main", "master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return branch

    return "main"


def create_worktree(
    name: str,
    branch: str | None = None,
    base_branch: str | None = None,
    repo_path: str | None = None,
    worktree_dir: str | None = None,
) -> str:
    """Create a git worktree and return its absolute path.

    Args:
        name: Worktree directory name (e.g. "elliot-funds-abc123").
        branch: Branch name to create. Defaults to "worktree/{name}".
        base_branch: Branch to base the new worktree from. Auto-detected if None.
        repo_path: Explicit repo path (overrides AGENT_WORKSPACE env var).
        worktree_dir: Explicit worktree dir (overrides AGENT_WORKTREE_DIR env var).

    Returns:
        Absolute path to the worktree directory.

    Raises:
        RuntimeError: If worktree creation fails.
    """
    ws = _workspace_root(repo_path)
    if base_branch is None:
        base_branch = _detect_default_branch(ws)
        logger.info("[worktree] Auto-detected default branch: %s", base_branch)
    base = _worktrees_base(worktree_dir)
    os.makedirs(base, exist_ok=True)

    wt_path = os.path.join(base, name)

    # Already exists — sync with latest main before reusing
    if os.path.isdir(wt_path):
        try:
            subprocess.run(
                ["git", "fetch", "origin", base_branch],
                cwd=wt_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "merge", f"origin/{base_branch}", "--no-edit"],
                cwd=wt_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info("[worktree] Reusing existing worktree (synced with %s): %s", base_branch, wt_path)
        except Exception as e:
            logger.warning("[worktree] Failed to sync worktree %s: %s — reusing as-is", wt_path, e)
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
        # Try without --force first to protect uncommitted work
        result = subprocess.run(
            ["git", "worktree", "remove", wt_path],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            # Fallback to --force only if clean remove fails
            logger.warning("[worktree] Clean remove failed, forcing: %s", wt_path)
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
        # Last resort: delete directory and prune
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
    base_branch: str | None = None,
    repo_path: str | None = None,
    worktree_dir: str | None = None,
) -> str:
    """Ensure a worktree exists for this agent/task and return its path.

    Args:
        agent_key: Agent identifier (e.g. "elliot").
        task_id: Optional task suffix. Defaults to "default".
        base_branch: Branch to base the worktree from. Auto-detected if None.
        repo_path: Explicit repo path (overrides AGENT_WORKSPACE env var).
        worktree_dir: Explicit worktree dir (overrides AGENT_WORKTREE_DIR env var).

    Naming:
        - With task_id: "{agent_key}-{task_id}"
        - Without task_id: "{agent_key}-default"
    """
    suffix = task_id or "default"
    name = f"{agent_key}-{suffix}"
    return create_worktree(
        name, base_branch=base_branch,
        repo_path=repo_path, worktree_dir=worktree_dir,
    )


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
