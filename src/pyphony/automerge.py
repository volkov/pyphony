"""Automerge helper — merges GitHub PRs via the ``gh`` CLI.

When multiple agents work in parallel, their PRs may fall behind ``main``
as other PRs get merged first.  Before attempting to merge we therefore
call the GitHub "update branch" API so the PR incorporates the latest
base-branch changes and avoids merge-conflict failures.
"""

from __future__ import annotations

import asyncio
import re

import structlog

log = structlog.stdlib.get_logger()

# How many times to retry merge after updating the branch.
_MAX_UPDATE_RETRIES = 3
_RETRY_DELAY_S = 5


def _parse_pr_ref(pr_url: str) -> tuple[str, str] | None:
    """Extract ``(owner/repo, pr_number)`` from a GitHub PR URL.

    Returns *None* if the URL doesn't look like a GitHub PR.
    """
    match = re.match(
        r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)",
        pr_url,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


async def _gh_update_branch(repo: str, pr_number: str) -> bool:
    """Update the PR branch from its base branch via the GitHub API.

    Uses ``gh api`` to call the "update branch" endpoint so the PR
    incorporates the latest changes from ``main`` (or whatever the base is).
    Returns *True* if the update succeeded (or the branch was already up to
    date), *False* otherwise.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh", "api",
        f"repos/{repo}/pulls/{pr_number}/update-branch",
        "--method", "PUT",
        "--repo", repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    stderr_text = stderr.decode(errors="replace").strip()

    if proc.returncode == 0:
        log.info("automerge_branch_updated", repo=repo, pr_number=pr_number)
        return True

    # 422 "merge conflict" means the branch can't be updated automatically
    # 202 is returned via stdout when update is queued (still success)
    if "already up-to-date" in stderr_text.lower() or "already up-to-date" in stdout.decode(errors="replace").lower():
        log.info("automerge_branch_already_up_to_date", repo=repo, pr_number=pr_number)
        return True

    log.warning(
        "automerge_branch_update_failed",
        repo=repo,
        pr_number=pr_number,
        stderr=stderr_text,
    )
    return False


async def _gh_merge(repo: str, pr_number: str) -> tuple[bool, str]:
    """Run ``gh pr merge --squash`` and return ``(success, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "pr", "merge", pr_number,
        "--squash",
        "--delete-branch",
        "--repo", repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode == 0, stderr.decode(errors="replace").strip()


async def try_automerge_pr(pr_url: str) -> bool:
    """Merge a GitHub PR, updating the branch first if needed.

    1. Try a direct ``gh pr merge --squash``.
    2. If that fails (e.g. branch is behind base), update the PR branch
       via the GitHub API and retry up to ``_MAX_UPDATE_RETRIES`` times.

    Returns *True* if the PR was merged, *False* otherwise.
    """
    ref = _parse_pr_ref(pr_url)
    if ref is None:
        log.warning("automerge_invalid_url", pr_url=pr_url)
        return False

    repo, pr_number = ref

    try:
        # Optimistic: try merge directly first
        ok, stderr = await _gh_merge(repo, pr_number)
        if ok:
            log.info("automerge_success", repo=repo, pr_number=pr_number)
            return True

        log.info(
            "automerge_direct_failed_updating_branch",
            repo=repo,
            pr_number=pr_number,
            stderr=stderr,
        )

        # Branch may be behind base — update and retry
        for attempt in range(1, _MAX_UPDATE_RETRIES + 1):
            updated = await _gh_update_branch(repo, pr_number)
            if not updated:
                log.warning(
                    "automerge_update_failed_giving_up",
                    repo=repo,
                    pr_number=pr_number,
                    attempt=attempt,
                )
                return False

            # Give GitHub a moment to process the branch update
            await asyncio.sleep(_RETRY_DELAY_S)

            ok, stderr = await _gh_merge(repo, pr_number)
            if ok:
                log.info(
                    "automerge_success_after_update",
                    repo=repo,
                    pr_number=pr_number,
                    attempt=attempt,
                )
                return True

            log.info(
                "automerge_retry_failed",
                repo=repo,
                pr_number=pr_number,
                attempt=attempt,
                stderr=stderr,
            )

        log.warning(
            "automerge_exhausted_retries",
            repo=repo,
            pr_number=pr_number,
            max_retries=_MAX_UPDATE_RETRIES,
        )
        return False

    except FileNotFoundError:
        log.error("automerge_gh_not_found", detail="gh CLI is not installed or not in PATH")
        return False
    except Exception as exc:
        log.error("automerge_error", error=str(exc))
        return False
