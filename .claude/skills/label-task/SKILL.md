---
name: label-task
description: Add or remove labels on a Linear issue (e.g. "plan required", "research", "interactive"). Use when asked to label, tag, or categorize a task.
---

# Label Linear Task

Add or remove labels on an existing Linear issue.

## When to use

Trigger phrases:
- "добавь лейбл plan required к SER-27"
- "поставь метку research на SER-10"
- "убери лейбл interactive с SER-15"
- "label SER-5 as research"
- "remove label from SER-20"

## How to use

1. Extract the issue **identifier** (e.g. SER-27) and the labels to add/remove from $ARGUMENTS.
2. If the identifier is not provided, ask the user for it.
3. Run the CLI command:

**Add labels:**
```bash
./pyphony label-issue SER-27 --add "plan required"
```

**Add multiple labels:**
```bash
./pyphony label-issue SER-27 --add "plan required" --add "research"
```

**Remove labels:**
```bash
./pyphony label-issue SER-27 --remove "interactive"
```

**Add and remove in one command:**
```bash
./pyphony label-issue SER-27 --add "research" --remove "plan required"
```

4. The command outputs JSON with `success`, `issue`, `added`, and `removed`.
5. Confirm to the user.

## Known labels

- `plan required` — agent will produce an implementation plan instead of executing
- `research` — agent will research and post findings as a comment
- `interactive` — requires manual/interactive work

Labels are created automatically if they don't exist yet.

## Arguments

$ARGUMENTS — should contain the issue identifier and label action. Parse it to extract what you can; ask for anything missing.

## Example

User: "добавь лейбл research к SER-27"

```bash
./pyphony label-issue SER-27 --add "research"
```

Output:
```json
{
  "success": true,
  "issue": "SER-27",
  "added": ["research"],
  "removed": []
}
```

Response: "Добавил лейбл 'research' к SER-27."
