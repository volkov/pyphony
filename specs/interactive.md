# Symphony — Interactive Work Sessions

## Relevant code

- [`src/pyphony/work.py`](../src/pyphony/work.py) — main `work` subcommand implementation
- [`src/pyphony/automerge.py`](../src/pyphony/automerge.py) — PR automerge logic
- [`src/pyphony/url_handler.py`](../src/pyphony/url_handler.py) — `pyphony://` URL scheme handler
- [`src/pyphony/cli.py`](../src/pyphony/cli.py) — CLI argument parsing

## 1. Overview

The `work` subcommand provides an interactive mode where the user collaborates with a Claude Code agent on a Linear issue in the same terminal. Unlike the automated orchestrator mode (where agents work autonomously), `work` gives the user direct control over the conversation.

```
pyphony work <ISSUE-ID> [--main]
```

## 2. Session Lifecycle

### 2.1 Initialization

1. **Fetch issue** — load issue from Linear including title, description, and comments
2. **Render prompt** — apply WORKFLOW.md Jinja2 template with issue data
3. **Prepare workspace**:
   - Default: create or reuse `<workspace.root>/<issue_identifier>` (with `after_create` hook on first creation)
   - `--main` flag: use `~/context` directly (requires clean working copy on main branch)
4. **Run `before_run` hook** if configured
5. **State transition** — move issue from `Todo` → `In Progress` (skipped if already in progress)
6. **Write task file** — save rendered prompt to `.pyphony-task.md` in the workspace

### 2.2 Interactive Session

Launch `claude --dangerously-skip-permissions --append-system-prompt <prompt>` in the workspace directory. The user interacts with Claude Code directly in their terminal.

The `CLAUDECODE` environment variable is removed to prevent nested-agent detection.

### 2.3 Post-Processing

After the user exits the Claude session:

1. **Find transcript** — locate the latest `.jsonl` transcript created during the session in `~/.claude/projects/`
2. **Extract last message** — parse the last substantial assistant text (>20 chars) from the transcript
3. **Collect PR URLs** — merge PR URLs from two sources:
   - Linear issue attachments (via `fetch_issue_pr_urls`)
   - Transcript text (regex scan for `github.com/.../pull/N`)
4. **Auto-merge PRs** — unless the issue has label `review required`:
   - Try `gh pr merge --squash --delete-branch`
   - On failure: update branch from base via GitHub API, retry up to 3 times
5. **Post comment** — post the last assistant message as a comment on the Linear issue, with a link to the transcript if available
6. **Transition issue**:
   - `review required` label → move to `In Review`
   - PR merged successfully → move to `Done`
7. **Run `after_run` hook** if configured

## 3. Automerge

The automerge subsystem (`automerge.py`) handles merging GitHub PRs created during work sessions or by automated agents.

### 3.1 Merge Strategy

1. **Optimistic merge** — try `gh pr merge --squash --delete-branch` directly
2. **Branch update + retry** — if merge fails (e.g. branch behind base):
   - Call GitHub API `PUT /repos/{owner}/{repo}/pulls/{number}/update-branch`
   - Wait 5 seconds for GitHub to process
   - Retry merge (up to 3 attempts)

### 3.2 PR URL Discovery

PR URLs are collected from:
- **Linear attachments** — GitHub integration links PRs to issues
- **Transcript scanning** — regex `https://github.com/.../pull/\d+` in the last 200 lines of the transcript JSONL

## 4. URL Scheme (`pyphony://`)

### 4.1 Format

```
pyphony://<ISSUE-ID>/work[?interactive=true]
```

Examples:
- `pyphony://SER-42/work` — open interactive session for SER-42
- `pyphony://SER-42/work?interactive=true` — same, explicit interactive flag

### 4.2 Registration

```bash
pyphony install-url-scheme
```

Creates a macOS `.app` bundle in `~/Applications/` that registers the `pyphony://` URL scheme. When a `pyphony://` URL is clicked (e.g. in a browser or Linear), it:

1. Parses the URL to extract issue identifier and action
2. Resolves the `pyphony` executable path
3. Opens a new iTerm2 tab (or Terminal.app window as fallback)
4. Runs `pyphony work <ISSUE-ID>` in the new tab

### 4.3 Manual URL Handling

```bash
pyphony open-url "pyphony://SER-42/work"
```
