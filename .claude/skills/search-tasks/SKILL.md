---
name: search-tasks
description: List and search Linear project issues, optionally filtered by state (Backlog, Todo, In Progress, Done, etc.). Use when asked to list, search, or browse tasks.
---

# Search Linear Tasks

List project issues from Linear, optionally filtered by state.

## When to use

Trigger phrases:
- "покажи все задачи"
- "что в бэклоге?"
- "list tasks"
- "какие задачи в работе?"
- "search issues in Todo"
- "покажи задачи в статусе Done"

## How to use

1. Determine if the user wants a specific state filter from $ARGUMENTS.
2. Run the CLI command:

**All active + backlog issues (default):**
```bash
./pyphony search-issues
```

**Filter by state:**
```bash
./pyphony search-issues --state "Backlog"
./pyphony search-issues --state "Todo,In Progress"
./pyphony search-issues --state "Done"
```

3. The command outputs YAML with a list of issues (identifier, title, state, labels, assignee).
4. Present the results to the user in a readable table format.

## Arguments

$ARGUMENTS — optional state filter. Parse it to extract the desired states.

## Example

User: "что в бэклоге?"

```bash
./pyphony search-issues --state "Backlog"
```

Present results as a table:
```
| Тикет   | Название                    | Статус   | Лейблы        |
|---------|---------------------------- |----------|---------------|
| SER-10  | Добавить кэширование        | Backlog  |               |
| SER-15  | Исправить логирование       | Backlog  | plan required |
```
