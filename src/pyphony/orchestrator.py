from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog

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
from .normalization import normalize_state, sort_issues_for_dispatch
from .tracker import LinearClient
from .workspace import WorkspaceManager

log = structlog.stdlib.get_logger()


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

    @property
    def state(self) -> OrchestratorRuntimeState:
        return self._state

    def update_config(self, config: ServiceConfig) -> None:
        self._config = config
        self._state.poll_interval_ms = config.polling.interval_ms
        self._state.max_concurrent_agents = config.agent.max_concurrent_agents

    async def poll_tick(self) -> None:
        await self.reconcile_running_issues()

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

        log.info(
            "poll_tick_complete",
            dispatched=dispatched,
            running=len(self._state.running),
            retrying=len(self._state.retry_attempts),
        )

    def _is_dispatch_eligible(self, issue: Issue) -> bool:
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
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

        if issue_state == "todo":
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

    async def _dispatch(self, issue: Issue) -> None:
        self._state.claimed.add(issue.id)

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            started_at=datetime.now(timezone.utc),
            status="running",
        )

        entry = RunningEntry(
            issue=issue,
            attempt=attempt,
        )

        self._state.running[issue.id] = entry

        log.info(
            "dispatch",
            issue_identifier=issue.identifier,
            issue_state=issue.state,
        )

        if self._run_agent_fn:
            task = asyncio.create_task(
                self._run_worker(issue, entry)
            )
            entry.worker_task = task

    async def _run_worker(self, issue: Issue, entry: RunningEntry) -> None:
        try:
            result = await self._run_agent_fn(issue, entry.attempt.attempt)
            self._on_worker_exit(issue.id, normal=True, error=None)
        except Exception as exc:
            log.error(
                "worker_failed",
                issue_identifier=issue.identifier,
                error=str(exc),
            )
            self._on_worker_exit(issue.id, normal=False, error=str(exc))

    def _on_worker_exit(
        self,
        issue_id: str,
        normal: bool,
        error: str | None,
    ) -> None:
        entry = self._state.running.pop(issue_id, None)
        if entry is None:
            return

        if entry.attempt.started_at:
            elapsed = (datetime.now(timezone.utc) - entry.attempt.started_at).total_seconds()
            self._state.agent_totals.seconds_running += elapsed

        session = entry.session
        self._state.agent_totals.input_tokens += session.agent_input_tokens
        self._state.agent_totals.output_tokens += session.agent_output_tokens
        self._state.agent_totals.total_tokens += session.agent_total_tokens

        if normal:
            self._schedule_retry(
                issue_id=issue_id,
                identifier=entry.issue.identifier,
                attempt=1,
                delay_ms=1000,
                error=None,
            )
        else:
            current_attempt = (
                self._state.retry_attempts[issue_id].attempt + 1
                if issue_id in self._state.retry_attempts
                else 1
            )
            delay_ms = min(
                10000 * (2 ** (current_attempt - 1)),
                self._config.agent.max_retry_backoff_ms,
            )
            self._schedule_retry(
                issue_id=issue_id,
                identifier=entry.issue.identifier,
                attempt=current_attempt,
                delay_ms=delay_ms,
                error=error,
            )

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
        await self._dispatch(target)

    def _is_dispatch_eligible_for_retry(self, issue: Issue) -> bool:
        active = {normalize_state(s) for s in self._config.tracker.active_states}
        terminal = {normalize_state(s) for s in self._config.tracker.terminal_states}
        issue_state = normalize_state(issue.state)
        return issue_state in active and issue_state not in terminal

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
                    self._on_worker_exit(issue_id, normal=False, error="stall_timeout")

        running_ids = list(self._state.running.keys())
        if not running_ids:
            return

        try:
            states = await self._tracker.fetch_issue_states_by_ids(running_ids)
        except Exception as exc:
            log.error("reconciliation_fetch_failed", error=str(exc))
            return

        terminal = {normalize_state(s) for s in self._config.tracker.terminal_states}
        active = {normalize_state(s) for s in self._config.tracker.active_states}

        for issue_id in running_ids:
            if issue_id not in self._state.running:
                continue

            entry = self._state.running[issue_id]
            current_state = states.get(issue_id)

            if current_state is None:
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
