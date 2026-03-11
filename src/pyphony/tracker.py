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
import structlog

from .tracker_queries import (
    CANDIDATE_ISSUES_QUERY,
    COMMENT_CREATE_MUTATION,
    ISSUE_ATTACHMENTS_QUERY,
    ISSUE_BY_IDENTIFIER_QUERY,
    ISSUE_FULL_BY_IDENTIFIER_QUERY,
    ISSUE_COMMENTS_QUERY,
    ISSUE_CREATE_MUTATION,
    ISSUE_LABEL_CREATE_MUTATION,
    ISSUE_LABEL_IDS_QUERY,
    ISSUE_STATES_BY_IDS_QUERY,
    ISSUE_TEAM_QUERY,
    ISSUE_UPDATE_MUTATION,
    ISSUE_UPDATE_STATE_MUTATION,
    ISSUES_BY_STATES_QUERY,
    PROJECT_TEAMS_QUERY,
    TEAM_LABELS_QUERY,
    WORKFLOW_STATES_QUERY,
)

log = structlog.stdlib.get_logger()

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
        self._workflow_states: dict[str, str] | None = None

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

    async def fetch_issue_states_by_ids(
        self, ids: list[str]
    ) -> dict[str, dict[str, object]]:
        """Return ``{issue_id: {"state": str, "labels": list[str]}}`` for the given IDs."""
        if not ids:
            return {}

        result: dict[str, dict[str, object]] = {}
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
                labels = [
                    label_node["name"].lower()
                    for label_node in (node.get("labels", {}) or {}).get("nodes", [])
                ]
                result[node["id"]] = {
                    "state": node["state"]["name"],
                    "labels": labels,
                }

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

    async def fetch_workflow_states(self, issue_id: str | None = None) -> dict[str, str]:
        """Return ``{state_name: state_id}``, cached after first call.

        Requires *issue_id* on the first call to resolve the team.
        """
        if self._workflow_states is not None:
            return self._workflow_states

        if not issue_id:
            raise LinearUnknownPayload(
                "issue_id is required to resolve workflow states on first call"
            )

        # Step 1: resolve team ID from an issue
        issue_data = await self._execute(
            ISSUE_TEAM_QUERY,
            {"issueId": issue_id},
        )
        issue_node = issue_data.get("issue")
        if not issue_node:
            raise LinearUnknownPayload(
                f"Issue '{issue_id}' not found"
            )
        team = issue_node.get("team")
        if not team or not team.get("id"):
            raise LinearUnknownPayload(
                f"No team found for issue '{issue_id}'"
            )
        team_id = team["id"]

        # Step 2: fetch workflow states for that team
        data = await self._execute(
            WORKFLOW_STATES_QUERY,
            {"teamId": team_id},
        )
        nodes = data.get("workflowStates", {}).get("nodes", [])
        self._workflow_states = {n["name"]: n["id"] for n in nodes}
        return self._workflow_states

    async def transition_issue(self, issue_id: str, target_state: str) -> bool:
        """Transition an issue to *target_state*. Returns True on success."""
        states = await self.fetch_workflow_states(issue_id=issue_id)
        state_id = states.get(target_state)
        if not state_id:
            log.warning(
                "transition_state_not_found",
                target_state=target_state,
                available=list(states.keys()),
            )
            return False

        data = await self._execute(
            ISSUE_UPDATE_STATE_MUTATION,
            {"issueId": issue_id, "stateId": state_id},
        )
        success = data.get("issueUpdate", {}).get("success", False)
        return success

    async def fetch_issue_pr_urls(self, issue_id: str) -> list[str]:
        """Return GitHub PR URLs attached to an issue."""
        data = await self._execute(
            ISSUE_ATTACHMENTS_QUERY,
            {"issueId": issue_id},
        )
        issue_node = data.get("issue")
        if not issue_node:
            return []

        urls: list[str] = []
        for attachment in (issue_node.get("attachments") or {}).get("nodes", []):
            url = attachment.get("url", "")
            if url and ("github.com" in url) and ("/pull/" in url):
                urls.append(url)
        return urls

    async def fetch_issue_comments(self, issue_id: str) -> list[dict]:
        """Fetch comments on an issue. Returns list of dicts with body, createdAt, user."""
        data = await self._execute(
            ISSUE_COMMENTS_QUERY,
            {"issueId": issue_id},
        )
        issue_node = data.get("issue")
        if not issue_node:
            return []

        comments: list[dict] = []
        for node in (issue_node.get("comments") or {}).get("nodes", []):
            comments.append({
                "body": node.get("body", ""),
                "created_at": node.get("createdAt", ""),
                "user": (node.get("user") or {}).get("name", ""),
            })
        comments.sort(key=lambda c: c["created_at"])
        return comments

    async def replace_issue_labels(
        self,
        issue_id: str,
        remove_labels: list[str],
        add_labels: list[str],
    ) -> bool:
        """Remove *remove_labels* and add *add_labels* on an issue (case-insensitive).

        Creates any *add_labels* that don't exist yet.  Returns True on success.
        """
        # 1. Fetch current label IDs on the issue
        data = await self._execute(ISSUE_LABEL_IDS_QUERY, {"issueId": issue_id})
        issue_node = data.get("issue")
        if not issue_node:
            log.warning("replace_labels_issue_not_found", issue_id=issue_id)
            return False

        current_labels: dict[str, str] = {}  # lowercase name → id
        for node in (issue_node.get("labels") or {}).get("nodes", []):
            current_labels[node["name"].lower()] = node["id"]

        # 2. Resolve team for label lookup / creation
        team_data = await self._execute(ISSUE_TEAM_QUERY, {"issueId": issue_id})
        team = (team_data.get("issue") or {}).get("team")
        if not team or not team.get("id"):
            log.warning("replace_labels_team_not_found", issue_id=issue_id)
            return False
        team_id = team["id"]

        # 3. Fetch all team labels so we can find IDs for add_labels
        team_labels_data = await self._execute(
            TEAM_LABELS_QUERY, {"teamId": team_id}
        )
        team_labels: dict[str, str] = {}  # lowercase name → id
        for node in (team_labels_data.get("issueLabels") or {}).get("nodes", []):
            team_labels[node["name"].lower()] = node["id"]

        # 4. Compute new label ID set
        remove_lower = {r.lower() for r in remove_labels}
        new_label_ids = [
            lid for lname, lid in current_labels.items()
            if lname not in remove_lower
        ]

        # 5. Resolve or create add_labels
        for label_name in add_labels:
            lower = label_name.lower()
            if lower in {lname for lname in current_labels if lname not in remove_lower}:
                continue  # already on the issue
            label_id = team_labels.get(lower)
            if not label_id:
                # Create the label
                create_data = await self._execute(
                    ISSUE_LABEL_CREATE_MUTATION,
                    {"teamId": team_id, "name": label_name},
                )
                created = (create_data.get("issueLabelCreate") or {}).get("issueLabel")
                if created:
                    label_id = created["id"]
                else:
                    log.warning(
                        "label_create_failed",
                        label_name=label_name,
                        issue_id=issue_id,
                    )
                    continue
            new_label_ids.append(label_id)

        # 6. Update issue with new label set
        data = await self._execute(
            ISSUE_UPDATE_MUTATION,
            {"issueId": issue_id, "input": {"labelIds": new_label_ids}},
        )
        return data.get("issueUpdate", {}).get("success", False)

    async def comment_on_issue(self, issue_id: str, body: str) -> bool:
        """Post a comment on an issue. Returns True on success."""
        data = await self._execute(
            COMMENT_CREATE_MUTATION,
            {"issueId": issue_id, "body": body},
        )
        return data.get("commentCreate", {}).get("success", False)

    async def get_issue(self, identifier: str) -> dict[str, str | None]:
        """Fetch an issue by identifier (e.g. SER-27). Returns dict with issue fields."""
        parts = identifier.upper().rsplit("-", 1)
        if len(parts) != 2:
            raise LinearUnknownPayload(f"Invalid identifier format: {identifier}")
        team_prefix, number_str = parts
        try:
            number = int(number_str)
        except ValueError:
            raise LinearUnknownPayload(f"Invalid identifier format: {identifier}")

        data = await self._execute(
            ISSUE_BY_IDENTIFIER_QUERY,
            {
                "filter": {
                    "team": {"key": {"eq": team_prefix}},
                    "number": {"eq": number},
                },
                "first": 1,
            },
        )
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            raise LinearUnknownPayload(f"Issue {identifier} not found")

        node = nodes[0]
        return {
            "id": node.get("id", ""),
            "identifier": node.get("identifier", ""),
            "title": node.get("title", ""),
            "description": node.get("description"),
            "state": (node.get("state") or {}).get("name", ""),
            "url": node.get("url", ""),
        }

    async def fetch_issue_by_identifier(self, identifier: str) -> Issue:
        """Fetch an issue by identifier (e.g. SER-27) as a full Issue model."""
        parts = identifier.upper().rsplit("-", 1)
        if len(parts) != 2:
            raise LinearUnknownPayload(f"Invalid identifier format: {identifier}")
        team_prefix, number_str = parts
        try:
            number = int(number_str)
        except ValueError:
            raise LinearUnknownPayload(f"Invalid identifier format: {identifier}")

        data = await self._execute(
            ISSUE_FULL_BY_IDENTIFIER_QUERY,
            {
                "filter": {
                    "team": {"key": {"eq": team_prefix}},
                    "number": {"eq": number},
                },
                "first": 1,
            },
        )
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            raise LinearUnknownPayload(f"Issue {identifier} not found")

        return self._normalize_issue(nodes[0])

    async def update_issue(
        self,
        identifier: str,
        title: str | None = None,
        description: str | None = None,
        state: str | None = None,
    ) -> dict[str, str | None]:
        """Update an issue by identifier. Returns dict with updated issue fields."""
        # First, get the issue to find its internal ID
        issue_data = await self.get_issue(identifier)
        issue_id = issue_data["id"]

        input_fields: dict = {}
        if title is not None:
            input_fields["title"] = title
        if description is not None:
            input_fields["description"] = description
        if state is not None:
            # Resolve state name to state ID
            states = await self.fetch_workflow_states(issue_id=issue_id)
            state_id = states.get(state)
            if not state_id:
                raise LinearUnknownPayload(
                    f"State '{state}' not found; available states: {list(states.keys())}"
                )
            input_fields["stateId"] = state_id

        if not input_fields:
            return issue_data  # nothing to update

        data = await self._execute(
            ISSUE_UPDATE_MUTATION,
            {"issueId": issue_id, "input": input_fields},
        )
        result = data.get("issueUpdate", {})
        if not result.get("success"):
            raise LinearUnknownPayload("issueUpdate returned success=false")

        issue = result.get("issue", {})
        return {
            "id": issue.get("id", ""),
            "identifier": issue.get("identifier", ""),
            "title": issue.get("title", ""),
            "description": issue.get("description"),
            "state": (issue.get("state") or {}).get("name", ""),
            "url": issue.get("url", ""),
        }

    async def create_issue(
        self,
        title: str,
        description: str | None = None,
        state: str | None = None,
    ) -> dict[str, str]:
        """Create an issue. Returns dict with id, identifier, title, url.

        *state* defaults to ``"Backlog"`` when not given.
        """
        # Step 1: resolve project ID and team ID from project slug
        proj_data = await self._execute(
            PROJECT_TEAMS_QUERY,
            {"projectSlug": self._project_slug},
        )
        projects = proj_data.get("projects", {}).get("nodes", [])
        if not projects:
            raise LinearUnknownPayload(
                f"Project with slug '{self._project_slug}' not found"
            )
        project = projects[0]
        project_id = project["id"]

        teams = project.get("teams", {}).get("nodes", [])
        if not teams:
            raise LinearUnknownPayload(
                f"No teams found for project '{self._project_slug}'"
            )
        team_id = teams[0]["id"]

        # Step 2: resolve target state ID for the team
        target_state = state or "Backlog"
        states_data = await self._execute(
            WORKFLOW_STATES_QUERY,
            {"teamId": team_id},
        )
        states = {
            n["name"]: n["id"]
            for n in states_data.get("workflowStates", {}).get("nodes", [])
        }
        resolved_state_id = states.get(target_state)
        if not resolved_state_id:
            raise LinearUnknownPayload(
                f"{target_state} state not found; available states: {list(states.keys())}"
            )

        # Step 3: create the issue
        variables: dict = {
            "teamId": team_id,
            "title": title,
            "projectId": project_id,
            "stateId": resolved_state_id,
        }
        if description:
            variables["description"] = description

        data = await self._execute(ISSUE_CREATE_MUTATION, variables)
        result = data.get("issueCreate", {})
        if not result.get("success"):
            raise LinearUnknownPayload("issueCreate returned success=false")

        issue = result.get("issue", {})
        return {
            "id": issue.get("id", ""),
            "identifier": issue.get("identifier", ""),
            "title": issue.get("title", ""),
            "url": issue.get("url", ""),
        }

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
        for rel in (node.get("inverseRelations", {}) or {}).get("nodes", []):
            if rel.get("type") == "blocks":
                related = rel.get("issue", {})
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
