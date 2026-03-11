from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from pyphony.errors import HookError, HookTimeoutError
from pyphony.models import MergeInfo, ServiceConfig, Workspace
from pyphony.normalization import sanitize_workspace_key

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, config: ServiceConfig) -> None:
        self._workspace_root = Path(config.workspace.root).resolve()
        self._hooks = config.hooks
        self._repo = (
            Path(config.workspace.repo).resolve()
            if config.workspace.repo
            else None
        )

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    # ------------------------------------------------------------------
    # Internal git helper
    # ------------------------------------------------------------------

    async def _run_git(self, *args: str, cwd: Path | str) -> str:
        """Run ``git <args>`` as a subprocess. Returns stdout on success."""
        timeout_s = self._hooks.timeout_ms / 1000.0

        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
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
                f"git {' '.join(args)} timed out after {self._hooks.timeout_ms}ms"
            )

        if proc.returncode != 0:
            raise HookError(
                f"git {' '.join(args)} failed (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

        return stdout.decode(errors="replace").strip()

    # ------------------------------------------------------------------
    # create / reuse
    # ------------------------------------------------------------------

    async def create_or_reuse(self, identifier: str) -> Workspace:
        workspace_key = sanitize_workspace_key(identifier)
        workspace_path = (self._workspace_root / workspace_key).resolve()

        if not str(workspace_path).startswith(str(self._workspace_root)):
            raise HookError(
                f"Path traversal detected: {workspace_path} is outside "
                f"workspace root {self._workspace_root}"
            )

        # ---- worktree mode ----
        if self._repo is not None:
            return await self._create_or_reuse_worktree(
                workspace_key, workspace_path
            )

        # ---- default (directory) mode ----
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

    async def _create_or_reuse_worktree(
        self, workspace_key: str, workspace_path: Path
    ) -> Workspace:
        assert self._repo is not None

        if not self._repo.is_dir():
            raise HookError(
                f"Repo path does not exist or is not a directory: {self._repo}"
            )

        branch_name = workspace_key

        # Already exists → reuse
        if workspace_path.exists():
            return Workspace(
                path=str(workspace_path),
                workspace_key=workspace_key,
                created_now=False,
            )

        # Try creating a new worktree with a new branch
        try:
            await self._run_git(
                "worktree",
                "add",
                str(workspace_path),
                "-b",
                branch_name,
                cwd=self._repo,
            )
        except HookError as exc:
            err_msg = str(exc)
            # Branch already exists — attach existing branch without -b
            if "already exists" in err_msg:
                await self._run_git(
                    "worktree",
                    "add",
                    str(workspace_path),
                    branch_name,
                    cwd=self._repo,
                )
            else:
                raise

        # Run after_create hook if configured
        if self._hooks.after_create:
            try:
                await self.run_hook(self._hooks.after_create, str(workspace_path))
            except Exception:
                # Clean up the worktree on hook failure
                try:
                    await self._run_git(
                        "worktree",
                        "remove",
                        "--force",
                        str(workspace_path),
                        cwd=self._repo,
                    )
                except Exception:
                    # Fallback: remove directory manually
                    shutil.rmtree(workspace_path, ignore_errors=True)
                raise

        return Workspace(
            path=str(workspace_path),
            workspace_key=workspace_key,
            created_now=True,
        )

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # rebase onto main
    # ------------------------------------------------------------------

    async def rebase_branch_onto_main(self, identifier: str) -> MergeInfo | None:
        """Rebase the worktree branch onto main and fast-forward main.

        Must be called *before* ``cleanup_workspace`` (the worktree must
        still exist so that git can rebase in it).

        Returns a :class:`MergeInfo` with the commit SHA and diffstat on
        success, or ``None`` on failure (conflicts, missing worktree,
        directory mode, etc.).
        """
        if self._repo is None:
            return None

        workspace_key = sanitize_workspace_key(identifier)
        workspace_path = (self._workspace_root / workspace_key).resolve()
        branch_name = workspace_key

        if not workspace_path.exists():
            logger.warning(
                "rebase_skipped: worktree %s does not exist", workspace_path
            )
            return None

        # Capture main HEAD before merge so we can compute diffstat later.
        try:
            old_main_sha = await self._run_git(
                "rev-parse", "main", cwd=self._repo
            )
        except (HookError, HookTimeoutError):
            old_main_sha = None

        # Step 1: Rebase the branch onto main inside the worktree.
        try:
            await self._run_git("rebase", "main", cwd=workspace_path)
        except (HookError, HookTimeoutError) as exc:
            logger.warning(
                "rebase_failed: %s — leaving branch %s as-is", exc, branch_name
            )
            # Abort rebase if in-progress to leave worktree in a clean state.
            try:
                await self._run_git("rebase", "--abort", cwd=workspace_path)
            except Exception:
                pass
            return None

        # Step 2: Fast-forward main to the rebased branch tip.
        try:
            await self._run_git(
                "merge", "--ff-only", branch_name, cwd=self._repo
            )
        except (HookError, HookTimeoutError) as exc:
            logger.warning(
                "ff_merge_failed: %s — branch %s rebased but main not updated",
                exc,
                branch_name,
            )
            return None

        # Collect merge details for the caller.
        try:
            new_main_sha = await self._run_git(
                "rev-parse", "main", cwd=self._repo
            )
        except (HookError, HookTimeoutError):
            new_main_sha = "unknown"

        diffstat = ""
        if old_main_sha:
            try:
                diffstat = await self._run_git(
                    "diff", "--stat", f"{old_main_sha}..{new_main_sha}",
                    cwd=self._repo,
                )
            except (HookError, HookTimeoutError):
                pass

        logger.info("branch %s rebased and merged into main", branch_name)
        return MergeInfo(commit_sha=new_main_sha, diffstat=diffstat)

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    async def cleanup_workspace(
        self, identifier: str, *, delete_branch: bool = False
    ) -> None:
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

        # ---- worktree mode ----
        if self._repo is not None:
            try:
                await self._run_git(
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace_path),
                    cwd=self._repo,
                )
            except Exception as exc:
                logger.warning("git worktree remove failed (ignored): %s", exc)

            if delete_branch:
                branch_name = workspace_key
                try:
                    await self._run_git(
                        "branch", "-d", branch_name, cwd=self._repo
                    )
                    logger.info("deleted branch %s", branch_name)
                except Exception as exc:
                    logger.warning(
                        "branch delete failed (ignored): %s", exc
                    )
            return

        # ---- default mode ----
        shutil.rmtree(workspace_path, ignore_errors=True)
