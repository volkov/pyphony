import argparse
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from pyphony.service import _poll_tick, _run_service
from pyphony.models import ServiceConfig, TrackerConfig, WorkspaceConfig, HooksConfig
from pyphony.workspace import WorkspaceManager


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


def _issue_node(id="id-1", identifier="PROJ-1", title="Test", state_name="Todo"):
    return {
        "id": id,
        "identifier": identifier,
        "title": title,
        "description": None,
        "priority": 1,
        "state": {"name": state_name},
        "branchName": None,
        "url": None,
        "labels": {"nodes": []},
        "relations": {"nodes": []},
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-01T00:00:00Z",
    }


ENDPOINT = "https://api.linear.app/graphql"


class TestPollTick:
    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_tick_logs_dispatch(self, tmp_path):
        respx.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json=_graphql_response([_issue_node()]),
            )
        )

        config = ServiceConfig(
            tracker=TrackerConfig(
                kind="linear",
                api_key="test-key",
                project_slug="test",
                active_states=["Todo"],
            ),
            workspace=WorkspaceConfig(root=str(tmp_path)),
        )
        from pyphony.tracker import LinearClient

        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        config_raw = {
            "tracker": {
                "kind": "linear",
                "api_key": "test-key",
                "project_slug": "test",
                "active_states": ["Todo"],
            },
            "workspace": {"root": str(tmp_path)},
            "codex": {"command": "claude"},
        }

        await _poll_tick(tracker, ws_mgr, config_raw)
        await tracker.close()

        assert (tmp_path / "PROJ-1").exists()

    @pytest.mark.asyncio
    async def test_poll_tick_validation_failure_skips(self, tmp_path):
        config = ServiceConfig(
            workspace=WorkspaceConfig(root=str(tmp_path)),
        )
        from pyphony.tracker import LinearClient

        tracker = LinearClient(config)
        ws_mgr = WorkspaceManager(config)

        # Empty config - no tracker.kind
        await _poll_tick(tracker, ws_mgr, {})
        await tracker.close()


class TestServiceStartup:
    def test_missing_workflow_file_exits(self):
        args = argparse.Namespace(
            workflow_file="/nonexistent/WORKFLOW.md",
            port=None,
            log_level="ERROR",
        )
        with pytest.raises(Exception):
            import asyncio
            asyncio.run(_run_service(args))
