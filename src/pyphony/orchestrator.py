from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone

import structlog

from .automerge import extract_pr_urls_from_transcript, try_automerge_pr

from .config import service_config_from_workflow, validate_dispatch_config
from .models import (
    AgentTotals,
    Issue,
    LiveSession,
    MergeInfo,
    OrchestratorRuntimeState,
    RetryEntry,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
    ThreadSession,
)
from .normalization import normalize_label, normalize_state, sort_issues_for_dispatch
from .prompt import render_prompt
from .tracker import LinearClient
from .workspace import WorkspaceManager

_IN_PROGRESS_STATE = "In Progress"
_WORKFLOW_ISSUE_LABEL = "workflow issue"

log = structlog.stdlib.get_logger()


def _build_transcript_url(base_url: str, transcript_path: str) -> str | None:
    """Build a claude-explorer URL from a transcript file path.

    transcript_path looks like:
      ~/.claude/projects/-Users-serg-v-symphony-workspaces-SER-31/5c0faff4-....jsonl

    Returns URL like:
      http://localhost:3939/#/session/-Users-serg-v-symphony-workspaces-SER-31/5c0faff4-...
    """
    if not transcript_path:
        return None
    project_dir = os.path.basename(os.path.dirname(transcript_path))
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]
    if not project_dir or not session_id:
        return None
    return f"{base_url.rstrip('/')}/#/session/{project_dir}/{session_id}"


def _build_transcript_comment(
    transcript_url: str,
    transcript_path: str,
    workspace_path: str = "",
) -> str:
    """Build a human-friendly comment about a running agent session.

    Includes the transcript link plus instructions on how to resume
    the session from a terminal.
    """
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]

    lines = [
        f"Agent started. [Transcript]({transcript_url})",
    ]

    # Add resume instructions when we have enough context.
    if workspace_path and session_id:
        lines.append("")
        lines.append("To resume this session in your terminal:")
        lines.append("```")
        lines.append(f"cd {workspace_path}")
        lines.append(f"claude --resume {session_id}")
        lines.append("```")

    return "\n".join(lines)


