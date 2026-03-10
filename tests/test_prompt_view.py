"""Tests for the prompt-view subcommand."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pyphony.cli import parse_args
from pyphony.models import Issue
from pyphony.prompt_view import _prompt_view


class TestPromptViewCLI:
    def test_parse_prompt_view(self):
        args = parse_args(["prompt-view", "SER-42"])
        assert args.command == "prompt-view"
        assert args.issue_identifier == "SER-42"
        assert args.workflow_file == "WORKFLOW.md"

    def test_parse_prompt_view_with_workflow(self):
        args = parse_args(["prompt-view", "SER-42", "custom.md"])
        assert args.command == "prompt-view"
        assert args.issue_identifier == "SER-42"
        assert args.workflow_files == ["custom.md"]
        assert args.workflow_file == "custom.md"


class TestPromptViewCommand:
    @pytest.fixture
    def sample_issue(self):
        return Issue(
            id="issue-id-123",
            identifier="SER-42",
            title="Prompt view subcommand",
            description="Add a subcommand to view prompts",
            state="Todo",
            labels=[],
        )

    @pytest.fixture
    def sample_issue_plan_required(self):
        return Issue(
            id="issue-id-456",
            identifier="SER-99",
            title="Plan this feature",
            description="Needs planning",
            state="Todo",
            labels=["plan required"],
        )

    @pytest.mark.asyncio
    async def test_prompt_view_renders_prompt(self, sample_issue, tmp_path, capsys):
        workflow_file = tmp_path / "WORKFLOW.md"
        workflow_file.write_text(
            "---\ntracker:\n  api_key: fake\n  project_slug: test\n---\n"
            "You are working on {{ issue.identifier }}: {{ issue.title }}\n"
        )

        args = parse_args(["prompt-view", "SER-42", str(workflow_file)])

        with patch("pyphony.prompt_view.LinearClient") as MockClient:
            client = AsyncMock()
            MockClient.return_value = client
            client.fetch_issue_by_identifier.return_value = sample_issue
            client.fetch_issue_comments.return_value = []

            await _prompt_view(args)

        captured = capsys.readouterr()
        assert "SER-42" in captured.out
        assert "Prompt view subcommand" in captured.out

    @pytest.mark.asyncio
    async def test_prompt_view_includes_comments(self, sample_issue, tmp_path, capsys):
        workflow_file = tmp_path / "WORKFLOW.md"
        workflow_file.write_text(
            "---\ntracker:\n  api_key: fake\n  project_slug: test\n---\n"
            "Work on {{ issue.identifier }}\n"
        )

        args = parse_args(["prompt-view", "SER-42", str(workflow_file)])

        comments = [
            {"body": "Please check the API", "created_at": "2025-01-01", "user": "Alice"},
        ]

        with patch("pyphony.prompt_view.LinearClient") as MockClient:
            client = AsyncMock()
            MockClient.return_value = client
            client.fetch_issue_by_identifier.return_value = sample_issue
            client.fetch_issue_comments.return_value = comments

            await _prompt_view(args)

        captured = capsys.readouterr()
        assert "Alice" in captured.out
        assert "Please check the API" in captured.out

    @pytest.mark.asyncio
    async def test_prompt_view_plan_required_label(
        self, sample_issue_plan_required, tmp_path, capsys
    ):
        workflow_file = tmp_path / "WORKFLOW.md"
        workflow_file.write_text(
            "---\ntracker:\n  api_key: fake\n  project_slug: test\n---\n"
            "Work on {{ issue.identifier }}\n"
        )

        args = parse_args(["prompt-view", "SER-99", str(workflow_file)])

        with patch("pyphony.prompt_view.LinearClient") as MockClient:
            client = AsyncMock()
            MockClient.return_value = client
            client.fetch_issue_by_identifier.return_value = sample_issue_plan_required
            client.fetch_issue_comments.return_value = []

            await _prompt_view(args)

        captured = capsys.readouterr()
        assert "plan required" in captured.out
