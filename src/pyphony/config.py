from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import ConfigValidationError
from .models import (
    AgentConfig,
    CodexConfig,
    HooksConfig,
    PollingConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)


def _resolve_env_var(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("$"):
        var_name = value[1:]
        resolved = os.environ.get(var_name, "")
        return resolved if resolved else None
    return value


def _expand_path(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value)
    if value.startswith("$"):
        resolved = _resolve_env_var(value)
        if resolved is None:
            return None
        value = resolved
    if value.startswith("~"):
        value = str(Path(value).expanduser())
    return value


def _parse_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_states(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return default


def _parse_by_state(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for k, v in value.items():
        try:
            iv = int(v)
            if iv > 0:
                result[k.strip().lower()] = iv
        except (ValueError, TypeError):
            continue
    return result


def service_config_from_workflow(config: dict[str, Any]) -> ServiceConfig:
    tracker_raw = config.get("tracker", {}) or {}
    polling_raw = config.get("polling", {}) or {}
    workspace_raw = config.get("workspace", {}) or {}
    hooks_raw = config.get("hooks", {}) or {}
    agent_raw = config.get("agent", {}) or {}
    codex_raw = config.get("codex", {}) or {}
    server_raw = config.get("server", {}) or {}

    api_key = _resolve_env_var(tracker_raw.get("api_key"))
    if api_key is None and tracker_raw.get("kind") == "linear":
        api_key = _resolve_env_var("$LINEAR_API_KEY")

    workspace_root = _expand_path(workspace_raw.get("root"))
    if workspace_root is None:
        workspace_root = str(Path(tempfile.gettempdir()) / "symphony_workspaces")

    hook_timeout = _parse_int(hooks_raw.get("timeout_ms"), 60000)
    if hook_timeout <= 0:
        hook_timeout = 60000

    tracker = TrackerConfig(
        kind=tracker_raw.get("kind"),
        endpoint=tracker_raw.get("endpoint", "https://api.linear.app/graphql"),
        api_key=api_key,
        project_slug=tracker_raw.get("project_slug"),
        active_states=_parse_states(tracker_raw.get("active_states"), ["Todo", "In Progress"]),
        terminal_states=_parse_states(
            tracker_raw.get("terminal_states"),
            ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"],
        ),
    )

    polling = PollingConfig(
        interval_ms=_parse_int(polling_raw.get("interval_ms"), 30000),
    )

    workspace = WorkspaceConfig(root=workspace_root)

    hooks = HooksConfig(
        after_create=hooks_raw.get("after_create"),
        before_run=hooks_raw.get("before_run"),
        after_run=hooks_raw.get("after_run"),
        before_remove=hooks_raw.get("before_remove"),
        timeout_ms=hook_timeout,
    )

    agent = AgentConfig(
        max_concurrent_agents=_parse_int(agent_raw.get("max_concurrent_agents"), 10),
        max_turns=_parse_int(agent_raw.get("max_turns"), 20),
        max_retry_backoff_ms=_parse_int(agent_raw.get("max_retry_backoff_ms"), 300000),
        max_concurrent_agents_by_state=_parse_by_state(
            agent_raw.get("max_concurrent_agents_by_state")
        ),
    )

    codex = CodexConfig(
        command=codex_raw.get("command", "claude") or "claude",
        approval_policy=codex_raw.get("approval_policy"),
        thread_sandbox=codex_raw.get("thread_sandbox"),
        turn_sandbox_policy=codex_raw.get("turn_sandbox_policy"),
        turn_timeout_ms=_parse_int(codex_raw.get("turn_timeout_ms"), 3600000),
        read_timeout_ms=_parse_int(codex_raw.get("read_timeout_ms"), 5000),
        stall_timeout_ms=_parse_int(codex_raw.get("stall_timeout_ms"), 300000),
    )

    server = ServerConfig(
        port=server_raw.get("port") if isinstance(server_raw.get("port"), int) else None,
    )

    return ServiceConfig(
        tracker=tracker,
        polling=polling,
        workspace=workspace,
        hooks=hooks,
        agent=agent,
        codex=codex,
        server=server,
    )


def validate_dispatch_config(config: ServiceConfig) -> list[str]:
    errors = []
    if not config.tracker.kind:
        errors.append("tracker.kind is required")
    elif config.tracker.kind != "linear":
        errors.append(f"Unsupported tracker.kind: {config.tracker.kind}")

    if not config.tracker.api_key:
        errors.append("tracker.api_key is required (or set LINEAR_API_KEY)")

    if config.tracker.kind == "linear" and not config.tracker.project_slug:
        errors.append("tracker.project_slug is required for Linear")

    if not config.codex.command:
        errors.append("codex.command is required")

    return errors
