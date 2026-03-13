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

# CLI subcommands (WORKFLOW.md is used by default):
./pyphony list-candidates                                         # Show dispatchable issues
./pyphony check-issue SER-52                                      # Why is/isn't issue dispatched
./pyphony get-issue SER-52                                        # Fetch issue from Linear
./pyphony create-issue --title "..." [--description "..."]        # Create issue
./pyphony update-issue SER-52 [--title/--description/--state]     # Update issue
./pyphony prompt-view SER-52                                      # Show rendered prompt for issue
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

## Python Environment

When running Python commands inside a workspace, **always use `uv run`** (e.g. `uv run python`, `uv run pytest`). Never use bare `python` or `pip` — the workspace may not have its own virtualenv, and bare `python` may resolve to the orchestrator's interpreter with wrong import paths.

If you need to install dependencies before running tests, run `uv sync` first.

## Test Project (Linear)

Для тестирования взаимодействия с Linear API есть специальный тестовый проект **Pyphony Test Sandbox** (slug: `2143f8bc976f`).

Используй его когда нужно проверить поведение тикетов через API — создание, обновление статусов, комментарии, лейблы и т.д. Тикеты в этом проекте одноразовые, можно свободно создавать и менять.

```bash
# Создать тикет в тестовом проекте:
./pyphony create-issue --title "Test ticket" --project-slug 2143f8bc976f

# Получить тикет:
./pyphony get-issue SER-<number>

# Обновить тикет:
./pyphony update-issue SER-<number> --state "In Progress"
```

**Важно:** не используй основной проект Pyphony (slug: `1fc8e25cc22b`) для тестов — только `2143f8bc976f`.

## Subagent Instructions

Before starting work, read `TODO.md` for the current implementation status.
After completing a subtask, mark completed items with `[x]` in `TODO.md`.
