"""Tests for thread-based replies and agent session resume (SER-130)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pyphony.models import (
    AgentConfig,
    CodexConfig,
    Issue,
    LiveSession,
    OrchestratorRuntimeState,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
    ThreadSession,
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
        codex=CodexConfig(command="claude"),
        agent=AgentConfig(max_concurrent_agents=3),
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


def _make_issue(
    id="id-1",
    identifier="PROJ-1",
    title="Test",
    state="In Progress",
    labels=None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        labels=labels or [],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _running_entry(issue, attempt=0, thread_root=None):
    return RunningEntry(
        issue=issue,
        attempt=RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt,
            started_at=datetime.now(timezone.utc),
            status="running",
        ),
        thread_root_comment_id=thread_root,
    )


class TestCommentOnIssueWithParent:
    """Tests for comment_on_issue with parent_comment_id."""

    @pytest.mark.asyncio
    async def test_comment_returns_id_on_success(self):
        """comment_on_issue returns the comment ID on success."""
        import httpx
        import respx
        endpoint = "https://api.linear.app/graphql"

        with respx.mock:
            respx.post(endpoint).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "commentCreate": {
                                "success": True,
                                "comment": {"id": "new-comment-id", "body": "test"},
                            }
                        }
                    },
                )
            )

            config = ServiceConfig(tracker=TrackerConfig(
                kind="linear", api_key="key", project_slug="slug",
            ))
            client = LinearClient(config)
            try:
                result = await client.comment_on_issue("issue-1", "body text")
                assert result == "new-comment-id"
            finally:
                await client.close()

    @pytest.mark.asyncio
    async def test_comment_returns_none_on_failure(self):
        """comment_on_issue returns None on failure."""
        import httpx
        import respx
        endpoint = "https://api.linear.app/graphql"

        with respx.mock:
            respx.post(endpoint).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "commentCreate": {
                                "success": False,
                                "comment": None,
                            }
                        }
                    },
                )
            )

            config = ServiceConfig(tracker=TrackerConfig(
                kind="linear", api_key="key", project_slug="slug",
            ))
            client = LinearClient(config)
            try:
                result = await client.comment_on_issue("issue-1", "body text")
                assert result is None
            finally:
                await client.close()


class TestThreadRootTracking:
    """Tests for thread root comment ID tracking during agent runs."""

    @pytest.mark.asyncio
    async def test_transcript_comment_becomes_thread_root(self, tmp_path):
        """The first transcript comment becomes the thread root."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        async def fake_agent_fn(issue, attempt, on_transcript=None, **kwargs):
            if on_transcript:
                await on_transcript("/some/transcript.jsonl", "/workspace")
            return RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                status="completed",
                result="All done [DONE]",
                session_id="session-123",
            )

        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=fake_agent_fn)
        entry = _running_entry(issue)
        orch.state.running[issue.id] = entry
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value="root-comment-id") as mock_comment, \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._run_worker(issue, entry)

            # The first call should be the transcript comment (no parent)
            first_call = mock_comment.call_args_list[0]
            assert first_call.kwargs.get("parent_comment_id") is None

            # The second call (exit comment) should use the thread root
            if len(mock_comment.call_args_list) > 1:
                second_call = mock_comment.call_args_list[1]
                assert second_call.kwargs.get("parent_comment_id") == "root-comment-id"

    @pytest.mark.asyncio
    async def test_thread_session_saved_after_completion(self, tmp_path):
        """Thread session is saved after agent completion for future resume."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        async def fake_agent_fn(issue, attempt, on_transcript=None, **kwargs):
            if on_transcript:
                await on_transcript("/some/transcript.jsonl", "/workspace")
            return RunAttempt(
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                status="completed",
                result="All done [DONE]",
                session_id="session-456",
                workspace_path="/workspace",
            )

        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=fake_agent_fn)
        entry = _running_entry(issue)
        orch.state.running[issue.id] = entry
        orch.state.claimed.add(issue.id)

        with patch.object(tracker, "comment_on_issue", new_callable=AsyncMock, return_value="root-comment-id"), \
             patch.object(tracker, "transition_issue", new_callable=AsyncMock, return_value=True), \
             patch.object(tracker, "fetch_issue_pr_urls", new_callable=AsyncMock, return_value=[]):
            await orch._run_worker(issue, entry)

        # Check that thread session was saved
        assert "root-comment-id" in orch.state.thread_sessions
        ts = orch.state.thread_sessions["root-comment-id"]
        assert ts.session_id == "session-456"
        assert ts.issue_id == issue.id
        assert ts.thread_root_comment_id == "root-comment-id"


class TestReplyDetection:
    """Tests for /reply command detection in thread comments."""

    @pytest.mark.asyncio
    async def test_reply_detected_in_thread(self, tmp_path):
        """A /reply comment in a thread triggers agent resume."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        # Set up a saved thread session
        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=AsyncMock())
        orch.state.thread_sessions["root-comment-id"] = ThreadSession(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            session_id="session-789",
            workspace_path="/workspace",
            thread_root_comment_id="root-comment-id",
        )

        # Mock comments with a /reply in the thread
        comments = [
            {
                "id": "root-comment-id",
                "body": "Agent started.",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Bot",
                "parent_id": None,
                "children": [
                    {
                        "id": "reply-1",
                        "body": "/reply Please also check the tests",
                        "created_at": "2024-01-01T01:00:00Z",
                        "user": "Volkov Sergey",
                    },
                ],
            },
        ]

        with patch.object(tracker, "fetch_issue_comments", new_callable=AsyncMock, return_value=comments), \
             patch.object(tracker, "fetch_issue_by_identifier", new_callable=AsyncMock, return_value=issue), \
             patch.object(orch, "_dispatch_thread_resume", new_callable=AsyncMock) as mock_dispatch:
            await orch._process_thread_replies([issue])

            mock_dispatch.assert_called_once()
            call_kwargs = mock_dispatch.call_args
            assert call_kwargs.kwargs["reply_text"] == "Please also check the tests"

    @pytest.mark.asyncio
    async def test_reply_without_prefix_ignored(self, tmp_path):
        """Comments without /reply prefix are ignored."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=AsyncMock())
        orch.state.thread_sessions["root-comment-id"] = ThreadSession(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            session_id="session-789",
            workspace_path="/workspace",
            thread_root_comment_id="root-comment-id",
        )

        comments = [
            {
                "id": "root-comment-id",
                "body": "Agent started.",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Bot",
                "parent_id": None,
                "children": [
                    {
                        "id": "reply-1",
                        "body": "Just a regular comment without the prefix",
                        "created_at": "2024-01-01T01:00:00Z",
                        "user": "Volkov Sergey",
                    },
                ],
            },
        ]

        with patch.object(tracker, "fetch_issue_comments", new_callable=AsyncMock, return_value=comments), \
             patch.object(orch, "_dispatch_thread_resume", new_callable=AsyncMock) as mock_dispatch:
            await orch._process_thread_replies([issue])

            mock_dispatch.assert_not_called()
            # But the comment should be marked as processed
            ts = orch.state.thread_sessions["root-comment-id"]
            assert "reply-1" in ts.processed_reply_ids

    @pytest.mark.asyncio
    async def test_already_processed_reply_skipped(self, tmp_path):
        """Already-processed replies are not dispatched again."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=AsyncMock())
        orch.state.thread_sessions["root-comment-id"] = ThreadSession(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            session_id="session-789",
            workspace_path="/workspace",
            thread_root_comment_id="root-comment-id",
            processed_reply_ids={"reply-1"},  # Already processed
        )

        comments = [
            {
                "id": "root-comment-id",
                "body": "Agent started.",
                "created_at": "2024-01-01T00:00:00Z",
                "user": "Bot",
                "parent_id": None,
                "children": [
                    {
                        "id": "reply-1",
                        "body": "/reply This was already handled",
                        "created_at": "2024-01-01T01:00:00Z",
                        "user": "Volkov Sergey",
                    },
                ],
            },
        ]

        with patch.object(tracker, "fetch_issue_comments", new_callable=AsyncMock, return_value=comments), \
             patch.object(orch, "_dispatch_thread_resume", new_callable=AsyncMock) as mock_dispatch:
            await orch._process_thread_replies([issue])

            mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_reply_when_agent_running(self, tmp_path):
        """Don't process replies when agent is already running for the issue."""
        config = _make_config(tmp_path)
        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        issue = _make_issue()

        orch = Orchestrator(config, tracker, ws_mgr, run_agent_fn=AsyncMock())
        orch.state.thread_sessions["root-comment-id"] = ThreadSession(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            session_id="session-789",
            workspace_path="/workspace",
            thread_root_comment_id="root-comment-id",
        )
        # Mark issue as running
        orch.state.running[issue.id] = _running_entry(issue)

        with patch.object(orch, "_dispatch_thread_resume", new_callable=AsyncMock) as mock_dispatch:
            await orch._process_thread_replies([issue])
            mock_dispatch.assert_not_called()


