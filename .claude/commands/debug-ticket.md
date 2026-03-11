# Debug Ticket

Analyze what happened with ticket $ARGUMENTS in the pyphony service.

## Steps

### 1. Find log entries

Search the log file at `logs/pyphony.log` for all lines containing the ticket identifier (e.g. `SER-12`). Use Grep with output_mode "content" to get full lines.

### 2. Extract run information

From the log entries, identify each run (dispatch/agent_start/agent_finish/agent_exit cycle). For each run extract:
- **Timestamp** and **attempt number**
- **Status**: completed or failed
- **Error**: if any
- **Duration** (`elapsed_s`)
- **Workspace path** (from `agent_finish` line, `workspace_path=...`)
- **Transcript path** (from `agent_finish` line, `transcript=...`)
- **Stderr log path** (from `agent_finish` line, `stderr_log=...`)
- **Token usage** (`input_tokens`, `output_tokens` from `agent_exit`)
- **Agent options** (model, max_turns, tools, mcp_servers from `agent_options`)
- Whether the issue was **re-dispatched** (multiple dispatch events = issue stayed in Todo after completion)

Also note retry scheduling events (`retry_scheduled`) and stall detections (`stall_detected`).

### 3. Read stderr logs

For each run that has a stderr log path, read it (use Read tool). Look for errors, warnings, crashes, or unusual output.

### 4. Analyze transcripts

For each run that has a transcript path, read the JSONL file. Each line is a JSON object. Focus on:
- **User messages** (`"type":"user"`) — the prompt sent to the agent
- **Assistant messages** (`"type":"assistant"`) — what the agent did and said
- **Tool calls** in assistant messages — what tools were used, any errors
- **The final assistant message** — the agent's conclusion/result
- Look for signs of: agent getting stuck in loops, tool errors, permission issues, wrong approach, incomplete work

Since transcripts can be large, read them in chunks. Start with the first 100 lines and last 100 lines to get the prompt and final result. If more context is needed, read middle sections.

### 5. Check workspace state

If the workspace path exists, check:
- `ls` the workspace to see what files were created
- `git log --oneline` to see commits made by the agent
- `git diff` to see any uncommitted changes
- Check if `.claude/` directory exists (session state)

### 6. Produce report

Output a structured report in markdown:

```
## Debug Report: {TICKET_ID}

### Summary
One-paragraph summary of what happened overall.

### Runs
For each run:
- When it started, how long it took, status
- What the agent was asked to do (brief)
- What the agent actually did (brief)
- How it ended (success/failure/shutdown)

### Issues Found
List any problems discovered:
- **pyphony bugs**: issues in the orchestrator/agent framework (e.g. re-dispatching completed work, lost state, wrong retry behavior)
- **ticket setup problems**: issues with the ticket itself (e.g. unclear task, missing context, wrong configuration)
- **agent problems**: the agent itself struggled (e.g. wrong approach, got stuck, tool failures)
- **infrastructure**: external issues (e.g. API errors, rate limits, network)

### Workspace State
Current state of the workspace (files, commits, uncommitted changes).

### Recommendations
What should be done to fix or re-run this ticket successfully.
```

Be thorough but concise. Focus on actionable findings.

### 7. Suggest next actions

If the report identified any issues or problems, present the user with action options:

```
### What would you like to do?

1. 🔧 **Fix it here** — I'll try to fix the issue right now in this session
2. 🚀 **Create a ticket & execute now (Todo)** — create a Linear ticket in Todo state so pyphony picks it up immediately
3. 📋 **Create a ticket in Backlog** — create a Linear ticket in Backlog for later
```

Wait for the user to choose an option, then:

- **Option 1**: Investigate the root cause and implement a fix directly. Follow normal development workflow (edit code, test, commit).
- **Option 2**: Use the create-task skill to create a ticket with `--state Todo`:
  ```
  ./pyphony create-issue --title "Fix: <concise problem summary>" --description "<details from debug report>" --state Todo
  ```
  This puts the issue in Todo state so pyphony will dispatch an agent to work on it immediately.
- **Option 3**: Use the create-task skill to create a ticket in Backlog (default):
  ```
  ./pyphony create-issue --title "Fix: <concise problem summary>" --description "<details from debug report>"
  ```
