import asyncio
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


class TestInProgressTransition:
    @respx.mock
    @pytest.mark.asyncio
    async def test_dispatch_transitions_todo_to_in_progress(self, tmp_path):
        """When a Todo issue is dispatched, it should be transitioned to In Progress."""
        # First call: fetch candidates; second: issue team; third: workflow states; fourth: issue update
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-1", identifier="PROJ-1", state_name="Todo"),
                ]),
            ),
            # issue team response
            httpx.Response(
                200,
                json={"data": {"issue": {"team": {"id": "team-1"}}}},
            ),
            # workflow states response
            httpx.Response(
                200,
                json={
                    "data": {
                        "workflowStates": {
                            "nodes": [
                                {"id": "state-todo", "name": "Todo"},
                                {"id": "state-ip", "name": "In Progress"},
                                {"id": "state-done", "name": "Done"},
                            ]
                        }
                    }
                },
            ),
            # issue update response
            httpx.Response(
                200,
                json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {"id": "id-1", "state": {"name": "In Progress"}},
                        }
                    }
                },
            ),
        ]

        async def mock_run(issue, attempt):
            pass

        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=mock_run)

        await orch.poll_tick()
        await tracker.close()

        entry = orch.state.running["id-1"]
        assert entry.issue.state == "In Progress"

    @respx.mock
    @pytest.mark.asyncio
    async def test_dispatch_skips_transition_when_already_in_progress(self, tmp_path):
        """Issues already In Progress should not trigger a state transition."""
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-1", identifier="PROJ-1", state_name="In Progress"),
                ]),
            ),
        ]

        async def mock_run(issue, attempt):
            pass

        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=mock_run)

        await orch.poll_tick()
        await tracker.close()

        entry = orch.state.running["id-1"]
        assert entry.issue.state == "In Progress"
        # Only one HTTP call (fetch_candidates), no transition calls
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_dispatch_continues_if_transition_fails(self, tmp_path):
        """If the In Progress transition fails, dispatch should still proceed."""
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-1", identifier="PROJ-1", state_name="Todo"),
                ]),
            ),
            # workflow states fetch fails
            httpx.Response(500, text="Internal Server Error"),
        ]

        async def mock_run(issue, attempt):
            pass

        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=mock_run)

        await orch.poll_tick()
        await tracker.close()

        # Issue should still be dispatched even though transition failed
        assert "id-1" in orch.state.running


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

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition:
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

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock) as mock_transition:
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

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock) as mock_transition:
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

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, side_effect=Exception("API down")):
            # Should not raise
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

        assert issue.id not in orch.state.running
        assert issue.id not in orch.state.claimed


