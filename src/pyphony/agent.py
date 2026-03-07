"""AppServerClient and AgentRunner for the agent app-server protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

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
    Issue,
    LiveSession,
    RunAttempt,
    ServiceConfig,
)
from pyphony.prompt import render_prompt
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
)
from pyphony.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

CONTINUATION_GUIDANCE = (
    "The issue may still be open. Please continue working on it. "
    "Check the current state and take the next step."
)


class AppServerClient:
    """Manages a subprocess speaking the app-server JSON-RPC protocol over stdio."""

    def __init__(
        self,
        command: str,
        cwd: str,
        read_timeout_ms: int = 5000,
        turn_timeout_ms: int = 3600000,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._read_timeout_ms = read_timeout_ms
        self._turn_timeout_ms = turn_timeout_ms
        self._process: asyncio.subprocess.Process | None = None

    @property
    def pid(self) -> int | None:
        if self._process and self._process.pid:
            return self._process.pid
        return None

    async def start(self) -> None:
        """Launch subprocess via: sh -lc <command>."""
        self._process = await asyncio.create_subprocess_exec(
            "sh", "-lc", self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )

    async def send(self, message: dict) -> None:
        """Write JSON line to stdin."""
        if self._process is None or self._process.stdin is None:
            raise AgentProcessExit("Process not started or stdin closed")
        line = json.dumps(message) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

    async def read_line(self, timeout_ms: int | None = None) -> str:
        """Read one line from stdout with timeout."""
        if self._process is None or self._process.stdout is None:
            raise AgentProcessExit("Process not started or stdout closed")

        timeout_s = (timeout_ms or self._read_timeout_ms) / 1000.0
        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            raise ResponseTimeout(
                f"No response within {timeout_ms or self._read_timeout_ms}ms"
            )

        if not line:
            raise AgentProcessExit("Process stdout closed (EOF)")

        return line.decode(errors="replace")

    async def handshake(
        self,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        cwd: str = "",
    ) -> tuple[str, str]:
        """Perform the startup handshake: initialize, initialized, thread/start."""
        # 1. Send initialize request and wait for response
        init_req = build_initialize_request()
        await self.send(init_req)
        init_response_line = await self.read_line()
        init_response = parse_response(init_response_line)
        if init_response is None:
            raise ResponseError("Invalid response to initialize request")

        # Check for error in response
        if "error" in init_response:
            raise ResponseError(
                f"Initialize error: {init_response['error']}"
            )

        # 2. Send initialized notification (no response expected)
        await self.send(build_initialized_notification())

        # 3. Send thread/start and wait for response
        thread_req = build_thread_start_request(
            approval_policy=approval_policy,
            sandbox=sandbox,
            cwd=cwd,
        )
        await self.send(thread_req)
        thread_response_line = await self.read_line()
        thread_response = parse_response(thread_response_line)
        if thread_response is None:
            raise ResponseError("Invalid response to thread/start request")

        if "error" in thread_response:
            raise ResponseError(
                f"thread/start error: {thread_response['error']}"
            )

        thread_id = extract_thread_id(thread_response)
        if thread_id is None:
            raise ResponseError("Missing thread ID in thread/start response")

        return (thread_id, "")

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        cwd: str = "",
        title: str = "",
        approval_policy: str | None = None,
        sandbox_policy: str | None = None,
    ) -> str:
        """Send turn/start and wait for response, return turn_id."""
        turn_req = build_turn_start_request(
            thread_id=thread_id,
            prompt_text=prompt,
            cwd=cwd,
            title=title,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
        )
        await self.send(turn_req)
        turn_response_line = await self.read_line()
        turn_response = parse_response(turn_response_line)
        if turn_response is None:
            raise ResponseError("Invalid response to turn/start request")

        if "error" in turn_response:
            raise ResponseError(
                f"turn/start error: {turn_response['error']}"
            )

        turn_id = extract_turn_id(turn_response)
        if turn_id is None:
            raise ResponseError("Missing turn ID in turn/start response")

        return turn_id

    async def stream_turn(self, on_event: object | None = None) -> str:
        """Read lines until turn completes, fails, cancels, or times out.

        Returns: "completed", "failed", or "cancelled".
        """
        deadline = time.monotonic() + (self._turn_timeout_ms / 1000.0)

        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TurnTimeout(
                    f"Turn timed out after {self._turn_timeout_ms}ms"
                )

            try:
                line = await self.read_line(timeout_ms=min(remaining_ms, self._read_timeout_ms))
            except ResponseTimeout:
                # Read timeout within a turn is not fatal; check the deadline
                if time.monotonic() >= deadline:
                    raise TurnTimeout(
                        f"Turn timed out after {self._turn_timeout_ms}ms"
                    )
                continue

            msg = parse_response(line)
            if msg is None:
                continue

            # Check for turn completion
            if is_turn_completed(msg):
                return "completed"

            if is_turn_failed(msg):
                return "failed"

            if is_turn_cancelled(msg):
                return "cancelled"

            # Handle user-input-required
            if is_user_input_required(msg):
                raise TurnInputRequired("Agent requested user input")

            # Handle approval requests (auto-approve)
            method = msg.get("method", "")
            if method in (
                "item/approve",
                "item/command/approve",
                "item/approval",
            ) or "approve" in method.lower():
                request_id = msg.get("id")
                if request_id is not None:
                    response = build_approval_response(request_id, approved=True)
                    await self.send(response)
                continue

            # Handle unsupported tool calls
            if method == "item/tool/call":
                request_id = msg.get("id")
                if request_id is not None:
                    response = build_tool_error_response(request_id)
                    await self.send(response)
                continue

    async def stop(self) -> None:
        """Kill subprocess if running."""
        if self._process is not None:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            try:
                await self._process.wait()
            except Exception:
                pass
            self._process = None


class AgentRunner:
    """Runs an agent session for one issue in a workspace."""

    def __init__(
        self,
        config: ServiceConfig,
        workspace_mgr: WorkspaceManager,
        prompt_template: str = "",
    ) -> None:
        self._config = config
        self._workspace_mgr = workspace_mgr
        self._prompt_template = prompt_template

    async def run(
        self,
        issue: Issue,
        attempt: int | None = None,
        on_event: object | None = None,
    ) -> RunAttempt:
        """Run an agent session for one issue."""
        run_attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt,
            started_at=datetime.now(timezone.utc),
            status="running",
        )

        client: AppServerClient | None = None
        try:
            # 1. Create/reuse workspace
            workspace = await self._workspace_mgr.create_or_reuse(issue.identifier)
            run_attempt.workspace_path = workspace.path

            # 2. Run before_run hook
            await self._workspace_mgr.run_before_run(workspace.path)

            # 3. Build prompt from template
            prompt = render_prompt(
                self._prompt_template, issue, attempt=attempt
            )

            # 4. Start app-server client
            codex = self._config.codex
            client = AppServerClient(
                command=codex.command,
                cwd=workspace.path,
                read_timeout_ms=codex.read_timeout_ms,
                turn_timeout_ms=codex.turn_timeout_ms,
            )
            await client.start()

            # 5. Handshake
            thread_id, _ = await client.handshake(
                approval_policy=codex.approval_policy,
                sandbox=codex.thread_sandbox,
                cwd=workspace.path,
            )

            # 6. Multi-turn loop
            max_turns = self._config.agent.max_turns
            title = f"{issue.identifier}: {issue.title}"

            for turn_num in range(max_turns):
                turn_prompt = prompt if turn_num == 0 else CONTINUATION_GUIDANCE

                turn_id = await client.start_turn(
                    thread_id=thread_id,
                    prompt=turn_prompt,
                    cwd=workspace.path,
                    title=title,
                    approval_policy=codex.approval_policy,
                    sandbox_policy=codex.turn_sandbox_policy,
                )

                result = await client.stream_turn(on_event=on_event)

                if result == "completed":
                    run_attempt.status = "completed"
                    break
                elif result == "failed":
                    run_attempt.status = "failed"
                    run_attempt.error = "turn_failed"
                    break
                elif result == "cancelled":
                    run_attempt.status = "cancelled"
                    run_attempt.error = "turn_cancelled"
                    break

            # 7. Run after_run hook
            await self._workspace_mgr.run_after_run(workspace.path)

        except TurnInputRequired as exc:
            run_attempt.status = "failed"
            run_attempt.error = "turn_input_required"
        except TurnTimeout as exc:
            run_attempt.status = "failed"
            run_attempt.error = "turn_timeout"
        except TurnFailed as exc:
            run_attempt.status = "failed"
            run_attempt.error = "turn_failed"
        except TurnCancelled as exc:
            run_attempt.status = "failed"
            run_attempt.error = "turn_cancelled"
        except AgentProcessExit as exc:
            run_attempt.status = "failed"
            run_attempt.error = "port_exit"
        except ResponseTimeout as exc:
            run_attempt.status = "failed"
            run_attempt.error = "response_timeout"
        except ResponseError as exc:
            run_attempt.status = "failed"
            run_attempt.error = "response_error"
        except Exception as exc:
            run_attempt.status = "failed"
            run_attempt.error = str(exc)
            logger.exception("Unexpected error in agent run")
        finally:
            if client is not None:
                await client.stop()

        return run_attempt
