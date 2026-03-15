# Symphony — Supervisor

## Relevant code

- [`src/pyphony/supervisor.py`](../src/pyphony/supervisor.py) — supervisor process implementation
- [`pyproject.toml`](../pyproject.toml) — `pyphony-sv` entry point definition

## 1. Overview

The supervisor (`pyphony-sv`) is a process wrapper that keeps the pyphony service running with the latest code. When an agent merges a PR, the supervisor pulls the updated code and restarts the service so subsequent agents work with the new codebase.

## 2. Usage

```bash
# Auto-discover all workflow files in workflows/
pyphony-sv

# Explicit workflow files
pyphony-sv workflows/project1.md workflows/project2.md

# Pass extra arguments to pyphony run
pyphony-sv -- --port 8080 --log-level DEBUG

# Custom pull interval
pyphony-sv --pull-interval 30
```

## 3. Restart Loop

```
┌─────────────────────────────────────────┐
│                                         │
│  1. git pull --rebase                   │
│  2. uv sync                             │
│  3. pyphony run --exit-on-merge [args]  │
│  4. Wait for process exit               │
│                                         │
│  Exit code 10 (merge) ──► goto 1        │
│  Exit code 0 ──► stop supervisor        │
│  Other exit code ──► wait 5s, goto 1    │
│                                         │
└─────────────────────────────────────────┘
```

### 3.1 Steps

1. **`git pull --rebase`** — fetch and rebase on latest remote. Timeout: 120 seconds. Failures are logged but don't stop the cycle.

2. **`uv sync`** — ensure the editable install points to the current directory and dependencies are up to date.

3. **Run pyphony** — start `python -m pyphony run <workflows> --exit-on-merge [extra_args]` as a subprocess. All discovered workflow files are passed to a single process (multi-workflow support).

4. **Wait for exit** — poll the subprocess every 0.5 seconds.

### 3.2 Exit Code Handling

| Exit code | Meaning | Action |
|-----------|---------|--------|
| `10` | Merge detected — an issue transitioned to Done | Pull and restart immediately |
| `0` | Clean exit | Stop supervisor |
| Other | Error or crash | Wait 5 seconds, then restart |

### 3.3 Workflow Discovery

If no workflow files are specified on the command line, the supervisor auto-discovers all `*.md` files in the `workflows/` directory. Discovery is re-run after each `git pull`, so new workflow files added via merged PRs are picked up automatically.

## 4. Signal Handling

The supervisor handles `SIGINT` and `SIGTERM` gracefully:
1. Sets a shutdown flag
2. Sends `SIGTERM` to the child process
3. Waits up to 10 seconds for clean exit
4. Sends `SIGKILL` if the child doesn't terminate

## 5. Exit-on-Merge Mechanism

The `--exit-on-merge` flag in the pyphony service triggers a drain mode when any issue transitions to `Done`:
1. Orchestrator stops dispatching new tasks
2. Waits for all running agents to finish
3. Exits with code 10

This is critical for the supervisor workflow: merged PRs change the codebase, so agents must be restarted to work with the updated code.
