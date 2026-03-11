# SER-86: Merge через привязанный PR вместо прямого merge из транскрипта

## Вопрос

Можно ли унифицировать automerge: вместо отдельной ветки "merge из транскрипта"
— всегда привязывать PR к тикету через Linear attachment, а мержить по
стандартному пути `fetch_issue_pr_urls` → `gh pr merge`?

## Текущая архитектура

```
Worker завершает работу
  └─ orchestrator._on_worker_exit()
       ├─ fetch_issue_pr_urls(issue_id)          # Linear attachments API
       │    └─ query { issue { attachments { nodes { url } } } }
       │         └─ фильтр: github.com + /pull/
       ├─ [fallback] extract_pr_urls_from_transcript()   # parse_transcript_prs=true
       │    └─ парсит последние 200 строк JSONL транскрипта
       └─ try_automerge_pr(pr_url)               # gh pr merge --squash
```

Два источника PR URL:
1. **Linear attachments** — основной путь (требует GitHub integration в Linear)
2. **Транскрипт** — fallback (SER-81, `parse_transcript_prs: true`)

## Исследование Linear Attachment API

### Два способа создать attachment программно

#### 1. `attachmentLinkGitHubPR` (рекомендуемый для GitHub PR)

```graphql
mutation {
  attachmentLinkGitHubPR(
    issueId: "SER-86"
    url: "https://github.com/owner/repo/pull/42"
  ) {
    success
    attachment { id url sourceType }
  }
}
```

- Создаёт "rich" attachment с интеграцией GitHub
- Автоматический статус-синхронизация (PR merged → issue Done)
- **Требует**: GitHub integration установлена в Linear workspace
- Минимальные поля: `issueId` + `url`

#### 2. `attachmentCreate` (generic, без интеграции)

```graphql
mutation {
  attachmentCreate(input: {
    issueId: "SER-86"
    title: "PR #42: feat: add attachment support"
    url: "https://github.com/owner/repo/pull/42"
  }) {
    success
    attachment { id url sourceType }
  }
}
```

- Работает без GitHub integration
- Не даёт авто-статус синхронизацию
- Минимальные поля: `issueId` + `title` + `url`

### Ключевые свойства

- **Идемпотентность**: пара `url` + `issueId` уникальна — повторный вызов обновляет, не дублирует
- **Гибкость issueId**: принимает как UUID, так и human-readable идентификатор (`SER-86`)
- **Формат URL**: стандартный `https://github.com/{owner}/{repo}/pull/{number}`

### Подхватит ли `fetch_issue_pr_urls` такой attachment?

**Да.** Текущая реализация:

```python
async def fetch_issue_pr_urls(self, issue_id: str) -> list[str]:
    data = await self._execute(ISSUE_ATTACHMENTS_QUERY, {"issueId": issue_id})
    # ...
    for attachment in issue_node.get("attachments", {}).get("nodes", []):
        url = attachment.get("url", "")
        if url and ("github.com" in url) and ("/pull/" in url):
            urls.append(url)
    return urls
```

Фильтрация идёт по URL-паттерну (`github.com` + `/pull/`), не по `sourceType`.
Attachment созданный через любой из двух мутаций будет подхвачен.

## Предложенная архитектура

```
Worker завершает работу
  └─ orchestrator._on_worker_exit()
       ├─ fetch_issue_pr_urls(issue_id)          # Linear attachments API
       ├─ [fallback] extract_pr_urls_from_transcript()
       │    └─ парсит PR URL из транскрипта
       │    └─ ▶ NEW: attach_pr_to_issue(issue_id, pr_url)  ◀
       │         └─ attachmentCreate / attachmentLinkGitHubPR
       └─ try_automerge_pr(pr_url)               # без изменений
```

### Что меняется

1. После `extract_pr_urls_from_transcript()` — вызываем `attach_pr_to_issue()`
   для каждого найденного PR URL
2. PR привязывается к тикету в Linear
3. При следующих попытках (resolve-conflict retry) — PR уже доступен через
   `fetch_issue_pr_urls`, не нужен повторный парсинг транскрипта

