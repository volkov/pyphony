from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pyphony.models import ServiceConfig, WorkflowDefinition
from pyphony.watcher import WorkflowWatcher

VALID_WORKFLOW = """\
---
tracker:
  kind: linear
  api_key: test-key
  project_slug: my-project
polling:
  interval_ms: 15000
agent:
  max_concurrent_agents: 5
claude:
  command: claude
---
You are working on {{ issue.identifier }}: {{ issue.title }}
"""

UPDATED_WORKFLOW = """\
---
tracker:
  kind: linear
  api_key: test-key
  project_slug: my-project
polling:
  interval_ms: 20000
agent:
  max_concurrent_agents: 3
claude:
  command: claude
---
Updated prompt for {{ issue.identifier }}
"""

INVALID_WORKFLOW = """\
---
: bad: yaml: [[
---
body
"""


class TestLoadInitial:
    def test_returns_valid_workflow_and_config(self, tmp_path: Path):
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(VALID_WORKFLOW)

        watcher = WorkflowWatcher(wf_path)
        wf, config = watcher.load_initial()

        assert isinstance(wf, WorkflowDefinition)
        assert isinstance(config, ServiceConfig)
        assert wf.config["polling"]["interval_ms"] == 15000
        assert config.polling.interval_ms == 15000
        assert config.agent.max_concurrent_agents == 5
        assert watcher.last_good_config is config

    def test_raises_on_missing_file(self, tmp_path: Path):
        watcher = WorkflowWatcher(tmp_path / "missing.md")
        with pytest.raises(Exception):
            watcher.load_initial()


class TestFileChangeReload:
    @pytest.mark.asyncio
    async def test_file_change_triggers_reload(self, tmp_path: Path):
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(VALID_WORKFLOW)

        reload_events: list[tuple[WorkflowDefinition, ServiceConfig]] = []

        async def on_reload(wf: WorkflowDefinition, config: ServiceConfig) -> None:
            reload_events.append((wf, config))

        watcher = WorkflowWatcher(wf_path, on_reload=on_reload)
        watcher.load_initial()

        await watcher.start()
        try:
            # Give the watcher time to start
            await asyncio.sleep(0.3)

            # Modify the file
            wf_path.write_text(UPDATED_WORKFLOW)

            # Wait for the watcher to pick up the change
            await asyncio.sleep(1.0)

            assert len(reload_events) >= 1
            wf, config = reload_events[-1]
            assert config.polling.interval_ms == 20000
            assert config.agent.max_concurrent_agents == 3
            assert "Updated prompt" in wf.prompt_template
            assert watcher.last_good_config is config
        finally:
            await watcher.stop()


class TestInvalidReloadKeepsLastGood:
    @pytest.mark.asyncio
    async def test_invalid_yaml_keeps_last_good(self, tmp_path: Path):
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(VALID_WORKFLOW)

        watcher = WorkflowWatcher(wf_path)
        wf, config = watcher.load_initial()
        original_config = config

        await watcher.start()
        try:
            await asyncio.sleep(0.3)

            # Write invalid YAML
            wf_path.write_text(INVALID_WORKFLOW)

            # Wait for the watcher to process
            await asyncio.sleep(1.0)

            # Last good config should still have original values
            assert watcher.last_good_config.polling.interval_ms == 15000
            assert watcher.last_good_config.agent.max_concurrent_agents == 5
        finally:
            await watcher.stop()
