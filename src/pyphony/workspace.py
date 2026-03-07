from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from pyphony.errors import HookError, HookTimeoutError
from pyphony.models import ServiceConfig, Workspace
from pyphony.normalization import sanitize_workspace_key

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, config: ServiceConfig) -> None:
        self._workspace_root = Path(config.workspace.root).resolve()
        self._hooks = config.hooks

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def create_or_reuse(self, identifier: str) -> Workspace:
        workspace_key = sanitize_workspace_key(identifier)
        workspace_path = (self._workspace_root / workspace_key).resolve()

        if not str(workspace_path).startswith(str(self._workspace_root)):
            raise HookError(
                f"Path traversal detected: {workspace_path} is outside "
                f"workspace root {self._workspace_root}"
            )

        created_now = not workspace_path.exists()
        workspace_path.mkdir(parents=True, exist_ok=True)

        if created_now and self._hooks.after_create:
            try:
                await self.run_hook(self._hooks.after_create, str(workspace_path))
            except Exception:
                shutil.rmtree(workspace_path, ignore_errors=True)
                raise

        return Workspace(
            path=str(workspace_path),
            workspace_key=workspace_key,
            created_now=created_now,
        )

    async def run_hook(self, hook_script: str, workspace_path: str) -> None:
        timeout_s = self._hooks.timeout_ms / 1000.0

        proc = await asyncio.create_subprocess_exec(
            "sh", "-lc", hook_script,
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HookTimeoutError(
                f"Hook timed out after {self._hooks.timeout_ms}ms"
            )

        if proc.returncode != 0:
            raise HookError(
                f"Hook failed with exit code {proc.returncode}: "
                f"{stderr.decode(errors='replace').strip()}"
            )

    async def run_before_run(self, workspace_path: str) -> None:
        if self._hooks.before_run:
            await self.run_hook(self._hooks.before_run, workspace_path)

    async def run_after_run(self, workspace_path: str) -> None:
        if self._hooks.after_run:
            try:
                await self.run_hook(self._hooks.after_run, workspace_path)
            except Exception as exc:
                logger.warning("after_run hook failed (ignored): %s", exc)

    async def cleanup_workspace(self, identifier: str) -> None:
        workspace_key = sanitize_workspace_key(identifier)
        workspace_path = (self._workspace_root / workspace_key).resolve()

        if not str(workspace_path).startswith(str(self._workspace_root)):
            logger.warning("Path traversal in cleanup, skipping: %s", workspace_path)
            return

        if not workspace_path.exists():
            return

        if self._hooks.before_remove:
            try:
                await self.run_hook(self._hooks.before_remove, str(workspace_path))
            except Exception as exc:
                logger.warning("before_remove hook failed (ignored): %s", exc)

        shutil.rmtree(workspace_path, ignore_errors=True)
