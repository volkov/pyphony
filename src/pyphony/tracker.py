"""Linear issue tracker client."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .errors import (
    LinearApiRequestError,
    LinearApiStatusError,
    LinearGraphQLError,
    LinearMissingEndCursor,
    LinearUnknownPayload,
)
from .models import BlockerRef, Issue, ServiceConfig
from .tracker_queries import (
    CANDIDATE_ISSUES_QUERY,
    ISSUE_STATES_BY_IDS_QUERY,
    ISSUES_BY_STATES_QUERY,
)

_PAGE_SIZE = 50


class LinearClient:
    """Async client for the Linear GraphQL API."""

    def __init__(self, config: ServiceConfig) -> None:
        self._endpoint = config.tracker.endpoint
        self._api_key = config.tracker.api_key
        self._project_slug = config.tracker.project_slug
        self._active_states = config.tracker.active_states
        self._terminal_states = config.tracker.terminal_states
        self._client = httpx.AsyncClient(timeout=30.0)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Paginate through all issues in active states for the project."""
        variables: dict = {
            "projectSlug": self._project_slug,
            "stateNames": self._active_states,
            "first": _PAGE_SIZE,
        }
        return await self._paginate(CANDIDATE_ISSUES_QUERY, variables)

    async def fetch_issue_states_by_ids(self, ids: list[str]) -> dict[str, str]:
        """Return ``{issue_id: state_name}`` for the given IDs."""
        if not ids:
            return {}

        result: dict[str, str] = {}
        after: str | None = None
        while True:
            variables: dict = {
                "ids": ids,
                "first": _PAGE_SIZE,
            }
            if after is not None:
                variables["after"] = after

            data = await self._execute(ISSUE_STATES_BY_IDS_QUERY, variables)
            issues_data = data.get("issues")
            if issues_data is None:
                raise LinearUnknownPayload("Missing 'issues' key in response data")

            for node in issues_data.get("nodes", []):
                result[node["id"]] = node["state"]["name"]

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage"):
                end_cursor = page_info.get("endCursor")
                if not end_cursor:
                    raise LinearMissingEndCursor(
                        "hasNextPage is true but endCursor is missing"
                    )
                after = end_cursor
            else:
                break

        return result

    async def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        """Fetch issues in the given states (used for terminal cleanup)."""
        if not states:
            return []

        variables: dict = {
            "projectSlug": self._project_slug,
            "stateNames": states,
            "first": _PAGE_SIZE,
        }
        return await self._paginate(ISSUES_BY_STATES_QUERY, variables)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _paginate(self, query: str, variables: dict) -> list[Issue]:
        """Execute a paginated GraphQL query and return normalized issues."""
        all_issues: list[Issue] = []
        after: str | None = None

        while True:
            page_vars = dict(variables)
            if after is not None:
                page_vars["after"] = after

            data = await self._execute(query, page_vars)
            issues_data = data.get("issues")
            if issues_data is None:
                raise LinearUnknownPayload("Missing 'issues' key in response data")

            for node in issues_data.get("nodes", []):
                all_issues.append(self._normalize_issue(node))

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage"):
                end_cursor = page_info.get("endCursor")
                if not end_cursor:
                    raise LinearMissingEndCursor(
                        "hasNextPage is true but endCursor is missing"
                    )
                after = end_cursor
            else:
                break

        return all_issues

    async def _execute(self, query: str, variables: dict) -> dict:
        """Send a GraphQL request and return the ``data`` portion."""
        payload = {"query": query, "variables": variables}
        headers = {
            "Authorization": self._api_key or "",
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(
                self._endpoint, json=payload, headers=headers
            )
        except httpx.RequestError as exc:
            raise LinearApiRequestError(str(exc)) from exc

        if response.status_code != 200:
            raise LinearApiStatusError(
                f"HTTP {response.status_code}: {response.text}"
            )

        body = response.json()

        if "errors" in body:
            messages = [e.get("message", str(e)) for e in body["errors"]]
            raise LinearGraphQLError("; ".join(messages))

        if "data" not in body:
            raise LinearUnknownPayload("Response missing 'data' key")

        return body["data"]

    def _normalize_issue(self, node: dict) -> Issue:
        """Convert a raw GraphQL issue node into an ``Issue`` model."""
        labels = [
            label_node["name"].lower()
            for label_node in (node.get("labels", {}) or {}).get("nodes", [])
        ]

        blocked_by: list[BlockerRef] = []
        for rel in (node.get("relations", {}) or {}).get("nodes", []):
            if rel.get("type") == "blocks":
                related = rel.get("relatedIssue", {})
                blocked_by.append(
                    BlockerRef(
                        id=related.get("id"),
                        identifier=related.get("identifier"),
                        state=(related.get("state") or {}).get("name"),
                    )
                )

        priority_raw = node.get("priority")
        priority = int(priority_raw) if priority_raw is not None else None

        created_at = _parse_iso(node.get("createdAt"))
        updated_at = _parse_iso(node.get("updatedAt"))

        return Issue(
            id=node["id"],
            identifier=node["identifier"],
            title=node["title"],
            description=node.get("description"),
            priority=priority,
            state=node["state"]["name"],
            branch_name=node.get("branchName"),
            url=node.get("url"),
            labels=labels,
            blocked_by=blocked_by,
            created_at=created_at,
            updated_at=updated_at,
        )


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning *None* on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
