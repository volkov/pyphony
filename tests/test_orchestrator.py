from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from pyphony.models import (
    AgentConfig,
    BlockerRef,
    CodexConfig,
    Issue,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from pyphony.orchestrator import Orchestrator
from pyphony.tracker import LinearClient
from pyphony.workspace import WorkspaceManager

ENDPOINT = "https://api.linear.app/graphql"


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


def _make_issue(
    id="id-1",
    identifier="PROJ-1",
    title="Test",
    state="Todo",
    priority=1,
    blocked_by=None,
    created_at=None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        priority=priority,
        blocked_by=blocked_by or [],
        created_at=created_at or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _graphql_response(nodes, has_next_page=False, end_cursor=None):
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": has_next_page,
                    "endCursor": end_cursor,
                },
            }
        }
    }


def _issue_node(id="id-1", identifier="PROJ-1", title="Test", state_name="Todo", priority=1):
    return {
        "id": id,
        "identifier": identifier,
        "title": title,
        "description": None,
        "priority": priority,
        "state": {"name": state_name},
        "branchName": None,
        "url": None,
        "labels": {"nodes": []},
        "relations": {"nodes": []},
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-01T00:00:00Z",
    }


def _running_entry(issue, attempt=0):
    return RunningEntry(
        issue=issue,
        attempt=RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            started_at=datetime.now(timezone.utc),
            attempt=attempt,
        ),
    )


class TestDispatchEligibility:
    def test_claimed_not_re_dispatched(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.claimed.add(issue.id)

        assert not orch._is_dispatch_eligible(issue)

    def test_running_not_re_dispatched(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)

        assert not orch._is_dispatch_eligible(issue)

    def test_terminal_state_not_eligible(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(state="Done")
        assert not orch._is_dispatch_eligible(issue)

    def test_non_active_state_not_eligible(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(state="Review")
        assert not orch._is_dispatch_eligible(issue)

    def test_todo_with_nonterminal_blocker_not_eligible(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(
            state="Todo",
            blocked_by=[BlockerRef(id="b1", state="In Progress")],
        )
        assert not orch._is_dispatch_eligible(issue)

    def test_todo_with_terminal_blocker_eligible(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(
            state="Todo",
            blocked_by=[BlockerRef(id="b1", state="Done")],
        )
        assert orch._is_dispatch_eligible(issue)


class TestConcurrency:
    def test_global_concurrency_limit(self, tmp_path):
        config = _make_config(tmp_path, agent=AgentConfig(max_concurrent_agents=2))
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        for i in range(2):
            issue = _make_issue(id=f"id-{i}", identifier=f"PROJ-{i}")
            orch.state.running[issue.id] = _running_entry(issue)

        assert orch._available_slots("Todo") == 0

    def test_per_state_concurrency_limit(self, tmp_path):
        config = _make_config(
            tmp_path,
            agent=AgentConfig(
                max_concurrent_agents=10,
                max_concurrent_agents_by_state={"todo": 1},
            ),
        )
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(id="id-0", state="Todo")
        orch.state.running[issue.id] = _running_entry(issue)

        assert orch._available_slots("Todo") == 0
        assert orch._available_slots("In Progress") > 0


class TestPollTick:
    @respx.mock
    @pytest.mark.asyncio
    async def test_dispatch_priority_order(self, tmp_path):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-2", identifier="PROJ-2", priority=2),
                    _issue_node(id="id-1", identifier="PROJ-1", priority=1),
                ]),
            )
        )

        dispatched = []

        async def mock_run(issue, attempt):
            dispatched.append(issue.identifier)

        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=mock_run)

        await orch.poll_tick()
        await tracker.close()

        assert "PROJ-1" in [e.issue.identifier for e in orch.state.running.values()]
        assert "PROJ-2" in [e.issue.identifier for e in orch.state.running.values()]


