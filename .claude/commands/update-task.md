# Update Linear Task

Update an existing task (issue) in the Linear project tracker.

## When to use

Use this skill when the user asks to update, change, or modify a task/issue in Linear. Trigger phrases:
- "обнови задачу SER-27"
- "поменяй статус SER-10 на Done"
- "измени название тикета SER-5"
- "update issue SER-15"
- "change state of SER-20 to In Progress"
- "переведи задачу SER-30 в Done"

## How to use

1. Extract the issue **identifier** (e.g. SER-27) and the fields to update from the user's message or from $ARGUMENTS.
2. If the identifier is not provided, ask the user for it.
3. At least one of `--title`, `--description`, or `--state` must be provided.
4. Run the CLI command:

```
uv run python -m pyphony update-issue WORKFLOW.md --identifier "SER-27" --title "New title" --description "New description" --state "In Progress"
```

Only include the flags for fields that need to change. The command reads `WORKFLOW.md` for the Linear API key and project slug, then updates the issue via the Linear GraphQL API.

5. The command outputs JSON with `id`, `identifier`, `title`, `description`, `state`, and `url` of the updated issue.
6. Show the user the result confirming what was updated.

## Arguments

$ARGUMENTS — should contain the issue identifier and fields to update. Parse it to extract what you can; ask for anything missing.

## Example

User: "переведи задачу SER-27 в Done"

```bash
uv run python -m pyphony update-issue WORKFLOW.md --identifier "SER-27" --state "Done"
```

Output:
```json
{
  "id": "abc123",
  "identifier": "SER-27",
  "title": "Добавить CLI команды для чтения задач",
  "description": "Описание задачи...",
  "state": "Done",
  "url": "https://linear.app/team/issue/SER-27"
}
```

Response: "Обновил задачу SER-27: статус изменён на Done — https://linear.app/team/issue/SER-27"
