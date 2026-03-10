# Symphony — Agent Runner Protocol

## Relevant code

- [`src/pyphony/agent.py`](../src/pyphony/agent.py)
- [`src/pyphony/protocol.py`](../src/pyphony/protocol.py)

## 1. Agent Runner Protocol (Coding Agent Integration)

This section defines the language-neutral contract for integrating a coding agent app-server.

Compatibility profile:

- The normative contract is message ordering, required behaviors, and the logical fields that must
  be extracted (for example session IDs, completion state, approval handling, and usage/rate-limit
  telemetry).
- Exact JSON field names may vary slightly across compatible app-server versions.
- Implementations should tolerate equivalent payload shapes when they carry the same logical
  meaning, especially for nested IDs, approval requests, user-input-required signals, and
  token/rate-limit metadata.

### 1.1 Launch Contract

Subprocess launch parameters:

- Command: `codex.command`
- Invocation: `bash -lc <codex.command>`
- Working directory: workspace path
- Stdout/stderr: separate streams
- Framing: line-delimited protocol messages on stdout (JSON-RPC-like JSON per line)

Notes:

- The default command is `codex app-server`.
- Approval policy, cwd, and prompt are expressed in the protocol messages in Section 1.2.

Recommended additional process settings:

- Max line size: 10 MB (for safe buffering)

### 1.2 Session Startup Handshake

Reference: https://developers.openai.com/codex/app-server/

The client must send these protocol messages in order:

