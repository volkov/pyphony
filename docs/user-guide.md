# Pyphony — User Guide

Pyphony — сервис, который автоматически берёт задачи из Linear и запускает Claude Code агентов для их выполнения. Этот гайд описывает, как пользоваться уже настроенным и работающим сервисом.

## Содержание

- [Обзор](#обзор)
- [Автоматический режим](#автоматический-режим)
- [Интерактивный режим (work)](#интерактивный-режим-work)
- [Управление задачами через CLI](#управление-задачами-через-cli)
- [Диагностика](#диагностика)
- [Специальные лейблы](#специальные-лейблы)
- [Supervisor](#supervisor)
- [HTTP Dashboard](#http-dashboard)
- [URL-схема (pyphony://)](#url-схема-pyphony)

---

## Обзор

Pyphony работает в цикле:
1. Поллит Linear каждые 30 секунд
2. Находит задачи в статусе **Todo** или **In Progress**
3. Создаёт изолированный workspace (git worktree) для каждой задачи
4. Запускает Claude Code агента с промптом из WORKFLOW.md
5. Агент работает, коммитит, создаёт PR
6. По завершении — автоматически мержит PR и переводит задачу в Done

Подробнее: [spec.md](../spec.md), [specs/overview.md](../specs/overview.md)

## Автоматический режим

### Запуск сервиса

```bash
# Основной запуск
./pyphony run

# С HTTP dashboard
./pyphony run --port 8080

# С детальным логированием
./pyphony run --log-level DEBUG
```

По умолчанию используется файл `WORKFLOW.md` из текущей директории.

### Как поставить задачу агенту

1. **Создать задачу в Linear** в нужном проекте (или через CLI — см. ниже)
2. **Перевести в Todo** — сервис подхватит задачу на следующем poll-цикле
3. **Дождаться результата** — агент создаст PR, пришлёт комментарий с результатом и переведёт задачу в Done

### Что происходит автоматически

- **Workspace** — создаётся `~/symphony_workspaces/<ISSUE-ID>`, клонируется репозиторий, создаётся ветка
- **Промпт** — рендерится из WORKFLOW.md с данными задачи (title, description, comments)
- **Агент** — работает до `[DONE]` или исчерпания turns
- **PR** — автоматический squash merge (если нет лейбла `review required`)
- **Retry** — при сбое агент перезапускается с exponential backoff
- **Reconciliation** — зависшие сессии обнаруживаются и останавливаются

Подробнее: [specs/orchestration.md](../specs/orchestration.md), [specs/workspace.md](../specs/workspace.md)

### Конкурентность

По умолчанию до 5 агентов работают параллельно (настраивается в `agent.max_concurrent_agents`). Можно задать лимиты per-state:

```yaml
agent:
  max_concurrent_agents: 5
  max_concurrent_agents_by_state:
    Todo: 3
    In Progress: 5
```

### Приоритизация

Задачи диспатчатся в порядке: **priority** (меньше = важнее) → **created_at** (старые первыми) → **identifier** (лексикографически).

## Интерактивный режим (work)

Команда `work` позволяет работать над задачей вместе с Claude Code в интерактивном режиме — вы общаетесь с агентом прямо в терминале.

```bash
# Запустить интерактивную сессию
./pyphony work SER-42

# Работать в основном репозитории (без worktree)
./pyphony work SER-42 --main
```

### Что делает `work`:

1. Загружает задачу из Linear (title, description, comments)
2. Рендерит промпт из WORKFLOW.md
3. Создаёт или переиспользует workspace
4. Переводит задачу в «In Progress» (если была «Todo»)
5. Запускает `claude` в интерактивном режиме — вы общаетесь с агентом
6. После завершения сессии:
   - Находит транскрипт
   - Собирает URL PR-ов (из Linear и из транскрипта)
   - Автоматически мержит PR-ы (если нет лейбла `review required`)
   - Постит результат как комментарий к задаче
   - Переводит задачу в Done (или In Review)

Подробнее: [specs/interactive.md](../specs/interactive.md)

## Управление задачами через CLI

Все команды работают с Linear API напрямую:

### Создание задачи

```bash
# В Backlog (не будет автоматически взята)
./pyphony create-issue --title "Добавить поддержку X"

# Сразу в Todo (агент возьмёт на следующем цикле)
./pyphony create-issue --title "Fix bug Y" --state "Todo"

# С описанием
./pyphony create-issue --title "Рефакторинг Z" --description "Нужно переписать модуль Z"
```

### Просмотр и обновление

```bash
# Получить задачу
./pyphony get-issue SER-42

# Обновить статус
./pyphony update-issue SER-42 --state "Done"

# Обновить описание
./pyphony update-issue SER-42 --description "Новое описание"

# Поиск задач по статусу
./pyphony search-issues --state "Todo,In Progress"
```

### Комментарии и лейблы

```bash
# Добавить комментарий
./pyphony comment-issue SER-42 --body "Нужно учесть edge case X"

# Добавить лейбл
./pyphony label-issue SER-42 --add "review required"

# Убрать лейбл
./pyphony label-issue SER-42 --remove "plan required"
```

Подробнее: [specs/tracker.md](../specs/tracker.md)

## Диагностика

### Почему задача не берётся?

```bash
# Показать все задачи и кандидатов на dispatch
./pyphony list-candidates

# Детальная диагностика конкретной задачи
./pyphony check-issue SER-42
```

`check-issue` покажет:
- В каком проекте задача (совпадает ли с настроенным?)
- В каком состоянии (активное ли?)
- Есть ли нерешённые блокеры

### Просмотр промпта

```bash
# Показать промпт, который получит агент
./pyphony prompt-view SER-42
```

### Логи

Логи пишутся в `logs/pyphony.log` (настраивается через `--log-file`). Формат — structured JSON (structlog).

```bash
# Запуск с детальным логированием
./pyphony run --log-level DEBUG
```

Подробнее: [specs/observability.md](../specs/observability.md)

## Специальные лейблы

Лейблы на задачах в Linear управляют поведением сервиса:

| Лейбл | Эффект |
|--------|--------|
| `plan required` | Агент работает в read-only режиме: исследует кодовую базу и пишет план. По завершении лейбл заменяется на `with plan`, задача уходит в In Review |
| `review required` | PR не мержится автоматически. Задача переводится в In Review вместо Done |
| `research` | Аналогично `plan required` — агент исследует и возвращает результат, не модифицирует код |
| `resolve conflict` | Агент запускается для разрешения merge-конфликтов |
| `with plan` | Задача с готовым планом (выставляется автоматически после plan required) |

### Типичный workflow с планом

1. Создать задачу с лейблом `plan required`
2. Перевести в Todo — агент напишет план реализации
3. Проверить план в комментарии к задаче
4. Убрать `with plan`, при необходимости скорректировать описание
5. Перевести в Todo — агент выполнит задачу

Подробнее: [specs/orchestration.md](../specs/orchestration.md)

## Supervisor

`pyphony-sv` — процесс-супервизор, который автоматически обновляет код и перезапускает сервис:

```bash
# Запуск с автообновлением
pyphony-sv

# С конкретными workflow файлами
pyphony-sv workflows/project1.md workflows/project2.md

# С дополнительными аргументами для pyphony
pyphony-sv -- --port 8080 --log-level DEBUG
```

### Цикл работы supervisor-а

1. `git pull --rebase` — обновляет код
2. `uv sync` — обновляет зависимости
3. Запускает `pyphony run --exit-on-merge`
4. Когда агент мержит PR и задача переходит в Done, сервис выходит с кодом 10
5. Supervisor пуллит обновлённый код и перезапускает сервис

Это гарантирует, что агенты всегда работают с актуальной версией кодовой базы.

Подробнее: [specs/supervisor.md](../specs/supervisor.md)

## HTTP Dashboard

При запуске с `--port` поднимается HTTP-сервер:

```bash
./pyphony run --port 8080
```

### Endpoints

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/` | GET | HTML dashboard |
| `/api/v1/state` | GET | Текущее состояние: running sessions, retry queue, token totals |
| `/api/v1/{identifier}` | GET | Детали конкретной задачи (issue, attempt, session) |
| `/api/v1/refresh` | POST | Запросить обновление состояния |

### Пример ответа `/api/v1/state`

```json
{
  "running": [
    {
      "issue_id": "...",
      "issue_identifier": "SER-42",
      "state": "In Progress",
      "turn_count": 5
    }
  ],
  "retrying": [],
  "agent_totals": {
    "input_tokens": 150000,
    "output_tokens": 30000,
    "total_tokens": 180000,
    "seconds_running": 342.5
  }
}
```

Подробнее: [specs/observability.md](../specs/observability.md)

## URL-схема (pyphony://)

На macOS можно зарегистрировать URL-схему `pyphony://` для быстрого открытия интерактивных сессий из браузера или Linear:

```bash
# Установить URL handler
./pyphony install-url-scheme

# Открыть сессию по URL
./pyphony open-url pyphony://SER-42/work
```

Формат URL: `pyphony://<ISSUE-ID>/work`

После установки клик по ссылке `pyphony://SER-42/work` откроет новый tab в iTerm2 с интерактивной сессией для задачи.

## Конфигурация (WORKFLOW.md)

Вся конфигурация сервиса хранится в файле WORKFLOW.md — YAML frontmatter + Jinja2 промпт. Сервис отслеживает изменения файла и применяет их без перезапуска (hot reload).

### Ключевые секции

```yaml
---
tracker:
  api_key: $LINEAR_API_KEY      # API ключ Linear (через переменную окружения)
  project_slug: <slug>           # Slug проекта в Linear
  active_states: [Todo, In Progress]  # Какие статусы считать активными

polling:
  interval_ms: 30000             # Интервал поллинга (30 сек)

workspace:
  root: ~/symphony_workspaces    # Корневая директория для workspace-ов

hooks:
  after_create: "git clone ... && git checkout -b $(basename $PWD)"

agent:
  max_concurrent_agents: 5       # Максимум параллельных агентов
  max_turns: 100                 # Максимум turns на сессию
  max_runs: 1                    # Максимум запусков на задачу

claude:
  command: claude                # Команда запуска Claude Code
  stall_timeout_ms: 1800000     # Таймаут на зависание (30 мин)
  turn_timeout_ms: 3600000      # Таймаут на turn (60 мин)
---
```

Переменные `$ENV_VAR` в значениях разрешаются из окружения.

Подробнее: [specs/workflow.md](../specs/workflow.md)
