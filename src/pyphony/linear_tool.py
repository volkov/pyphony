"""Linear GraphQL client-side tool for agents."""

from __future__ import annotations

import json
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "GraphQL query or mutation string",
        },
        "variables": {
            "type": "object",
            "description": "Optional GraphQL variables",
        },
    },
    "required": ["query"],
}


def _mcp_result(data: dict, is_error: bool = False) -> dict:
    """Wrap a dict into MCP tool result format."""
    return {
        "content": [{"type": "text", "text": json.dumps(data)}],
        "is_error": is_error,
    }


async def _handle_linear_graphql(
    args: dict,
    endpoint: str,
    api_key: str,
    http_client: httpx.AsyncClient,
) -> dict:
    """Core handler logic for the linear_graphql tool."""
    query_str = args.get("query", "")
    variables = args.get("variables", {})

    if not query_str or not query_str.strip():
        return _mcp_result(
            {"success": False, "error": "query must be a non-empty string"},
            is_error=True,
        )

    if not isinstance(variables, dict):
        return _mcp_result(
            {"success": False, "error": "variables must be an object"},
            is_error=True,
        )

    try:
        resp = await http_client.post(
            endpoint,
            json={"query": query_str, "variables": variables},
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        return _mcp_result(
            {"success": False, "error": f"HTTP {exc.response.status_code}"},
            is_error=True,
        )
    except (httpx.HTTPError, Exception) as exc:
        return _mcp_result(
            {"success": False, "error": f"transport_error: {exc}"},
            is_error=True,
        )

    if "errors" in body:
        return _mcp_result({"success": False, "body": body})

    return _mcp_result({"success": True, "data": body.get("data")})


def create_linear_tool(
    endpoint: str,
    api_key: str,
    http_client: httpx.AsyncClient,
) -> dict:
    """Create an MCP server config with a linear_graphql tool."""

    @tool(
        name="linear_graphql",
        description=(
            "Execute a GraphQL query or mutation against the Linear API. "
            "Use this to update issue states, add comments, etc."
        ),
        input_schema=TOOL_INPUT_SCHEMA,
    )
    async def handler(args: dict) -> dict:
        return await _handle_linear_graphql(args, endpoint, api_key, http_client)

    return create_sdk_mcp_server(name="linear", tools=[handler])
