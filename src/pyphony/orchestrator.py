from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import structlog

from .automerge import try_automerge_pr

from .config import service_config_from_workflow, validate_dispatch_config
from .models import (
    AgentTotals,
    Issue,
    LiveSession,
    OrchestratorRuntimeState,
    RetryEntry,
    RunAttempt,
    RunningEntry,
    ServiceConfig,
)
from .normalization import normalize_label, normalize_state, sort_issues_for_dispatch
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
    entry: RunningEntry,
) -> str:
    """Build a human-friendly comment about a running agent session.

    Includes the transcript link plus instructions on how to resume
    the session from a terminal.
    """
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]
    workspace_path = entry.attempt.workspace_path

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


class Orchestrator:
    def __init__(
        self,
        config: ServiceConfig,
        tracker: LinearClient,
        workspace_mgr: WorkspaceManager,
        run_agent_fn=None,
    ) -> None:
        self._config = config
        self._tracker = tracker
        self._workspace_mgr = workspace_mgr
        self._run_agent_fn = run_agent_fn
        self._state = OrchestratorRuntimeState(
            poll_interval_ms=config.polling.interval_ms,
            max_concurrent_agents=config.agent.max_concurrent_agents,
        )
        self.exit_on_merge: bool = False
        self.merge_detected: bool = False
        self.merge_detected_event: asyncio.Event | None = None
        self._draining: bool = False

    @property
    def state(self) -> OrchestratorRuntimeState:
        return self._state

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
                self._signal_merge_exit()
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

        for blocker in issue.blocked_by:
            if blocker.state and normalize_state(blocker.state) not in terminal:
                return False

        return True

    def _available_slots(self, state: str) -> int:
        running_count = len(self._state.running)
        global_available = max(self._state.max_concurrent_agents - running_count, 0)

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

        if self._run_agent_fn:
            task = asyncio.create_task(
                self._run_worker(issue, entry)
            )
            entry.worker_task = task

    async def _run_worker(self, issue: Issue, entry: RunningEntry) -> None:
        async def _post_transcript_comment(transcript_path: str) -> None:
            """Post a comment with the transcript link as soon as it's available."""
            transcript_url = _build_transcript_url(
                self._config.server.explorer_base_url, transcript_path
            )
            if transcript_url:
                body = _build_transcript_comment(
                    transcript_url, transcript_path, entry,
                )
                try:
                    await self._tracker.comment_on_issue(issue.id, body)
                    log.info(
                        "transcript_comment_posted",
                        issue_identifier=issue.identifier,
                        transcript_url=transcript_url,
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
            )
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
        entry = self._state.running.pop(issue_id, None)
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

        if is_plan_required and plan_text:
            comment_body = plan_text
        elif result:
            comment_body = result
        elif not normal and error:
            comment_body = f"⚠️ Agent exited with error: {error}"
        elif not normal:
            comment_body = "⚠️ Agent exited abnormally without producing a result."
        else:
            comment_body = "Agent completed without producing a result."

        try:
            await self._tracker.comment_on_issue(issue_id, comment_body)
            log.info(
                "comment_posted",
                issue_identifier=entry.issue.identifier,
            )
        except Exception as exc:
            log.warning(
                "comment_post_failed",
                issue_identifier=entry.issue.identifier,
                error=str(exc),
            )

        # Transition issue based on completion and review requirements
        plan_required = is_plan_required
        done_signaled = normal and result and "[DONE]" in result

        # For plan-required issues, a normal exit is sufficient to trigger
        # the transition — the agent may not include [DONE] in its result.
        # Check for resolve-conflict label — agents dispatched for conflict
        # resolution follow a similar flow to plan-required.
        resolve_conflict = "resolve conflict" in issue_labels_norm

        if done_signaled or (normal and plan_required) or (normal and resolve_conflict):
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
                        rebased = await self._workspace_mgr.rebase_branch_onto_main(
                            entry.issue.identifier,
                        )
                        if rebased:
                            log.info(
                                "branch_rebased_onto_main",
                                issue_identifier=entry.issue.identifier,
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

            # Plan-required or merge-conflict work is complete — release claim
            # and skip retries to prevent re-dispatch on the next poll cycle.
            if plan_required or merge_conflict:
                self._release_claim(issue_id)
                return

        # While draining, skip retries and check if drain is complete
        if self._draining:
            self._release_claim(issue_id)
            if not self._state.running:
                self._signal_merge_exit()
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

    def _enter_drain_mode(self, trigger_identifier: str) -> None:
        """Enter drain mode: stop accepting new work, wait for running jobs."""
        if self._draining:
            return
        self._draining = True
        log.info(
            "drain_started",
            triggered_by=trigger_identifier,
            running=len(self._state.running),
        )

        # Cancel all pending retries — we don't want to start new work
        for issue_id, retry in list(self._state.retry_attempts.items()):
            if retry.timer_handle:
                retry.timer_handle.cancel()
        self._state.retry_attempts.clear()

        # If nothing is running right now, signal immediately
        if not self._state.running:
            self._signal_merge_exit()

    def _signal_merge_exit(self) -> None:
        """Signal the service to stop after drain completes."""
        log.info("drain_complete")
        self.merge_detected = True
        if self.merge_detected_event:
            self.merge_detected_event.set()

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
