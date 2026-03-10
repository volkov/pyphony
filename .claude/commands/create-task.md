# Create Linear Task

Create a new task (issue) in the Linear project tracker in Backlog state.

## When to use

Use this skill when the user asks to create a task, issue, or ticket in Linear. Trigger phrases:
- "создай задачу в линеар"
- "создай тикет"
- "create a task in Linear"
- "add an issue"
- "заведи задачу"

## How to use

1. Ask the user for the task **title** if not provided: short summary of what needs to be done.
2. Ask for an optional **description** (markdown) with details, acceptance criteria, etc. If the user gave enough context, compose the description yourself.
3. Run the CLI command:

```
uv run python -m pyphony create-issue WORKFLOW.md --title "Title here" --description "Description here"
```

The command reads `WORKFLOW.md` for the Linear API key and project slug, then creates an issue in **Backlog** state via the Linear GraphQL API.

4. The command outputs JSON with `id`, `identifier`, `title`, and `url` of the created issue.
5. Show the user the result: issue identifier and a link to it.

## Arguments

$ARGUMENTS — can contain the task title and/or description provided by the user. Parse it to extract what you can; ask for anything missing.

## Example

User: "создай задачу: добавить кэширование в API клиент"

```bash
uv run python -m pyphony create-issue WORKFLOW.md --title "Добавить кэширование в API клиент"
```

Output:
```json
{
  "id": "abc123",
  "identifier": "SER-25",
  "title": "Добавить кэширование в API клиент",
  "url": "https://linear.app/team/issue/SER-25"
}
```

Response: "Создал задачу SER-25: Добавить кэширование в API клиент — https://linear.app/team/issue/SER-25"
