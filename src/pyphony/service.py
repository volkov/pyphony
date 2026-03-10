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


class _WorkflowContext:
    """Holds all components for a single workflow."""

    def __init__(
        self,
        workflow_path: Path,
        orchestrator: Orchestrator,
        tracker: LinearClient,
        agent_runner: AgentRunner,
    ) -> None:
        self.workflow_path = workflow_path
        self.orchestrator = orchestrator
        self.tracker = tracker
        self.agent_runner = agent_runner


async def _run_workflow_loop(
    ctx: _WorkflowContext,
    stop_event: asyncio.Event,
) -> None:
    """Independent poll loop for a single workflow."""
    wf_name = ctx.workflow_path.stem

    await ctx.orchestrator.startup_terminal_cleanup()

    while not stop_event.is_set():
        wf = load_workflow(ctx.workflow_path)
        new_config = service_config_from_workflow(wf.config, workflow_path=ctx.workflow_path)
        ctx.orchestrator.update_config(new_config)
        ctx.agent_runner._prompt_template = wf.prompt_template

        poll_interval = new_config.polling.interval_ms / 1000.0

        stats = await ctx.orchestrator.poll_tick()

        log.info(
            "poll_tick",
            workflow=wf_name,
            next_poll_in_s=poll_interval,
            **stats,
        )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


async def _run_service(args: argparse.Namespace) -> None:
    configure_logging(args.log_level, log_file=getattr(args, "log_file", None))

    workflow_paths = [Path(f) for f in args.workflow_files]
    log.info("starting_service", workflow_files=[str(p) for p in workflow_paths])

    # Build contexts for each workflow
    contexts: list[_WorkflowContext] = []
    for workflow_path in workflow_paths:
        wf = load_workflow(workflow_path)
        config = service_config_from_workflow(wf.config, workflow_path=workflow_path)

        errors = validate_dispatch_config(config)
        if errors:
            for err in errors:
                log.error("startup_validation_failed", workflow=str(workflow_path), error=err)
            raise SystemExit(1)

        log.info(
            "config_loaded",
            workflow=workflow_path.stem,
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

        contexts.append(_WorkflowContext(workflow_path, orchestrator, tracker, agent_runner))

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    exit_on_merge = getattr(args, "exit_on_merge", False)
    if exit_on_merge:
        for ctx in contexts:
            ctx.orchestrator.exit_on_merge = True
            ctx.orchestrator.merge_detected_event = stop_event
        log.info("exit_on_merge_enabled")

    # Start HTTP server if port is configured
    server_started = False
    port = getattr(args, "port", None)
    if port is None and contexts:
        # Use port from first workflow config if available
        first_config = contexts[0].orchestrator._config
        if first_config.server and first_config.server.port:
            port = first_config.server.port

    if port:
        try:
            from .server import create_app
            import uvicorn

            def _get_combined_state():
                """Aggregate state from all orchestrators."""
                from .models import OrchestratorRuntimeState, AgentTotals
                combined = OrchestratorRuntimeState(
                    poll_interval_ms=0,
                    max_concurrent_agents=0,
                )
                for ctx in contexts:
                    st = ctx.orchestrator.state
                    combined.running.update(st.running)
                    combined.retry_attempts.update(st.retry_attempts)
                    combined.claimed.update(st.claimed)
                    combined.agent_totals.input_tokens += st.agent_totals.input_tokens
                    combined.agent_totals.output_tokens += st.agent_totals.output_tokens
                    combined.agent_totals.total_tokens += st.agent_totals.total_tokens
                    combined.agent_totals.seconds_running += st.agent_totals.seconds_running
                return combined

            app = create_app(get_state_fn=_get_combined_state)
            config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
            server = uvicorn.Server(config)
            asyncio.create_task(server.serve())
            log.info("http_server_started", port=port)
            server_started = True
        except Exception as exc:
            log.warning("http_server_failed", error=str(exc))

    # Run all workflow loops concurrently
    tasks = [
        asyncio.create_task(_run_workflow_loop(ctx, stop_event))
        for ctx in contexts
    ]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Shutdown all orchestrators and trackers
        for ctx in contexts:
            await ctx.orchestrator.shutdown()
            await ctx.tracker.close()
        log.info("service_stopped")

    # If any orchestrator detected a merge, exit with code 10
    if exit_on_merge:
        for ctx in contexts:
            if ctx.orchestrator.merge_detected:
                raise SystemExit(10)


def run_service(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_run_service(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
