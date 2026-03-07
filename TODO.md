# Pyphony Implementation Plan

## 0. CLAUDE.md + TODO.md
- [x] Create CLAUDE.md
- [x] Create TODO.md
- [x] Create pyproject.toml

## 1. Scaffolding + Domain Models + Normalization
- [x] `src/pyphony/__init__.py`
- [x] `src/pyphony/models.py` — Pydantic models
- [x] `src/pyphony/errors.py` — typed errors
- [x] `src/pyphony/normalization.py` — sanitize_workspace_key, normalize_state, sort_issues_for_dispatch
- [x] Tests: sanitization, sort order, state normalization, Pydantic validation

## 2. Workflow Loader + Config Layer
- [x] `src/pyphony/workflow.py` — load_workflow(path)
- [x] `src/pyphony/config.py` — ServiceConfig with defaults, $VAR resolution, ~ expansion, validation
- [x] Tests: valid WORKFLOW.md, no front matter, non-map error, missing file, $VAR resolution, ~ expansion, defaults, comma-separated states, dispatch preflight validation

## 3. Prompt Rendering
- [x] `src/pyphony/prompt.py` — Jinja2 StrictUndefined render_prompt
- [x] Tests: render with issue fields, attempt null/int, nested labels, unknown variable/filter error, empty body default, malformed syntax error

## 4. Workspace Manager
- [x] `src/pyphony/workspace.py` — WorkspaceManager: create/reuse dirs, hooks, safety invariants
- [x] Tests: create + reuse, after_create only on new, before_run failure aborts, after_run failure ignored, hook timeout, path traversal blocked, cleanup with before_remove

## 5. Linear Issue Tracker Client
- [x] `src/pyphony/tracker.py` — LinearClient (httpx async)
- [x] `src/pyphony/tracker_queries.py` — GraphQL queries
- [x] Tests (respx): single/multi-page fetch, normalization, empty states, error mapping

## MVP 1: Minimal Service
- [x] `src/pyphony/logging.py` — structlog config
- [x] `src/pyphony/cli.py` — CLI argument parsing
- [x] `src/pyphony/service.py` — start_service() with poll loop
- [x] `src/pyphony/__main__.py` — entry point

## 6. Agent Runner + Agent Protocol
- [ ] `src/pyphony/protocol.py` — JSON-RPC message builders/parsers
- [ ] `src/pyphony/agent.py` — AppServerClient + AgentRunner
- [ ] `tests/helpers/fake_agent.py` — mock subprocess
- [ ] Tests: handshake, session_id, turn completion/failure, timeouts, subprocess exit, multi-turn, max_turns

## 7. Orchestrator Core
- [x] `src/pyphony/orchestrator.py` — dispatch, concurrency, retry with backoff
- [x] Tests: priority sort, blocker rules, concurrency limits, retry behavior

## 8. Reconciliation + Stall Detection
- [x] Reconciliation, stall detection, startup cleanup (included in orchestrator.py)
- [x] Tests: terminal kill+clean, stall detection, startup cleanup

## 9. Dynamic Reload + Logging Improvements
- [x] `src/pyphony/watcher.py` — watchfiles for WORKFLOW.md
- [x] Tests: file change reload, invalid reload keeps last good

## 10. Optional HTTP Server
- [x] `src/pyphony/server.py` — Starlette dashboard + API
- [x] Tests: GET state, GET issue, 404, POST refresh, dashboard HTML