class TestThreadSessionModel:
    """Tests for the ThreadSession model."""

    def test_thread_session_creation(self):
        ts = ThreadSession(
            issue_id="id-1",
            issue_identifier="PROJ-1",
            session_id="session-123",
            workspace_path="/workspace",
            thread_root_comment_id="comment-123",
        )
        assert ts.issue_id == "id-1"
        assert ts.processed_reply_ids == set()

    def test_thread_session_in_orchestrator_state(self):
        state = OrchestratorRuntimeState()
        state.thread_sessions["root-1"] = ThreadSession(
            issue_id="id-1",
            issue_identifier="PROJ-1",
            session_id="session-123",
            workspace_path="/workspace",
            thread_root_comment_id="root-1",
        )
        assert "root-1" in state.thread_sessions


class TestReplyRegex:
    """Tests for the /reply regex pattern."""

    def test_simple_reply(self):
        match = Orchestrator._REPLY_RE.search("/reply Please fix the tests")
        assert match is not None
        assert match.group(1).strip() == "Please fix the tests"

    def test_multiline_reply(self):
        text = "/reply Please check:\n1. Unit tests\n2. Integration tests"
        match = Orchestrator._REPLY_RE.search(text)
        assert match is not None
        assert "Unit tests" in match.group(1)

    def test_no_match_without_prefix(self):
        match = Orchestrator._REPLY_RE.search("Just a normal comment")
        assert match is None

    def test_reply_needs_content(self):
        match = Orchestrator._REPLY_RE.search("/reply")
        assert match is None