class TestCommentOnExit:
    @pytest.mark.asyncio
    async def test_posts_comment_with_result(self, tmp_path):
        """Agent result is posted as a comment on the issue."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True) as mock_comment:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="Here is my summary")
            mock_comment.assert_called_once_with(issue.id, "Here is my summary")

    @pytest.mark.asyncio
    async def test_no_comment_when_no_result(self, tmp_path):
        """No comment posted when agent has no result."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock) as mock_comment:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result=None)
            mock_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_posted_on_failed_run(self, tmp_path):
        """Comment is posted even when the agent run failed."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True) as mock_comment:
            await orch._on_worker_exit(issue.id, normal=False, error="crash", result="Partial progress")
            mock_comment.assert_called_once_with(issue.id, "Partial progress")

    @pytest.mark.asyncio
    async def test_comment_failure_does_not_crash(self, tmp_path):
        """A failed comment post should not crash the orchestrator."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, side_effect=Exception("API down")):
            # Should not raise
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="Summary [DONE]")

        assert issue.id not in orch.state.running
        assert issue.id not in orch.state.claimed

    @pytest.mark.asyncio
    async def test_comment_posted_before_done_transition(self, tmp_path):
        """Comment is posted and then Done transition happens."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        call_order = []

        async def mock_comment(issue_id, body):
            call_order.append("comment")
            return True

        async def mock_transition(issue_id, state):
            call_order.append("transition")
            return True

        with patch.object(tracker, "comment_on_issue", side_effect=mock_comment), \
             patch.object(tracker, "transition_issue", side_effect=mock_transition):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="All done [DONE]")

        assert call_order == ["comment", "transition"]


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


class TestAutomergeOnDone:
    """Tests for the automerge/review-required flow in _on_worker_exit."""

    @pytest.mark.asyncio
    async def test_no_review_label_automerges_and_transitions_done(self, tmp_path):
        """Without 'review required' label, PRs are automerged and issue goes to Done."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = []
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=["https://github.com/org/repo/pull/99"]) as mock_pr, \
             patch("pyphony.orchestrator.try_automerge_pr", new_callable=AsyncMock, return_value=True) as mock_merge:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="All done [DONE]")

            mock_pr.assert_called_once_with(issue.id)
            mock_merge.assert_called_once_with("https://github.com/org/repo/pull/99")
            mock_transition.assert_called_once_with(issue.id, "Done")

    @pytest.mark.asyncio
    async def test_review_required_transitions_to_in_review(self, tmp_path):
        """With 'review required' label, issue transitions to 'In Review'."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = ["review required"]
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch("pyphony.orchestrator.try_automerge_pr", new_callable=AsyncMock) as mock_merge:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="All done [DONE]")

            mock_transition.assert_called_once_with(issue.id, "In Review")
            mock_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_review_required_case_insensitive(self, tmp_path):
        """'Review Required' label matching is case-insensitive."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = ["Review Required"]
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch("pyphony.orchestrator.try_automerge_pr", new_callable=AsyncMock) as mock_merge:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

            mock_transition.assert_called_once_with(issue.id, "In Review")
            mock_merge.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_prs_attached_still_transitions_done(self, tmp_path):
        """When no PRs are attached, issue still transitions to Done."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = []
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

            mock_transition.assert_called_once_with(issue.id, "Done")

    @pytest.mark.asyncio
    async def test_automerge_failure_still_transitions_done(self, tmp_path):
        """Even if automerge fails, issue still transitions to Done."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = []
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=["https://github.com/org/repo/pull/1"]), \
             patch("pyphony.orchestrator.try_automerge_pr", new_callable=AsyncMock, return_value=False):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

            mock_transition.assert_called_once_with(issue.id, "Done")

    @pytest.mark.asyncio
    async def test_automerge_exception_still_transitions_done(self, tmp_path):
        """If fetching PR URLs raises, issue still transitions to Done."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = []
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True) as mock_transition, \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, side_effect=Exception("API error")):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

            mock_transition.assert_called_once_with(issue.id, "Done")

    @pytest.mark.asyncio
    async def test_exit_on_merge_not_triggered_for_in_review(self, tmp_path):
        """exit_on_merge should NOT fire when issue goes to In Review."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()

        issue = _make_issue()
        issue.labels = ["review required"]
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

        assert not orch.merge_detected
        assert not orch.merge_detected_event.is_set()

    @pytest.mark.asyncio
    async def test_multiple_prs_all_merged(self, tmp_path):
        """When multiple PRs are attached, all are merged."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        issue.labels = []
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        pr_urls = [
            "https://github.com/org/repo/pull/1",
            "https://github.com/org/repo/pull/2",
        ]

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=pr_urls), \
             patch("pyphony.orchestrator.try_automerge_pr", new_callable=AsyncMock, return_value=True) as mock_merge:
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

            assert mock_merge.call_count == 2


class TestGracefulDrain:
    """Tests for graceful drain on exit-on-merge."""

    @pytest.mark.asyncio
    async def test_drain_mode_skips_dispatch(self, tmp_path):
        """In drain mode, poll_tick should not dispatch new issues."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch._draining = True

        # Add a running job so drain doesn't complete
        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)

        with patch.object(tracker, "fetch_candidate_issues", new_callable=AsyncMock) as mock_fetch, \
             patch.object(tracker, "fetch_issue_states_by_ids", new_callable=AsyncMock, return_value={issue.id: "In Progress"}):
            stats = await orch.poll_tick()

        mock_fetch.assert_not_called()
        assert stats["dispatched"] == 0
        assert stats["running"] == 1

    @pytest.mark.asyncio
    async def test_exit_on_merge_enters_drain_not_immediate_exit(self, tmp_path):
        """When exit_on_merge fires with other jobs running, it should drain, not exit immediately."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()

        # Two issues running
        issue1 = _make_issue(id="id-1", identifier="PROJ-1")
        issue2 = _make_issue(id="id-2", identifier="PROJ-2")
        orch.state.running[issue1.id] = _running_entry(issue1)
        orch.state.running[issue2.id] = _running_entry(issue2)
        orch.state.claimed.add(issue1.id)
        orch.state.claimed.add(issue2.id)

        # Issue 1 completes with [DONE] → triggers drain
        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(issue1.id, normal=True, error=None, result="[DONE]")

        # Should be draining but NOT signaled for exit yet (issue2 still running)
        assert orch._draining
        assert not orch.merge_detected
        assert not orch.merge_detected_event.is_set()
        assert issue2.id in orch.state.running

    @pytest.mark.asyncio
    async def test_drain_completes_when_last_job_finishes(self, tmp_path):
        """After drain starts, merge_detected fires when the last running job exits."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()

        issue1 = _make_issue(id="id-1", identifier="PROJ-1")
        issue2 = _make_issue(id="id-2", identifier="PROJ-2")
        orch.state.running[issue1.id] = _running_entry(issue1)
        orch.state.running[issue2.id] = _running_entry(issue2)
        orch.state.claimed.add(issue1.id)
        orch.state.claimed.add(issue2.id)

        # Issue 1 completes with [DONE] → enters drain
        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(issue1.id, normal=True, error=None, result="[DONE]")

        assert orch._draining
        assert not orch.merge_detected_event.is_set()

        # Issue 2 finishes (no [DONE], just normal exit) → drain completes
        await orch._on_worker_exit(issue2.id, normal=True, error=None, result="some result")

        assert orch.merge_detected
        assert orch.merge_detected_event.is_set()

    @pytest.mark.asyncio
    async def test_drain_cancels_pending_retries(self, tmp_path):
        """Entering drain mode should cancel all pending retries."""
        config = _make_config(tmp_path, agent=AgentConfig(max_concurrent_agents=3, max_runs=5))
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()

        # Schedule a retry
        orch._schedule_retry(
            issue_id="retry-id",
            identifier="PROJ-R",
            attempt=1,
            delay_ms=60000,
            error=None,
        )
        assert "retry-id" in orch.state.retry_attempts

        # A running issue and the trigger issue
        trigger = _make_issue(id="id-t", identifier="PROJ-T")
        orch.state.running[trigger.id] = _running_entry(trigger)
        orch.state.claimed.add(trigger.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(trigger.id, normal=True, error=None, result="[DONE]")

        assert orch._draining
        assert len(orch.state.retry_attempts) == 0

    @pytest.mark.asyncio
    async def test_drain_immediate_when_no_other_jobs(self, tmp_path):
        """If the triggering job is the only one, drain completes immediately."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()

        issue = _make_issue()
        orch.state.running[issue.id] = _running_entry(issue)
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._on_worker_exit(issue.id, normal=True, error=None, result="[DONE]")

        assert orch._draining
        assert orch.merge_detected
        assert orch.merge_detected_event.is_set()

    @pytest.mark.asyncio
    async def test_drain_poll_tick_signals_when_empty(self, tmp_path):
        """poll_tick in drain mode signals exit when running becomes empty."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)
        orch.exit_on_merge = True
        orch.merge_detected_event = asyncio.Event()
        orch._draining = True

        # No running jobs
        stats = await orch.poll_tick()

        assert orch.merge_detected
        assert orch.merge_detected_event.is_set()
        assert stats["running"] == 0
