"""Automerge helper — merges GitHub PRs via the ``gh`` CLI."""

from __future__ import annotations

import asyncio
import re

import structlog

log = structlog.stdlib.get_logger()


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


async def try_automerge_pr(pr_url: str) -> bool:
    """Attempt to merge a GitHub PR using ``gh pr merge``.

    Returns *True* if the merge succeeded, *False* otherwise.
    """
    ref = _parse_pr_ref(pr_url)
    if ref is None:
        log.warning("automerge_invalid_url", pr_url=pr_url)
        return False

    repo, pr_number = ref

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "merge", pr_number,
            "--squash",
            "--delete-branch",
            "--repo", repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            log.info(
                "automerge_success",
                repo=repo,
                pr_number=pr_number,
            )
            return True

        log.warning(
            "automerge_gh_failed",
            repo=repo,
            pr_number=pr_number,
            returncode=proc.returncode,
            stderr=stderr.decode(errors="replace").strip(),
        )
        return False

    except FileNotFoundError:
        log.error("automerge_gh_not_found", detail="gh CLI is not installed or not in PATH")
        return False
    except Exception as exc:
        log.error("automerge_error", error=str(exc))
        return False
