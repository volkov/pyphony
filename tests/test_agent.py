"""Tests for protocol message building/parsing and AppServerClient/AgentRunner."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pyphony.errors import (
    AgentProcessExit,
    ResponseError,
    ResponseTimeout,
    TurnCancelled,
    TurnFailed,
    TurnInputRequired,
    TurnTimeout,
)
from pyphony.models import (
    AgentConfig,
    CodexConfig,
    HooksConfig,
    Issue,
    LiveSession,
    RunAttempt,
    ServiceConfig,
    WorkspaceConfig,
)
from pyphony.protocol import (
    build_approval_response,
    build_initialize_request,
    build_initialized_notification,
    build_thread_start_request,
    build_tool_error_response,
    build_turn_start_request,
    extract_thread_id,
    extract_turn_id,
    is_turn_cancelled,
    is_turn_completed,
    is_turn_failed,
    is_user_input_required,
    parse_response,
    reset_id_counter,
)
from pyphony.agent import AgentRunner, AppServerClient
from pyphony.workspace import WorkspaceManager

FAKE_AGENT_PATH = str(Path(__file__).parent / "helpers" / "fake_agent.py")


@pytest.fixture(autouse=True)
def _reset_ids():
    """Reset protocol message ID counter before each test."""
    reset_id_counter()


# ---------------------------------------------------------------------------
# Protocol message building tests
# ---------------------------------------------------------------------------


class TestProtocolBuilders:

    def test_build_initialize_request(self):
        msg = build_initialize_request()
        assert msg["id"] == 1
        assert msg["method"] == "initialize"
        assert msg["params"]["clientInfo"]["name"] == "symphony"
        assert "capabilities" in msg["params"]

    def test_build_initialized_notification(self):
        msg = build_initialized_notification()
        assert msg["method"] == "initialized"
        assert "id" not in msg  # notifications have no id

    def test_build_thread_start_request(self):
        msg = build_thread_start_request(
            approval_policy="auto", sandbox="none", cwd="/workspace"
        )
        assert msg["method"] == "thread/start"
        assert msg["params"]["approvalPolicy"] == "auto"
        assert msg["params"]["sandbox"] == "none"
        assert msg["params"]["cwd"] == "/workspace"
        assert isinstance(msg["id"], int)

    def test_build_thread_start_request_minimal(self):
        msg = build_thread_start_request()
        assert msg["method"] == "thread/start"
        assert "approvalPolicy" not in msg["params"]
        assert "sandbox" not in msg["params"]

    def test_build_turn_start_request(self):
        msg = build_turn_start_request(
            thread_id="t-1",
            prompt_text="Fix the bug",
            cwd="/workspace",
            title="ABC-123: Fix",
            approval_policy="auto",
            sandbox_policy="docker",
        )
        assert msg["method"] == "turn/start"
        assert msg["params"]["threadId"] == "t-1"
        assert msg["params"]["input"] == [{"type": "text", "text": "Fix the bug"}]
        assert msg["params"]["cwd"] == "/workspace"
        assert msg["params"]["title"] == "ABC-123: Fix"
        assert msg["params"]["approvalPolicy"] == "auto"
        assert msg["params"]["sandboxPolicy"] == {"type": "docker"}

    def test_message_ids_increment(self):
        m1 = build_initialize_request()
        m2 = build_thread_start_request()
        m3 = build_turn_start_request(thread_id="t", prompt_text="p")
        assert m1["id"] == 1
        assert m2["id"] == 2
        assert m3["id"] == 3


# ---------------------------------------------------------------------------
# Response parsing tests
# ---------------------------------------------------------------------------


class TestResponseParsing:

    def test_parse_valid_json(self):
        line = '{"id": 1, "result": {"ok": true}}'
        result = parse_response(line)
        assert result is not None
        assert result["id"] == 1

    def test_parse_invalid_json(self):
        assert parse_response("not json") is None

    def test_parse_empty_line(self):
        assert parse_response("") is None
        assert parse_response("  \n") is None

    def test_extract_thread_id(self):
        resp = {"id": 2, "result": {"thread": {"id": "thread-abc"}}}
        assert extract_thread_id(resp) == "thread-abc"

    def test_extract_thread_id_missing(self):
        assert extract_thread_id({"id": 2, "result": {}}) is None
        assert extract_thread_id({}) is None

    def test_extract_turn_id(self):
        resp = {"id": 3, "result": {"turn": {"id": "turn-xyz"}}}
        assert extract_turn_id(resp) == "turn-xyz"

    def test_extract_turn_id_missing(self):
        assert extract_turn_id({"id": 3, "result": {}}) is None


# ---------------------------------------------------------------------------
# Turn completion detection tests
# ---------------------------------------------------------------------------


class TestTurnDetection:

    def test_is_turn_completed(self):
        assert is_turn_completed({"method": "turn/completed", "params": {}})
        assert not is_turn_completed({"method": "turn/failed", "params": {}})
        assert not is_turn_completed({"method": "other"})

    def test_is_turn_failed(self):
        assert is_turn_failed({"method": "turn/failed", "params": {}})
        assert not is_turn_failed({"method": "turn/completed"})

    def test_is_turn_cancelled(self):
        assert is_turn_cancelled({"method": "turn/cancelled", "params": {}})
        assert not is_turn_cancelled({"method": "turn/completed"})

    def test_is_user_input_required_method(self):
        assert is_user_input_required(
            {"method": "item/tool/requestUserInput", "params": {}}
        )

    def test_is_user_input_required_flag(self):
        assert is_user_input_required(
            {"method": "turn/status", "params": {"userInputRequired": True}}
        )

    def test_not_user_input_required(self):
        assert not is_user_input_required({"method": "turn/completed"})

    def test_build_approval_response(self):
        resp = build_approval_response(42, approved=True)
        assert resp["id"] == 42
        assert resp["result"]["approved"] is True

    def test_build_tool_error_response(self):
        resp = build_tool_error_response(99)
        assert resp["id"] == 99
        assert resp["result"]["success"] is False
        assert resp["result"]["error"] == "unsupported_tool_call"

    def test_build_tool_error_response_custom_error(self):
        resp = build_tool_error_response(99, error="custom_error")
        assert resp["result"]["error"] == "custom_error"


# ---------------------------------------------------------------------------
# AppServerClient tests with fake_agent subprocess
# ---------------------------------------------------------------------------


class TestAppServerClient:

    @pytest.fixture
    def tmp_workspace(self, tmp_path):
        return str(tmp_path)

    @pytest.mark.asyncio
    async def test_handshake_and_turn(self, tmp_workspace):
        client = AppServerClient(
            command=f"{sys.executable} {FAKE_AGENT_PATH}",
            cwd=tmp_workspace,
            read_timeout_ms=5000,
            turn_timeout_ms=10000,
        )
        try:
            await client.start()
            thread_id, _ = await client.handshake(cwd=tmp_workspace)
            assert thread_id == "test-thread-1"

            turn_id = await client.start_turn(
                thread_id=thread_id,
                prompt="Test prompt",
                cwd=tmp_workspace,
            )
            assert turn_id == "test-turn-1"

            result = await client.stream_turn()
            assert result == "completed"
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_session_id_composition(self, tmp_workspace):
        """Test that session_id = thread_id-turn_id."""
        client = AppServerClient(
            command=f"{sys.executable} {FAKE_AGENT_PATH}",
            cwd=tmp_workspace,
            read_timeout_ms=5000,
            turn_timeout_ms=10000,
        )
        try:
            await client.start()
            thread_id, _ = await client.handshake(cwd=tmp_workspace)
            turn_id = await client.start_turn(
                thread_id=thread_id,
                prompt="Test prompt",
                cwd=tmp_workspace,
            )
            session_id = f"{thread_id}-{turn_id}"
            assert session_id == "test-thread-1-test-turn-1"

            await client.stream_turn()
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_turn_timeout(self, tmp_workspace):
        """Test that a turn that never completes raises TurnTimeout."""
        # Use a script that responds to handshake but never sends turn/completed
        hang_script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'serverInfo': {'name': 'hang'}}}))\n"
            "    elif m == 'thread/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't1'}}}))\n"
            "    elif m == 'turn/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'u1'}}}))\n"
            "    sys.stdout.flush()\n"
        )
        script_path = os.path.join(tmp_workspace, "hang_agent.py")
        with open(script_path, "w") as f:
            f.write(hang_script)

        client = AppServerClient(
            command=f"{sys.executable} {script_path}",
            cwd=tmp_workspace,
            read_timeout_ms=200,
            turn_timeout_ms=500,
        )
        try:
            await client.start()
            await client.handshake(cwd=tmp_workspace)
            await client.start_turn(thread_id="t1", prompt="test", cwd=tmp_workspace)
            with pytest.raises(TurnTimeout):
                await client.stream_turn()
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_user_input_raises(self, tmp_workspace):
        """Test that user-input-required raises TurnInputRequired."""
        input_script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'serverInfo': {'name': 'inp'}}}))\n"
            "    elif m == 'thread/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't1'}}}))\n"
            "    elif m == 'turn/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'u1'}}}))\n"
            "        print(json.dumps({'method': 'item/tool/requestUserInput', 'params': {}}))\n"
            "    sys.stdout.flush()\n"
        )
        script_path = os.path.join(tmp_workspace, "input_agent.py")
        with open(script_path, "w") as f:
            f.write(input_script)

        client = AppServerClient(
            command=f"{sys.executable} {script_path}",
            cwd=tmp_workspace,
            read_timeout_ms=5000,
            turn_timeout_ms=10000,
        )
        try:
            await client.start()
            await client.handshake(cwd=tmp_workspace)
            await client.start_turn(thread_id="t1", prompt="test", cwd=tmp_workspace)
            with pytest.raises(TurnInputRequired):
                await client.stream_turn()
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_unsupported_tool_call_gets_error_response(self, tmp_workspace):
        """Test that an unsupported tool call receives an error response and the turn continues."""
        # This script sends a tool call, reads the error response, then sends turn/completed
        tool_script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'serverInfo': {'name': 'tool'}}}))\n"
            "    elif m == 'thread/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't1'}}}))\n"
            "    elif m == 'turn/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'u1'}}}))\n"
            "        print(json.dumps({'id': 'tool-1', 'method': 'item/tool/call', 'params': {'name': 'unsupported'}}))\n"
            "        sys.stdout.flush()\n"
            "        # Read the error response\n"
            "        resp = sys.stdin.readline()\n"
            "        # Now send turn/completed\n"
            "        print(json.dumps({'method': 'turn/completed', 'params': {}}))\n"
            "    sys.stdout.flush()\n"
        )
        script_path = os.path.join(tmp_workspace, "tool_agent.py")
        with open(script_path, "w") as f:
            f.write(tool_script)

        client = AppServerClient(
            command=f"{sys.executable} {script_path}",
            cwd=tmp_workspace,
            read_timeout_ms=5000,
            turn_timeout_ms=10000,
        )
        try:
            await client.start()
            await client.handshake(cwd=tmp_workspace)
            await client.start_turn(thread_id="t1", prompt="test", cwd=tmp_workspace)
            result = await client.stream_turn()
            assert result == "completed"
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_turn_failed_detection(self, tmp_workspace):
        """Test that turn/failed is detected correctly."""
        fail_script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'serverInfo': {'name': 'fail'}}}))\n"
            "    elif m == 'thread/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't1'}}}))\n"
            "    elif m == 'turn/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'u1'}}}))\n"
            "        print(json.dumps({'method': 'turn/failed', 'params': {}}))\n"
            "    sys.stdout.flush()\n"
        )
        script_path = os.path.join(tmp_workspace, "fail_agent.py")
        with open(script_path, "w") as f:
            f.write(fail_script)

        client = AppServerClient(
            command=f"{sys.executable} {script_path}",
            cwd=tmp_workspace,
            read_timeout_ms=5000,
            turn_timeout_ms=10000,
        )
        try:
            await client.start()
            await client.handshake(cwd=tmp_workspace)
            await client.start_turn(thread_id="t1", prompt="test", cwd=tmp_workspace)
            result = await client.stream_turn()
            assert result == "failed"
        finally:
            await client.stop()


# ---------------------------------------------------------------------------
# AgentRunner tests
# ---------------------------------------------------------------------------


class TestAgentRunner:

    @pytest.fixture
    def tmp_workspace_root(self, tmp_path):
        root = tmp_path / "workspaces"
        root.mkdir()
        return str(root)

    @pytest.fixture
    def issue(self):
        return Issue(
            id="issue-1",
            identifier="TEST-1",
            title="Fix the bug",
            description="Something is broken",
            state="Todo",
        )

    @pytest.fixture
    def service_config(self, tmp_workspace_root):
        return ServiceConfig(
            workspace=WorkspaceConfig(root=tmp_workspace_root),
            hooks=HooksConfig(),
            agent=AgentConfig(max_turns=3),
            codex=CodexConfig(
                command=f"{sys.executable} {FAKE_AGENT_PATH}",
                read_timeout_ms=5000,
                turn_timeout_ms=10000,
            ),
        )

    @pytest.mark.asyncio
    async def test_run_produces_completed_attempt(
        self, service_config, issue, tmp_workspace_root
    ):
        workspace_mgr = WorkspaceManager(service_config)
        runner = AgentRunner(
            config=service_config,
            workspace_mgr=workspace_mgr,
            prompt_template="Fix issue {{ issue.identifier }}: {{ issue.title }}",
        )
        result = await runner.run(issue, attempt=1)
        assert isinstance(result, RunAttempt)
        assert result.status == "completed"
        assert result.issue_id == "issue-1"
        assert result.issue_identifier == "TEST-1"
        assert result.attempt == 1
        assert result.workspace_path != ""

    @pytest.mark.asyncio
    async def test_run_with_empty_template(
        self, service_config, issue, tmp_workspace_root
    ):
        workspace_mgr = WorkspaceManager(service_config)
        runner = AgentRunner(
            config=service_config,
            workspace_mgr=workspace_mgr,
            prompt_template="",
        )
        result = await runner.run(issue)
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_run_with_failing_agent(self, issue, tmp_workspace_root):
        """Test that a failing agent produces a failed RunAttempt."""
        fail_script = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    m = msg.get('method')\n"
            "    if m == 'initialize':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'serverInfo': {'name': 'fail'}}}))\n"
            "    elif m == 'thread/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 't1'}}}))\n"
            "    elif m == 'turn/start':\n"
            "        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'u1'}}}))\n"
            "        print(json.dumps({'method': 'turn/failed', 'params': {}}))\n"
            "    sys.stdout.flush()\n"
        )
        script_dir = os.path.join(tmp_workspace_root, "_scripts")
        os.makedirs(script_dir, exist_ok=True)
        script_path = os.path.join(script_dir, "fail_agent.py")
        with open(script_path, "w") as f:
            f.write(fail_script)

        config = ServiceConfig(
            workspace=WorkspaceConfig(root=tmp_workspace_root),
            hooks=HooksConfig(),
            agent=AgentConfig(max_turns=3),
            codex=CodexConfig(
                command=f"{sys.executable} {script_path}",
                read_timeout_ms=5000,
                turn_timeout_ms=10000,
            ),
        )
        workspace_mgr = WorkspaceManager(config)
        runner = AgentRunner(
            config=config,
            workspace_mgr=workspace_mgr,
            prompt_template="Fix it",
        )
        result = await runner.run(issue, attempt=1)
        assert result.status == "failed"
        assert result.error == "turn_failed"
