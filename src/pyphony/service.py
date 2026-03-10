from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

import structlog

from .agent import AgentRunner
from .config import service_config_from_workflow, validate_dispatch_config
from .logging import configure_logging
from .orchestrator import Orchestrator
from .tracker import LinearClient
from .workflow import load_workflow
from .workspace import WorkspaceManager

log = structlog.stdlib.get_logger()


async def _run_service(args: argparse.Namespace) -> None:
    configure_logging(args.log_level, log_file=getattr(args, "log_file", None))

    workflow_path = Path(args.workflow_file)
    log.info("starting_service", workflow_file=str(workflow_path))

    wf = load_workflow(workflow_path)
    config = service_config_from_workflow(wf.config, workflow_path=workflow_path)

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
    agent_runner = AgentRunner(config, workspace_mgr, wf.prompt_template, tracker=tracker)
    orchestrator = Orchestrator(
        config, tracker, workspace_mgr, run_agent_fn=agent_runner.run
    )

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    exit_on_merge = getattr(args, "exit_on_merge", False)
    if exit_on_merge:
        orchestrator.exit_on_merge = True
        orchestrator.merge_detected_event = stop_event
        log.info("exit_on_merge_enabled")

    await orchestrator.startup_terminal_cleanup()

    try:
        while not stop_event.is_set():
            wf = load_workflow(workflow_path)
            new_config = service_config_from_workflow(wf.config, workflow_path=workflow_path)
            orchestrator.update_config(new_config)
            agent_runner._prompt_template = wf.prompt_template

            poll_interval = new_config.polling.interval_ms / 1000.0

            stats = await orchestrator.poll_tick()

            log.info(
                "poll_tick",
                workflow=workflow_path.stem,
                next_poll_in_s=poll_interval,
                **stats,
            )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await orchestrator.shutdown()
        await tracker.close()
        log.info("service_stopped")

    if exit_on_merge and orchestrator.merge_detected:
        raise SystemExit(10)


def run_service(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_run_service(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
