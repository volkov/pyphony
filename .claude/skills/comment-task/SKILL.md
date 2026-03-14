---
name: comment-task
description: Add a comment to a Linear issue by its identifier (e.g. SER-27). Use when asked to comment on, reply to, or write a note on a task/ticket.
---

# Comment on Linear Task

Add a comment to an existing Linear issue.

## When to use

Trigger phrases:
- "напиши комментарий к SER-27"
- "добавь коммент в тикет SER-10"
- "comment on SER-15"
- "оставь заметку в задаче SER-5"
- "ответь в тикете SER-30"

## How to use

1. Extract the issue **identifier** (e.g. SER-27) from the user's message or from $ARGUMENTS.
2. If the identifier is not provided, ask the user for it.
3. Compose the comment body. If the user provided the text — use it. Otherwise ask what to write.
4. Run the CLI command:

```bash
./pyphony comment-issue SER-27 --body "Comment text here (markdown supported)"
```

For threaded replies (reply to an existing comment), add `--parent-id`:

```bash
./pyphony comment-issue SER-27 --body "Reply text" --parent-id "<comment_id>"
```

The comment ID can be found in the output of `./pyphony get-issue SER-27` (each comment has an `id` field).

5. The command outputs JSON with `success`, `comment_id`, and `issue`.
6. Confirm to the user that the comment was posted.

## Arguments

$ARGUMENTS — should contain the issue identifier and optionally the comment text. Parse it to extract what you can; ask for anything missing.

## Example

User: "напиши в SER-27 что задача заблокирована ожиданием API"

```bash
./pyphony comment-issue SER-27 --body "Задача заблокирована: ожидаем готовности API."
```

Output:
```json
{
  "success": true,
  "comment_id": "abc123",
  "issue": "SER-27"
}
```

Response: "Добавил комментарий к SER-27."
