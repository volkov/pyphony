"""AgentRunner using claude-agent-sdk to run coding agents."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    query,
)

from pyphony.linear_tool import create_linear_tool

from pyphony.models import (
    Issue,
    RunAttempt,
    ServiceConfig,
)
from pyphony.prompt import render_prompt
from pyphony.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


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

            # 4. Build SDK options
            codex = self._config.codex
            mcp_servers = {}
            tracker = self._config.tracker
            if tracker.kind == "linear" and tracker.api_key:
                mcp_servers["linear"] = create_linear_tool(
                    endpoint=tracker.endpoint,
                    api_key=tracker.api_key,
                    http_client=httpx.AsyncClient(timeout=30.0),
                )

            options = ClaudeAgentOptions(
                cwd=workspace.path,
                permission_mode=codex.permission_mode,
                allowed_tools=codex.allowed_tools,
                disallowed_tools=codex.disallowed_tools if codex.disallowed_tools else None,
                model=codex.model,
                max_turns=codex.max_turns or self._config.agent.max_turns,
                system_prompt=codex.system_prompt,
                cli_path=codex.command if codex.command != "claude" else None,
                mcp_servers=mcp_servers or None,
            )

            # 5. Run query with timeout
            # Remove CLAUDECODE to allow launching from within a Claude Code session
            os.environ.pop("CLAUDECODE", None)
            async with asyncio.timeout(codex.turn_timeout_ms / 1000.0):
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        if message.is_error:
                            run_attempt.status = "failed"
                            run_attempt.error = message.result or "agent_error"
                        else:
                            run_attempt.status = "completed"

            # 6. Run after_run hook
            await self._workspace_mgr.run_after_run(workspace.path)

        except TimeoutError:
            run_attempt.status = "failed"
            run_attempt.error = "turn_timeout"
        except CLINotFoundError:
            run_attempt.status = "failed"
            run_attempt.error = "codex_not_found"
        except ProcessError:
            run_attempt.status = "failed"
            run_attempt.error = "port_exit"
        except ClaudeSDKError as exc:
            run_attempt.status = "failed"
            run_attempt.error = str(exc)
        except Exception as exc:
            run_attempt.status = "failed"
            run_attempt.error = str(exc)
            logger.exception("Unexpected error in agent run")

        return run_attempt
