"""AgentRunner using claude-agent-sdk to run coding agents."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    query,
)

import structlog

from pyphony.models import (
    Issue,
    RunAttempt,
    ServiceConfig,
)
from pyphony.normalization import normalize_label
from pyphony.prompt import render_prompt
from pyphony.workspace import WorkspaceManager

# Type alias to avoid circular import with LinearClient
from typing import TYPE_CHECKING, Awaitable, Callable
if TYPE_CHECKING:
    from pyphony.tracker import LinearClient

log = structlog.stdlib.get_logger()


def _transcript_path(cwd: str, session_id: str) -> str:
    """Build the expected Claude Code transcript file path."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    sanitized = cwd.replace("/", "-").replace("_", "-")
    return os.path.join(config_dir, "projects", sanitized, f"{session_id}.jsonl")


def _plans_dir() -> str:
    """Return the path to the Claude Code plans directory."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return os.path.join(config_dir, "plans")


def _snapshot_plan_files(plans_dir: str) -> set[str]:
    """Return the set of plan file names currently in the plans directory."""
    try:
        return set(os.listdir(plans_dir))
    except OSError:
        return set()


def _read_new_plan_file(plans_dir: str, before: set[str]) -> str | None:
    """Read the newest plan file created after *before* snapshot.

    Returns the file content or ``None`` if no new file was found.
    """
    try:
        current = set(os.listdir(plans_dir))
    except OSError:
        return None

    new_files = current - before
    if not new_files:
        return None

    # Pick the newest file by mtime
    newest: str | None = None
    newest_mtime: float = 0.0
    for fname in new_files:
        fpath = os.path.join(plans_dir, fname)
        try:
            mtime = os.path.getmtime(fpath)
            if mtime > newest_mtime:
                newest = fpath
                newest_mtime = mtime
        except OSError:
            continue

    if newest is None:
        return None

    try:
        return Path(newest).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _extract_plan_from_transcript(transcript_path: str) -> str | None:
    """Parse a Claude Code transcript JSONL and extract the full plan.

    Strategy (in priority order):
    1. Find ``ExitPlanMode`` tool-use input — that contains the full plan the
       agent intended to submit (even if the tool errored).
    2. Find the longest assistant text block (excluding short [DONE] messages)
       which is likely the detailed plan output.
    """
    if not transcript_path:
        return None

    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    exit_plan_text: str | None = None
    longest_assistant_text: str = ""

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Strategy 1: ExitPlanMode tool-use input
        if entry.get("type") == "tool_use" and entry.get("name") == "ExitPlanMode":
            plan_input = entry.get("input", {})
            if isinstance(plan_input, dict):
                text = plan_input.get("plan") or plan_input.get("text") or plan_input.get("content") or ""
            elif isinstance(plan_input, str):
                text = plan_input
            else:
                text = ""
            if text and (not exit_plan_text or len(text) > len(exit_plan_text)):
                exit_plan_text = text

        # Also check nested content blocks (Claude Code transcript format)
        if entry.get("type") == "assistant" and isinstance(entry.get("message"), dict):
            for block in entry["message"].get("content", []):
                # Check tool_use blocks inside assistant messages
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "ExitPlanMode"
                ):
                    plan_input = block.get("input", {})
                    if isinstance(plan_input, dict):
                        text = plan_input.get("plan") or plan_input.get("text") or plan_input.get("content") or ""
                    elif isinstance(plan_input, str):
                        text = plan_input
                    else:
                        text = ""
                    if text and (not exit_plan_text or len(text) > len(exit_plan_text)):
                        exit_plan_text = text

                # Strategy 2: collect assistant text blocks
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) > len(longest_assistant_text):
                        longest_assistant_text = text

    if exit_plan_text:
        return exit_plan_text

    # Only use the longest assistant text if it's substantially longer than a
    # typical short summary (at least 200 chars) to avoid false positives.
    if len(longest_assistant_text) >= 200:
        return longest_assistant_text

    return None


class AgentRunner:
    """Runs an agent session for one issue in a workspace."""

    def __init__(
        self,
        config: ServiceConfig,
        workspace_mgr: WorkspaceManager,
        prompt_template: str = "",
        tracker: "LinearClient | None" = None,
    ) -> None:
        self._config = config
        self._workspace_mgr = workspace_mgr
        self._prompt_template = prompt_template
        self._tracker = tracker

    async def run(
        self,
        issue: Issue,
        attempt: int | None = None,
        on_event: object | None = None,
        on_transcript: "Callable[[str], Awaitable[None]] | None" = None,
        resume_session_id: str | None = None,
        resume_workspace_path: str | None = None,
        reply_prompt: str | None = None,
    ) -> RunAttempt:
        """Run an agent session for one issue."""
        run_attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt,
            started_at=datetime.now(timezone.utc),
            status="running",
        )

        start_mono = time.monotonic()

        log.info(
            "agent_start",
            issue_identifier=issue.identifier,
            attempt=attempt,
        )

        try:
            # 1. Create/reuse workspace
            if resume_session_id and resume_workspace_path:
                # Reuse the existing workspace for resume
                from .models import Workspace
                workspace = Workspace(
                    path=resume_workspace_path,
                    workspace_key=issue.identifier,
                    created_now=False,
                )
            else:
                workspace = await self._workspace_mgr.create_or_reuse(issue.identifier)
            run_attempt.workspace_path = workspace.path

            # 2. Run before_run hook (skip for resume — workspace already set up)
            if not resume_session_id:
                await self._workspace_mgr.run_before_run(workspace.path)

            # 3. Fetch previous comments for context (skip for resume)
            comments = None
            if self._tracker and not resume_session_id:
                try:
                    comments = await self._tracker.fetch_issue_comments(issue.id)
                except Exception as exc:
                    log.warning(
                        "fetch_comments_failed",
                        issue_identifier=issue.identifier,
                        error=str(exc),
                    )

            # 4. Build prompt
            if resume_session_id and reply_prompt:
                # For resume, the prompt is the reply text — the agent already
                # has full context from the previous session.
                prompt = reply_prompt
            else:
                prompt = render_prompt(
                    self._prompt_template, issue, attempt=attempt, comments=comments
                )

            # 5. Build SDK options (restrict tools for plan-only issues)
            claude = self._config.claude
            issue_labels_norm = [
                normalize_label(label) for label in issue.labels
            ]
            plan_required = "plan required" in issue_labels_norm
            research = "research" in issue_labels_norm
            read_only_mode = plan_required or research

            # 5a. Snapshot plan files so we can detect new ones after the run
            plans_before: set[str] = set()
            if plan_required:
                plans_before = _snapshot_plan_files(_plans_dir())

            # 5b. Open stderr log file
            stderr_path = os.path.join(
                workspace.path, f".claude-stderr-{attempt or 0}.log"
            )
            stderr_file = open(stderr_path, "w")

            try:
                # For "plan required" and "research" issues, use plan permission
                # mode but keep all tools available (plan mode itself restricts
                # write operations via Claude Code's built-in guardrails).
                if read_only_mode:
                    effective_allowed_tools = claude.allowed_tools
                    effective_permission_mode = "plan"
                else:
                    effective_allowed_tools = claude.allowed_tools
                    effective_permission_mode = claude.permission_mode

                options = ClaudeAgentOptions(
                    cwd=workspace.path,
                    permission_mode=effective_permission_mode,
                    allowed_tools=effective_allowed_tools,
                    disallowed_tools=claude.disallowed_tools if claude.disallowed_tools else None,
                    model=claude.model,
                    max_turns=claude.max_turns or self._config.agent.max_turns,
                    system_prompt=claude.system_prompt,
                    cli_path=claude.command if claude.command != "claude" else None,
                    stderr=lambda line: stderr_file.write(line + "\n"),
                    resume=resume_session_id,
                )

                log.info(
                    "agent_options",
                    issue_identifier=issue.identifier,
                    cwd=workspace.path,
                    permission_mode=claude.permission_mode,
                    allowed_tools=claude.allowed_tools,
                    disallowed_tools=claude.disallowed_tools,
                    model=claude.model,
                    max_turns=options.max_turns,
                    system_prompt_len=len(claude.system_prompt) if claude.system_prompt else 0,
                    cli_path=options.cli_path,
                    prompt_len=len(prompt),
                    resume=resume_session_id,
                )

                # 6. Run query
                # Remove CLAUDECODE to allow launching from within a Claude Code session
                os.environ.pop("CLAUDECODE", None)
                # Remove Python virtual-env variables so the spawned agent
                # doesn't inherit our venv.  Without this, `python` inside the
                # workspace resolves to the orchestrator's interpreter and
                # editable-install paths point back to the source repo instead
                # of the workspace copy.  See SER-93.
                _VENV_ENV_VARS = (
                    "VIRTUAL_ENV",
                    "UV_INTERNAL__PARENT_INTERPRETER",
                    "CONDA_DEFAULT_ENV",
                    "CONDA_PREFIX",
                    "PYTHONPATH",
                )
                _saved_env: dict[str, str] = {}
                for _var in _VENV_ENV_VARS:
                    _val = os.environ.pop(_var, None)
                    if _val is not None:
                        _saved_env[_var] = _val
                transcript_notified = False
                async with asyncio.timeout(claude.turn_timeout_ms / 1000.0):
                    async for message in query(
                        prompt=prompt,
                        options=options,
                    ):
                        # Try to extract session_id from any message as
                        # early as possible so the transcript link can be
                        # posted while the agent is still running.
                        if not transcript_notified and on_transcript:
                            sid = getattr(message, "session_id", None)
                            if not sid and isinstance(message, SystemMessage):
                                sid = message.data.get("session_id")
                            if sid:
                                tp = _transcript_path(workspace.path, sid)
                                run_attempt.transcript_path = tp
                                run_attempt.session_id = sid
                                transcript_notified = True
                                try:
                                    await on_transcript(tp, workspace.path)
                                except Exception:
                                    log.warning(
                                        "on_transcript_callback_failed",
                                        issue_identifier=issue.identifier,
                                    )

                        if isinstance(message, ResultMessage):
                            run_attempt.session_id = message.session_id
                            if not run_attempt.transcript_path:
                                run_attempt.transcript_path = _transcript_path(
                                    workspace.path, message.session_id
                                )
                            run_attempt.result = message.result
                            if message.is_error:
                                run_attempt.status = "failed"
                                run_attempt.error = message.result or "agent_error"
                            else:
                                run_attempt.status = "completed"

                # 7. Run after_run hook
                await self._workspace_mgr.run_after_run(workspace.path)

                # 8. For plan-required issues, extract the full plan text
                if plan_required:
                    plan_text = _read_new_plan_file(_plans_dir(), plans_before)
                    plan_source = "plan_file"
                    if not plan_text:
                        plan_text = _extract_plan_from_transcript(
                            run_attempt.transcript_path
                        )
                        plan_source = "transcript"
                    if plan_text:
                        run_attempt.plan_text = plan_text
                        log.info(
                            "plan_extracted",
                            issue_identifier=issue.identifier,
                            plan_len=len(plan_text),
                            source=plan_source,
                        )

                # 8b. For research issues, extract the research text from transcript
                if research:
                    research_text = _extract_plan_from_transcript(
                        run_attempt.transcript_path
                    )
                    if research_text:
                        run_attempt.plan_text = research_text
                        log.info(
                            "research_extracted",
                            issue_identifier=issue.identifier,
                            research_len=len(research_text),
                        )
            finally:
                stderr_file.close()
                # Restore virtual-env variables removed before query() (SER-93)
                for _var, _val in _saved_env.items():
                    os.environ[_var] = _val

        except TimeoutError:
            run_attempt.status = "failed"
            run_attempt.error = "turn_timeout"
        except CLINotFoundError:
            run_attempt.status = "failed"
            run_attempt.error = "cli_not_found"
        except ProcessError:
            run_attempt.status = "failed"
            run_attempt.error = "port_exit"
        except ClaudeSDKError as exc:
            run_attempt.status = "failed"
            run_attempt.error = str(exc)
        except Exception as exc:
            run_attempt.status = "failed"
            run_attempt.error = str(exc)
            log.exception("Unexpected error in agent run")

        elapsed_s = round(time.monotonic() - start_mono, 2)
        stderr_log = (
            os.path.join(run_attempt.workspace_path, f".claude-stderr-{attempt or 0}.log")
            if run_attempt.workspace_path
            else None
        )
        log.info(
            "agent_finish",
            issue_identifier=issue.identifier,
            status=run_attempt.status,
            error=run_attempt.error,
            elapsed_s=elapsed_s,
            workspace_path=run_attempt.workspace_path,
            stderr_log=stderr_log,
            transcript=run_attempt.transcript_path,
        )

        return run_attempt
