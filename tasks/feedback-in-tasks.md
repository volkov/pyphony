# Task: Post comment to Linear issue after each agent run

## Goal

After an agent finishes working on an issue (success or failure), the orchestrator posts a summary comment to the Linear issue. This gives stakeholders visibility into what happened — status, duration, token usage, error details, and the agent's output.

This implements the SPEC.md TODO (line 2101): "Add first-class tracker write APIs (comments/state transitions) in the orchestrator instead of only via agent tools."

## Current completion flow

When `AgentRunner.run()` finishes (in `src/pyphony/agent.py`), it returns a `RunAttempt`. The orchestrator's `_run_worker()` checks the status and calls `_on_worker_exit()`.

### Data available at completion

**`RunAttempt`** (`src/pyphony/models.py:104-111`):
```python
class RunAttempt(BaseModel):
    issue_id: str
    issue_identifier: str
    attempt: int | None = None
    workspace_path: str = ""
    started_at: datetime | None = None
    status: str = "pending"       # -> "completed" or "failed"
    error: str | None = None      # set on failure only
```

**`LiveSession`** (`src/pyphony/models.py:114-128`):
```python
class LiveSession(BaseModel):
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_total_tokens: int = 0
    turn_count: int = 0
    last_agent_message: str = ""
    # ... other fields
```

**`ResultMessage`** (from `claude_agent_sdk`, received in `agent.py:102`):
- `is_error: bool`
- `result: str` — the agent's final text output
- `usage`, `num_turns`, `duration_ms`, `total_cost_usd`

**Problem:** On success, `ResultMessage.result` is currently discarded (only captured on error in `run_attempt.error`). We need to save it.

### Worker exit flow (`src/pyphony/orchestrator.py:159-179`)

```python
async def _run_worker(self, issue: Issue, entry: RunningEntry) -> None:
    try:
        result = await self._run_agent_fn(issue, entry.attempt.attempt)
        if hasattr(result, "status") and result.status == "failed":
            self._on_worker_exit(issue.id, normal=False, error=result.error)
        else:
            self._on_worker_exit(issue.id, normal=True, error=None)
    except Exception as exc:
        self._on_worker_exit(issue.id, normal=False, error=str(exc))
```

`_on_worker_exit()` (`orchestrator.py:181-220`) accumulates tokens, then schedules a retry (1s for normal exit, exponential backoff for failure). The comment should be posted **before** `_on_worker_exit` since that pops the entry from `state.running` (losing access to `session`).

### Tracker (`src/pyphony/tracker.py`)

`LinearClient` currently has only read methods. It uses `_execute(query, variables)` internally which sends a GraphQL request to Linear and returns `body["data"]`. We need to add a write method.

## Changes required

### 1. `src/pyphony/models.py` — add `result` field

Add `result: str | None = None` to `RunAttempt` after the `error` field (line 111):

```python
class RunAttempt(BaseModel):
    issue_id: str
    issue_identifier: str
    attempt: int | None = None
    workspace_path: str = ""
    started_at: datetime | None = None
    status: str = "pending"
    error: str | None = None
    result: str | None = None  # <-- NEW
```

### 2. `src/pyphony/agent.py` — capture result on both paths

In `AgentRunner.run()`, around lines 102-107:

```python
# Current:
if message.is_error:
    run_attempt.status = "failed"
    run_attempt.error = message.result or "agent_error"
else:
    run_attempt.status = "completed"

# Change to:
if message.is_error:
    run_attempt.status = "failed"
    run_attempt.error = message.result or "agent_error"
    run_attempt.result = message.result
else:
    run_attempt.status = "completed"
    run_attempt.result = message.result
```

### 3. `src/pyphony/tracker_queries.py` — add GraphQL mutation

Add at end of file:

```python
COMMENT_CREATE_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment {
      id
    }
  }
}
"""
```

### 4. `src/pyphony/tracker.py` — add `create_comment()` method

Import the new mutation at the top:
```python
from .tracker_queries import (
    CANDIDATE_ISSUES_QUERY,
    COMMENT_CREATE_MUTATION,  # <-- NEW
    ISSUE_STATES_BY_IDS_QUERY,
    ISSUES_BY_STATES_QUERY,
)
```

Add public method after `fetch_issues_by_states()` (after line 96):
```python
async def create_comment(self, issue_id: str, body: str) -> str | None:
    """Post a comment on an issue. Returns comment ID, or None on failure."""
    data = await self._execute(COMMENT_CREATE_MUTATION, {"issueId": issue_id, "body": body})
    comment_create = data.get("commentCreate", {})
    if not comment_create.get("success"):
        return None
    return comment_create.get("comment", {}).get("id")
```

### 5. `src/pyphony/orchestrator.py` — build and post comment

Add two methods to `Orchestrator`:

**`_build_completion_comment(result: RunAttempt, session: LiveSession) -> str`**
Formats a markdown comment with:
- Status line (success/failure)
- Attempt number (if > 0)
- Duration (computed from `result.started_at` to now)
- Token usage from `session` (input/output/total)
- Error message if `result.error` is set
- Agent output from `result.result` (truncated to 2000 chars)

**`_post_completion_comment(issue_id: str, result: RunAttempt, session: LiveSession) -> None`**
Calls `self._tracker.create_comment(issue_id, body)`. Wraps everything in try/except — log warning on failure, never crash.

**Modify `_run_worker`** to call `_post_completion_comment` before `_on_worker_exit`:

```python
async def _run_worker(self, issue: Issue, entry: RunningEntry) -> None:
    try:
        result = await self._run_agent_fn(issue, entry.attempt.attempt)
        session = entry.session
        if hasattr(result, "status") and result.status == "failed":
            log.error("worker_failed", issue_identifier=issue.identifier, error=getattr(result, "error", None))
            await self._post_completion_comment(issue.id, result, session)
            self._on_worker_exit(issue.id, normal=False, error=getattr(result, "error", "agent_failed"))
        else:
            await self._post_completion_comment(issue.id, result, session)
            self._on_worker_exit(issue.id, normal=True, error=None)
    except Exception as exc:
        log.error("worker_failed", issue_identifier=issue.identifier, error=str(exc))
        # Skip comment on infra failure — HTTP likely broken too
        self._on_worker_exit(issue.id, normal=False, error=str(exc))
```

Note: `self._tracker` is accessible as `LinearClient` is stored on the orchestrator. Check how it's passed in — the orchestrator constructor receives `tracker` or accesses it via config. Look at `__init__` to confirm the attribute name.

### 6. Tests

**`tests/test_tracker.py`** — test `create_comment()`:
- Use `respx` to mock POST to Linear endpoint
- Success case: return `{"data": {"commentCreate": {"success": true, "comment": {"id": "comment-123"}}}}`, verify returns `"comment-123"`
- Failure case: return GraphQL errors, verify raises `LinearGraphQLError`

**`tests/test_orchestrator.py`** — test comment posting:
- Mock `tracker.create_comment` (it's injected or accessible)
- Verify it's called after successful agent run with correct issue_id and a body containing status info
- Verify it's called after failed agent run
- Verify that if `create_comment` raises, `_on_worker_exit` still proceeds normally

**`tests/test_agent.py`** — test result capture:
- Verify `RunAttempt.result` is set on success (mock `ResultMessage` with `is_error=False`, check `result` field)
- Verify `RunAttempt.result` is set on failure too

## Verification

```bash
uv run pytest
```

All existing tests must still pass. New tests should cover the 3 areas above.
