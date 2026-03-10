import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pyphony.models import (
    AgentConfig,
    Issue,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
    CodexConfig,
    PollingConfig,
)
from pyphony.orchestrator import Orchestrator
from pyphony.service import _run_service, _WorkflowContext, _ProcessorGeneration, _configs_differ
from pyphony.tracker import LinearClient
from pyphony.workspace import WorkspaceManager


def _make_config(tmp_path, **overrides) -> ServiceConfig:
    defaults = dict(
        tracker=TrackerConfig(
            kind="linear",
            api_key="key",
            project_slug="slug",
            active_states=["Todo", "In Progress"],
            terminal_states=["Done", "Cancelled"],
        ),
        workspace=WorkspaceConfig(root=str(tmp_path)),
        codex=CodexConfig(command="claude"),
        agent=AgentConfig(max_concurrent_agents=3),
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


def _make_issue(id="id-1", identifier="PROJ-1", state="In Progress") -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="Test",
        state=state,
        priority=1,
        blocked_by=[],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        labels=[],
    )


def _running_entry(issue: Issue) -> RunningEntry:
    return RunningEntry(
        issue=issue,
        attempt=RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=0,
            started_at=datetime.now(timezone.utc),
            status="running",
        ),
    )


class TestServiceStartup:
    def test_missing_workflow_file_exits(self):
        args = argparse.Namespace(
            workflow_files=["/nonexistent/WORKFLOW.md"],
            workflow_file="/nonexistent/WORKFLOW.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            asyncio.run(_run_service(args))

    def test_multiple_workflow_files_arg(self):
        """Verify that args with multiple workflow files are accepted by _run_service signature."""
        args = argparse.Namespace(
            workflow_files=["/nonexistent/wf1.md", "/nonexistent/wf2.md"],
            workflow_file="/nonexistent/wf1.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            asyncio.run(_run_service(args))


class TestDrainCoordinator:
    """Tests for multi-orchestrator drain coordination (SER-52)."""

    @pytest.mark.asyncio
    async def test_drain_coordinator_waits_for_all_orchestrators(self, tmp_path):
        """When one orchestrator signals merge, drain coordinator must wait
        for ALL orchestrators to have zero running agents before setting stop_event."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        # Two orchestrators simulating two workflows
        orch_a = Orchestrator(config, tracker, ws_mgr)
        orch_b = Orchestrator(config, tracker, ws_mgr)

        merge_trigger = asyncio.Event()
        stop_event = asyncio.Event()

        for orch in (orch_a, orch_b):
            orch.exit_on_merge = True
            orch.merge_detected_event = merge_trigger

        # Orchestrator B has a running agent
        issue_b = _make_issue(id="id-b", identifier="PROJ-B")
        orch_b.state.running[issue_b.id] = _running_entry(issue_b)
        orch_b.state.claimed.add(issue_b.id)

        # Start the drain coordinator (mirrors service.py logic)
        contexts_for_test = [orch_a, orch_b]

        async def _drain_coordinator():
            await merge_trigger.wait()
            for orch in contexts_for_test:
                orch._enter_drain_mode("test_coordination")
            while True:
                total_running = sum(len(o.state.running) for o in contexts_for_test)
                if total_running == 0:
                    break
                await asyncio.sleep(0.05)
            stop_event.set()

        coord_task = asyncio.create_task(_drain_coordinator())

        # Orchestrator A completes a [DONE] issue — triggers drain + merge signal
        issue_a = _make_issue(id="id-a", identifier="PROJ-A")
        orch_a.state.running[issue_a.id] = _running_entry(issue_a)
        orch_a.state.claimed.add(issue_a.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch_a._on_worker_exit(issue_a.id, normal=True, error=None, result="[DONE]")

        # merge_trigger is set, but stop_event must NOT be set yet
        assert merge_trigger.is_set()
        await asyncio.sleep(0.1)
        assert not stop_event.is_set(), (
            "stop_event should not be set while orchestrator B still has running agents"
        )

        # Both orchestrators should be in drain mode
        assert orch_a._draining
        assert orch_b._draining

        # Orchestrator B's agent finishes
        orch_b.state.running.pop(issue_b.id)
        orch_b.state.claimed.discard(issue_b.id)

        # Now stop_event should be set within a short time
        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        assert stop_event.is_set()

        coord_task.cancel()
        try:
            await coord_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_drain_coordinator_immediate_when_all_empty(self, tmp_path):
        """If all orchestrators have zero running agents, drain completes immediately."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        orch = Orchestrator(config, tracker, ws_mgr)
        merge_trigger = asyncio.Event()
        stop_event = asyncio.Event()
        orch.exit_on_merge = True
        orch.merge_detected_event = merge_trigger

        contexts_for_test = [orch]

        async def _drain_coordinator():
            await merge_trigger.wait()
            for o in contexts_for_test:
                o._enter_drain_mode("test")
            while True:
                total_running = sum(len(o.state.running) for o in contexts_for_test)
                if total_running == 0:
                    break
                await asyncio.sleep(0.05)
            stop_event.set()

        coord_task = asyncio.create_task(_drain_coordinator())

        # Sole issue completes → drain + immediate merge
        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

        await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        assert stop_event.is_set()

        coord_task.cancel()
        try:
            await coord_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_merge_trigger_is_separate_from_stop_event(self, tmp_path):
        """Verify that merge_detected_event is NOT stop_event (the core SER-52 fix)."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        orch = Orchestrator(config, tracker, ws_mgr)
        merge_trigger = asyncio.Event()
        stop_event = asyncio.Event()

        orch.exit_on_merge = True
        orch.merge_detected_event = merge_trigger

        # Trigger drain with no running agents → immediate _signal_merge_exit
        orch._enter_drain_mode("test")

        # merge_trigger fires, but stop_event must NOT fire
        assert merge_trigger.is_set()
        assert not stop_event.is_set()


class TestRollingReplacement:
    """Tests for SER-56: rolling replacement of processors on config reload."""

    def _make_ctx(self, tmp_path) -> _WorkflowContext:
        """Create a _WorkflowContext with mock components."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        agent_runner = MagicMock()
        agent_runner._prompt_template = "original prompt"
        orch = Orchestrator(config, tracker, ws_mgr)
        return _WorkflowContext(
            workflow_path=tmp_path / "WORKFLOW.md",
            orchestrator=orch,
            tracker=tracker,
            agent_runner=agent_runner,
            workspace_mgr=ws_mgr,
        )

    def test_spawn_generation_creates_new_orchestrator(self, tmp_path):
        """Spawning a new generation creates a fresh orchestrator and drains the old one."""
        ctx = self._make_ctx(tmp_path)
        old_orch = ctx.orchestrator
        config = _make_config(tmp_path)

        gen = ctx.spawn_generation(config, "new prompt")

        assert len(ctx.generations) == 2
        assert ctx.orchestrator is gen.orchestrator
        assert ctx.orchestrator is not old_orch
        assert old_orch._draining
        assert old_orch._drain_kind == "reload"
        assert gen.generation == 1

    def test_spawn_multiple_generations(self, tmp_path):
        """Multiple rapid config changes create multiple draining generations."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        gen1 = ctx.spawn_generation(config, "prompt v2")
        gen2 = ctx.spawn_generation(config, "prompt v3")

        assert len(ctx.generations) == 3
        assert ctx.generations[0].orchestrator._draining
        assert ctx.generations[1].orchestrator._draining
        assert not ctx.generations[2].orchestrator._draining
        assert ctx.orchestrator is gen2.orchestrator

    def test_reap_drained_removes_finished_generations(self, tmp_path):
        """Fully drained generations are removed by reap_drained()."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Spawn new gen — old one enters drain
        ctx.spawn_generation(config, "new prompt")
        old_orch = ctx.generations[0].orchestrator

        # Old orchestrator has no running agents → is_fully_drained
        assert old_orch.is_fully_drained

        reaped = ctx.reap_drained()
        assert len(reaped) == 1
        assert reaped[0].orchestrator is old_orch
        assert len(ctx.generations) == 1

    def test_reap_does_not_remove_active_generation(self, tmp_path):
        """The active (newest) generation is never reaped."""
        ctx = self._make_ctx(tmp_path)
        assert len(ctx.generations) == 1
        reaped = ctx.reap_drained()
        assert len(reaped) == 0
        assert len(ctx.generations) == 1

    def test_reap_keeps_generation_with_running_agents(self, tmp_path):
        """Draining generations with running agents are not reaped."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Add running agent to current generation
        issue = _make_issue()
        ctx.orchestrator.state.running[issue.id] = _running_entry(issue)

        # Spawn new gen — old one drains but has running agent
        ctx.spawn_generation(config, "new prompt")
        old_orch = ctx.generations[0].orchestrator
        assert not old_orch.is_fully_drained

        reaped = ctx.reap_drained()
        assert len(reaped) == 0
        assert len(ctx.generations) == 2

    def test_draining_issue_ids_excludes_active(self, tmp_path):
        """_draining_issue_ids returns IDs from draining gens only."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Add running agent to current generation
        issue_old = _make_issue(id="old-1")
        ctx.orchestrator.state.running[issue_old.id] = _running_entry(issue_old)

        # Spawn new gen
        ctx.spawn_generation(config, "new prompt")

        # Add running agent to active (new) generation
        issue_new = _make_issue(id="new-1")
        ctx.orchestrator.state.running[issue_new.id] = _running_entry(issue_new)

        draining_ids = ctx._draining_issue_ids()
        assert "old-1" in draining_ids
        assert "new-1" not in draining_ids

    def test_peer_running_count_sums_draining(self, tmp_path):
        """_peer_running_count returns sum of agents on draining orchestrators."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Add running agents to current generation
        for i in range(2):
            issue = _make_issue(id=f"old-{i}")
            ctx.orchestrator.state.running[issue.id] = _running_entry(issue)

        # Spawn new gen
        ctx.spawn_generation(config, "new prompt")

        assert ctx._peer_running_count() == 2

    def test_new_orchestrator_excludes_draining_issues(self, tmp_path):
        """New orchestrator skips issues handled by draining orchestrators."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Add running agent to current generation
        issue = _make_issue(id="drain-1", state="Todo")
        ctx.orchestrator.state.running[issue.id] = _running_entry(issue)

        # Spawn new gen
        ctx.spawn_generation(config, "new prompt")
        new_orch = ctx.orchestrator

        # New orchestrator should reject issue that's in drain
        assert not new_orch._is_dispatch_eligible(issue)

    def test_new_orchestrator_accepts_fresh_issues(self, tmp_path):
        """New orchestrator accepts issues not handled by draining orchestrators."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        # Add a running agent to old gen
        old_issue = _make_issue(id="drain-1", state="Todo")
        ctx.orchestrator.state.running[old_issue.id] = _running_entry(old_issue)

        # Spawn new gen
        ctx.spawn_generation(config, "new prompt")
        new_orch = ctx.orchestrator

        # Fresh issue should be eligible
        fresh_issue = _make_issue(id="fresh-1", state="Todo")
        assert new_orch._is_dispatch_eligible(fresh_issue)

    def test_available_slots_accounts_for_peer_running(self, tmp_path):
        """New orchestrator counts draining agents against max_concurrent_agents."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path, agent=AgentConfig(max_concurrent_agents=3))

        # Fill old gen with 2 running agents
        for i in range(2):
            issue = _make_issue(id=f"old-{i}", state="Todo")
            ctx.orchestrator.state.running[issue.id] = _running_entry(issue)

        # Spawn new gen
        ctx.spawn_generation(config, "new prompt")
        new_orch = ctx.orchestrator

        # 3 max - 2 peer running = 1 available
        assert new_orch._available_slots("Todo") == 1

    def test_configs_differ_detects_change(self, tmp_path):
        """_configs_differ returns True when configs differ."""
        config_a = _make_config(tmp_path)
        config_b = _make_config(tmp_path, agent=AgentConfig(max_concurrent_agents=5))
        assert _configs_differ(config_a, config_b)

    def test_configs_differ_same_config(self, tmp_path):
        """_configs_differ returns False for identical configs."""
        config_a = _make_config(tmp_path)
        config_b = _make_config(tmp_path)
        assert not _configs_differ(config_a, config_b)

    @pytest.mark.asyncio
    async def test_drain_complete_event_fires_on_reload_drain(self, tmp_path):
        """When an orchestrator is drained for reload, drain_complete_event fires."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        orch = Orchestrator(config, tracker, ws_mgr)

        # Add a running agent
        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        # Enter reload drain
        orch._enter_drain_mode("test_reload", kind="reload")
        assert orch._draining
        assert orch._drain_kind == "reload"
        assert not orch.drain_complete_event.is_set()

        # Simulate agent completion
        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="done")

        # drain_complete_event should fire
        assert orch.drain_complete_event.is_set()
        # merge_detected should NOT fire (this is reload, not merge)
        assert not orch.merge_detected

    @pytest.mark.asyncio
    async def test_reload_drain_does_not_trigger_merge(self, tmp_path):
        """Reload drain with no running agents fires drain_complete but not merge."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        orch = Orchestrator(config, tracker, ws_mgr)
        merge_trigger = asyncio.Event()
        orch.merge_detected_event = merge_trigger

        orch._enter_drain_mode("test_reload", kind="reload")

        assert orch.drain_complete_event.is_set()
        assert not merge_trigger.is_set()
        assert not orch.merge_detected

    def test_all_orchestrators_includes_all_generations(self, tmp_path):
        """all_orchestrators returns orchestrators from all generations."""
        ctx = self._make_ctx(tmp_path)
        config = _make_config(tmp_path)

        first_orch = ctx.orchestrator
        ctx.spawn_generation(config, "v2")
        second_orch = ctx.orchestrator

        all_orchs = ctx.all_orchestrators
        assert len(all_orchs) == 2
        assert first_orch in all_orchs
        assert second_orch in all_orchs