### Что НЕ меняется

- `try_automerge_pr` — без изменений
- `extract_pr_urls_from_transcript` — без изменений
- `fetch_issue_pr_urls` — без изменений
- Конфиг `parse_transcript_prs` — остаётся

## Оценка сложности

### Что нужно сделать

| Шаг | Сложность | Описание |
|-----|-----------|----------|
| 1. Добавить GraphQL mutation | Тривиально | Одна строка в `tracker_queries.py` |
| 2. Добавить метод `attach_pr_to_issue` | Тривиально | ~10 строк в `tracker.py` |
| 3. Вызвать из orchestrator | Тривиально | ~5 строк после `extract_pr_urls_from_transcript` |
| 4. Тесты | Легко | Mock GraphQL mutation, проверить вызов |

**Общая оценка: ~30-50 строк кода, ~2 часа работы.**

### Пример реализации

```python
# tracker_queries.py
ATTACHMENT_CREATE_MUTATION = """
mutation AttachmentCreate($issueId: String!, $title: String!, $url: String!) {
  attachmentCreate(input: { issueId: $issueId, title: $title, url: $url }) {
    success
    attachment { id url }
  }
}
"""

# tracker.py
async def attach_pr_to_issue(self, issue_id: str, pr_url: str) -> bool:
    """Attach a GitHub PR URL to a Linear issue."""
    title = f"PR: {pr_url.split('/')[-1]}"
    data = await self._execute(
        ATTACHMENT_CREATE_MUTATION,
        {"issueId": issue_id, "title": title, "url": pr_url},
    )
    return data.get("attachmentCreate", {}).get("success", False)
```

## Плюсы и минусы

### Плюсы

1. **Единый источник правды** — PR всегда привязан к тикету в Linear, видим в UI
2. **Надёжный retry** — при resolve-conflict не нужен транскрипт для повторного merge
3. **Видимость** — в Linear UI видно какой PR привязан к задаче (полезно для отладки)
4. **Идемпотентность** — безопасно вызывать повторно
5. **Минимальные изменения** — не ломает текущий flow, просто добавляет шаг

### Минусы

1. **Дополнительный API-вызов** — один extra request к Linear на каждый PR из транскрипта
2. **Нужен API key с правами на attachments** — обычно уже есть
3. **Не полностью убирает fallback** — транскрипт-парсинг всё равно нужен как
   источник PR URL (если нет GitHub integration)

## Альтернативы

### A. Полная унификация (убрать parse_transcript_prs)

Не рекомендуется. `parse_transcript_prs` останется нужным как источник PR URL
для workspace'ов без GitHub integration. Мы лишь добавляем шаг "привязать к тикету"
после извлечения URL.

### B. Использовать `attachmentLinkGitHubPR` вместо `attachmentCreate`

Лучше если GitHub integration установлена — даёт rich attachment. Но падает
если integration отсутствует. Можно попробовать `attachmentLinkGitHubPR` → fallback
на `attachmentCreate`, но это усложняет код ради минимальной выгоды.

### C. Агент сам привязывает PR через Linear CLI/API

Если Claude Code агент использует Linear MCP или CLI для привязки PR к тикету —
это уже решит проблему. Но:
- Нельзя гарантировать что агент это сделает
- Нет контроля на нашей стороне
- Лучше привязывать явно в orchestrator

## Рекомендация

**Делать. Сложность минимальная, польза существенная.**

Конкретный план:
1. Добавить `ATTACHMENT_CREATE_MUTATION` в `tracker_queries.py`
2. Добавить `attach_pr_to_issue()` в `LinearClient`
3. В `orchestrator._on_worker_exit()`: после `extract_pr_urls_from_transcript()` —
   вызвать `attach_pr_to_issue()` для каждого PR
4. Тесты

Это не заменяет SER-81, а дополняет его — превращает найденные в транскрипте PR
в "first-class" attachments в Linear, упрощая retry и давая видимость.
