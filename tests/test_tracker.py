"""Tests for the Linear tracker client."""

from __future__ import annotations

import httpx
import pytest
import respx

from pyphony.errors import (
    LinearApiRequestError,
    LinearApiStatusError,
    LinearGraphQLError,
    LinearUnknownPayload,
)
from pyphony.models import ServiceConfig, TrackerConfig
from pyphony.tracker import LinearClient


ENDPOINT = "https://api.linear.app/graphql"


def _make_config(**overrides) -> ServiceConfig:
    defaults = dict(
        kind="linear",
        endpoint=ENDPOINT,
        api_key="test-api-key",
        project_slug="test-project",
        active_states=["Todo", "In Progress"],
        terminal_states=["Done", "Cancelled"],
    )
    defaults.update(overrides)
    return ServiceConfig(tracker=TrackerConfig(**defaults))


def _issue_node(
    *,
    id: str = "id-1",
    identifier: str = "PROJ-1",
    title: str = "Test issue",
    description: str | None = "A description",
    priority: int | None = 2,
    state_name: str = "Todo",
    branch_name: str | None = "feature/proj-1",
    url: str | None = "https://linear.app/proj-1",
    labels: list[str] | None = None,
    relations: list[dict] | None = None,
    created_at: str = "2025-01-15T10:00:00.000Z",
    updated_at: str = "2025-01-16T12:00:00.000Z",
) -> dict:
    label_nodes = [{"name": l} for l in (labels or [])]
    return {
        "id": id,
        "identifier": identifier,
        "title": title,
        "description": description,
        "priority": priority,
        "state": {"name": state_name},
        "branchName": branch_name,
        "url": url,
        "labels": {"nodes": label_nodes},
        "inverseRelations": {"nodes": relations or []},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def _graphql_response(nodes, has_next_page=False, end_cursor=None):
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {
                    "hasNextPage": has_next_page,
                    "endCursor": end_cursor,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchCandidateIssuesSinglePage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_single_page_returns_normalized_issues(self):
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(
                        labels=["Bug", "URGENT"],
                        relations=[
                            {
                                "type": "blocks",
                                "issue": {
                                    "id": "blocker-id",
                                    "identifier": "PROJ-2",
                                    "state": {"name": "In Progress"},
                                },
                            }
                        ],
                    )
                ]),
            )
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert len(issues) == 1
        issue = issues[0]
        assert issue.id == "id-1"
        assert issue.identifier == "PROJ-1"
        assert issue.title == "Test issue"
        assert issue.description == "A description"
        assert issue.priority == 2
        assert issue.state == "Todo"
        assert issue.branch_name == "feature/proj-1"
        assert issue.url == "https://linear.app/proj-1"
        # Labels should be lowercased
        assert issue.labels == ["bug", "urgent"]
        # Blockers from inverse relations
        assert len(issue.blocked_by) == 1
        assert issue.blocked_by[0].id == "blocker-id"
        assert issue.blocked_by[0].identifier == "PROJ-2"
        assert issue.blocked_by[0].state == "In Progress"
        # Timestamps parsed
        assert issue.created_at is not None
        assert issue.updated_at is not None
        assert route.called


class TestFetchCandidateIssuesMultiPage:
    @respx.mock
    @pytest.mark.asyncio
    async def test_multi_page_returns_all_issues(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=_graphql_response(
                        [_issue_node(id="id-1", identifier="PROJ-1")],
                        has_next_page=True,
                        end_cursor="cursor-1",
                    ),
                ),
                httpx.Response(
                    200,
                    json=_graphql_response(
                        [_issue_node(id="id-2", identifier="PROJ-2")],
                        has_next_page=False,
                    ),
                ),
            ]
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert len(issues) == 2
        assert issues[0].id == "id-1"
        assert issues[1].id == "id-2"


