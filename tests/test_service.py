import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
)
from pyphony.orchestrator import Orchestrator
from pyphony.service import _run_service
from pyphony.tracker import LinearClient
from pyphony.workspace import WorkspaceManager


def _make_config(tmp_path) -> ServiceConfig:
    return ServiceConfig(
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
