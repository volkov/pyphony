from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

import structlog

from .config import service_config_from_workflow, validate_dispatch_config
from .logging import configure_logging
from .normalization import normalize_state, sort_issues_for_dispatch
from .tracker import LinearClient
from .workflow import load_workflow
from .workspace import WorkspaceManager

log = structlog.stdlib.get_logger()


async def _poll_tick(
    tracker: LinearClient,
    workspace_mgr: WorkspaceManager,
    config_raw: dict,
) -> None:
    from .config import service_config_from_workflow, validate_dispatch_config

    config = service_config_from_workflow(config_raw)
    errors = validate_dispatch_config(config)
    if errors:
        for err in errors:
            log.error("dispatch_validation_failed", error=err)
        return

    tracker._active_states = config.tracker.active_states
    tracker._project_slug = config.tracker.project_slug

    try:
        issues = await tracker.fetch_candidate_issues()
    except Exception as exc:
        log.error("candidate_fetch_failed", error=str(exc))
        return

    sorted_issues = sort_issues_for_dispatch(issues)

    terminal_states = {normalize_state(s) for s in config.tracker.terminal_states}

    for issue in sorted_issues:
        has_nonterminal_blockers = any(
            b.state and normalize_state(b.state) not in terminal_states
            for b in issue.blocked_by
        )
        if normalize_state(issue.state) == "todo" and has_nonterminal_blockers:
            log.debug(
                "skipping_blocked_todo",
                issue_identifier=issue.identifier,
            )
            continue

        ws = await workspace_mgr.create_or_reuse(issue.identifier)
        log.info(
            "would_dispatch",
            issue_identifier=issue.identifier,
            issue_title=issue.title,
            issue_state=issue.state,
            workspace=ws.path,
            created_now=ws.created_now,
        )


async def _run_service(args: argparse.Namespace) -> None:
    configure_logging(args.log_level)

    workflow_path = Path(args.workflow_file)
    log.info("starting_service", workflow_file=str(workflow_path))

    wf = load_workflow(workflow_path)
    config = service_config_from_workflow(wf.config)

    errors = validate_dispatch_config(config)
    if errors:
        for err in errors:
            log.error("startup_validation_failed", error=err)
        raise SystemExit(1)

    log.info(
        "config_loaded",
        tracker_kind=config.tracker.kind,
        project_slug=config.tracker.project_slug,
        poll_interval_ms=config.polling.interval_ms,
        workspace_root=config.workspace.root,
        max_concurrent=config.agent.max_concurrent_agents,
    )

    tracker = LinearClient(config)
    workspace_mgr = WorkspaceManager(config)

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    poll_interval = config.polling.interval_ms / 1000.0

    try:
        while not stop_event.is_set():
            log.info("poll_tick_start")

            wf = load_workflow(workflow_path)

            await _poll_tick(tracker, workspace_mgr, wf.config)

            log.info("poll_tick_complete", next_poll_in_s=poll_interval)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await tracker.close()
        log.info("service_stopped")


def run_service(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_run_service(args))
    except KeyboardInterrupt:
        pass