class TestNormalization:
    @respx.mock
    @pytest.mark.asyncio
    async def test_labels_lowercased(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(labels=["Feature", "HIGH-PRIORITY"]),
                ]),
            )
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert issues[0].labels == ["feature", "high-priority"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_blockers_from_inverse_relations(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(
                        relations=[
                            {
                                "type": "blocks",
                                "issue": {
                                    "id": "b1",
                                    "identifier": "PROJ-9",
                                    "state": {"name": "Done"},
                                },
                            },
                            {
                                "type": "related",
                                "issue": {
                                    "id": "r1",
                                    "identifier": "PROJ-10",
                                    "state": {"name": "Todo"},
                                },
                            },
                        ],
                    )
                ]),
            )
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert len(issues[0].blocked_by) == 1
        assert issues[0].blocked_by[0].id == "b1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_priority_int_and_none(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(id="id-1", priority=3),
                    _issue_node(id="id-2", priority=None),
                ]),
            )
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert issues[0].priority == 3
        assert issues[1].priority is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_timestamps_parsed(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([
                    _issue_node(
                        created_at="2025-06-01T08:30:00.000Z",
                        updated_at="2025-06-02T14:00:00.000Z",
                    )
                ]),
            )
        )

        client = LinearClient(_make_config())
        try:
            issues = await client.fetch_candidate_issues()
        finally:
            await client.close()

        assert issues[0].created_at is not None
        assert issues[0].created_at.year == 2025
        assert issues[0].created_at.month == 6
        assert issues[0].updated_at is not None


class TestEmptyInputs:
    @pytest.mark.asyncio
    async def test_empty_states_returns_empty_list(self):
        client = LinearClient(_make_config())
        try:
            result = await client.fetch_issues_by_states([])
        finally:
            await client.close()

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_ids_returns_empty_dict(self):
        client = LinearClient(_make_config())
        try:
            result = await client.fetch_issue_states_by_ids([])
        finally:
            await client.close()

        assert result == {}


