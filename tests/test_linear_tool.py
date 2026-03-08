"""Tests for linear_graphql client-side tool."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import Response

from pyphony.linear_tool import _handle_linear_graphql, create_linear_tool

ENDPOINT = "https://api.linear.app/graphql"
API_KEY = "lin_api_test_key"


@pytest.fixture
def mock_client():
    with respx.mock:
        yield httpx.AsyncClient(timeout=5.0)


async def _call(client, args):
    result = await _handle_linear_graphql(args, ENDPOINT, API_KEY, client)
    # Unwrap MCP format to get the payload dict
    text = result["content"][0]["text"]
    return json.loads(text), result.get("is_error", False)


@pytest.mark.asyncio
async def test_valid_query_success(mock_client):
    respx.post(ENDPOINT).mock(
        return_value=Response(200, json={"data": {"issue": {"id": "123"}}})
    )
    payload, is_error = await _call(mock_client, {"query": "{ issue { id } }"})
    assert payload["success"] is True
    assert payload["data"] == {"issue": {"id": "123"}}
    assert is_error is False


@pytest.mark.asyncio
async def test_query_with_variables(mock_client):
    respx.post(ENDPOINT).mock(
        return_value=Response(200, json={"data": {"updateIssue": {"success": True}}})
    )
    payload, is_error = await _call(mock_client, {
        "query": "mutation($id: String!) { updateIssue(id: $id) { success } }",
        "variables": {"id": "abc"},
    })
    assert payload["success"] is True
    assert is_error is False


@pytest.mark.asyncio
async def test_graphql_errors_returns_failure(mock_client):
    body = {
        "data": None,
        "errors": [{"message": "Not found"}],
    }
    respx.post(ENDPOINT).mock(return_value=Response(200, json=body))
    payload, is_error = await _call(mock_client, {"query": "{ bad }"})
    assert payload["success"] is False
    assert payload["body"] == body
    assert is_error is False


@pytest.mark.asyncio
async def test_http_error_returns_failure(mock_client):
    respx.post(ENDPOINT).mock(return_value=Response(401))
    payload, is_error = await _call(mock_client, {"query": "{ issue { id } }"})
    assert payload["success"] is False
    assert "401" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_transport_error_returns_failure(mock_client):
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("connection refused"))
    payload, is_error = await _call(mock_client, {"query": "{ issue { id } }"})
    assert payload["success"] is False
    assert "transport_error" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_empty_query_validation_error(mock_client):
    payload, is_error = await _call(mock_client, {"query": ""})
    assert payload["success"] is False
    assert "non-empty" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_whitespace_query_validation_error(mock_client):
    payload, is_error = await _call(mock_client, {"query": "   "})
    assert payload["success"] is False
    assert "non-empty" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_missing_query_validation_error(mock_client):
    payload, is_error = await _call(mock_client, {})
    assert payload["success"] is False
    assert "non-empty" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_invalid_variables_type(mock_client):
    payload, is_error = await _call(mock_client, {"query": "{ x }", "variables": "bad"})
    assert payload["success"] is False
    assert "variables" in payload["error"]
    assert is_error is True


@pytest.mark.asyncio
async def test_auth_header_sent(mock_client):
    route = respx.post(ENDPOINT).mock(
        return_value=Response(200, json={"data": {}})
    )
    await _call(mock_client, {"query": "{ x }"})
    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"] == API_KEY


@pytest.mark.asyncio
async def test_mcp_result_format(mock_client):
    """Verify raw MCP result has correct structure."""
    respx.post(ENDPOINT).mock(
        return_value=Response(200, json={"data": {"ok": True}})
    )
    result = await _handle_linear_graphql(
        {"query": "{ x }"}, ENDPOINT, API_KEY, mock_client
    )
    assert "content" in result
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    assert isinstance(result["content"][0]["text"], str)
    parsed = json.loads(result["content"][0]["text"])
    assert parsed["success"] is True


@pytest.mark.asyncio
async def test_create_linear_tool_returns_sdk_config(mock_client):
    server = create_linear_tool(ENDPOINT, API_KEY, mock_client)
    assert server["type"] == "sdk"
    assert server["name"] == "linear"
