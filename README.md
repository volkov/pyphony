# Pyphony

Python-реализация [Symphony Service Specification](SPEC.md) — long-running сервис, который поллит Linear, создаёт изолированные workspace'ы и запускает coding agent (Claude Code) для каждого issue.

## Быстрый старт

```bash
uv sync
./pyphony run WORKFLOW.md
```

## WORKFLOW.md

```markdown
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: my-project
polling:
  interval_ms: 30000
workspace:
  root: ~/symphony_workspaces
hooks:
  after_create: git clone git@github.com:org/repo.git .
agent:
  max_concurrent_agents: 5
claude:
  command: claude
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}
```

## Возможности

- Поллинг Linear с пагинацией и нормализацией issue
- Приоритизация dispatch (priority → created_at → identifier)
- Global и per-state concurrency limits
- Workspace lifecycle hooks (after_create, before_run, after_run, before_remove)
- Jinja2 prompt templates с strict undefined
- JSON-RPC протокол для agent app-server (multi-turn)
- Exponential backoff retry + continuation retry
- Reconciliation: stall detection, terminal cleanup
- Hot reload WORKFLOW.md через watchfiles
- HTTP dashboard и REST API (`--port 8080`)

## CLI

```bash
./pyphony run WORKFLOW.md                          # запуск
./pyphony run --port 8080 WORKFLOW.md              # с HTTP сервером
./pyphony run --log-level DEBUG w.md               # verbose логи
./pyphony get-issue SER-52                         # получить задачу
./pyphony update-issue SER-52 --state "Done"       # обновить задачу
./pyphony create-issue --title "..."               # создать задачу
./pyphony list-candidates                          # кандидаты на dispatch
./pyphony check-issue SER-52                       # диагностика dispatch
./pyphony prompt-view SER-52                       # показать промпт
```

## Тесты

```bash
uv run pytest           # все 166 тестов
uv run pytest -v        # verbose
uv run pytest -k agent  # только agent тесты
```

## Стек

Python 3.12+, pydantic, httpx, jinja2, structlog, watchfiles, starlette, pytest
