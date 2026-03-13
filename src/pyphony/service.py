from __future__ import annotations

import argparse
import asyncio
import signal
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .agent import AgentRunner
from .config import service_config_from_workflow, validate_dispatch_config
from .logging import configure_logging
from .models import ServiceConfig, WorkflowDefinition
from .orchestrator import Orchestrator
from .tracker import LinearClient
from .watcher import WorkflowWatcher
from .workflow import load_workflow
from .workspace import WorkspaceManager

log = structlog.stdlib.get_logger()


@dataclass
class _ProcessorGeneration:
    """A single config-generation of a workflow processor."""

    orchestrator: Orchestrator
    agent_runner: AgentRunner
    generation: int = 0


class _WorkflowContext:
    """Holds all components for a single workflow, including multiple processor
    generations that can coexist during rolling replacement."""

    def __init__(
        self,
        workflow_path: Path,
        orchestrator: Orchestrator,
        tracker: LinearClient,
        agent_runner: AgentRunner,
        workspace_mgr: WorkspaceManager,
    ) -> None:
        self.workflow_path = workflow_path
        self.tracker = tracker
        self.workspace_mgr = workspace_mgr
        self._generation_counter = 0
        self.generations: list[_ProcessorGeneration] = [
            _ProcessorGeneration(
                orchestrator=orchestrator,
                agent_runner=agent_runner,
                generation=0,
            )
        ]

    # ------------------------------------------------------------------
    # Convenience accessors (point at the *active* generation)
    # ------------------------------------------------------------------

    @property
    def orchestrator(self) -> Orchestrator:
        return self.generations[-1].orchestrator

    @property
    def agent_runner(self) -> AgentRunner:
        return self.generations[-1].agent_runner

    @property
    def all_orchestrators(self) -> list[Orchestrator]:
        return [g.orchestrator for g in self.generations]

    # ------------------------------------------------------------------
    # Rolling replacement helpers
    # ------------------------------------------------------------------

    def _draining_issue_ids(self) -> set[str]:
        """Return issue IDs handled by all draining (non-active) orchestrators."""
        ids: set[str] = set()
        for gen in self.generations[:-1]:
            ids.update(gen.orchestrator.state.running.keys())
            ids.update(gen.orchestrator.state.claimed)
        return ids

    def _peer_running_count(self) -> int:
        """Return the number of agents running on draining orchestrators."""
        return sum(
            len(gen.orchestrator.state.running)
            for gen in self.generations[:-1]
        )

    def spawn_generation(
        self, config: ServiceConfig, prompt_template: str,
    ) -> _ProcessorGeneration:
        """Create a new processor generation and drain the current active one."""
        current = self.generations[-1]
        current.orchestrator._enter_drain_mode("config_reload", kind="reload")

        self._generation_counter += 1
        new_agent_runner = AgentRunner(
            config, self.workspace_mgr, prompt_template, tracker=self.tracker,
        )
        new_orchestrator = Orchestrator(
            config,
            self.tracker,
            self.workspace_mgr,
            run_agent_fn=new_agent_runner.run,
            prompt_template=prompt_template,
            excluded_issue_ids_fn=self._draining_issue_ids,
            peer_running_fn=self._peer_running_count,
        )
        # Inherit exit_on_merge settings from previous generation
        if current.orchestrator.exit_on_merge:
            new_orchestrator.exit_on_merge = True
            new_orchestrator.merge_detected_event = current.orchestrator.merge_detected_event

        gen = _ProcessorGeneration(
            orchestrator=new_orchestrator,
            agent_runner=new_agent_runner,
            generation=self._generation_counter,
        )
        self.generations.append(gen)

        log.info(
            "processor_generation_spawned",
            workflow=self.workflow_path.stem,
            generation=gen.generation,
            draining_generations=len(self.generations) - 1,
        )
        return gen

    def reap_drained(self) -> list[_ProcessorGeneration]:
        """Remove and return fully-drained generations (never the active one)."""
        reaped: list[_ProcessorGeneration] = []
        alive: list[_ProcessorGeneration] = []
        for gen in self.generations:
            # Never reap the active (last) generation
            if gen is self.generations[-1]:
                alive.append(gen)
            elif gen.orchestrator.is_fully_drained:
                reaped.append(gen)
            else:
                alive.append(gen)
        self.generations = alive
        return reaped


def _configs_differ(a: ServiceConfig, b: ServiceConfig) -> bool:
    """Return True if two configs differ in a meaningful way."""
    return a.model_dump() != b.model_dump()