class TestFetchIssueStatesByIds:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_id_to_state_mapping(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [
                                {"id": "id-1", "state": {"name": "Done"}, "labels": {"nodes": []}},
                                {"id": "id-2", "state": {"name": "Todo"}, "labels": {"nodes": [{"name": "bug"}]}},
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            result = await client.fetch_issue_states_by_ids(["id-1", "id-2"])
        finally:
            await client.close()

        assert result == {
            "id-1": {"state": "Done", "labels": []},
            "id-2": {"state": "Todo", "labels": ["bug"]},
        }


def _issue_team_response(team_id="team-1"):
    return httpx.Response(
        200,
        json={
            "data": {
                "issue": {
                    "team": {"id": team_id},
                }
            }
        },
    )


def _workflow_states_response(states):
    return httpx.Response(
        200,
        json={
            "data": {
                "workflowStates": {
                    "nodes": states,
                }
            }
        },
    )


class TestFetchWorkflowStates:
    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_name_to_id_mapping(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                _issue_team_response(),
                _workflow_states_response([
                    {"id": "state-1", "name": "Todo"},
                    {"id": "state-2", "name": "In Progress"},
                    {"id": "state-3", "name": "Done"},
                ]),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.fetch_workflow_states(issue_id="issue-1")
        finally:
            await client.close()

        assert result == {
            "Todo": "state-1",
            "In Progress": "state-2",
            "Done": "state-3",
        }

    @respx.mock
    @pytest.mark.asyncio
    async def test_caches_after_first_call(self):
        route = respx.post(ENDPOINT).mock(
            side_effect=[
                _issue_team_response(),
                _workflow_states_response([{"id": "s1", "name": "Todo"}]),
            ]
        )

        client = LinearClient(_make_config())
        try:
            await client.fetch_workflow_states(issue_id="issue-1")
            await client.fetch_workflow_states(issue_id="issue-1")
        finally:
            await client.close()

        # 2 calls for first fetch (issue team + workflow states), 0 for second (cached)
        assert route.call_count == 2


class TestTransitionIssue:
    @respx.mock
    @pytest.mark.asyncio
    async def test_successful_transition(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                # First call: fetch issue team
                _issue_team_response(),
                # Second call: fetch workflow states
                _workflow_states_response([{"id": "state-done", "name": "Done"}]),
                # Third call: issue update
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issueUpdate": {
                                "success": True,
                                "issue": {"id": "issue-1", "state": {"name": "Done"}},
                            }
                        }
                    },
                ),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.transition_issue("issue-1", "Done")
        finally:
            await client.close()

        assert result is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_state_returns_false(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                _issue_team_response(),
                _workflow_states_response([{"id": "s1", "name": "Todo"}]),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.transition_issue("issue-1", "Nonexistent")
        finally:
            await client.close()

        assert result is False


class TestCommentOnIssue:
    @respx.mock
    @pytest.mark.asyncio
    async def test_successful_comment(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "commentCreate": {
                            "success": True,
                            "comment": {"id": "comment-1", "body": "Agent summary"},
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            result = await client.comment_on_issue("issue-1", "Agent summary")
        finally:
            await client.close()

        assert result is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_failed_comment_returns_false(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "commentCreate": {
                            "success": False,
                            "comment": None,
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            result = await client.comment_on_issue("issue-1", "Agent summary")
        finally:
            await client.close()

        assert result is False


class TestErrorHandling:
    @respx.mock
    @pytest.mark.asyncio
    async def test_http_error_raises_status_error(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        client = LinearClient(_make_config())
        with pytest.raises(LinearApiStatusError, match="500"):
            try:
                await client.fetch_candidate_issues()
            finally:
                await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_graphql_errors_raises_graphql_error(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "errors": [{"message": "Field 'foo' not found"}],
                },
            )
        )

        client = LinearClient(_make_config())
        with pytest.raises(LinearGraphQLError, match="Field 'foo' not found"):
            try:
                await client.fetch_candidate_issues()
            finally:
                await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_transport_error_raises_request_error(self):
        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("Connection refused"))

        client = LinearClient(_make_config())
        with pytest.raises(LinearApiRequestError, match="Connection refused"):
            try:
                await client.fetch_candidate_issues()
            finally:
                await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_response_raises_unknown_payload(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"something": "unexpected"},
            )
        )

        client = LinearClient(_make_config())
        with pytest.raises(LinearUnknownPayload, match="missing 'data' key"):
            try:
                await client.fetch_candidate_issues()
            finally:
                await client.close()


class TestFetchIssueComments:
    @respx.mock
    @pytest.mark.asyncio
    async def test_comments_sorted_by_created_at_ascending(self):
        """Comments returned by API in reverse order should be sorted oldest-first."""
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issue": {
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "c3",
                                        "body": "Newest",
                                        "createdAt": "2025-03-03T00:00:00.000Z",
                                        "user": {"name": "Charlie"},
                                    },
                                    {
                                        "id": "c1",
                                        "body": "Oldest",
                                        "createdAt": "2025-03-01T00:00:00.000Z",
                                        "user": {"name": "Alice"},
                                    },
                                    {
                                        "id": "c2",
                                        "body": "Middle",
                                        "createdAt": "2025-03-02T00:00:00.000Z",
                                        "user": {"name": "Bob"},
                                    },
                                ]
                            }
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            comments = await client.fetch_issue_comments("issue-1")
        finally:
            await client.close()

        assert len(comments) == 3
        assert comments[0]["user"] == "Alice"
        assert comments[0]["body"] == "Oldest"
        assert comments[1]["user"] == "Bob"
        assert comments[1]["body"] == "Middle"
        assert comments[2]["user"] == "Charlie"
        assert comments[2]["body"] == "Newest"

    @respx.mock
    @pytest.mark.asyncio
    async def test_comments_empty_when_issue_not_found(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"issue": None}},
            )
        )

        client = LinearClient(_make_config())
        try:
            comments = await client.fetch_issue_comments("nonexistent")
        finally:
            await client.close()

        assert comments == []


class TestGetIssue:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_issue_returns_fields(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "issue-id-1",
                                    "identifier": "SER-27",
                                    "title": "Test issue",
                                    "description": "A description",
                                    "state": {"name": "Todo"},
                                    "url": "https://linear.app/issue/SER-27",
                                }
                            ]
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            result = await client.get_issue("SER-27")
        finally:
            await client.close()

        assert result["id"] == "issue-id-1"
        assert result["identifier"] == "SER-27"
        assert result["title"] == "Test issue"
        assert result["description"] == "A description"
        assert result["state"] == "Todo"
        assert result["url"] == "https://linear.app/issue/SER-27"

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_issue_not_found_raises(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"issues": {"nodes": []}}},
            )
        )

        client = LinearClient(_make_config())
        with pytest.raises(LinearUnknownPayload, match="not found"):
            try:
                await client.get_issue("SER-999")
            finally:
                await client.close()

    @pytest.mark.asyncio
    async def test_get_issue_invalid_identifier_raises(self):
        client = LinearClient(_make_config())
        with pytest.raises(LinearUnknownPayload, match="Invalid identifier"):
            try:
                await client.get_issue("INVALID")
            finally:
                await client.close()


