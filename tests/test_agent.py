"""Tests for AgentRunner using claude-agent-sdk."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from claude_agent_sdk import (
    CLINotFoundError,
    ClaudeSDKError,
    ProcessError,
    ResultMessage,
)

from pyphony.models import (
    AgentConfig,
    CodexConfig,
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
        codex=CodexConfig(
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
        service_config.codex.turn_timeout_ms = 100

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
        assert result.error == "codex_not_found"

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

        async def on_transcript(path):
            transcript_paths.append(path)

        async def fake_query(**kwargs):
            yield _result_message()

        mock_query.side_effect = fake_query
        runner = _make_runner(service_config)
        result = await runner.run(issue, attempt=1, on_transcript=on_transcript)

        assert result.status == "completed"
        assert len(transcript_paths) == 1
        assert "s1" in transcript_paths[0]  # session_id from _result_message

    @pytest.mark.asyncio
    @patch("pyphony.agent.query")
    async def test_on_transcript_called_once_for_early_session_id(self, mock_query, service_config, issue):
        """on_transcript is called only once even if multiple messages have session_id."""
        from claude_agent_sdk import SystemMessage
        transcript_paths = []

        async def on_transcript(path):
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