async def _run_workflow_loop(
    ctx: _WorkflowContext,
    stop_event: asyncio.Event,
) -> None:
    """Independent poll loop for a single workflow with rolling replacement."""
    wf_name = ctx.workflow_path.stem

    await ctx.orchestrator.startup_terminal_cleanup()

    # Set up file watcher for event-driven reload detection
    pending_reload: tuple[WorkflowDefinition, ServiceConfig] | None = None
    reload_lock = asyncio.Lock()

    async def _on_reload(wf: WorkflowDefinition, config: ServiceConfig) -> None:
        nonlocal pending_reload
        async with reload_lock:
            pending_reload = (wf, config)

    watcher = WorkflowWatcher(ctx.workflow_path, on_reload=_on_reload)
    try:
        watcher.load_initial()
    except Exception:
        pass  # Initial load already done in _run_service
    await watcher.start()

    try:
        while not stop_event.is_set():
            # ----------------------------------------------------------
            # Check for pending config reload → rolling replacement
            # ----------------------------------------------------------
            async with reload_lock:
                reload_data = pending_reload
                pending_reload = None

            if reload_data is not None:
                new_wf, new_config = reload_data
                current_config = ctx.orchestrator._config
                if _configs_differ(current_config, new_config) or \
                   ctx.agent_runner._prompt_template != new_wf.prompt_template:
                    log.info(
                        "config_change_detected",
                        workflow=wf_name,
                        new_generation=ctx._generation_counter + 1,
                    )
                    ctx.spawn_generation(new_config, new_wf.prompt_template)
                else:
                    # Config unchanged — apply minor updates in-place
                    ctx.orchestrator.update_config(new_config)
                    ctx.agent_runner._prompt_template = new_wf.prompt_template

            # ----------------------------------------------------------
            # Poll the active orchestrator
            # ----------------------------------------------------------
            poll_interval = ctx.orchestrator._config.polling.interval_ms / 1000.0

            stats = await ctx.orchestrator.poll_tick()

            # Also tick draining orchestrators so they reconcile state
            for gen in ctx.generations[:-1]:
                await gen.orchestrator.poll_tick()

            # Reap fully-drained generations
            reaped = ctx.reap_drained()
            for gen in reaped:
                await gen.orchestrator.shutdown()
                log.info(
                    "processor_generation_reaped",
                    workflow=wf_name,
                    generation=gen.generation,
                )

            draining_count = len(ctx.generations) - 1
            log.info(
                "poll_tick",
                workflow=wf_name,
                next_poll_in_s=poll_interval,
                generation=ctx.generations[-1].generation,
                draining_generations=draining_count,
                **(stats or {}),
            )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await watcher.stop()


async def _run_service(args: argparse.Namespace) -> None:
    configure_logging(args.log_level, log_file=getattr(args, "log_file", None))

    workflow_paths = [Path(f) for f in args.workflow_files]
    log.info("starting_service", workflow_files=[str(p) for p in workflow_paths])

    # Build contexts for each workflow
    pyphony_slug = getattr(args, "pyphony_slug", None)
    # If no explicit --pyphony-slug, use the first workflow's project_slug
    # (first workflow is always pyphony / WORKFLOW.md) so that bug reports
    # created from any workflow land in the pyphony project.
    if not pyphony_slug and workflow_paths:
        first_wf = load_workflow(workflow_paths[0])
        first_cfg = service_config_from_workflow(
            first_wf.config, workflow_path=workflow_paths[0],
        )
        pyphony_slug = first_cfg.tracker.project_slug

    contexts: list[_WorkflowContext] = []
    for workflow_path in workflow_paths:
        wf = load_workflow(workflow_path)
        config = service_config_from_workflow(wf.config, workflow_path=workflow_path)

        # Ensure bug reports always go to the pyphony project
        if pyphony_slug:
            config.tracker.pyphony_slug = pyphony_slug

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
            config, tracker, workspace_mgr, run_agent_fn=agent_runner.run,
            prompt_template=wf.prompt_template,
        )

        contexts.append(
            _WorkflowContext(workflow_path, orchestrator, tracker, agent_runner, workspace_mgr)
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
        # Use a separate event so that a single orchestrator's drain completion
        # does NOT immediately stop the whole service.  The drain coordinator
        # below will wait for ALL orchestrators to finish before setting
        # stop_event.
        merge_trigger = asyncio.Event()
        has_restart_orchestrators = False
        for ctx in contexts:
            if not ctx.orchestrator._config.supervisor_restart:
                continue
            has_restart_orchestrators = True
            for orch in ctx.all_orchestrators:
                orch.exit_on_merge = True
                orch.merge_detected_event = merge_trigger
        if has_restart_orchestrators:
            log.info("exit_on_merge_enabled")
        else:
            log.info("exit_on_merge_skipped_no_supervisor_restart_workflows")
            exit_on_merge = False

        async def _drain_coordinator() -> None:
            """Wait for any orchestrator to signal merge, drain all, then stop."""
            await merge_trigger.wait()
            log.info("drain_coordinator_triggered")

            # Put every orchestrator into drain mode so none dispatch new work.
            for ctx in contexts:
                for orch in ctx.all_orchestrators:
                    orch._enter_drain_mode("exit_on_merge_coordination")

            # Wait until every orchestrator has zero running agents.
            while True:
                total_running = sum(
                    len(orch.state.running)
                    for ctx in contexts
                    for orch in ctx.all_orchestrators
                )
                if total_running == 0:
                    break
                log.info("drain_coordinator_waiting", total_running=total_running)
                await asyncio.sleep(2)

            log.info("drain_coordinator_complete")
            stop_event.set()

        asyncio.create_task(_drain_coordinator())

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
                """Aggregate state from all orchestrators across all generations."""
                from .models import OrchestratorRuntimeState, AgentTotals
                combined = OrchestratorRuntimeState(
                    poll_interval_ms=0,
                    max_concurrent_agents=0,
                )
                for ctx in contexts:
                    for orch in ctx.all_orchestrators:
                        st = orch.state
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
        # Shutdown all orchestrators (all generations) and trackers
        for ctx in contexts:
            for orch in ctx.all_orchestrators:
                await orch.shutdown()
            await ctx.tracker.close()
        log.info("service_stopped")

    # If any orchestrator detected a merge, exit with code 10
    if exit_on_merge:
        for ctx in contexts:
            for orch in ctx.all_orchestrators:
                if orch.merge_detected:
                    raise SystemExit(10)


def run_service(args: argparse.Namespace) -> None:
    try:
        asyncio.run(_run_service(args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
