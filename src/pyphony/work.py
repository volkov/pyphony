"""CLI subcommand: work — interactive agent session for a Linear issue.

Usage::

    pyphony work SER-11

Flow:
1. Fetch issue from Linear (title, description, comments)
2. Render prompt from WORKFLOW.md template
3. Create / reuse workspace (git worktree or plain directory)
4. Transition issue to "In Progress" if it was "Todo"
5. Launch ``claude`` interactively (user talks to the agent in the same terminal)
6. On exit — post-process:
   a. Find the session transcript
   b. Collect PR URLs (Linear attachments + transcript)
   c. Auto-merge PRs (unless "review required" label is set)
   d. Post the last assistant message as a comment on the issue
   e. Transition to "Done" (or "In Review" if review required)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .automerge import extract_pr_urls_from_transcript, try_automerge_pr
from .config import service_config_from_workflow
from .orchestrator import _build_transcript_url
from .prompt import render_prompt
from .tracker import LinearClient
from .workflow import load_workflow
from .workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


def _find_latest_transcript(workspace_path: str, started_after: float) -> str | None:
    """Return path to the newest Claude transcript created after *started_after*."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    sanitized = workspace_path.replace("/", "-").replace("_", "-")
    projects_dir = Path(config_dir) / "projects" / sanitized

    if not projects_dir.is_dir():
        return None

    newest: Path | None = None
    newest_mtime: float = 0.0

    for f in projects_dir.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
            if mtime > started_after and mtime > newest_mtime:
                newest = f
                newest_mtime = mtime
        except OSError:
            continue

    return str(newest) if newest else None


def _extract_last_assistant_message(transcript_path: str) -> str | None:
    """Extract the last substantial assistant text from a transcript JSONL.

    Walks the file backwards and returns the first (= last chronologically)
    assistant text block that is longer than 20 characters.
    """
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in reversed(lines):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") == "assistant" and isinstance(entry.get("message"), dict):
            texts: list[str] = []
            for block in entry["message"].get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            full = "\n".join(texts).strip()
            if full and len(full) > 20:
                return full

    return None


# ---------------------------------------------------------------------------
# Main async implementation
# ---------------------------------------------------------------------------


async def _work(args: argparse.Namespace) -> None:
    identifier = args.issue_identifier.upper()

    wf = load_workflow(Path(args.workflow_file))
    config = service_config_from_workflow(wf.config, workflow_path=args.workflow_file)

    tracker = LinearClient(config)
    try:
        # ── 1. Fetch issue ──────────────────────────────────────────────
        print(f"🔍 Fetching {identifier}...")
        issue = await tracker.fetch_issue_by_identifier(identifier)
        comments = await tracker.fetch_issue_comments(issue.id)

        print(f"📋 {issue.identifier}: {issue.title}")
        print(f"   State: {issue.state}")
        if issue.labels:
            print(f"   Labels: {', '.join(issue.labels)}")
        print()

        # ── 2. Render prompt ────────────────────────────────────────────
        prompt = render_prompt(
            wf.prompt_template, issue, attempt=1, comments=comments or None
        )

        # ── 3. Prepare workspace ────────────────────────────────────────
        workspace_mgr = WorkspaceManager(config)
        if getattr(args, "main", False):
            repo_path = Path("~/context").expanduser()
            workspace = await workspace_mgr.use_main_repo(repo_path)
        else:
            workspace = await workspace_mgr.create_or_reuse(issue.identifier)
        print(f"📁 Workspace: {workspace.path}")

        # Run before_run hook
        await workspace_mgr.run_before_run(workspace.path)

        # ── 4. Transition to "In Progress" if needed ────────────────────
        if issue.state == "Todo":
            print("🔄 Transitioning to 'In Progress'...")
            await tracker.transition_issue(issue.id, "In Progress")

        # ── 5. Write task prompt to file for reference ──────────────────
        task_file = os.path.join(workspace.path, ".pyphony-task.md")
        Path(task_file).write_text(prompt, encoding="utf-8")

        # ── 6. Launch interactive Claude session ────────────────────────
        print("\n🚀 Launching interactive Claude session...")
        print("─" * 60 + "\n")

        started_at = time.time()

        # Remove CLAUDECODE so claude doesn't think it's nested
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        # Use --append-system-prompt to keep Claude Code's default
        # system prompt while adding the task context.
        cmd = ["claude", "--dangerously-skip-permissions", "--append-system-prompt", prompt]
        subprocess.run(cmd, cwd=workspace.path, env=env)

        print("\n" + "─" * 60)
        print("\n🔄 Post-processing...\n")

        # ── 7. Find transcript ──────────────────────────────────────────
        transcript_path = _find_latest_transcript(workspace.path, started_at)

        last_message: str | None = None
        transcript_pr_urls: list[str] = []

        if transcript_path:
            print(f"📝 Transcript: {transcript_path}")
            last_message = _extract_last_assistant_message(transcript_path)
            transcript_pr_urls = extract_pr_urls_from_transcript(transcript_path)

        # ── 8. Collect PR URLs (Linear attachments + transcript) ────────
        pr_urls = await tracker.fetch_issue_pr_urls(issue.id)
        all_pr_urls = list(dict.fromkeys(pr_urls + transcript_pr_urls))

        # ── 9. Check review-required label ─────────────────────────────
        issue_labels_norm = {l.lower() for l in (issue.labels or [])}
        review_required = "review required" in issue_labels_norm

        # ── 10. Auto-merge PRs (standard workflow) ─────────────────────
        merged_any = False
        if all_pr_urls:
            print(f"\n🔀 Found {len(all_pr_urls)} PR(s):")
            for url in all_pr_urls:
                print(f"   • {url}")

            if review_required:
                print("\n⏸️  Skipping auto-merge: 'review required' label is set")
            else:
                print("\n🔄 Auto-merging PRs...")
                for url in all_pr_urls:
                    merged = await try_automerge_pr(url)
                    status = "✅ Merged" if merged else "⚠️  Failed to merge"
                    print(f"   {status}: {url}")
                    if merged:
                        merged_any = True

        # ── 11. Post session summary as comment ─────────────────────────
        if last_message:
            transcript_url = _build_transcript_url(
                config.server.explorer_base_url, transcript_path or ""
            )
            transcript_line = (
                f"[Transcript]({transcript_url})\n\n" if transcript_url else ""
            )
            comment_body = f"### Interactive work session\n\n{transcript_line}{last_message}"
            print(f"\n💬 Posting session summary to {issue.identifier}...")
            posted = await tracker.comment_on_issue(issue.id, comment_body)
            if posted:
                print("   ✅ Comment posted!")
            else:
                print("   ⚠️  Failed to post comment")

        # ── 12. Transition issue state ──────────────────────────────────
        if review_required:
            print("🔄 Transitioning to 'In Review'...")
            await tracker.transition_issue(issue.id, "In Review")
        elif merged_any:
            print("🔄 Transitioning to 'Done'...")
            await tracker.transition_issue(issue.id, "Done")

        # ── 13. Run after_run hook ──────────────────────────────────────
        await workspace_mgr.run_after_run(workspace.path)

        print("\n✅ Done!")

    finally:
        await tracker.close()


# ---------------------------------------------------------------------------
# Sync entry-point called by CLI
# ---------------------------------------------------------------------------


def work(args: argparse.Namespace) -> None:
    asyncio.run(_work(args))
