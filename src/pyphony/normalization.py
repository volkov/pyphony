from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Issue

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_workspace_key(identifier: str) -> str:
    return _UNSAFE_CHARS.sub("_", identifier)


def normalize_state(state: str) -> str:
    return state.strip().lower()


def normalize_label(label: str) -> str:
    """Normalize a label for comparison.

    Converts to lowercase and replaces hyphens/underscores with spaces so that
    ``"plan-required"``, ``"Plan Required"`` and ``"plan_required"`` all become
    ``"plan required"``.
    """
    return label.strip().lower().replace("-", " ").replace("_", " ")


def sort_issues_for_dispatch(issues: list[Issue]) -> list[Issue]:
    def sort_key(issue: Issue) -> tuple:
        priority_key = issue.priority if issue.priority is not None else float("inf")
        created_key = issue.created_at if issue.created_at is not None else ""
        return (priority_key, created_key, issue.identifier)

    return sorted(issues, key=sort_key)
