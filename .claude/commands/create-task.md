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
./pyphony create-issue --title "Title here" --description "Description here" [--state "Todo"]
```

The command reads the default `WORKFLOW.md` for the Linear API key and project slug, then creates an issue via the Linear GraphQL API.

- By default (no `--state`) the issue is created in **Backlog** state.
- Use `--state Todo` to create the issue in **Todo** state — pyphony will pick it up and dispatch an agent to work on it immediately.

4. The command outputs JSON with `id`, `identifier`, `title`, and `url` of the created issue.
5. **Labels**: CLI does not support adding labels at creation time. If the task needs a label (e.g. `plan required`), add it after creation via the tracker API:
   ```python
   uv run python -c "
   import asyncio
   from pyphony.workflow import load_workflow
   from pyphony.config import service_config_from_workflow
   from pyphony.tracker import LinearClient

   async def main():
       wf = load_workflow('WORKFLOW.md')
       cfg = service_config_from_workflow(wf.config)
       client = LinearClient(cfg)
       await client.replace_issue_labels('<issue_id>', remove_labels=[], add_labels=['plan required'])
       await client.close()

   asyncio.run(main())
   "
   ```
6. Show the user the result: issue identifier and a link to it.

## When to add `plan required` label

Add the `plan required` label when the task is about **planning an implementation** rather than direct code execution. Examples:
- "спланировать реализацию...", "составить план..."
- "design approach", "plan implementation"
- User explicitly asks for a plan ("создай задачу на plan")

With this label, the agent will produce an implementation plan and post it as a comment instead of executing changes directly.

## When to add `research` label

Add the `research` label when the task is about **researching and gathering information** rather than writing code or creating plans. Examples:
- "исследовать варианты...", "разобраться как...", "собрать информацию..."
- "investigate", "explore options", "research how X works"
- User explicitly asks for research ("создай задачу на research")

With this label, the agent will research the codebase, gather the requested information, and post it as a comment instead of executing changes directly.

## Arguments

$ARGUMENTS — can contain the task title and/or description provided by the user. Parse it to extract what you can; ask for anything missing.

## Example

User: "создай задачу: добавить кэширование в API клиент"

```bash
./pyphony create-issue --title "Добавить кэширование в API клиент"
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
