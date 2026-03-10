# Symphony — Logging, Status & Observability

## Relevant code

- [`src/pyphony/logging.py`](../src/pyphony/logging.py)
- [`src/pyphony/server.py`](../src/pyphony/server.py)

---

## 1. Logging, Status, and Observability

### 1.1 Logging Conventions

Required context fields for issue-related logs:

- `issue_id`
- `issue_identifier`

Required context for coding-agent session lifecycle logs:

- `session_id`

Message formatting requirements:

- Use stable `key=value` phrasing.
- Include action outcome (`completed`, `failed`, `retrying`, etc.).
- Include concise failure reason when present.
- Avoid logging large raw payloads unless necessary.

### 1.2 Logging Outputs and Sinks

The spec does not prescribe where logs must go (stderr, file, remote sink, etc.).

Requirements:

- Operators must be able to see startup/validation/dispatch failures without attaching a debugger.
- Implementations may write to one or more sinks.
- If a configured log sink fails, the service should continue running when possible and emit an
  operator-visible warning through any remaining sink.

### 1.3 Runtime Snapshot / Monitoring Interface (Optional but Recommended)

If the implementation exposes a synchronous runtime snapshot (for dashboards or monitoring), it
should return:

- `running` (list of running session rows)
- each running row should include `turn_count`
- `retrying` (list of retry queue rows)
- `codex_totals`
  - `input_tokens`
  - `output_tokens`
  - `total_tokens`
  - `seconds_running` (aggregate runtime seconds as of snapshot time, including active sessions)
- `rate_limits` (latest coding-agent rate limit payload, if available)

Recommended snapshot error modes:

- `timeout`
- `unavailable`

### 1.4 Optional Human-Readable Status Surface

A human-readable status surface (terminal output, dashboard, etc.) is optional and
implementation-defined.

If present, it should draw from orchestrator state/metrics only and must not be required for
correctness.

### 1.5 Session Metrics and Token Accounting

Token accounting rules:

- Agent events may include token counts in multiple payload shapes.
- Prefer absolute thread totals when available, such as:
  - `thread/tokenUsage/updated` payloads
  - `total_token_usage` within token-count wrapper events
- Ignore delta-style payloads such as `last_token_usage` for dashboard/API totals.
- Extract input/output/total token counts leniently from common field names within the selected
  payload.
- For absolute totals, track deltas relative to last reported totals to avoid double-counting.
- Do not treat generic `usage` maps as cumulative totals unless the event type defines them that
  way.
- Accumulate aggregate totals in orchestrator state.

Runtime accounting:

- Runtime should be reported as a live aggregate at snapshot/render time.
- Implementations may maintain a cumulative counter for ended sessions and add active-session
  elapsed time derived from `running` entries (for example `started_at`) when producing a
  snapshot/status view.
- Add run duration seconds to the cumulative ended-session runtime when a session ends (normal exit
  or cancellation/termination).
- Continuous background ticking of runtime totals is not required.

Rate-limit tracking:

- Track the latest rate-limit payload seen in any agent update.
- Any human-readable presentation of rate-limit data is implementation-defined.

### 1.6 Humanized Agent Event Summaries (Optional)

Humanized summaries of raw agent protocol events are optional.

If implemented:

- Treat them as observability-only output.
- Do not make orchestrator logic depend on humanized strings.

### 1.7 Optional HTTP Server Extension

This section defines an optional HTTP interface for observability and operational control.

If implemented:

- The HTTP server is an extension and is not required for conformance.
- The implementation may serve server-rendered HTML or a client-side application for the dashboard.
- The dashboard/API must be observability/control surfaces only and must not become required for
  orchestrator correctness.

Enablement (extension):

- Start the HTTP server when a CLI `--port` argument is provided.
- Start the HTTP server when `server.port` is present in `WORKFLOW.md` front matter.
- `server.port` is extension configuration and is intentionally not part of the core front-matter
  schema in Section 5.3.
- Precedence: CLI `--port` overrides `server.port` when both are present.
- `server.port` must be an integer. Positive values bind that port. `0` may be used to request an
  ephemeral port for local development and tests.
- Implementations should bind loopback by default (`127.0.0.1` or host equivalent) unless explicitly
  configured otherwise.
- Changes to HTTP listener settings (for example `server.port`) do not need to hot-rebind;
  restart-required behavior is conformant.

