"""Tests for AgentRunner using claude-agent-sdk."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import pyphony.agent

from claude_agent_sdk import (
    CLINotFoundError,
    ClaudeSDKError,
    ProcessError,
    ResultMessage,
)

from pyphony.models import (
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    Issue,
    RunAttempt,
    ServiceConfig,
    WorkspaceConfig,
)
from pyphony.agent import AgentRunner
from pyphony.workspace import WorkspaceManager


def _result_message(is_error=False, result="Done"):
    return ResultMessage(
        subtype="result",
        duration_ms=100,
        duration_api_ms=90,
        is_error=is_error,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.0,
        usage={},
        result=result,
    )


@pytest.fixture
def issue():
    return Issue(
        id="issue-1",
        identifier="TEST-1",
        title="Fix the bug",
        description="Something is broken",
        state="Todo",
    )


@pytest.fixture
def service_config(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    return ServiceConfig(
        workspace=WorkspaceConfig(root=str(root)),
        hooks=HooksConfig(),
        agent=AgentConfig(max_turns=3),
        claude=ClaudeConfig(
            command="claude",
            turn_timeout_ms=10000,
        ),
    )


def _make_runner(service_config):
    workspace_mgr = WorkspaceManager(service_config)
    return AgentRunner(
        config=service_config,
        workspace_mgr=workspace_mgr,
        prompt_template="Fix issue {{ issue.identifier }}: {{ issue.title }}",
    )


class TestAgentRunner:

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_successful_run(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1)

        assert isinstance(result, RunAttempt)
        assert result.status == "completed"
        assert result.issue_id == "issue-1"
        assert result.issue_identifier == "TEST-1"
        assert result.attempt == 1
        assert result.workspace_path != ""
        assert result.result == "Done"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_result_captured_on_error(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            yield _result_message(is_error=True, result="Something went wrong")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1)

        assert result.status == "failed"
        assert result.result == "Something went wrong"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_error_run(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            yield _result_message(is_error=True, result="Something went wrong")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1)

        assert result.status == "failed"
        assert result.error == "Something went wrong"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_timeout(self, mock_query, service_config, issue):
        service_config.claude.turn_timeout_ms = 100

        async def fake_query(**kwargs):
            await asyncio.sleep(10)
            yield  # never reached

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue)

        assert result.status == "failed"
        assert result.error == "turn_timeout"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_cli_not_found(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            raise CLINotFoundError("claude not found")
            yield  # make it a generator

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue)

        assert result.status == "failed"
        assert result.error == "cli_not_found"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_process_error(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            raise ProcessError("process crashed")
            yield  # make it a generator

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue)

        assert result.status == "failed"
        assert result.error == "port_exit"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_sdk_error(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            raise ClaudeSDKError("sdk error")
            yield  # make it a generator

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue)

        assert result.status == "failed"
        assert "sdk error" in result.error

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_hooks_still_run(self, mock_query, service_config, issue):
        hook_log = []

        async def fake_query(**kwargs):
            yield _result_message()

        mock_query.side_effect = fake_query

        workspace_mgr = WorkspaceManager(service_config)
        original_before = workspace_mgr.run_before_run
        original_after = workspace_mgr.run_after_run

        async def track_before(path):
            hook_log.append("before_run")
            return await original_before(path)

        async def track_after(path):
            hook_log.append("after_run")
            return await original_after(path)

        workspace_mgr.run_before_run = track_before
        workspace_mgr.run_after_run = track_after

        runner = AgentRunner(
            config=service_config,
            workspace_mgr=workspace_mgr,
            prompt_template="Fix it",
        )
        result = await runner.run(issue)

        assert result.status == "completed"
        assert "before_run" in hook_log
        assert "after_run" in hook_log

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_empty_template(self, mock_query, service_config, issue):
        async def fake_query(**kwargs):
            yield _result_message()

        mock_query.side_effect = fake_query
        workspace_mgr = WorkspaceManager(service_config)
        runner = AgentRunner(
            config=service_config,
            workspace_mgr=workspace_mgr,
            prompt_template="",
        )
        result = await runner.run(issue)
        assert result.status == "completed"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_on_transcript_called_on_result(self, mock_query, service_config, issue):
        """on_transcript callback is called when session_id is found in ResultMessage."""
        transcript_paths = []
        workspace_paths = []

        async def on_transcript(path, workspace_path=""):
            transcript_paths.append(path)
            workspace_paths.append(workspace_path)

        async def fake_query(**kwargs):
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1, on_transcript=on_transcript)

        assert result.status == "completed"
        assert len(transcript_paths) == 1
        assert "s1" in transcript_paths[0]  # session_id from _result_message
        assert workspace_paths[0]  # workspace_path should be set

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_on_transcript_called_once_for_early_session_id(self, mock_query, service_config, issue):
        """on_transcript is called only once even if multiple messages have session_id."""
        from claude_agent_sdk import SystemMessage
        transcript_paths = []

        async def on_transcript(path, workspace_path=""):
            transcript_paths.append(path)

        async def fake_query(**kwargs):
            yield SystemMessage(subtype="init", data={"session_id": "early-sid"})
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1, on_transcript=on_transcript)

        assert result.status == "completed"
        assert len(transcript_paths) == 1
        assert "early-sid" in transcript_paths[0]

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_query_receives_correct_options(self, mock_query, service_config, issue):
        captured_kwargs = {}

        async def fake_query(**kwargs):
            captured_kwargs.update(kwargs)
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        await runner.run(issue)

        assert "prompt" in captured_kwargs
        prompt_value = captured_kwargs["prompt"]
        assert isinstance(prompt_value, str)
        assert "TEST-1" in prompt_value
        options = captured_kwargs["options"]
        assert options.permission_mode == "bypassPermissions"
        assert options.max_turns == 3  # from agent config
        assert options.cli_path is None  # "claude" maps to None

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_plan_text_extracted_from_plan_file(self, mock_query, service_config, issue, tmp_path):
        """For plan-required issues, plan_text is extracted from new plan files."""
        issue.labels = ["plan required"]

        plan_content = "## Detailed Plan\n\n1. Modify file A\n2. Modify file B\n3. Add tests"

        async def fake_query(**kwargs):
            # Simulate plan file being written during agent run
            plans_dir = pyphony.agent._plans_dir()
            os.makedirs(plans_dir, exist_ok=True)
            Path(plans_dir, "test-plan.md").write_text(plan_content)
            yield _result_message(result="Short summary [DONE]")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=0)

        assert result.status == "completed"
        assert result.plan_text == plan_content
        assert result.result == "Short summary [DONE]"

        # Clean up
        plans_dir = pyphony.agent._plans_dir()
        plan_file = os.path.join(plans_dir, "test-plan.md")
        if os.path.exists(plan_file):
            os.remove(plan_file)

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_plan_text_extracted_from_transcript(self, mock_query, service_config, issue, tmp_path):
        """For plan-required issues, plan_text is extracted from transcript when no plan file."""
        issue.labels = ["plan required"]

        async def fake_query(**kwargs):
            yield _result_message(result="Short summary [DONE]")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)

        # We need to write a fake transcript file after the agent sets the path
        with patch("pyphony.agent._extract_plan_from_transcript") as mock_extract:
            mock_extract.return_value = "## Full plan from transcript\n\n1. Do X\n2. Do Y"
            result = await runner.run(issue, attempt=0)

        assert result.status == "completed"
        assert result.plan_text == "## Full plan from transcript\n\n1. Do X\n2. Do Y"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_research_text_extracted_from_transcript(self, mock_query, service_config, issue):
        """For research issues, plan_text is extracted from transcript."""
        issue.labels = ["research"]

        async def fake_query(**kwargs):
            yield _result_message(result="Short summary [DONE]")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)

        with patch("pyphony.agent._extract_plan_from_transcript") as mock_extract:
            mock_extract.return_value = "## Research Findings\n\n1. Finding A\n2. Finding B"
            result = await runner.run(issue, attempt=0)

        assert result.status == "completed"
        assert result.plan_text == "## Research Findings\n\n1. Finding A\n2. Finding B"

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_research_uses_read_only_tools(self, mock_query, service_config, issue):
        """Research issues should be restricted to read-only tools."""
        issue.labels = ["research"]
        captured_kwargs = {}

        async def fake_query(**kwargs):
            captured_kwargs.update(kwargs)
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        await runner.run(issue)

        options = captured_kwargs["options"]
        assert options.permission_mode == "plan"
        # Should not have Write, Edit, or Bash in allowed_tools
        allowed = options.allowed_tools or []
        assert "Write" not in allowed
        assert "Edit" not in allowed

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_no_plan_text_for_regular_issues(self, mock_query, service_config, issue):
        """For non-plan-required issues, plan_text should remain None."""
        issue.labels = []

        async def fake_query(**kwargs):
            yield _result_message(result="Done [DONE]")

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=0)

        assert result.status == "completed"
        assert result.plan_text is None


class TestExtractPlanFromTranscript:
    """Unit tests for _extract_plan_from_transcript."""

    def test_extracts_exit_plan_mode_input(self, tmp_path):
        """ExitPlanMode tool-use input is extracted as plan text."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me analyze the code..."},
                        {
                            "type": "tool_use",
                            "name": "ExitPlanMode",
                            "input": {"plan": "## Plan\n\n1. Change file A\n2. Change file B\n3. Add tests"},
                        },
                    ]
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Done [DONE]"},
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result == "## Plan\n\n1. Change file A\n2. Change file B\n3. Add tests"

    def test_extracts_top_level_exit_plan_mode(self, tmp_path):
        """ExitPlanMode at top-level tool_use entry is also extracted."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({
                "type": "tool_use",
                "name": "ExitPlanMode",
                "input": {"plan": "## Top-level plan\n\nDetailed steps here..."},
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result == "## Top-level plan\n\nDetailed steps here..."

    def test_falls_back_to_longest_assistant_text(self, tmp_path):
        """When no ExitPlanMode, falls back to longest assistant text block."""
        transcript = tmp_path / "transcript.jsonl"
        long_plan = "## Detailed plan\n\n" + "Step details. " * 30  # > 200 chars
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": long_plan},
                    ]
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Short summary [DONE]"},
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result == long_plan

    def test_ignores_short_text_blocks(self, tmp_path):
        """Short text blocks (< 200 chars) are not returned as plan."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Short summary [DONE]"},
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result is None

    def test_returns_none_for_missing_file(self):
        """Missing transcript file returns None."""
        result = pyphony.agent._extract_plan_from_transcript("/nonexistent/file.jsonl")
        assert result is None

    def test_returns_none_for_none_path(self):
        """None transcript path returns None."""
        result = pyphony.agent._extract_plan_from_transcript(None)
        assert result is None

    def test_handles_malformed_jsonl(self, tmp_path):
        """Malformed lines are skipped gracefully."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            "not valid json",
            json.dumps({
                "type": "tool_use",
                "name": "ExitPlanMode",
                "input": {"plan": "## Valid plan"},
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result == "## Valid plan"

    def test_exit_plan_mode_with_text_key(self, tmp_path):
        """ExitPlanMode input with 'text' key instead of 'plan'."""
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({
                "type": "tool_use",
                "name": "ExitPlanMode",
                "input": {"text": "## Plan via text key"},
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = pyphony.agent._extract_plan_from_transcript(str(transcript))
        assert result == "## Plan via text key"


class TestReadNewPlanFile:
    """Unit tests for _read_new_plan_file and _snapshot_plan_files."""

    def test_detects_new_plan_file(self, tmp_path):
        """New plan file created after snapshot is read."""
        plans_dir = str(tmp_path / "plans")
        os.makedirs(plans_dir)

        # Snapshot empty dir
        before = pyphony.agent._snapshot_plan_files(plans_dir)
        assert before == set()

        # Create a plan file
        plan_path = os.path.join(plans_dir, "my-plan.md")
        Path(plan_path).write_text("## New Plan\n\nDetails here")

        result = pyphony.agent._read_new_plan_file(plans_dir, before)
        assert result == "## New Plan\n\nDetails here"

    def test_ignores_preexisting_files(self, tmp_path):
        """Files that existed before snapshot are ignored."""
        plans_dir = str(tmp_path / "plans")
        os.makedirs(plans_dir)
        Path(os.path.join(plans_dir, "old-plan.md")).write_text("Old plan")

        before = pyphony.agent._snapshot_plan_files(plans_dir)
        assert "old-plan.md" in before

        result = pyphony.agent._read_new_plan_file(plans_dir, before)
        assert result is None

    def test_returns_none_for_nonexistent_dir(self):
        """Non-existent directory returns empty snapshot and None read."""
        before = pyphony.agent._snapshot_plan_files("/nonexistent/dir")
        assert before == set()

        result = pyphony.agent._read_new_plan_file("/nonexistent/dir", before)
        assert result is None

    def test_picks_newest_file(self, tmp_path):
        """When multiple new files exist, the newest by mtime is read."""
        import time

        plans_dir = str(tmp_path / "plans")
        os.makedirs(plans_dir)

        before = pyphony.agent._snapshot_plan_files(plans_dir)

        Path(os.path.join(plans_dir, "plan-1.md")).write_text("First plan")
        time.sleep(0.05)  # Ensure different mtime
        Path(os.path.join(plans_dir, "plan-2.md")).write_text("Second plan (newest)")

        result = pyphony.agent._read_new_plan_file(plans_dir, before)
        assert result == "Second plan (newest)"
