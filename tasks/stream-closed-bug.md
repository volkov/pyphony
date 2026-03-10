# Bug: MCP tool calls fail with "Stream closed"

## Symptom

When an agent tries to use the `linear_graphql` MCP tool (e.g. to transition an issue to Done), the call fails with `"Stream closed"`. The agent sees the error in the tool result and cannot interact with Linear.

Observed in SER-12 transcript (`e6a2f367-...`): the agent completed all coding work successfully, then tried to call `linear_graphql` to transition the issue — got "Stream closed" twice and gave up.

## Impact

- Agent can't transition issues to Done via Linear API
- Issue stays in Todo → pyphony re-dispatches it as a new issue on the next poll cycle → wasted duplicate run
- Any agent workflow that relies on `linear_graphql` at the end of a run is broken

## Reproduction

1. Start pyphony with a WORKFLOW.md that has a Linear project
2. Let an agent run on any issue for 2+ minutes
3. At the end of the run, the agent tries to call `linear_graphql` → "Stream closed"

## Root cause analysis

The MCP server is created in `src/pyphony/agent.py:94-103` via `create_linear_tool()` which returns a `create_sdk_mcp_server(...)` config. This sets up a stdio-based MCP server that the Claude CLI subprocess communicates with through the parent pyphony process.

The `claude_agent_sdk` manages the MCP server lifecycle — it spawns a stdio pipe between the Claude CLI and the MCP server handler. "Stream closed" means the stdio pipe between the CLI subprocess and the MCP server handler has been broken.

Possible causes:

1. **SDK-level bug**: The `claude_agent_sdk` MCP server implementation may close the stream prematurely or have a timeout on the stdio pipe. After extended inactivity (the tool isn't called during the main coding work, only at the very end), the connection may be reaped.

2. **Pipe buffer issue**: If the MCP server's stdio pipe fills up or encounters a write error, subsequent reads will get "Stream closed".

3. **httpx client state**: The `httpx.AsyncClient` is created before the agent starts and lives for the entire run. It shouldn't cause "Stream closed" on the MCP stdio pipe, but worth checking if a timeout or connection pool issue propagates oddly.

## Where to investigate

- `src/pyphony/linear_tool.py` — the tool handler itself (lines 35-82). The handler is async and uses the shared `http_client`. The error message format doesn't match what this handler returns — "Stream closed" is not from `_mcp_result()`, which means the error comes from the SDK/CLI layer before the handler is even called.

- `claude_agent_sdk` — look at how `create_sdk_mcp_server` works and how the stdio pipe is managed. Check if there's a keepalive mechanism or idle timeout.

- Claude CLI source — the CLI is the other end of the MCP pipe. Check if it has an MCP connection timeout.

## Possible fixes

1. **Keepalive/ping**: If the SDK supports MCP keepalive pings, enable them to prevent idle timeout on the stdio pipe.

2. **Lazy tool registration**: Instead of an MCP server, register `linear_graphql` as a regular tool (if the SDK supports custom non-MCP tools). This avoids the stdio pipe entirely.

3. **Retry in handler**: Add retry logic in the tool handler for transient stream errors — though this won't help if the stream is truly closed at the transport level.

4. **Move state transition to orchestrator**: Instead of relying on the agent to call `linear_graphql`, have the orchestrator transition the issue to Done after a successful run (in `_on_worker_exit`). This sidesteps the MCP issue entirely for the most common use case. The agent would still have the tool for ad-hoc queries during the run.

## Workaround

Option 4 above is both a fix and an architectural improvement: the orchestrator should own issue state transitions, not delegate them to agents via MCP. The agent shouldn't need to know about Linear state management.