#### 1.7.1 Human-Readable Dashboard (`/`)

- Host a human-readable dashboard at `/`.
- The returned document should depict the current state of the system (for example active sessions,
  retry delays, token consumption, runtime totals, recent events, and health/error indicators).
- It is up to the implementation whether this is server-generated HTML or a client-side app that
  consumes the JSON API below.

#### 1.7.2 JSON REST API (`/api/v1/*`)

Provide a JSON REST API under `/api/v1/*` for current runtime state and operational debugging.

Minimum endpoints:

- `GET /api/v1/state`
  - Returns a summary view of the current system state (running sessions, retry queue/delays,
    aggregate token/runtime totals, latest rate limits, and any additional tracked summary fields).
  - Suggested response shape:

    ```json
    {
      "generated_at": "2026-02-24T20:15:30Z",
      "counts": {
        "running": 2,
        "retrying": 1
      },
      "running": [
        {
          "issue_id": "abc123",
          "issue_identifier": "MT-649",
          "state": "In Progress",
          "session_id": "thread-1-turn-1",
          "turn_count": 7,
          "last_event": "turn_completed",
          "last_message": "",
          "started_at": "2026-02-24T20:10:12Z",
          "last_event_at": "2026-02-24T20:14:59Z",
          "tokens": {
            "input_tokens": 1200,
            "output_tokens": 800,
            "total_tokens": 2000
          }
        }
      ],
      "retrying": [
        {
          "issue_id": "def456",
          "issue_identifier": "MT-650",
          "attempt": 3,
          "due_at": "2026-02-24T20:16:00Z",
          "error": "no available orchestrator slots"
        }
      ],
      "codex_totals": {
        "input_tokens": 5000,
        "output_tokens": 2400,
        "total_tokens": 7400,
        "seconds_running": 1834.2
      },
      "rate_limits": null
    }
    ```

- `GET /api/v1/<issue_identifier>`
  - Returns issue-specific runtime/debug details for the identified issue, including any information
    the implementation tracks that is useful for debugging.
  - Suggested response shape:

    ```json
    {
      "issue_identifier": "MT-649",
      "issue_id": "abc123",
      "status": "running",
      "workspace": {
        "path": "/tmp/symphony_workspaces/MT-649"
      },
      "attempts": {
        "restart_count": 1,
        "current_retry_attempt": 2
      },
      "running": {
        "session_id": "thread-1-turn-1",
        "turn_count": 7,
        "state": "In Progress",
        "started_at": "2026-02-24T20:10:12Z",
        "last_event": "notification",
        "last_message": "Working on tests",
        "last_event_at": "2026-02-24T20:14:59Z",
        "tokens": {
          "input_tokens": 1200,
          "output_tokens": 800,
          "total_tokens": 2000
        }
      },
      "retry": null,
      "logs": {
        "codex_session_logs": [
          {
            "label": "latest",
            "path": "/var/log/symphony/codex/MT-649/latest.log",
            "url": null
          }
        ]
      },
      "recent_events": [
        {
          "at": "2026-02-24T20:14:59Z",
          "event": "notification",
          "message": "Working on tests"
        }
      ],
      "last_error": null,
      "tracked": {}
    }
    ```

  - If the issue is unknown to the current in-memory state, return `404` with an error response (for
    example `{"error":{"code":"issue_not_found","message":"..."}}`).

- `POST /api/v1/refresh`
  - Queues an immediate tracker poll + reconciliation cycle (best-effort trigger; implementations
    may coalesce repeated requests).
  - Suggested request body: empty body or `{}`.
  - Suggested response (`202 Accepted`) shape:

    ```json
    {
      "queued": true,
      "coalesced": false,
      "requested_at": "2026-02-24T20:15:30Z",
      "operations": ["poll", "reconcile"]
    }
    ```

API design notes:

- The JSON shapes above are the recommended baseline for interoperability and debugging ergonomics.
- Implementations may add fields, but should avoid breaking existing fields within a version.
- Endpoints should be read-only except for operational triggers like `/refresh`.
- Unsupported methods on defined routes should return `405 Method Not Allowed`.
- API errors should use a JSON envelope such as `{"error":{"code":"...","message":"..."}}`.
- If the dashboard is a client-side app, it should consume this API rather than duplicating state
  logic.
