"""Tests for the /bug-report comment command processing."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from pyphony.models import (
    AgentConfig,
    ClaudeConfig,
    Issue,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)
from pyphony.orchestrator import Orchestrator
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
        claude=ClaudeConfig(command="claude"),
        agent=AgentConfig(max_concurrent_agents=3),
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


def _make_issue(
    id="id-1",
    identifier="SER-42",
    title="Some task",
    state="Todo",
    priority=1,
    labels=None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        priority=priority,
        blocked_by=[],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        labels=labels or [],
    )


class TestBugReportRegex:
    """Test the regex matching for /bug-report commands."""

    def test_simple_bug_report(self):
        match = Orchestrator._BUG_REPORT_RE.search("/bug-report агент зацикливается")
        assert match is not None
        assert match.group(1) == "агент зацикливается"

    def test_bug_report_multiline_body(self):
        body = "Some intro text\n/bug-report the agent is stuck in a loop\nmore text"
        match = Orchestrator._BUG_REPORT_RE.search(body)
        assert match is not None
        assert match.group(1) == "the agent is stuck in a loop"

    def test_no_match_without_prefix(self):
        match = Orchestrator._BUG_REPORT_RE.search("bug-report something")
        assert match is None

    def test_no_match_empty_message(self):
        match = Orchestrator._BUG_REPORT_RE.search("/bug-report")
        assert match is None

    def test_bug_report_at_start_of_comment(self):
        match = Orchestrator._BUG_REPORT_RE.search("/bug-report test message here")
        assert match is not None
        assert match.group(1) == "test message here"


class TestProcessBugReportCommands:
    """Test _process_bug_report_commands orchestrator method."""

    @pytest.mark.asyncio
    async def test_creates_issue_on_bug_report_comment(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report агент не может найти файл",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            }
        ])
        tracker.create_issue = AsyncMock(return_value={
            "id": "new-id",
            "identifier": "SER-99",
            "title": "Bug: агент не может найти файл",
            "url": "https://linear.app/team/issue/SER-99",
        })
        tracker.replace_issue_labels = AsyncMock(return_value=True)
        tracker.comment_on_issue = AsyncMock(return_value="comment-mock-id")

        await orch._process_bug_report_commands([issue])

        # Verify issue was created with correct params
        tracker.create_issue.assert_called_once()
        call_args = tracker.create_issue.call_args
        assert call_args.kwargs["title"].startswith("Bug:")
        assert "агент не может найти файл" in call_args.kwargs["title"]
        assert call_args.kwargs["state"] == "Todo"
        assert "SER-42" in call_args.kwargs["description"]
        assert "/debug-ticket SER-42" in call_args.kwargs["description"]

        # Verify labels were added
        tracker.replace_issue_labels.assert_called_once_with(
            "new-id",
            remove_labels=[],
            add_labels=["bug", "research"],
        )

        # Verify confirmation comment was posted as a reply to the /bug-report comment
        tracker.comment_on_issue.assert_called_once()
        confirm_call = tracker.comment_on_issue.call_args
        assert confirm_call.args[0] == issue.id
        assert "SER-99" in confirm_call.args[1]
        assert confirm_call.kwargs.get("parent_comment_id") == "comment-1"

    @pytest.mark.asyncio
    async def test_skips_already_processed_comments(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        # Pre-mark comment as processed
        orch.state.processed_bug_reports.add("comment-1")

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report some problem",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            }
        ])
        tracker.create_issue = AsyncMock()

        await orch._process_bug_report_commands([issue])

        # Should not create an issue — comment already processed
        tracker.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_comments_without_bug_report(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "This is a normal comment without any command",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            }
        ])
        tracker.create_issue = AsyncMock()

        await orch._process_bug_report_commands([issue])

        tracker.create_issue.assert_not_called()
        # But comment is still marked as processed
        assert "comment-1" in orch.state.processed_bug_reports

    @pytest.mark.asyncio
    async def test_handles_fetch_comments_failure(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(
            side_effect=Exception("API error")
        )
        tracker.create_issue = AsyncMock()

        # Should not raise
        await orch._process_bug_report_commands([issue])
        tracker.create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_create_issue_failure(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report something broke",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            }
        ])
        tracker.create_issue = AsyncMock(
            side_effect=Exception("Create failed")
        )

        # Should not raise — error is logged
        await orch._process_bug_report_commands([issue])

        # Comment is still marked as processed to avoid retrying
        assert "comment-1" in orch.state.processed_bug_reports

    @pytest.mark.asyncio
    async def test_description_contains_debug_ticket_instruction(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue(identifier="SER-77", title="Feature X")

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report tests fail after merge",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            }
        ])
        tracker.create_issue = AsyncMock(return_value={
            "id": "new-id",
            "identifier": "SER-100",
            "title": "Bug: tests fail after merge",
            "url": "https://linear.app/team/issue/SER-100",
        })
        tracker.replace_issue_labels = AsyncMock(return_value=True)
        tracker.comment_on_issue = AsyncMock(return_value="comment-mock-id")

        await orch._process_bug_report_commands([issue])

        call_args = tracker.create_issue.call_args
        desc = call_args.kwargs["description"]
        assert "/debug-ticket SER-77" in desc
        assert "SER-77" in desc
        assert "Feature X" in desc
        assert "tests fail after merge" in desc

    @pytest.mark.asyncio
    async def test_skips_bug_report_when_confirmation_comment_exists(self, tmp_path):
        """Do not create a duplicate issue if a confirmation comment already exists."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report агент зацикливается",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            },
            {
                "id": "comment-2",
                "body": "🐛 Создан баг-репорт [SER-99](https://linear.app/team/issue/SER-99): агент зацикливается",
                "created_at": "2024-01-01T00:01:00Z",
                "user": "Bot",
            },
        ])
        tracker.create_issue = AsyncMock()

        await orch._process_bug_report_commands([issue])

        # Should NOT create a new issue — confirmation already exists
        tracker.create_issue.assert_not_called()
        # Comment is still marked as processed
        assert "comment-1" in orch.state.processed_bug_reports

    @pytest.mark.asyncio
    async def test_creates_issue_when_confirmation_for_different_message(self, tmp_path):
        """Create issue when confirmation exists but for a different bug message."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report новая проблема",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Test User",
            },
            {
                "id": "comment-2",
                "body": "🐛 Создан баг-репорт [SER-99](https://linear.app/team/issue/SER-99): старая проблема",
                "created_at": "2024-01-01T00:01:00Z",
                "user": "Bot",
            },
        ])
        tracker.create_issue = AsyncMock(return_value={
            "id": "new-id",
            "identifier": "SER-100",
            "title": "Bug: новая проблема",
            "url": "https://linear.app/team/issue/SER-100",
        })
        tracker.replace_issue_labels = AsyncMock(return_value=True)
        tracker.comment_on_issue = AsyncMock(return_value="comment-mock-id")

        await orch._process_bug_report_commands([issue])

        # Should create issue for the new, unconfirmed message
        tracker.create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_duplicate_bug_reports_in_same_batch(self, tmp_path):
        """Two /bug-report comments with the same message should create only one issue."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        issue = _make_issue()

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-1",
                "body": "/bug-report одна и та же проблема",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "User A",
            },
            {
                "id": "comment-2",
                "body": "/bug-report одна и та же проблема",
                "created_at": "2024-01-01T00:01:00Z",
                "user": "User B",
            },
        ])
        tracker.create_issue = AsyncMock(return_value={
            "id": "new-id",
            "identifier": "SER-100",
            "title": "Bug: одна и та же проблема",
            "url": "https://linear.app/team/issue/SER-100",
        })
        tracker.replace_issue_labels = AsyncMock(return_value=True)
        tracker.comment_on_issue = AsyncMock(return_value="comment-mock-id")

        await orch._process_bug_report_commands([issue])

        # Should create only ONE issue, not two
        tracker.create_issue.assert_called_once()
        assert "comment-1" in orch.state.processed_bug_reports
        assert "comment-2" in orch.state.processed_bug_reports

    @pytest.mark.asyncio
    async def test_bug_report_on_backlog_issue(self, tmp_path):
        """Bug report commands on Backlog issues should be processed (SER-146)."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)
        orch = Orchestrator(config, tracker, ws_mgr)

        # Issue is in Backlog — NOT in active_states
        issue = _make_issue(
            id="id-backlog",
            identifier="SER-101",
            title="Backlog task",
            state="Backlog",
        )

        tracker.fetch_issue_comments = AsyncMock(return_value=[
            {
                "id": "comment-backlog-1",
                "body": "/bug-report агент зацикливается на этом тикете",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Sergey",
            }
        ])
        tracker.create_issue = AsyncMock(return_value={
            "id": "new-bug-id",
            "identifier": "SER-150",
            "title": "Bug: агент зацикливается на этом тикете",
            "url": "https://linear.app/team/issue/SER-150",
        })
        tracker.replace_issue_labels = AsyncMock(return_value=True)
        tracker.comment_on_issue = AsyncMock(return_value="comment-mock-id")

        # _process_bug_report_commands now receives issues in any state
        await orch._process_bug_report_commands([issue])

        # Verify issue was created even though the source issue is in Backlog
        tracker.create_issue.assert_called_once()
        call_args = tracker.create_issue.call_args
        assert "агент зацикливается на этом тикете" in call_args.kwargs["title"]
        assert "/debug-ticket SER-101" in call_args.kwargs["description"]
        assert "comment-backlog-1" in orch.state.processed_bug_reports
