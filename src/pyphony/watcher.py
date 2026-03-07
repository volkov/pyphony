from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Awaitable

import structlog
from watchfiles import awatch, Change

from .workflow import load_workflow
from .config import service_config_from_workflow
from .models import ServiceConfig, WorkflowDefinition

log = structlog.stdlib.get_logger()


class WorkflowWatcher:
    def __init__(
        self,
        workflow_path: str | Path,
        on_reload: Callable[[WorkflowDefinition, ServiceConfig], Awaitable[None]] | None = None,
    ):
        self._path = Path(workflow_path)
        self._on_reload = on_reload
        self._last_good_workflow: WorkflowDefinition | None = None
        self._last_good_config: ServiceConfig | None = None
        self._task: asyncio.Task | None = None

    @property
    def last_good_config(self) -> ServiceConfig | None:
        return self._last_good_config

    def load_initial(self) -> tuple[WorkflowDefinition, ServiceConfig]:
        """Load and return initial workflow + config. Raises on error."""
        wf = load_workflow(self._path)
        config = service_config_from_workflow(wf.config, workflow_path=self._path)
        self._last_good_workflow = wf
        self._last_good_config = config
        return wf, config

    async def start(self) -> None:
        """Start watching for file changes in background."""
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _watch_loop(self) -> None:
        try:
            async for changes in awatch(self._path.parent):
                for change_type, changed_path in changes:
                    if Path(changed_path).name == self._path.name:
                        await self._handle_change()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("watcher_error", error=str(exc))

    async def _handle_change(self) -> None:
        log.info("workflow_file_changed", path=str(self._path))
        try:
            wf = load_workflow(self._path)
            config = service_config_from_workflow(wf.config, workflow_path=self._path)
            self._last_good_workflow = wf
            self._last_good_config = config
            log.info("workflow_reloaded")
            if self._on_reload:
                await self._on_reload(wf, config)
        except Exception as exc:
            log.error("workflow_reload_failed", error=str(exc), keeping="last_good")
