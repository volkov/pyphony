from __future__ import annotations

import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route


def create_app(get_state_fn=None) -> Starlette:
    """Create a Starlette app with dashboard and API routes.

    get_state_fn: callable that returns the current OrchestratorRuntimeState
    """

    async def dashboard(request: Request) -> Response:
        # Return simple HTML dashboard
        html = "<html><body><h1>Pyphony Dashboard</h1><p>Running</p></body></html>"
        return HTMLResponse(html)

    async def api_state(request: Request) -> Response:
        if request.method != "GET":
            return Response(status_code=405)
        # Return state as JSON
        data: dict[str, Any] = {
            "running": [],
            "retrying": [],
            "agent_totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 0.0},
        }
        if get_state_fn:
            orch_state = get_state_fn()
            data["running"] = [
                {
                    "issue_id": entry.issue.id,
                    "issue_identifier": entry.issue.identifier,
                    "state": entry.issue.state,
                    "turn_count": entry.session.turn_count,
                }
                for entry in orch_state.running.values()
            ]
            data["retrying"] = [
                {
                    "issue_id": r.issue_id,
                    "identifier": r.identifier,
                    "attempt": r.attempt,
                    "error": r.error,
                }
                for r in orch_state.retry_attempts.values()
            ]
            data["agent_totals"] = orch_state.agent_totals.model_dump()
        return JSONResponse(data)

    async def api_issue(request: Request) -> Response:
        if request.method != "GET":
            return Response(status_code=405)
        identifier = request.path_params["identifier"]
        if get_state_fn:
            orch_state = get_state_fn()
            for entry in orch_state.running.values():
                if entry.issue.identifier == identifier:
                    return JSONResponse({
                        "issue": entry.issue.model_dump(mode="json"),
                        "attempt": entry.attempt.model_dump(mode="json"),
                        "session": entry.session.model_dump(mode="json"),
                    })
        return JSONResponse({"error": "not_found"}, status_code=404)

    async def api_refresh(request: Request) -> Response:
        if request.method != "POST":
            return Response(status_code=405)
        return JSONResponse({"status": "accepted"}, status_code=202)

    routes = [
        Route("/", dashboard),
        Route("/api/v1/state", api_state),
        Route("/api/v1/refresh", api_refresh, methods=["POST"]),
        Route("/api/v1/{identifier}", api_issue),
    ]

    return Starlette(routes=routes)
