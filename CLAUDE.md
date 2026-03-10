# Pyphony

Python implementation of the Symphony Service Specification.

## Спецификации

- `spec.md` — краткий обзор системы (макс 2 страницы), ссылается на детальные спеки
- `specs/symphony.md` — полная language-agnostic спецификация Symphony

**Важно:** при изменении функциональности обновляй соответствующие спецификации. Если добавляешь новую подсистему — добавь спеку в `specs/` и ссылку в `spec.md`. Используй `/write-spec` для создания и обновления спек.

## Stack

- Python 3.12+, uv, asyncio
- pydantic for domain models
- httpx for async HTTP (Linear GraphQL)
- jinja2 for prompt templates
- structlog for structured logging
- watchfiles for WORKFLOW.md hot reload
- starlette/uvicorn for optional HTTP server
- pytest + pytest-asyncio + respx for tests

## Commands

```bash
uv run pytest                        # Run all tests
uv run pytest tests/test_models.py   # Run specific test file
uv run python -m pyphony WORKFLOW.md # Start the service

# CLI subcommands (all take WORKFLOW.md as positional arg):
uv run python -m pyphony list-candidates WORKFLOW.md              # Show dispatchable issues
uv run python -m pyphony check-issue SER-52 WORKFLOW.md           # Why is/isn't issue dispatched
uv run python -m pyphony get-issue WORKFLOW.md --identifier SER-52      # Fetch issue from Linear
uv run python -m pyphony create-issue WORKFLOW.md --title "..." [--description "..."]
uv run python -m pyphony update-issue WORKFLOW.md --identifier SER-52 [--title/--description/--state]
uv run python -m pyphony prompt-view SER-52 WORKFLOW.md           # Show rendered prompt for issue
```

## Package Structure

```
src/pyphony/
  __init__.py
  __main__.py        # Entry point
  models.py          # Pydantic domain entities
  errors.py          # Typed error classes
  normalization.py   # Workspace key sanitization, state normalization, sorting
  workflow.py        # WORKFLOW.md loader (YAML front matter + prompt body)
  config.py          # Typed config getters, defaults, $VAR resolution
  prompt.py          # Jinja2 strict template rendering
  workspace.py       # WorkspaceManager with hooks and safety invariants
  tracker.py         # Linear GraphQL client
  tracker_queries.py # GraphQL query strings
  protocol.py        # JSON-RPC message builders/parsers for agent app-server
  agent.py           # AppServerClient + AgentRunner
  orchestrator.py    # State machine, dispatch, retry, reconciliation
  watcher.py         # WORKFLOW.md file watcher
  logging.py         # structlog configuration
  service.py         # start_service() main entry point
  cli.py             # CLI argument parsing
  server.py          # Optional HTTP server (Starlette)
```

## Subagent Instructions

Before starting work, read `TODO.md` for the current implementation status.
After completing a subtask, mark completed items with `[x]` in `TODO.md`.