Illustrative startup transcript (equivalent payload shapes are acceptable if they preserve the same
semantics):

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"symphony","version":"1.0"},"capabilities":{}}}
{"method":"initialized","params":{}}
{"id":2,"method":"thread/start","params":{"approvalPolicy":"<implementation-defined>","sandbox":"<implementation-defined>","cwd":"/abs/workspace"}}
{"id":3,"method":"turn/start","params":{"threadId":"<thread-id>","input":[{"type":"text","text":"<rendered prompt-or-continuation-guidance>"}],"cwd":"/abs/workspace","title":"ABC-123: Example","approvalPolicy":"<implementation-defined>","sandboxPolicy":{"type":"<implementation-defined>"}}}
```

1. `initialize` request
   - Params include:
     - `clientInfo` object (for example `{name, version}`)
     - `capabilities` object (may be empty)
   - If the targeted Codex app-server requires capability negotiation for dynamic tools, include the
     necessary capability flag(s) here.
   - Wait for response (`read_timeout_ms`)
2. `initialized` notification
3. `thread/start` request
   - Params include:
     - `approvalPolicy` = implementation-defined session approval policy value
     - `sandbox` = implementation-defined session sandbox value
     - `cwd` = absolute workspace path
     - If optional client-side tools are implemented, include their advertised tool specs using the
       protocol mechanism supported by the targeted Codex app-server version.
4. `turn/start` request
   - Params include:
     - `threadId`
     - `input` = single text item containing rendered prompt for the first turn, or continuation
       guidance for later turns on the same thread
     - `cwd`
     - `title` = `<issue.identifier>: <issue.title>`
     - `approvalPolicy` = implementation-defined turn approval policy value
     - `sandboxPolicy` = implementation-defined object-form sandbox policy payload when required by
       the targeted app-server version

Session identifiers:

- Read `thread_id` from `thread/start` result `result.thread.id`
- Read `turn_id` from each `turn/start` result `result.turn.id`
- Emit `session_id = "<thread_id>-<turn_id>"`
- Reuse the same `thread_id` for all continuation turns inside one worker run

### 1.3 Streaming Turn Processing

The client reads line-delimited messages until the turn terminates.

Completion conditions:

- `turn/completed` -> success
- `turn/failed` -> failure
- `turn/cancelled` -> failure
- turn timeout (`turn_timeout_ms`) -> failure
- subprocess exit -> failure

Continuation processing:

- If the worker decides to continue after a successful turn, it should issue another `turn/start`
  on the same live `threadId`.
- The app-server subprocess should remain alive across those continuation turns and be stopped only
  when the worker run is ending.

Line handling requirements:

- Read protocol messages from stdout only.
- Buffer partial stdout lines until newline arrives.
- Attempt JSON parse on complete stdout lines.
- Stderr is not part of the protocol stream:
  - ignore it or log it as diagnostics
  - do not attempt protocol JSON parsing on stderr

### 1.4 Emitted Runtime Events (Upstream to Orchestrator)

The app-server client emits structured events to the orchestrator callback. Each event should
include:

- `event` (enum/string)
- `timestamp` (UTC timestamp)
- `codex_app_server_pid` (if available)
- optional `usage` map (token counts)
- payload fields as needed

Important emitted events may include:

- `session_started`
- `startup_failed`
- `turn_completed`
- `turn_failed`
- `turn_cancelled`
- `turn_ended_with_error`
- `turn_input_required`
- `approval_auto_approved`
- `unsupported_tool_call`
- `notification`
- `other_message`
- `malformed`

### 1.5 Approval, Tool Calls, and User Input Policy

Approval, sandbox, and user-input behavior is implementation-defined.

Policy requirements:

- Each implementation should document its chosen approval, sandbox, and operator-confirmation
  posture.
- Approval requests and user-input-required events must not leave a run stalled indefinitely. An
  implementation should either satisfy them, surface them to an operator, auto-resolve them, or
  fail the run according to its documented policy.

Example high-trust behavior:

- Auto-approve command execution approvals for the session.
- Auto-approve file-change approvals for the session.
- Treat user-input-required turns as hard failure.

Unsupported dynamic tool calls:

- Supported dynamic tool calls that are explicitly implemented and advertised by the runtime should
  be handled according to their extension contract.
- If the agent requests a dynamic tool call (`item/tool/call`) that is not supported, return a tool
  failure response and continue the session.
- This prevents the session from stalling on unsupported tool execution paths.

Optional client-side tool extension:

- An implementation may expose a limited set of client-side tools to the app-server session.
- Current optional standardized tool: `linear_graphql`.
- If implemented, supported tools should be advertised to the app-server session during startup
  using the protocol mechanism supported by the targeted Codex app-server version.
- Unsupported tool names should still return a failure result and continue the session.

`linear_graphql` extension contract:

- Purpose: execute a raw GraphQL query or mutation against Linear using Symphony's configured
  tracker auth for the current session.
- Availability: only meaningful when `tracker.kind == "linear"` and valid Linear auth is configured.
- Preferred input shape:

  ```json
  {
    "query": "single GraphQL query or mutation document",
    "variables": {
      "optional": "graphql variables object"
    }
  }
  ```

- `query` must be a non-empty string.
- `query` must contain exactly one GraphQL operation.
- `variables` is optional and, when present, must be a JSON object.
- Implementations may additionally accept a raw GraphQL query string as shorthand input.
- Execute one GraphQL operation per tool call.
- If the provided document contains multiple operations, reject the tool call as invalid input.
- `operationName` selection is intentionally out of scope for this extension.
- Reuse the configured Linear endpoint and auth from the active Symphony workflow/runtime config; do
  not require the coding agent to read raw tokens from disk.
- Tool result semantics:
  - transport success + no top-level GraphQL `errors` -> `success=true`
  - top-level GraphQL `errors` present -> `success=false`, but preserve the GraphQL response body
    for debugging
  - invalid input, missing auth, or transport failure -> `success=false` with an error payload
- Return the GraphQL response or error payload as structured tool output that the model can inspect
  in-session.

Illustrative responses (equivalent payload shapes are acceptable if they preserve the same outcome):

```json
{"id":"<approval-id>","result":{"approved":true}}
{"id":"<tool-call-id>","result":{"success":false,"error":"unsupported_tool_call"}}
```

Hard failure on user input requirement:

- If the agent requests user input, fail the run attempt immediately.
- The client detects this via:
  - explicit method (`item/tool/requestUserInput`), or
  - turn methods/flags indicating input is required.

### 1.6 Timeouts and Error Mapping

Timeouts:

- `codex.read_timeout_ms`: request/response timeout during startup and sync requests
- `codex.turn_timeout_ms`: total turn stream timeout
- `codex.stall_timeout_ms`: enforced by orchestrator based on event inactivity

Error mapping (recommended normalized categories):

- `codex_not_found`
- `invalid_workspace_cwd`
- `response_timeout`
- `turn_timeout`
- `port_exit`
- `response_error`
- `turn_failed`
- `turn_cancelled`
- `turn_input_required`

### 1.7 Agent Runner Contract

The `Agent Runner` wraps workspace + prompt + app-server client.

Behavior:

1. Create/reuse workspace for issue.
2. Build prompt from workflow template.
3. Start app-server session.
4. Forward app-server events to orchestrator.
5. On any error, fail the worker attempt (the orchestrator will retry).

Note:

- Workspaces are intentionally preserved after successful runs.
