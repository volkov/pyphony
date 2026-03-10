from datetime import datetime, timezone

import httpx
import pytest
import respx

from pyphony.models import (
    AgentConfig,
    BlockerRef,
    CodexConfig,
    Issue,
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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(issue_id=issue.id, issue_identifier=issue.identifier),
        )

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

        from pyphony.models import RunAttempt, RunningEntry
        for i in range(2):
            issue = _make_issue(id=f"id-{i}", identifier=f"PROJ-{i}")
            orch.state.running[issue.id] = RunningEntry(
                issue=issue,
                attempt=RunAttempt(issue_id=issue.id, issue_identifier=issue.identifier),
            )

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

        from pyphony.models import RunAttempt, RunningEntry
        issue = _make_issue(id="id-0", state="Todo")
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(issue_id=issue.id, issue_identifier=issue.identifier),
        )

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
        # First call: fetch candidates; second: workflow states; third: issue update
        route = respx.post(ENDPOINT)
        route.side_effect = [
            httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-1", identifier="PROJ-1", state_name="Todo"),
                ]),
            ),
            # workflow states response
            httpx.Response(
                200,
                json={
                    "data": {
                        "projects": {
                            "nodes": [{
                                "teams": {
                                    "nodes": [{
                                        "states": {
                                            "nodes": [
                                                {"id": "state-todo", "name": "Todo"},
                                                {"id": "state-ip", "name": "In Progress"},
                                                {"id": "state-done", "name": "Done"},
                                            ]
                                        }
                                    }]
                                }
                            }]
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
    def test_normal_exit_schedules_continuation(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime.now(timezone.utc),
            ),
        )
        orch.state.claimed.add(issue.id)

        orch._on_worker_exit(issue.id, normal=True, error=None)

        assert issue.id in orch.state.retry_attempts
        retry = orch.state.retry_attempts[issue.id]
        assert retry.attempt == 1

    def test_abnormal_exit_exponential_backoff(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime.now(timezone.utc),
            ),
        )

        orch._on_worker_exit(issue.id, normal=False, error="crash")

        assert issue.id in orch.state.retry_attempts
        retry = orch.state.retry_attempts[issue.id]
        assert retry.attempt == 1
        assert retry.error == "crash"


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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime.now(timezone.utc),
            ),
        )
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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime.now(timezone.utc),
            ),
        )

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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime.now(timezone.utc),
            ),
        )

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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
        )

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
        from pyphony.models import RunAttempt, RunningEntry
        orch.state.running[issue.id] = RunningEntry(
            issue=issue,
            attempt=RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
        )

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