class TestReplaceIssueLabels:
    @respx.mock
    @pytest.mark.asyncio
    async def test_removes_and_adds_labels(self):
        """Removes 'plan required' and adds 'with plan' label."""
        respx.post(ENDPOINT).mock(
            side_effect=[
                # 1. Fetch issue label IDs
                httpx.Response(200, json={
                    "data": {
                        "issue": {
                            "labels": {
                                "nodes": [
                                    {"id": "label-plan-req", "name": "Plan Required"},
                                    {"id": "label-backend", "name": "Backend"},
                                ]
                            }
                        }
                    }
                }),
                # 2. Fetch issue team
                _issue_team_response(),
                # 3. Fetch team labels
                httpx.Response(200, json={
                    "data": {
                        "issueLabels": {
                            "nodes": [
                                {"id": "label-plan-req", "name": "Plan Required"},
                                {"id": "label-backend", "name": "Backend"},
                                {"id": "label-with-plan", "name": "with plan"},
                            ]
                        }
                    }
                }),
                # 4. Update issue labels
                httpx.Response(200, json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {
                                "id": "issue-1",
                                "identifier": "SER-1",
                                "title": "Test",
                                "description": None,
                                "state": {"name": "Todo"},
                                "url": "https://linear.app/issue/SER-1",
                            },
                        }
                    }
                }),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.replace_issue_labels(
                "issue-1",
                remove_labels=["plan required"],
                add_labels=["with plan"],
            )
        finally:
            await client.close()

        assert result is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_creates_missing_label(self):
        """Creates a label that doesn't exist yet."""
        respx.post(ENDPOINT).mock(
            side_effect=[
                # 1. Fetch issue label IDs
                httpx.Response(200, json={
                    "data": {
                        "issue": {
                            "labels": {
                                "nodes": [
                                    {"id": "label-plan-req", "name": "Plan Required"},
                                ]
                            }
                        }
                    }
                }),
                # 2. Fetch issue team
                _issue_team_response(),
                # 3. Fetch team labels (no "with plan" label)
                httpx.Response(200, json={
                    "data": {
                        "issueLabels": {
                            "nodes": [
                                {"id": "label-plan-req", "name": "Plan Required"},
                            ]
                        }
                    }
                }),
                # 4. Create "with plan" label
                httpx.Response(200, json={
                    "data": {
                        "issueLabelCreate": {
                            "success": True,
                            "issueLabel": {"id": "label-new", "name": "with plan"},
                        }
                    }
                }),
                # 5. Update issue labels
                httpx.Response(200, json={
                    "data": {
                        "issueUpdate": {
                            "success": True,
                            "issue": {
                                "id": "issue-1",
                                "identifier": "SER-1",
                                "title": "Test",
                                "description": None,
                                "state": {"name": "Todo"},
                                "url": "https://linear.app/issue/SER-1",
                            },
                        }
                    }
                }),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.replace_issue_labels(
                "issue-1",
                remove_labels=["plan required"],
                add_labels=["with plan"],
            )
        finally:
            await client.close()

        assert result is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_issue_not_found_returns_false(self):
        """Returns False when issue is not found."""
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={
                "data": {"issue": None}
            })
        )

        client = LinearClient(_make_config())
        try:
            result = await client.replace_issue_labels(
                "nonexistent", remove_labels=["x"], add_labels=["y"]
            )
        finally:
            await client.close()

        assert result is False


