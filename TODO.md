# Pyphony Implementation Plan

## 0. CLAUDE.md + TODO.md
- [x] Create CLAUDE.md
- [x] Create TODO.md
- [x] Create pyproject.toml

## 1. Scaffolding + Domain Models + Normalization
- [ ] `src/pyphony/__init__.py`
- [ ] `src/pyphony/models.py` — Pydantic models: Issue, BlockerRef, WorkflowDefinition, ServiceConfig, Workspace, RunAttempt, LiveSession, RetryEntry, OrchestratorRuntimeState
- [ ] `src/pyphony/errors.py` — typed errors
- [ ] `src/pyphony/normalization.py` — sanitize_workspace_key, normalize_state, sort_issues_for_dispatch
- [ ] Tests: sanitization, sort order, state normalization, Pydantic validation

## 2. Workflow Loader + Config Layer
- [ ] `src/pyphony/workflow.py` — load_workflow(path)
- [ ] `src/pyphony/config.py` — ServiceConfig.from_workflow() with defaults, $VAR resolution, ~ expansion, validation
- [ ] Tests: valid WORKFLOW.md, no front matter, non-map error, missing file, $VAR resolution, ~ expansion, defaults, comma-separated states, dispatch preflight validation

## 3. Prompt Rendering
- [ ] `src/pyphony/prompt.py` — Jinja2 StrictUndefined render_prompt
- [ ] Tests: render with issue fields, attempt null/int, nested labels, unknown variable error, unknown filter error, empty body default, malformed syntax error

## 4. Workspace Manager
- [ ] `src/pyphony/workspace.py` — WorkspaceManager: create/reuse dirs, hooks, safety invariants
- [ ] Tests: create + reuse, after_create only on new, before_run failure aborts, after_run failure ignored, hook timeout, path traversal blocked, cleanup with before_remove

## 5. Linear Issue Tracker Client
- [ ] `src/pyphony/tracker.py` — LinearClient (httpx async)
- [ ] `src/pyphony/tracker_queries.py` — GraphQL queries
- [ ] Tests (respx): single/multi-page fetch, normalization, empty states, error mapping

## MVP 1: Minimal Service
- [ ] `src/pyphony/logging.py` — structlog config
- [ ] `src/pyphony/cli.py` — CLI argument parsing
- [ ] `src/pyphony/service.py` — start_service() with poll loop
- [ ] `src/pyphony/__main__.py` — entry point
- [ ] Manual test: `uv run python -m pyphony WORKFLOW.md`

## 6. Agent Runner + Agent Protocol
- [ ] `src/pyphony/protocol.py` — JSON-RPC message builders/parsers
- [ ] `src/pyphony/agent.py` — AppServerClient + AgentRunner
- [ ] `tests/helpers/fake_agent.py` — mock subprocess
- [ ] Tests: handshake, session_id, turn completion/failure, timeouts, subprocess exit, multi-turn, max_turns

## 7. Orchestrator Core
- [ ] `src/pyphony/orchestrator.py` — dispatch, concurrency, retry with backoff
- [ ] Tests: priority sort, blocker rules, concurrency limits, retry behavior

## 8. Reconciliation + Stall Detection
- [ ] Extend orchestrator: reconcile_running_issues, startup_terminal_cleanup
- [ ] Tests: terminal kill+clean, stall detection, startup cleanup

## 9. Dynamic Reload + Logging Improvements
- [ ] `src/pyphony/watcher.py` — watchfiles for WORKFLOW.md
- [ ] Tests: file change reload, invalid reload keeps last good

## 10. Optional HTTP Server
- [ ] `src/pyphony/server.py` — Starlette dashboard + API
- [ ] Tests: GET state, GET issue, 404, POST refresh, 405, dashboard HTML
