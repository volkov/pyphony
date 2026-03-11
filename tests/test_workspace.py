import subprocess
from pathlib import Path

import pytest

from pyphony.errors import HookError, HookTimeoutError
from pyphony.models import HooksConfig, ServiceConfig, WorkspaceConfig
from pyphony.workspace import WorkspaceManager


def _config(tmp_path, **hook_kwargs) -> ServiceConfig:
    return ServiceConfig(
        workspace=WorkspaceConfig(root=str(tmp_path)),
        hooks=HooksConfig(**hook_kwargs),
    )


def _config_with_repo(tmp_path, repo_path, **hook_kwargs) -> ServiceConfig:
    return ServiceConfig(
        workspace=WorkspaceConfig(root=str(tmp_path), repo=str(repo_path)),
        hooks=HooksConfig(**hook_kwargs),
    )


def _init_git_repo(path: Path) -> Path:
    """Create a bare-minimum git repo with one commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("init")
    subprocess.run(
        ["git", "add", "."], cwd=str(path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    return path


# ======================================================================
# Existing tests (directory mode)
# ======================================================================


@pytest.mark.asyncio
async def test_create_workspace_creates_directory(tmp_path):
    mgr = WorkspaceManager(_config(tmp_path))
    ws = await mgr.create_or_reuse("ISSUE-1")

    assert ws.created_now is True
    assert Path(ws.path).is_dir()
    assert ws.workspace_key == "ISSUE-1"


@pytest.mark.asyncio
async def test_reuse_existing_workspace(tmp_path):
    mgr = WorkspaceManager(_config(tmp_path))
    ws1 = await mgr.create_or_reuse("ISSUE-2")
    ws2 = await mgr.create_or_reuse("ISSUE-2")

    assert ws1.created_now is True
    assert ws2.created_now is False
    assert ws1.path == ws2.path


@pytest.mark.asyncio
async def test_after_create_runs_only_on_new(tmp_path):
    marker = tmp_path / "hook_ran"
    mgr = WorkspaceManager(
        _config(tmp_path, after_create=f"touch {marker}")
    )

    ws1 = await mgr.create_or_reuse("ISSUE-3")
    assert ws1.created_now is True
    assert marker.exists()

    marker.unlink()
    ws2 = await mgr.create_or_reuse("ISSUE-3")
    assert ws2.created_now is False
    assert not marker.exists()


@pytest.mark.asyncio
async def test_after_create_failure_removes_workspace(tmp_path):
    mgr = WorkspaceManager(
        _config(tmp_path, after_create="exit 1")
    )

    with pytest.raises(HookError):
        await mgr.create_or_reuse("ISSUE-FAIL")

    assert not (tmp_path / "ISSUE-FAIL").exists()


@pytest.mark.asyncio
async def test_before_run_failure_raises(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    mgr = WorkspaceManager(
        _config(tmp_path, before_run="exit 42")
    )

    with pytest.raises(HookError):
        await mgr.run_before_run(str(ws_dir))


@pytest.mark.asyncio
async def test_after_run_failure_is_ignored(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    mgr = WorkspaceManager(
        _config(tmp_path, after_run="exit 1")
    )

    await mgr.run_after_run(str(ws_dir))


@pytest.mark.asyncio
async def test_hook_timeout_raises(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    mgr = WorkspaceManager(
        _config(tmp_path, timeout_ms=100)
    )

    with pytest.raises(HookTimeoutError, match="timed out"):
        await mgr.run_hook("sleep 10", str(ws_dir))


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path):
    mgr = WorkspaceManager(_config(tmp_path))

    ws = await mgr.create_or_reuse("../../etc")
    assert ws.path.startswith(str(tmp_path.resolve()))
    assert Path(ws.path).is_dir()


@pytest.mark.asyncio
async def test_cleanup_removes_directory(tmp_path):
    mgr = WorkspaceManager(_config(tmp_path))
    ws = await mgr.create_or_reuse("ISSUE-DEL")
    assert Path(ws.path).is_dir()

    await mgr.cleanup_workspace("ISSUE-DEL")
    assert not Path(ws.path).exists()


@pytest.mark.asyncio
async def test_cleanup_runs_before_remove_hook(tmp_path):
    marker = tmp_path / "removed"
    mgr = WorkspaceManager(
        _config(tmp_path, before_remove=f"touch {marker}")
    )

    ws = await mgr.create_or_reuse("ISSUE-RM")
    assert Path(ws.path).is_dir()

    await mgr.cleanup_workspace("ISSUE-RM")
    assert marker.exists()
    assert not Path(ws.path).exists()


@pytest.mark.asyncio
async def test_cleanup_before_remove_failure_ignored(tmp_path):
    mgr = WorkspaceManager(
        _config(tmp_path, before_remove="exit 1")
    )

    ws = await mgr.create_or_reuse("ISSUE-RMF")
    assert Path(ws.path).is_dir()

    await mgr.cleanup_workspace("ISSUE-RMF")
    assert not Path(ws.path).exists()


# ======================================================================
# Worktree mode tests
# ======================================================================


@pytest.mark.asyncio
async def test_worktree_create_new(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-41")

    assert ws.created_now is True
    assert Path(ws.path).is_dir()
    assert ws.workspace_key == "SER-41"

    # Branch should exist in source repo
    result = subprocess.run(
        ["git", "branch", "--list", "SER-41"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "SER-41" in result.stdout


@pytest.mark.asyncio
async def test_worktree_reuse_existing(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws1 = await mgr.create_or_reuse("SER-42")
    ws2 = await mgr.create_or_reuse("SER-42")

    assert ws1.created_now is True
    assert ws2.created_now is False
    assert ws1.path == ws2.path


@pytest.mark.asyncio
async def test_worktree_existing_branch(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    # Pre-create the branch in the repo
    subprocess.run(
        ["git", "branch", "SER-43"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-43")

    assert ws.created_now is True
    assert Path(ws.path).is_dir()


@pytest.mark.asyncio
async def test_worktree_cleanup(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-44")
    assert Path(ws.path).is_dir()

    await mgr.cleanup_workspace("SER-44")
    assert not Path(ws.path).exists()

    # Branch should still exist in source repo (not deleted)
    result = subprocess.run(
        ["git", "branch", "--list", "SER-44"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "SER-44" in result.stdout


@pytest.mark.asyncio
async def test_worktree_after_create_hook(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    marker = tmp_path / "hook_ran"

    mgr = WorkspaceManager(
        _config_with_repo(workspaces, repo, after_create=f"touch {marker}")
    )
    ws = await mgr.create_or_reuse("SER-45")

    assert ws.created_now is True
    assert marker.exists()


@pytest.mark.asyncio
async def test_worktree_hook_failure_cleans_up(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(
        _config_with_repo(workspaces, repo, after_create="exit 1")
    )

    with pytest.raises(HookError):
        await mgr.create_or_reuse("SER-46")

    assert not (workspaces / "SER-46").exists()


@pytest.mark.asyncio
async def test_worktree_invalid_repo(tmp_path):
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    fake_repo = tmp_path / "nonexistent"

    mgr = WorkspaceManager(_config_with_repo(workspaces, fake_repo))

    with pytest.raises(HookError, match="does not exist"):
        await mgr.create_or_reuse("SER-47")


# ======================================================================
# Rebase branch onto main tests
# ======================================================================


@pytest.mark.asyncio
async def test_rebase_branch_onto_main_success(tmp_path):
    """Rebase + ff-merge updates main and branch can be deleted."""
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-60")

    # Create a commit on the branch inside the worktree
    (Path(ws.path) / "feature.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=ws.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add feature"],
        cwd=ws.path,
        check=True,
        capture_output=True,
    )

    result = await mgr.rebase_branch_onto_main("SER-60")
    assert result is not None
    assert len(result.commit_sha) >= 7
    assert "feature.txt" in result.diffstat

    # Main should now contain the commit
    log_out = subprocess.run(
        ["git", "log", "--oneline", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "add feature" in log_out.stdout

    # Cleanup with branch deletion
    await mgr.cleanup_workspace("SER-60", delete_branch=True)
    assert not Path(ws.path).exists()

    # Branch should be deleted
    branches = subprocess.run(
        ["git", "branch", "--list", "SER-60"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "SER-60" not in branches.stdout


@pytest.mark.asyncio
async def test_rebase_branch_onto_main_conflict(tmp_path):
    """Rebase with conflicts logs warning and returns False."""
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-61")

    # Create a conflicting commit on main
    (repo / "conflict.txt").write_text("main version")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "main conflict"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Create a conflicting commit on the branch
    (Path(ws.path) / "conflict.txt").write_text("branch version")
    subprocess.run(["git", "add", "."], cwd=ws.path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "branch conflict"],
        cwd=ws.path,
        check=True,
        capture_output=True,
    )

    result = await mgr.rebase_branch_onto_main("SER-61")
    assert result is None

    # Branch should still exist untouched
    branches = subprocess.run(
        ["git", "branch", "--list", "SER-61"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "SER-61" in branches.stdout


@pytest.mark.asyncio
async def test_rebase_noop_when_no_repo(tmp_path):
    """In directory mode (no repo), rebase is a no-op returning False."""
    mgr = WorkspaceManager(_config(tmp_path))
    result = await mgr.rebase_branch_onto_main("SER-62")
    assert result is None


@pytest.mark.asyncio
async def test_rebase_missing_worktree(tmp_path):
    """If worktree doesn't exist, rebase returns False."""
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    result = await mgr.rebase_branch_onto_main("SER-63")
    assert result is None


@pytest.mark.asyncio
async def test_cleanup_delete_branch(tmp_path):
    """cleanup_workspace with delete_branch=True removes the branch."""
    repo = _init_git_repo(tmp_path / "repo")
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    mgr = WorkspaceManager(_config_with_repo(workspaces, repo))
    ws = await mgr.create_or_reuse("SER-64")

    # Merge the branch into main first so -d succeeds
    subprocess.run(
        ["git", "merge", "--ff-only", "SER-64"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    await mgr.cleanup_workspace("SER-64", delete_branch=True)
    assert not Path(ws.path).exists()

    branches = subprocess.run(
        ["git", "branch", "--list", "SER-64"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert "SER-64" not in branches.stdout
