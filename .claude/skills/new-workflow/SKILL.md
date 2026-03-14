---
name: new-workflow
description: Create a new pyphony workflow file. Use when asked to create a new workflow, add a workflow, or set up a new project workflow — "новый воркфлоу", "new workflow", "создай воркфлоу".
---

# Создание нового воркфлоу

Создай новый workflow-файл для pyphony.

## Когда использовать

Trigger phrases:
- "новый воркфлоу"
- "new workflow"
- "создай воркфлоу"
- "добавь воркфлоу"
- "создай workflow"

## Аргументы

$ARGUMENTS — название проекта, GitHub-репозиторий, или другие детали. Если не указано — спроси у пользователя.

## Что нужно узнать у пользователя

Перед созданием уточни (если не указано в аргументах):

1. **Название проекта** — для имени файла в `workflows/` (например `my-project.md`)
2. **Linear project slug** — slug проекта в Linear. Если пользователь не знает — предложи создать новый проект или найти slug через API
3. **Тип проекта** — для выбора шаблона:
   - **GitHub-репозиторий** (с remote) — клонирование через `after_create` хук, PR при завершении
   - **Локальный репозиторий** (без remote) — работа в локальной папке, только коммиты
4. **GitHub repo URL** — если проект с remote (формат `git@github.com:org/repo.git`)
5. **Дополнительные настройки** (опционально):
   - `max_concurrent_agents` (по умолчанию 5)
   - Нужен ли automerge

## Шаблоны

### Шаблон: GitHub-репозиторий (с remote)

Используй этот шаблон когда проект — это GitHub-репозиторий с возможностью создания PR.

```markdown
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: <PROJECT_SLUG>
polling:
  interval_ms: 30000
hooks:
  after_create: "git clone <REPO_URL> . && git checkout -b $(basename $PWD)"
workspace:
  root: ~/symphony_workspaces
agent:
  max_concurrent_agents: 5
claude:
  command: claude
  stall_timeout_ms: 1800000
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Когда задача выполнена:
1. Закоммить изменения
2. Запушь ветку и создай Pull Request с помощью `gh pr create`
3. Напиши [DONE] в последнем сообщении.
```

### Шаблон: Локальный репозиторий (без remote)

Используй этот шаблон когда проект — это локальная папка без GitHub remote.

```markdown
---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: <PROJECT_SLUG>
polling:
  interval_ms: 30000
workspace:
  root: ~/symphony_workspaces
  repo: <PATH_TO_LOCAL_REPO>
agent:
  max_concurrent_agents: 3
claude:
  command: claude
  stall_timeout_ms: 1800000
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Ты работаешь в локальном репозитории без remote.
Все файлы и изменения делай прямо в текущей директории (cwd).
НЕ создавай дополнительных worktree или веток — просто работай в текущей папке.

Когда задача выполнена:
1. Закоммить изменения в текущей ветке
2. Напиши [DONE] в последнем сообщении.
```

## Алгоритм создания

### Шаг 1: Сбор информации

Спроси у пользователя недостающие параметры (см. "Что нужно узнать у пользователя").

**Жди ответа пользователя. Не продолжай без него.**

### Шаг 2: Создание файла

Создай файл `workflows/<project-name>.md` по подходящему шаблону, подставив реальные значения.

### Шаг 3: Создание симлинка (опционально)

Если пользователь хочет использовать этот воркфлоу как основной, создай/обнови симлинк:

```bash
ln -sf workflows/<project-name>.md WORKFLOW.md
```

### Шаг 4: Проверка

Валидируй созданный воркфлоу:

```bash
./pyphony list-candidates --workflow workflows/<project-name>.md
```

Если команда отработала без ошибок — воркфлоу валиден.

### Шаг 5: Результат

Покажи пользователю:
- Путь к созданному файлу
- Ключевые параметры (project slug, repo, max agents)
- Как запустить: `uv run python -m pyphony workflows/<project-name>.md`

## Дополнительные опции

### Automerge

Если пользователь хочет автоматический мерж PR, добавь в front matter:

```yaml
automerge:
  parse_transcript_prs: true
```

### Supervisor restart

Если нужен автоматический перезапуск супервизора:

```yaml
supervisor_restart: true
```

## Справка по полям front matter

Полная документация полей: `specs/workflow.md`, секция "Front Matter Schema".

Основные поля:
- `tracker.kind` — тип трекера (`linear`)
- `tracker.api_key` — API ключ (обычно `$LINEAR_API_KEY`)
- `tracker.project_slug` — slug проекта в Linear
- `polling.interval_ms` — интервал опроса трекера (мс, по умолчанию 30000)
- `hooks.after_create` — скрипт после создания воркспейса
- `workspace.root` — корневая директория для воркспейсов
- `workspace.repo` — путь к локальному репозиторию (для проектов без remote)
- `agent.max_concurrent_agents` — максимум параллельных агентов
- `claude.command` — команда для запуска claude
- `claude.stall_timeout_ms` — таймаут зависания агента

## Важные правила

1. **Всегда сохраняй воркфлоу в `workflows/`** — это каноническая директория
2. **`api_key` всегда `$LINEAR_API_KEY`** — не хардкодь токены
3. **Prompt на русском** — все воркфлоу в проекте используют русский для промптов
4. **Jinja2 переменные** — `{{ issue.identifier }}`, `{{ issue.title }}`, `{{ issue.description }}` обязательны в промпте
5. **Язык общения — русский**
