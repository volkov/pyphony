from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from pyphony.models import (
    AgentTotals,
    BlockerRef,
    CodexConfig,
    Issue,
    LiveSession,
    OrchestratorRuntimeState,
    RetryEntry,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
    TrackerConfig,
    Workspace,
    WorkflowDefinition,
)


class TestIssue:
    def test_minimal_issue(self):
        issue = Issue(id="abc", identifier="ABC-1", title="Fix bug", state="Todo")
        assert issue.id == "abc"
        assert issue.labels == []
        assert issue.blocked_by == []
        assert issue.priority is None
        assert issue.description is None

    def test_full_issue(self):
        now = datetime.now(timezone.utc)
        issue = Issue(
            id="abc",
            identifier="ABC-1",
            title="Fix bug",
            description="desc",
            priority=1,
            state="Todo",
            branch_name="feature/abc-1",
            url="https://linear.app/abc-1",
            labels=["bug", "urgent"],
            blocked_by=[BlockerRef(id="x", identifier="ABC-2", state="Todo")],
            created_at=now,
            updated_at=now,
        )
        assert issue.priority == 1
        assert len(issue.blocked_by) == 1
        assert issue.blocked_by[0].identifier == "ABC-2"

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            Issue(id="abc", identifier="ABC-1")  # type: ignore[call-arg]

    def test_serialization_roundtrip(self):
        issue = Issue(id="abc", identifier="ABC-1", title="Fix", state="Todo")
        data = issue.model_dump()
        issue2 = Issue.model_validate(data)
        assert issue == issue2


class TestBlockerRef:
    def test_all_none(self):
        b = BlockerRef()
        assert b.id is None
        assert b.identifier is None
        assert b.state is None

    def test_partial(self):
        b = BlockerRef(id="x", state="Done")
        assert b.id == "x"
        assert b.identifier is None
        assert b.state == "Done"


class TestWorkflowDefinition:
    def test_defaults(self):
        w = WorkflowDefinition()
        assert w.config == {}
        assert w.prompt_template == ""

    def test_with_values(self):
        w = WorkflowDefinition(config={"tracker": {"kind": "linear"}}, prompt_template="Do work")
        assert w.config["tracker"]["kind"] == "linear"


class TestServiceConfig:
    def test_all_defaults(self):
        cfg = ServiceConfig()
        assert cfg.tracker.kind is None
        assert cfg.tracker.endpoint == "https://api.linear.app/graphql"
        assert cfg.tracker.active_states == ["Todo", "In Progress"]
        assert cfg.tracker.terminal_states == ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
        assert cfg.polling.interval_ms == 30000
        assert cfg.workspace.root is None
        assert cfg.hooks.timeout_ms == 60000
        assert cfg.agent.max_concurrent_agents == 10
        assert cfg.agent.max_turns == 100
        assert cfg.agent.max_retry_backoff_ms == 300000
        assert cfg.codex.command == "claude"
        assert cfg.codex.permission_mode == "bypassPermissions"
        assert cfg.codex.turn_timeout_ms == 3600000
        assert cfg.codex.stall_timeout_ms == 300000


class TestWorkspace:
    def test_defaults(self):
        ws = Workspace(path="/tmp/ws/ABC-1", workspace_key="ABC-1")
        assert ws.created_now is False

    def test_created_now(self):
        ws = Workspace(path="/tmp/ws/ABC-1", workspace_key="ABC-1", created_now=True)
        assert ws.created_now is True


class TestRunAttempt:
    def test_defaults(self):
        ra = RunAttempt(issue_id="abc", issue_identifier="ABC-1")
        assert ra.attempt is None
        assert ra.status == "pending"
        assert ra.error is None


class TestLiveSession:
    def test_defaults(self):
        ls = LiveSession()
        assert ls.session_id == ""
        assert ls.turn_count == 0
        assert ls.agent_total_tokens == 0


class TestRetryEntry:
    def test_creation(self):
        r = RetryEntry(issue_id="abc", identifier="ABC-1", attempt=2, due_at_ms=1000.0)
        assert r.attempt == 2


class TestOrchestratorRuntimeState:
    def test_defaults(self):
        state = OrchestratorRuntimeState()
        assert state.poll_interval_ms == 30000
        assert state.max_concurrent_agents == 10
        assert len(state.running) == 0
        assert len(state.claimed) == 0
        assert state.agent_totals.input_tokens == 0
