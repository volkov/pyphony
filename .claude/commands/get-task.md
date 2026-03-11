# Get Linear Task

Read an existing task (issue) from the Linear project tracker by its identifier.

## When to use

Use this skill when the user asks to read, show, or get a task/issue from Linear. Trigger phrases:
- "покажи задачу SER-27"
- "прочитай тикет SER-10"
- "get issue SER-15"
- "show task SER-5"
- "что в задаче SER-30?"

## How to use

1. Extract the issue **identifier** (e.g. SER-27) from the user's message or from $ARGUMENTS.
2. If the identifier is not provided, ask the user for it.
3. Run the CLI command:

```
./pyphony get-issue SER-27
```

The command reads the default `WORKFLOW.md` for the Linear API key and project slug, then fetches the issue via the Linear GraphQL API.

4. The command outputs JSON with `id`, `identifier`, `title`, `description`, `state`, and `url` of the issue.
5. Show the user the result in a readable format.

## Arguments

$ARGUMENTS — should contain the issue identifier (e.g. SER-27). Parse it to extract the identifier; ask if missing.

## Example

User: "покажи задачу SER-27"

```bash
./pyphony get-issue SER-27
```

Output:
```json
{
  "id": "abc123",
  "identifier": "SER-27",
  "title": "Добавить CLI команды для чтения задач",
  "description": "Описание задачи...",
  "state": "In Progress",
  "url": "https://linear.app/team/issue/SER-27"
}
```

Response: "SER-27: Добавить CLI команды для чтения задач\nСтатус: In Progress\nhttps://linear.app/team/issue/SER-27"