class TestRetry:
    @pytest.mark.asyncio
    async def test_normal_exit_releases_claim_default_max_runs(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        await orch._on_worker_exit(issue.id, normal=True, error=None)

        assert issue.id not in orch.state.retry_attempts
        assert issue.id not in orch.state.claimed

    @pytest.mark.asyncio
    async def test_abnormal_exit_releases_claim_default_max_runs(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        await orch._on_worker_exit(issue.id, normal=False, error="crash")

        assert issue.id not in orch.state.retry_attempts
        assert issue.id not in orch.state.claimed

    @pytest.mark.asyncio
    async def test_max_runs_allows_retries(self, tmp_path):
        config = _make_config(tmp_path, agent=AgentConfig(max_concurrent_agents=3, max_runs=3))
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        # First run (attempt=0) → should schedule retry
        orch.state.running[issue.id] = _running_entry(issue, attempt=0)
        orch.state.claimed.add(issue.id)
        await orch._on_worker_exit(issue.id, normal=True, error=None)
        assert issue.id in orch.state.retry_attempts
        assert orch.state.retry_attempts[issue.id].attempt == 1

        # Second run (attempt=1) → should schedule retry
        orch.state.running[issue.id] = _running_entry(issue, attempt=1)
        orch.state.retry_attempts.pop(issue.id, None)
        await orch._on_worker_exit(issue.id, normal=False, error="crash")
        assert issue.id in orch.state.retry_attempts
        assert orch.state.retry_attempts[issue.id].attempt == 2

        # Third run (attempt=2) → should release (max_runs=3 reached)
        orch.state.running[issue.id] = _running_entry(issue, attempt=2)
        orch.state.retry_attempts.pop(issue.id, None)
        await orch._on_worker_exit(issue.id, normal=True, error=None)
        assert issue.id not in orch.state.retry_attempts
        assert issue.id not in orch.state.claimed


class TestIssueTransition:
    @pytest.mark.asyncio
    async def test_done_marker_triggers_transition(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="All done [DONE]")
            mock_transition.assert_called_once_with(issue.id, "Done")

    @pytest.mark.asyncio
    async def test_no_done_marker_no_transition(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "transition_issue", new_callable=AsyncMock) as mock_transition:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="Finished work")
            mock_transition.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_run_no_transition(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "transition_issue", new_callable=AsyncMock) as mock_transition:
            await orch._on_worker_exit(issue.id, normal=False, error="crash", result="[DONE]")
            mock_transition.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_failure_does_not_crash(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "transition_issue", new_callable=AsyncMock, side_effect=Exception("API down")):
            # Should not raise
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

        assert issue.id not in orch.state.running
        assert issue.id not in orch.state.claimed


class TestReconciliation:
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_op_with_no_running(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        await orch.reconcile_running_issues()
        await tracker.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_terminal_kills_and_cleans(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        (tmp_path / "PROJ-1").mkdir()

        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [{"id": "id-1", "state": {"name": "Done"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        )

        await orch.reconcile_running_issues()
        await tracker.close()

        assert issue.id not in orch.state.running
        assert issue.id not in orch.state.claimed

    @respx.mock
    @pytest.mark.asyncio
    async def test_active_updates_snapshot(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(state="Todo")
        orch.state.running[issue.id] = _running_entry(issue)

        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [{"id": "id-1", "state": {"name": "In Progress"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        )

        await orch.reconcile_running_issues()
        await tracker.close()

        assert issue.id in orch.state.running
        assert orch.state.running[issue.id].issue.state == "In Progress"

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_failure_keeps_workers(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)

        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("fail"))

        await orch.reconcile_running_issues()
        await tracker.close()

        assert issue.id in orch.state.running


class TestStallDetection:
    @respx.mock
    @pytest.mark.asyncio
    async def test_stall_detected(self, tmp_path):
        config = _make_config(
            tmp_path,
            codex=CodexConfig(command="claude", stall_timeout_ms=1),
        )
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        entry = _running_entry(issue)
        entry.attempt.started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        orch.state.running[issue.id] = entry

        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        )

        await orch.reconcile_running_issues()
        await tracker.close()

        assert issue.id not in orch.state.running

    @respx.mock
    @pytest.mark.asyncio
    async def test_stall_disabled_when_zero(self, tmp_path):
        config = _make_config(
            tmp_path,
            codex=CodexConfig(command="claude", stall_timeout_ms=0),
        )
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        entry = _running_entry(issue)
        entry.attempt.started_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        orch.state.running[issue.id] = entry

        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [{"id": "id-1", "state": {"name": "Todo"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        )

        await orch.reconcile_running_issues()
        await tracker.close()

        assert issue.id in orch.state.running


class TestStartupCleanup:
    @respx.mock
    @pytest.mark.asyncio
    async def test_cleanup_removes_terminal_workspaces(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        (tmp_path / "PROJ-DONE").mkdir()

        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-done", identifier="PROJ-DONE", state_name="Done"),
                ]),
            )
        )

        await orch.startup_terminal_cleanup()
        await tracker.close()

        assert not (tmp_path / "PROJ-DONE").exists()

    @respx.mock
    @pytest.mark.asyncio
    async def test_cleanup_failure_continues(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("fail"))

        await orch.startup_terminal_cleanup()
        await tracker.close()