class TestUpdateIssue:
    @respx.mock
    @pytest.mark.asyncio
    async def test_update_issue_title(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                # First call: get_issue (to resolve internal ID)
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "issue-id-1",
                                        "identifier": "SER-27",
                                        "title": "Old title",
                                        "description": None,
                                        "state": {"name": "Todo"},
                                        "url": "https://linear.app/issue/SER-27",
                                    }
                                ]
                            }
                        }
                    },
                ),
                # Second call: issueUpdate mutation
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issueUpdate": {
                                "success": True,
                                "issue": {
                                    "id": "issue-id-1",
                                    "identifier": "SER-27",
                                    "title": "New title",
                                    "description": None,
                                    "state": {"name": "Todo"},
                                    "url": "https://linear.app/issue/SER-27",
                                },
                            }
                        }
                    },
                ),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.update_issue("SER-27", title="New title")
        finally:
            await client.close()

        assert result["title"] == "New title"
        assert result["identifier"] == "SER-27"

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_issue_state(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                # get_issue
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "issue-id-1",
                                        "identifier": "SER-27",
                                        "title": "Test",
                                        "description": None,
                                        "state": {"name": "Todo"},
                                        "url": "https://linear.app/issue/SER-27",
                                    }
                                ]
                            }
                        }
                    },
                ),
                # fetch_workflow_states: issue team
                _issue_team_response(),
                # fetch_workflow_states: states
                _workflow_states_response([
                    {"id": "state-todo", "name": "Todo"},
                    {"id": "state-done", "name": "Done"},
                ]),
                # issueUpdate mutation
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issueUpdate": {
                                "success": True,
                                "issue": {
                                    "id": "issue-id-1",
                                    "identifier": "SER-27",
                                    "title": "Test",
                                    "description": None,
                                    "state": {"name": "Done"},
                                    "url": "https://linear.app/issue/SER-27",
                                },
                            }
                        }
                    },
                ),
            ]
        )

        client = LinearClient(_make_config())
        try:
            result = await client.update_issue("SER-27", state="Done")
        finally:
            await client.close()

        assert result["state"] == "Done"

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_issue_no_fields_returns_current(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": [
                                {
                                    "id": "issue-id-1",
                                    "identifier": "SER-27",
                                    "title": "Test",
                                    "description": None,
                                    "state": {"name": "Todo"},
                                    "url": "https://linear.app/issue/SER-27",
                                }
                            ]
                        }
                    }
                },
            )
        )

        client = LinearClient(_make_config())
        try:
            result = await client.update_issue("SER-27")
        finally:
            await client.close()

        assert result["identifier"] == "SER-27"
        assert result["title"] == "Test"

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_issue_invalid_state_raises(self):
        respx.post(ENDPOINT).mock(
            side_effect=[
                # get_issue
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "issue-id-1",
                                        "identifier": "SER-27",
                                        "title": "Test",
                                        "description": None,
                                        "state": {"name": "Todo"},
                                        "url": "https://linear.app/issue/SER-27",
                                    }
                                ]
                            }
                        }
                    },
                ),
                # fetch_workflow_states: issue team
                _issue_team_response(),
                # fetch_workflow_states: states
                _workflow_states_response([
                    {"id": "state-todo", "name": "Todo"},
                ]),
            ]
        )

        client = LinearClient(_make_config())
        with pytest.raises(LinearUnknownPayload, match="not found"):
            try:
                await client.update_issue("SER-27", state="Nonexistent")
            finally:
                await client.close()


class TestAttachPrToIssue:
    @respx.mock
    @pytest.mark.asyncio
    async def test_attach_pr_success(self):
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "attachmentCreate": {
                        "success": True,
                        "attachment": {
                            "id": "att-1",
                            "url": "https://github.com/org/repo/pull/42",
                            "title": "org/repo#42",
                        },
                    }
                }
            })
        )
        client = LinearClient(_make_config())
        try:
            result = await client.attach_pr_to_issue(
                "issue-1", "https://github.com/org/repo/pull/42"
            )
            assert result is True
            assert route.called
            body = route.calls[0].request.content
            import json
            payload = json.loads(body)
            assert payload["variables"]["issueId"] == "issue-1"
            assert payload["variables"]["url"] == "https://github.com/org/repo/pull/42"
            assert payload["variables"]["title"] == "org/repo#42"
        finally:
            await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_attach_pr_custom_title(self):
        route = respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "attachmentCreate": {
                        "success": True,
                        "attachment": {"id": "att-2", "url": "u", "title": "My PR"},
                    }
                }
            })
        )
        client = LinearClient(_make_config())
        try:
            result = await client.attach_pr_to_issue(
                "issue-1", "https://github.com/org/repo/pull/7", title="My PR"
            )
            assert result is True
            import json
            payload = json.loads(route.calls[0].request.content)
            assert payload["variables"]["title"] == "My PR"
        finally:
            await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_attach_pr_failure(self):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "attachmentCreate": {
                        "success": False,
                        "attachment": None,
                    }
                }
            })
        )
        client = LinearClient(_make_config())
        try:
            result = await client.attach_pr_to_issue(
                "issue-1", "https://github.com/org/repo/pull/1"
            )
            assert result is False
        finally:
            await client.close()