def _build_merge_comment(merge_info: MergeInfo) -> str:
    """Build a comment describing a direct merge (no PR).

    Includes the commit SHA and a per-file diffstat so reviewers can
    see what changed at a glance.
    """
    short_sha = merge_info.commit_sha[:10]
    lines = [f"Merged directly to main — commit `{short_sha}`"]

    if merge_info.diffstat:
        lines.append("")
        lines.append("```")
        lines.append(merge_info.diffstat)
        lines.append("```")

    return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: LinearClient,
        workspace_mgr: WorkspaceManager,
        run_agent_fn=None,
        *,
        prompt_template: str = "",
        excluded_issue_ids_fn: callable | None = None,
        peer_running_fn: callable | None = None,
    ) -> None:
        self._config = config
        self._tracker = tracker
        self._workspace_mgr = workspace_mgr
        self._run_agent_fn = run_agent_fn
        self._prompt_template = prompt_template
        self._excluded_issue_ids_fn = excluded_issue_ids_fn
        self._peer_running_fn = peer_running_fn
        self._state = OrchestratorRuntimeState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self.exit_on_merge: bool = False
        self.merge_detected: bool = False
        self.merge_detected_event: asyncio.Event | None = None
        self._draining: bool = False
        self._drain_kind: str | None = None  # "merge" or "reload"
        self.drain_complete_event: asyncio.Event = asyncio.Event()

    @property
    def state(self) -> OrchestratorRuntimeState:
        return self._state

    @property
    def is_fully_drained(self) -> bool:
        """True when draining and no running agents or pending retries remain."""
        return (
            self._draining
            and not self._state.running
            and not self._state.retry_attempts
        )

    @property
    def draining(self) -> bool:
        return self._draining

    def update_config(self, config: ServiceConfig) -> None:
        self._config = config
        self._state.poll_interval_ms = config.polling.interval_ms
        self._state.max_concurrent_agents = config.agent.max_concurrent_agents

    async def poll_tick(self) -> dict[str, int]:
        await self.reconcile_running_issues()

        if self._draining:
            running = len(self._state.running)
            log.info("draining", running=running)
            if running == 0:
                self._signal_drain_complete()
            return {
                "dispatched": 0,
                "running": running,
                "retrying": 0,
            }

        errors = validate_dispatch_config(self._config)
        if errors:
            for err in errors:
                log.error("dispatch_validation_failed", error=err)
            return

        try:
            issues = await self._tracker.fetch_candidate_issues()
        except Exception as exc:
            log.error("candidate_fetch_failed", error=str(exc))
            return

        # Process /bug-report commands in comments before dispatching
        await self._process_bug_report_commands(issues)

        # Process /reply commands in thread comments (resume agent sessions)
        await self._process_thread_replies(issues)

        sorted_issues = sort_issues_for_dispatch(issues)
        dispatched = 0

        for issue in sorted_issues:
            if not self._is_dispatch_eligible(issue):
                continue

            slots = self._available_slots(issue.state)
            if slots <= 0:
                continue

            await self._dispatch(issue)
            dispatched += 1

        return {
            "dispatched": dispatched,
            "running": len(self._state.running),
            "retrying": len(self._state.retry_attempts),
        }

    @staticmethod
    def _has_workflow_issue_label(issue: Issue) -> bool:
        """Return True if the issue carries the 'workflow issue' label."""
        return _WORKFLOW_ISSUE_LABEL in [
            normalize_label(l) for l in issue.labels
        ]

    def _is_dispatch_eligible(self, issue: Issue) -> bool:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False

        if issue.assignee:
            return False

        if self._has_workflow_issue_label(issue):
            return False

        active = {normalize_state(s) for s in self._config.tracker.active_states}
        terminal = {normalize_state(s) for s in self._config.tracker.terminal_states}
        issue_state = normalize_state(issue.state)

        if issue_state not in active or issue_state in terminal:
            return False

        if issue.id in self._state.running:
            return False
        if issue.id in self._state.claimed:
            return False

        # Skip issues being processed by peer (draining) orchestrators
        if self._excluded_issue_ids_fn:
            excluded = self._excluded_issue_ids_fn()
            if issue.id in excluded:
                return False

        for blocker in issue.blocked_by:
            if blocker.state and normalize_state(blocker.state) not in terminal:
                return False

        return True

    def _available_slots(self, state: str) -> int:
        running_count = len(self._state.running)
        peer_running = self._peer_running_fn() if self._peer_running_fn else 0
        total_running = running_count + peer_running
        global_available = max(self._state.max_concurrent_agents - total_running, 0)

        normalized = normalize_state(state)
        by_state = self._config.agent.max_concurrent_agents_by_state
        if normalized in by_state:
            state_limit = by_state[normalized]
            state_running = sum(
                1 for entry in self._state.running.values()
                if normalize_state(entry.issue.state) == normalized
            )
            state_available = max(state_limit - state_running, 0)
            return min(global_available, state_available)

        return global_available

    async def _attach_pr_to_issue(
        self, issue_id: str, issue_identifier: str, pr_url: str
    ) -> None:
        """Best-effort: attach a PR URL to the Linear issue as an attachment."""
        try:
            ok = await self._tracker.attach_pr_to_issue(issue_id, pr_url)
            log.info(
                "pr_attached_to_issue",
                issue_identifier=issue_identifier,
                pr_url=pr_url,
                success=ok,
            )
        except Exception as exc:
            log.warning(
                "pr_attach_failed",
                issue_identifier=issue_identifier,
                pr_url=pr_url,
                error=str(exc),
            )

    async def _dispatch(self, issue: Issue, retry_attempt: int = 0) -> None:
        self._state.claimed.add(issue.id)

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=retry_attempt,
            started_at=datetime.now(timezone.utc),
            status="running",
        )

        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
        )

        self._state.running[issue.id] = entry

        # Transition issue to "In Progress" in Linear if not already
        if normalize_state(issue.state) != normalize_state(_IN_PROGRESS_STATE):
            try:
                await self._tracker.transition_issue(
                    issue.id, _IN_PROGRESS_STATE
                )
                issue.state = _IN_PROGRESS_STATE
            except Exception as exc:
                log.warning(
                    "in_progress_transition_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

        reason = "retry" if retry_attempt > 0 else "new_issue"
        log.info(
            "dispatch",
            issue_identifier=issue.identifier,
            issue_state=issue.state,
            attempt=retry_attempt,
            reason=reason,
        )

        # Check for interactive label — handle without launching agent
        issue_labels_norm = [normalize_label(l) for l in issue.labels]
        if "interactive" in issue_labels_norm:
            await self._handle_interactive_issue(issue, entry)
            return

        if self._run_agent_fn:
            task = asyncio.create_task(
                self._run_worker(issue, entry)
            )
            entry.worker_task = task

    async def _handle_interactive_issue(self, issue: Issue, entry: RunningEntry) -> None:
        """Handle an issue tagged with 'interactive' label.

        Prepares the workspace and prompt but does NOT launch an agent.
        Instead, posts a comment with instructions for the user to run
        Claude Code manually, swaps labels, and transitions to In Review.
        """
        try:
            # 1. Create/reuse workspace
            workspace = await self._workspace_mgr.create_or_reuse(issue.identifier)
            workspace_path = workspace.path

            # 2. Run before_run hook
            await self._workspace_mgr.run_before_run(workspace_path)

            # 3. Fetch comments and render prompt
            comments = None
            try:
                comments = await self._tracker.fetch_issue_comments(issue.id)
            except Exception as exc:
                log.warning(
                    "interactive_fetch_comments_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

            prompt = render_prompt(
                self._prompt_template, issue, attempt=entry.attempt.attempt,
                comments=comments,
            )

            # 4. Post comment with instructions
            pyphony_url = f"pyphony://{issue.identifier}/work?interactive=true"
            comment_body = (
                "🖥️ Interactive task — открой в один клик:\n\n"
                f"👉 [{pyphony_url}]({pyphony_url})\n\n"
                "Или запусти вручную:\n"
                "```\n"
                f"cd {workspace_path}\n"
                "claude\n"
                "```\n\n"
                "Промпт для задачи:\n"
                f"{prompt}"
            )
            try:
                await self._tracker.comment_on_issue(issue.id, comment_body)
                log.info(
                    "interactive_comment_posted",
                    issue_identifier=issue.identifier,
                )
            except Exception as exc:
                log.warning(
                    "interactive_comment_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

            # 5. Swap labels: remove 'interactive', add 'interactive-ready'
            try:
                await self._tracker.replace_issue_labels(
                    issue.id,
                    remove_labels=["interactive"],
                    add_labels=["interactive-ready"],
                )
                log.info(
                    "interactive_labels_swapped",
                    issue_identifier=issue.identifier,
                )
            except Exception as exc:
                log.warning(
                    "interactive_label_swap_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

            # 6. Transition to In Review
            try:
                await self._tracker.transition_issue(issue.id, "In Review")
                log.info(
                    "interactive_transitioned",
                    issue_identifier=issue.identifier,
                )
            except Exception as exc:
                log.warning(
                    "interactive_transition_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

        except Exception as exc:
            log.error(
                "interactive_handling_failed",
                issue_identifier=issue.identifier,
                error=str(exc),
            )
        finally:
            # 7. Release claim — interactive task does not occupy a slot
            self._release_claim(issue.id)

    # ------------------------------------------------------------------
    # /bug-report comment command processing
    # ------------------------------------------------------------------

    _BUG_REPORT_RE = re.compile(
        r"^/bug-report\s+(.+)", re.MULTILINE
    )

    _REPLY_RE = re.compile(
        r"^/reply\s+(.+)", re.MULTILINE | re.DOTALL
    )

    _BUG_CONFIRM_PREFIX = "🐛 Создан баг-репорт"

    async def _process_bug_report_commands(self, issues: list[Issue]) -> None:
        """Scan comments on candidate issues for ``/bug-report`` commands.

        When a ``/bug-report <message>`` comment is found, create a new issue
        in the same project with labels ``bug`` and ``research``, and a
        description that instructs the agent to run ``/debug-ticket`` against
        the source ticket.

        To prevent duplicate issue creation across orchestrator restarts,
        the method checks existing comments for confirmation markers
        (``🐛 Создан баг-репорт ... <message>``) before creating a new issue.
        """
        for issue in issues:
            try:
                comments = await self._tracker.fetch_issue_comments(issue.id)
            except Exception as exc:
                log.warning(
                    "bug_report_fetch_comments_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )
                continue

            # Collect bug messages that already have confirmation comments
            # to avoid creating duplicate issues after orchestrator restarts.
            confirmed_messages: set[str] = set()
            for comment in comments:
                body = comment.get("body", "")
                if body.startswith(self._BUG_CONFIRM_PREFIX):
                    # Confirmation format:
                    #   "🐛 Создан баг-репорт [SER-XX](url): <message>"
                    # Extract the message after the last ": "
                    colon_pos = body.find("): ")
                    if colon_pos != -1:
                        confirmed_messages.add(body[colon_pos + 3:].strip())

            for comment in comments:
                comment_id = comment.get("id", "")
                if not comment_id:
                    continue
                if comment_id in self._state.processed_bug_reports:
                    continue

                body = comment.get("body", "")
                match = self._BUG_REPORT_RE.search(body)
                if not match:
                    # Mark as processed even if no match, to avoid re-scanning
                    self._state.processed_bug_reports.add(comment_id)
                    continue

                bug_message = match.group(1).strip()

                # Skip if a confirmation comment for this message already exists
                if bug_message in confirmed_messages:
                    log.info(
                        "bug_report_already_confirmed",
                        issue_identifier=issue.identifier,
                        bug_message=bug_message,
                    )
                    self._state.processed_bug_reports.add(comment_id)
                    continue

                await self._create_bug_report_issue(issue, bug_message)
                self._state.processed_bug_reports.add(comment_id)
                # Add to confirmed set so subsequent /bug-report comments
                # with the same message in this issue are also skipped.
                confirmed_messages.add(bug_message)

    async def _create_bug_report_issue(
        self, source_issue: Issue, bug_message: str
    ) -> None:
        """Create a bug+research issue triggered by a ``/bug-report`` comment."""
        title = f"Bug: {bug_message[:120]}"
        description = (
            f"Автоматически создано из комментария к тикету "
            f"**{source_issue.identifier}** ({source_issue.title}).\n\n"
            f"**Сообщение о проблеме:**\n{bug_message}\n\n"
            f"---\n"
            f"Запусти /debug-ticket {source_issue.identifier} и проанализируй "
            f"проблему, описанную выше."
        )

        try:
            result = await self._tracker.create_issue(
                title=title,
                description=description,
                state="Todo",
            )
            new_id = result.get("id", "")
            new_identifier = result.get("identifier", "")
            log.info(
                "bug_report_issue_created",
                source_issue=source_issue.identifier,
                new_issue=new_identifier,
                bug_message=bug_message,
            )

            # Add 'bug' and 'research' labels
            if new_id:
                try:
                    await self._tracker.replace_issue_labels(
                        new_id,
                        remove_labels=[],
                        add_labels=["bug", "research"],
                    )
                    log.info(
                        "bug_report_labels_added",
                        issue=new_identifier,
                    )
                except Exception as exc:
                    log.warning(
                        "bug_report_labels_failed",
                        issue=new_identifier,
                        error=str(exc),
                    )

            # Post confirmation comment on the source issue
            confirm_url = result.get("url", "")
            confirm_body = (
                f"🐛 Создан баг-репорт [{new_identifier}]({confirm_url}): "
                f"{bug_message}"
            )
            try:
                await self._tracker.comment_on_issue(
                    source_issue.id, confirm_body
                )
            except Exception as exc:
                log.warning(
                    "bug_report_confirm_comment_failed",
                    source_issue=source_issue.identifier,
                    error=str(exc),
                )

        except Exception as exc:
            log.error(
                "bug_report_issue_creation_failed",
                source_issue=source_issue.identifier,
                bug_message=bug_message,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # /reply thread comment processing — resume agent sessions
    # ------------------------------------------------------------------

    async def _process_thread_replies(self, issues: list[Issue]) -> None:
        """Scan thread replies for ``/reply`` commands and resume agent sessions.

        For each saved thread session, check if new replies appeared in the
        thread that start with ``/reply``.  When found, resume the agent
        session with the reply text as additional user input.
        """
        if not self._state.thread_sessions:
            return

        # Build a quick lookup: issue_id → issue
        issue_by_id: dict[str, Issue] = {i.id: i for i in issues}

        # Iterate over a copy since we may modify thread_sessions
        for thread_root, ts in list(self._state.thread_sessions.items()):
            # Skip if this issue already has a running agent
            if ts.issue_id in self._state.running:
                continue
            if ts.issue_id in self._state.claimed:
                continue

            # We need the issue object — look it up or fetch it
            issue = issue_by_id.get(ts.issue_id)
            if not issue:
                # Issue might no longer be in candidate states — try to fetch
                try:
                    issue = await self._tracker.fetch_issue_by_identifier(
                        ts.issue_identifier
                    )
                except Exception:
                    continue

            try:
                comments = await self._tracker.fetch_issue_comments(ts.issue_id)
            except Exception as exc:
                log.warning(
                    "reply_fetch_comments_failed",
                    issue_identifier=ts.issue_identifier,
                    error=str(exc),
                )
                continue

            # Find the thread root comment and check its children
            root_comment = None
            for comment in comments:
                if comment.get("id") == thread_root:
                    root_comment = comment
                    break

            if not root_comment:
                continue

            children = root_comment.get("children", [])
            # Process replies in chronological order
            for child in children:
                child_id = child.get("id", "")
                if not child_id:
                    continue
                if child_id in ts.processed_reply_ids:
                    continue

                body = child.get("body", "")
                match = self._REPLY_RE.search(body)
                if not match:
                    # Mark as processed even without match to avoid re-scanning
                    ts.processed_reply_ids.add(child_id)
                    continue

                reply_text = match.group(1).strip()
                ts.processed_reply_ids.add(child_id)

                log.info(
                    "thread_reply_detected",
                    issue_identifier=ts.issue_identifier,
                    thread_root=thread_root,
                    reply_comment_id=child_id,
                    reply_len=len(reply_text),
                )

                # Resume agent session with the reply text
                await self._dispatch_thread_resume(
                    issue=issue,
                    thread_session=ts,
                    reply_text=reply_text,
                )
                # Only process one reply at a time per thread
                break

    async def _dispatch_thread_resume(
        self,
        issue: Issue,
        thread_session: ThreadSession,
        reply_text: str,
    ) -> None:
        """Resume an agent session in response to a /reply thread comment."""
        self._state.claimed.add(issue.id)

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=0,
            started_at=datetime.now(timezone.utc),
            status="running",
        )

        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
            thread_root_comment_id=thread_session.thread_root_comment_id,
        )

        self._state.running[issue.id] = entry

        log.info(
            "dispatch_thread_resume",
            issue_identifier=issue.identifier,
            session_id=thread_session.session_id,
            thread_root=thread_session.thread_root_comment_id,
        )

        task = asyncio.create_task(
            self._run_worker(
                issue, entry,
                resume_session_id=thread_session.session_id,
                resume_workspace_path=thread_session.workspace_path,
                reply_prompt=reply_text,
            )
        )
        entry.worker_task = task

    async def _run_worker(
        self,
        issue: Issue,
        entry: RunningEntry,
        *,
        resume_session_id: str | None = None,
        resume_workspace_path: str | None = None,
        reply_prompt: str | None = None,
    ) -> None:
        async def _post_transcript_comment(transcript_path: str, workspace_path: str = "") -> None:
            """Post a comment with the transcript link as soon as it's available.

            The first transcript comment becomes the thread root. Its ID is
            stored in ``entry.thread_root_comment_id`` so that all subsequent
            comments for this agent run are posted as replies in the same
            thread.
            """
            transcript_url = _build_transcript_url(
                self._config.server.explorer_base_url, transcript_path
            )
            if transcript_url:
                body = _build_transcript_comment(
                    transcript_url, transcript_path, workspace_path,
                )
                try:
                    # For resumed sessions, post as reply in existing thread
                    parent_id = entry.thread_root_comment_id
                    comment_id = await self._tracker.comment_on_issue(
                        issue.id, body, parent_comment_id=parent_id,
                    )
                    # If this is the first comment (no parent), it becomes the
                    # thread root for all subsequent messages.
                    if comment_id and not entry.thread_root_comment_id:
                        entry.thread_root_comment_id = comment_id
                    log.info(
                        "transcript_comment_posted",
                        issue_identifier=issue.identifier,
                        transcript_url=transcript_url,
                        thread_root=entry.thread_root_comment_id,
                    )
                except Exception as exc:
                    log.warning(
                        "transcript_comment_failed",
                        issue_identifier=issue.identifier,
                        error=str(exc),
                    )

        try:
            result = await self._run_agent_fn(
                issue, entry.attempt.attempt,
                on_transcript=_post_transcript_comment,
                resume_session_id=resume_session_id,
                resume_workspace_path=resume_workspace_path,
                reply_prompt=reply_prompt,
            )
            # Propagate plan_text from agent's RunAttempt to orchestrator's
            if hasattr(result, "plan_text") and result.plan_text:
                entry.attempt.plan_text = result.plan_text

            # Propagate workspace_path from agent result
            if hasattr(result, "workspace_path") and result.workspace_path:
                entry.attempt.workspace_path = result.workspace_path

            # Store session_id for potential thread resume later
            if hasattr(result, "session_id") and result.session_id:
                entry.attempt.session_id = result.session_id
                # Also store in entry.session for convenience
                if not entry.session.session_id:
                    entry.session.session_id = result.session_id

            if hasattr(result, "status") and result.status == "failed":
                log.error(
                    "worker_failed",
                    issue_identifier=issue.identifier,
                    error=getattr(result, "error", None),
                )
                await self._on_worker_exit(
                    issue.id, normal=False, error=getattr(result, "error", "agent_failed"),
                    result=getattr(result, "result", None),
                )
            else:
                await self._on_worker_exit(
                    issue.id, normal=True, error=None,
                    result=getattr(result, "result", None),
                )
        except Exception as exc:
            log.error(
                "worker_failed",
                issue_identifier=issue.identifier,
                error=str(exc),
            )
            await self._on_worker_exit(issue.id, normal=False, error=str(exc))

    async def _on_worker_exit(
        self,
        issue_id: str,
        normal: bool,
        error: str | None,
        result: str | None = None,
    ) -> None:
        entry = self._state.running.get(issue_id)
        if entry is None:
            return

        elapsed: float | None = None
        if entry.attempt.started_at:
            elapsed = (datetime.now(timezone.utc) - entry.attempt.started_at).total_seconds()
            self._state.agent_totals.seconds_running += elapsed

        session = entry.session
        self._state.agent_totals.input_tokens += session.agent_input_tokens
        self._state.agent_totals.output_tokens += session.agent_output_tokens
        self._state.agent_totals.total_tokens += session.agent_total_tokens

        log.info(
            "agent_exit",
            issue_identifier=entry.issue.identifier,
            normal=normal,
            error=error,
            elapsed_s=round(elapsed, 2) if elapsed is not None else None,
            input_tokens=session.agent_input_tokens,
            output_tokens=session.agent_output_tokens,
        )

        # Post agent's last message as a comment on the issue
        # For plan-required issues, prefer the full plan text over the
        # (potentially abbreviated) final assistant message.
        plan_text = getattr(entry.attempt, "plan_text", None)
        issue_labels_norm = [normalize_label(label) for label in entry.issue.labels]
        is_plan_required = "plan required" in issue_labels_norm
        is_research = "research" in issue_labels_norm

        if (is_plan_required or is_research) and plan_text:
            comment_body = plan_text
        elif result:
            comment_body = result
        elif not normal and error:
            comment_body = f"⚠️ Agent exited with error: {error}"
        elif not normal:
            comment_body = "⚠️ Agent exited abnormally without producing a result."
        else:
            comment_body = "Agent completed without producing a result."

        # Post as reply in thread if we have a thread root
        parent_comment_id = entry.thread_root_comment_id
        try:
            comment_id = await self._tracker.comment_on_issue(
                issue_id, comment_body,
                parent_comment_id=parent_comment_id,
            )
            log.info(
                "comment_posted",
                issue_identifier=entry.issue.identifier,
                thread_root=parent_comment_id,
            )
        except Exception as exc:
            log.warning(
                "comment_post_failed",
                issue_identifier=entry.issue.identifier,
                error=str(exc),
            )

        # Save thread session for potential /reply resume
        session_id = getattr(entry.attempt, "session_id", None) or entry.session.session_id
        thread_root = entry.thread_root_comment_id
        workspace_path = entry.attempt.workspace_path

        if session_id and thread_root and workspace_path:
            self._state.thread_sessions[thread_root] = ThreadSession(
                issue_id=issue_id,
                issue_identifier=entry.issue.identifier,
                session_id=session_id,
                workspace_path=workspace_path,
                thread_root_comment_id=thread_root,
            )
            log.info(
                "thread_session_saved",
                issue_identifier=entry.issue.identifier,
                session_id=session_id,
                thread_root=thread_root,
            )

        # Transition issue based on completion and review requirements
        plan_required = is_plan_required
        research = is_research
        done_signaled = normal and result and "[DONE]" in result

        # For plan-required and research issues, a normal exit is sufficient to
        # trigger the transition — the agent may not include [DONE] in its result.
        # Check for resolve-conflict label — agents dispatched for conflict
        # resolution follow a similar flow to plan-required.
        resolve_conflict = "resolve conflict" in issue_labels_norm

        if done_signaled or (normal and plan_required) or (normal and research) or (normal and resolve_conflict):
            review_required = "review required" in issue_labels_norm
            merge_conflict = False

            if plan_required:
                # Plan is complete — swap labels and move to In Review
                try:
                    await self._tracker.replace_issue_labels(
                        issue_id,
                        remove_labels=["plan required"],
                        add_labels=["with plan"],
                    )
                    log.info(
                        "plan_labels_swapped",
                        issue_identifier=entry.issue.identifier,
                    )
                except Exception as exc:
                    log.warning(
                        "plan_label_swap_failed",
                        issue_identifier=entry.issue.identifier,
                        error=str(exc),
                    )

                target_state = "In Review"
            elif research:
                # Research is complete — swap labels and move to In Review
                try:
                    await self._tracker.replace_issue_labels(
                        issue_id,
                        remove_labels=["research"],
                        add_labels=["with research"],
                    )
                    log.info(
                        "research_labels_swapped",
                        issue_identifier=entry.issue.identifier,
                    )
                except Exception as exc:
                    log.warning(
                        "research_label_swap_failed",
                        issue_identifier=entry.issue.identifier,
                        error=str(exc),
                    )

                target_state = "In Review"
            elif resolve_conflict:
                # Conflict resolution agent finished — remove label, retry automerge
                try:
                    await self._tracker.replace_issue_labels(
                        issue_id,
                        remove_labels=["resolve-conflict"],
                        add_labels=[],
                    )
                    log.info(
                        "resolve_conflict_label_removed",
                        issue_identifier=entry.issue.identifier,
                    )
                except Exception as exc:
                    log.warning(
                        "resolve_conflict_label_remove_failed",
                        issue_identifier=entry.issue.identifier,
                        error=str(exc),
                    )

                # Re-attempt automerge after conflict resolution
                target_state = "Done"
                try:
                    pr_urls = await self._tracker.fetch_issue_pr_urls(issue_id)
                    if (
                        not pr_urls
                        and self._config.automerge.parse_transcript_prs
                    ):
                        pr_urls = extract_pr_urls_from_transcript(
                            entry.attempt.transcript_path,
                        )
                        if pr_urls:
                            log.info(
                                "automerge_pr_urls_from_transcript",
                                issue_identifier=entry.issue.identifier,
                                pr_urls=pr_urls,
                            )
                            for url in pr_urls:
                                await self._attach_pr_to_issue(
                                    issue_id,
                                    entry.issue.identifier,
                                    url,
                                )
                    for pr_url in pr_urls:
                        merged = await try_automerge_pr(pr_url)
                        log.info(
                            "automerge_after_resolve_attempt",
                            issue_identifier=entry.issue.identifier,
                            pr_url=pr_url,
                            merged=merged,
                        )
                        if not merged:
                            merge_conflict = True
                except Exception as exc:
                    log.warning(
                        "automerge_after_resolve_failed",
                        issue_identifier=entry.issue.identifier,
                        error=str(exc),
                    )

                if merge_conflict:
                    # Still can't merge — re-add label and move to In Review
                    target_state = "In Review"
                    try:
                        await self._tracker.replace_issue_labels(
                            issue_id,
                            remove_labels=[],
                            add_labels=["resolve-conflict"],
                        )
                    except Exception:
                        pass
                    try:
                        await self._tracker.comment_on_issue(
                            issue_id,
                            "⚠️ Conflict resolution completed but PR still cannot be merged. "
                            "Moving to In Review for another attempt.",
                        )
                    except Exception:
                        pass
            elif review_required:
                # Review is needed — move to "In Review" instead of "Done"
                target_state = "In Review"
            else:
                # No review required — try to automerge any attached PRs first
                target_state = "Done"
                merge_conflict = False
                conflict_pr_url: str | None = None
                try:
                    pr_urls = await self._tracker.fetch_issue_pr_urls(issue_id)
                    if (
                        not pr_urls
                        and self._config.automerge.parse_transcript_prs
                    ):
                        pr_urls = extract_pr_urls_from_transcript(
                            entry.attempt.transcript_path,
                        )
                        if pr_urls:
                            log.info(
                                "automerge_pr_urls_from_transcript",
                                issue_identifier=entry.issue.identifier,
                                pr_urls=pr_urls,
                            )
                            for url in pr_urls:
                                await self._attach_pr_to_issue(
                                    issue_id,
                                    entry.issue.identifier,
                                    url,
                                )
                    for pr_url in pr_urls:
                        merged = await try_automerge_pr(pr_url)
                        log.info(
                            "automerge_attempt",
                            issue_identifier=entry.issue.identifier,
                            pr_url=pr_url,
                            merged=merged,
                        )
                        if not merged:
                            merge_conflict = True
                            conflict_pr_url = pr_url
                except Exception as exc:
                    log.warning(
                        "automerge_failed",
                        issue_identifier=entry.issue.identifier,
                        error=str(exc),
                    )

                if merge_conflict:
                    # PR could not be merged — add resolve-conflict label,
                    # post a comment, and move to In Review instead of Done.
                    target_state = "In Review"
                    try:
                        await self._tracker.replace_issue_labels(
                            issue_id,
                            remove_labels=[],
                            add_labels=["resolve-conflict"],
                        )
                        log.info(
                            "resolve_conflict_label_added",
                            issue_identifier=entry.issue.identifier,
                        )
                    except Exception as exc:
                        log.warning(
                            "resolve_conflict_label_failed",
                            issue_identifier=entry.issue.identifier,
                            error=str(exc),
                        )

                    conflict_comment = (
                        f"⚠️ PR could not be merged due to conflicts: {conflict_pr_url}\n\n"
                        "The issue has been moved to In Review with the `resolve-conflict` label.\n"
                        "Move it to Todo to trigger automatic conflict resolution."
                    )
                    try:
                        await self._tracker.comment_on_issue(issue_id, conflict_comment)
                        log.info(
                            "resolve_conflict_comment_posted",
                            issue_identifier=entry.issue.identifier,
                        )
                    except Exception as exc:
                        log.warning(
                            "resolve_conflict_comment_failed",
                            issue_identifier=entry.issue.identifier,
                            error=str(exc),
                        )
                else:
                    # Rebase worktree branch onto main (linear history)
                    try:
                        merge_info = await self._workspace_mgr.rebase_branch_onto_main(
                            entry.issue.identifier,
                        )
                        if merge_info:
                            log.info(
                                "branch_rebased_onto_main",
                                issue_identifier=entry.issue.identifier,
                                commit_sha=merge_info.commit_sha,
                            )

                            # Post a comment with changed files and commit SHA
                            merge_comment = _build_merge_comment(merge_info)
                            try:
                                await self._tracker.comment_on_issue(
                                    issue_id, merge_comment,
                                )
                            except Exception as exc:
                                log.warning(
                                    "merge_comment_failed",
                                    issue_identifier=entry.issue.identifier,
                                    error=str(exc),
                                )

                            # Clean up worktree and delete the merged branch
                            await self._workspace_mgr.cleanup_workspace(
                                entry.issue.identifier, delete_branch=True,
                            )
                    except Exception as exc:
                        log.warning(
                            "rebase_onto_main_failed",
                            issue_identifier=entry.issue.identifier,
                            error=str(exc),
                        )

            try:
                await self._tracker.transition_issue(issue_id, target_state)
                log.info(
                    "issue_transitioned",
                    issue_identifier=entry.issue.identifier,
                    target_state=target_state,
                )
            except Exception as exc:
                log.warning(
                    "issue_transition_failed",
                    issue_identifier=entry.issue.identifier,
                    error=str(exc),
                )

            if self.exit_on_merge and target_state == "Done":
                self._enter_drain_mode(entry.issue.identifier)

            # Plan-required, research, or merge-conflict work is complete —
            # release claim and skip retries to prevent re-dispatch on the
            # next poll cycle.
            if plan_required or research or merge_conflict:
                self._release_claim(issue_id)
                return

        # Remove from running only after all post-completion HTTP calls
        # (comment, automerge, transition) have finished.  This prevents a
        # race where the drain coordinator sees running == 0 and closes the
        # HTTP client while these calls are still in flight (SER-63).
        self._state.running.pop(issue_id, None)

        # While draining, skip retries and check if drain is complete
        if self._draining:
            self._release_claim(issue_id)
            if not self._state.running:
                self._signal_drain_complete()
            return

        current_attempt = entry.attempt.attempt or 0
        max_runs = self._config.agent.max_runs
        next_attempt = current_attempt + 1

        if next_attempt >= max_runs:
            if not normal:
                log.warning(
                    "max_runs_exceeded",
                    issue_identifier=entry.issue.identifier,
                    attempt=current_attempt,
                    max_runs=max_runs,
                    error=error,
                )
            self._release_claim(issue_id)
            return

        if normal:
            delay_ms = 1000
        else:
            delay_ms = min(
                10000 * (2 ** next_attempt),
                self._config.agent.max_retry_backoff_ms,
            )

        self._schedule_retry(
            issue_id=issue_id,
            identifier=entry.issue.identifier,
            attempt=next_attempt,
            delay_ms=delay_ms,
            error=error,
        )

    def _enter_drain_mode(
        self, trigger_identifier: str, *, kind: str = "merge",
    ) -> None:
        """Enter drain mode: stop accepting new work, wait for running jobs.

        *kind* controls post-drain behaviour:
        - ``"merge"`` — signal merge exit (existing behaviour for exit_on_merge).
        - ``"reload"`` — set ``drain_complete_event`` so the service layer can
          clean up this processor generation without stopping the whole service.
        """
        if self._draining:
            return
        self._draining = True
        self._drain_kind = kind
        log.info(
            "drain_started",
            triggered_by=trigger_identifier,
            kind=kind,
            running=len(self._state.running),
        )

        # Cancel all pending retries — we don't want to start new work
        for issue_id, retry in list(self._state.retry_attempts.items()):
            if retry.timer_handle:
                retry.timer_handle.cancel()
        self._state.retry_attempts.clear()

        # If nothing is running right now, signal immediately
        if not self._state.running:
            self._signal_drain_complete()

    def _signal_drain_complete(self) -> None:
        """Signal that drain has finished — dispatch to merge or reload handler."""
        log.info("drain_complete", kind=self._drain_kind)
        self.drain_complete_event.set()
        if self._drain_kind == "merge":
            self.merge_detected = True
            if self.merge_detected_event:
                self.merge_detected_event.set()

    # Keep legacy alias used by service.py drain coordinator.
    _signal_merge_exit = _signal_drain_complete

    def _schedule_retry(
        self,
        issue_id: str,
        identifier: str,
        attempt: int,
        delay_ms: float,
        error: str | None,
    ) -> None:
        existing = self._state.retry_attempts.pop(issue_id, None)
        if existing and existing.timer_handle:
            existing.timer_handle.cancel()

        due_at_ms = time.monotonic() * 1000 + delay_ms

        loop = asyncio.get_event_loop()
        timer = loop.call_later(
            delay_ms / 1000.0,
            lambda: asyncio.ensure_future(self._handle_retry(issue_id)),
        )

        self._state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_ms=due_at_ms,
            timer_handle=timer,
            error=error,
        )

        log.info(
            "retry_scheduled",
            issue_identifier=identifier,
            attempt=attempt,
            delay_ms=delay_ms,
            reason=error or "normal_completion",
        )

    async def _handle_retry(self, issue_id: str) -> None:
        entry = self._state.retry_attempts.pop(issue_id, None)
        if entry is None:
            return

        try:
            issues = await self._tracker.fetch_candidate_issues()
        except Exception as exc:
            log.error("retry_fetch_failed", error=str(exc))
            self._release_claim(issue_id)
            return

        target = None
        for issue in issues:
            if issue.id == issue_id:
                target = issue
                break

        if target is None:
            log.info("retry_issue_not_found", issue_id=issue_id)
            self._release_claim(issue_id)
            return

        if not self._is_dispatch_eligible_for_retry(target):
            log.info("retry_issue_no_longer_eligible", issue_identifier=target.identifier)
            self._release_claim(issue_id)
            return

        slots = self._available_slots(target.state)
        if slots <= 0:
            self._schedule_retry(
                issue_id=issue_id,
                identifier=entry.identifier,
                attempt=entry.attempt,
                delay_ms=1000,
                error="no available orchestrator slots",
            )
            return

        self._state.claimed.discard(issue_id)
        await self._dispatch(target, retry_attempt=entry.attempt)

    def _is_dispatch_eligible_for_retry(self, issue: Issue) -> bool:
        if self._has_workflow_issue_label(issue):
            return False
        active = {normalize_state(s) for s in self._config.tracker.active_states}
        terminal = {normalize_state(s) for s in self._config.tracker.terminal_states}
        issue_state = normalize_state(issue.state)
        if issue_state not in active or issue_state in terminal:
            return False
        for blocker in issue.blocked_by:
            if blocker.state and normalize_state(blocker.state) not in terminal:
                return False
        return True

    def _release_claim(self, issue_id: str) -> None:
        self._state.claimed.discard(issue_id)
        self._state.running.pop(issue_id, None)
        retry = self._state.retry_attempts.pop(issue_id, None)
        if retry and retry.timer_handle:
            retry.timer_handle.cancel()

    async def reconcile_running_issues(self) -> None:
        if not self._state.running:
            return

        stall_timeout_ms = self._config.codex.stall_timeout_ms

        if stall_timeout_ms > 0:
            now = datetime.now(timezone.utc)
            stalled = []
            for issue_id, entry in self._state.running.items():
                ref_time = entry.session.last_agent_timestamp or entry.attempt.started_at
                if ref_time:
                    elapsed_ms = (now - ref_time).total_seconds() * 1000
                    if elapsed_ms > stall_timeout_ms:
                        stalled.append(issue_id)

            for issue_id in stalled:
                entry = self._state.running.get(issue_id)
                if entry:
                    log.warning(
                        "stall_detected",
                        issue_identifier=entry.issue.identifier,
                    )
                    await self._kill_worker(issue_id)
                    await self._on_worker_exit(issue_id, normal=False, error="stall_timeout")

        running_ids = list(self._state.running.keys())
        if not running_ids:
            return

        try:
            issue_info = await self._tracker.fetch_issue_states_by_ids(running_ids)
        except Exception as exc:
            log.error("reconciliation_fetch_failed", error=str(exc))
            return

        terminal = {normalize_state(s) for s in self._config.tracker.terminal_states}
        active = {normalize_state(s) for s in self._config.tracker.active_states}

        for issue_id in running_ids:
            if issue_id not in self._state.running:
                continue

            entry = self._state.running[issue_id]
            info = issue_info.get(issue_id)

            if info is None:
                continue

            current_state = info["state"]
            current_labels = info.get("labels", [])

            # Kill agent if issue now has "workflow issue" label
            if _WORKFLOW_ISSUE_LABEL in [
                normalize_label(l) for l in current_labels
            ]:
                log.info(
                    "reconcile_workflow_issue",
                    issue_identifier=entry.issue.identifier,
                )
                await self._kill_worker(issue_id)
                await self._post_kill_comment(
                    issue_id,
                    "Agent killed: 'workflow issue' label detected",
                )
                self._state.running.pop(issue_id, None)
                self._release_claim(issue_id)
                continue

            normalized = normalize_state(current_state)

            if normalized in terminal:
                log.info(
                    "reconcile_terminal",
                    issue_identifier=entry.issue.identifier,
                    state=current_state,
                )
                await self._kill_worker(issue_id)
                await self._post_kill_comment(
                    issue_id,
                    f"Agent killed: issue reached terminal state '{current_state}'",
                )
                self._state.running.pop(issue_id, None)
                self._release_claim(issue_id)
                await self._workspace_mgr.cleanup_workspace(entry.issue.identifier)
            elif normalized in active:
                entry.issue.state = current_state
            else:
                log.info(
                    "reconcile_non_active",
                    issue_identifier=entry.issue.identifier,
                    state=current_state,
                )
                await self._kill_worker(issue_id)
                await self._post_kill_comment(
                    issue_id,
                    f"Agent killed: issue moved to non-active state '{current_state}'",
                )
                self._state.running.pop(issue_id, None)
                self._release_claim(issue_id)

    async def startup_terminal_cleanup(self) -> None:
        try:
            terminal_issues = await self._tracker.fetch_issues_by_states(
                self._config.tracker.terminal_states
            )
        except Exception as exc:
            log.warning("startup_cleanup_fetch_failed", error=str(exc))
            return

        for issue in terminal_issues:
            try:
                await self._workspace_mgr.cleanup_workspace(issue.identifier)
                log.info("startup_cleanup", issue_identifier=issue.identifier)
            except Exception as exc:
                log.warning(
                    "startup_cleanup_failed",
                    issue_identifier=issue.identifier,
                    error=str(exc),
                )

    async def _post_kill_comment(
        self, issue_id: str, reason: str
    ) -> None:
        """Post a Linear comment explaining why the agent was killed."""
        entry = self._state.running.get(issue_id)
        identifier = entry.issue.identifier if entry else issue_id
        comment_body = f"⚠️ {reason}"
        try:
            await self._tracker.comment_on_issue(issue_id, comment_body)
            log.info(
                "kill_comment_posted",
                issue_identifier=identifier,
                reason=reason,
            )
        except Exception as exc:
            log.warning(
                "kill_comment_post_failed",
                issue_identifier=identifier,
                error=str(exc),
            )

    async def _kill_worker(self, issue_id: str) -> None:
        entry = self._state.running.get(issue_id)
        if entry and entry.worker_task and not entry.worker_task.done():
            entry.worker_task.cancel()
            try:
                await entry.worker_task
            except (asyncio.CancelledError, Exception):
                pass

    async def shutdown(self) -> None:
        for issue_id in list(self._state.running.keys()):
            await self._kill_worker(issue_id)

        for issue_id, retry in list(self._state.retry_attempts.items()):
            if retry.timer_handle:
                retry.timer_handle.cancel()

        self._state.running.clear()
        self._state.retry_attempts.clear()
        self._state.claimed.clear()
