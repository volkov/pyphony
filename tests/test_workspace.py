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
