from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import (
    AgentConfig,
    AutomergeConfig,
    ClaudeConfig,
    HooksConfig,
    PollingConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    WorkspaceConfig,
)


def _env(value: str | None) -> str | None:
    """Resolve ``$VAR`` references; return *None* when empty or unset."""
    if not value:
        return None
    if isinstance(value, str) and value.startswith("$"):
        return os.environ.get(value[1:]) or None
    return value


def _int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _states(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return default


def _tool_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return value if isinstance(value, list) else None


def service_config_from_workflow(
    config: dict[str, Any],
    workflow_path: Path | str | None = None,
) -> ServiceConfig:
    # Load .env from current working directory (does not override real env vars)
    env_path = Path.cwd() / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)

    t = config.get("tracker") or {}
    p = config.get("polling") or {}
    w = config.get("workspace") or {}
    h = config.get("hooks") or {}
    a = config.get("agent") or {}
    c = config.get("claude") or config.get("codex") or {}
    am = config.get("automerge") or {}
    s = config.get("server") or {}

    # --- tracker api key (env-var resolve + Linear fallback) ---
    api_key = _env(t.get("api_key"))
    if api_key is None and t.get("kind") == "linear":
        api_key = _env("$LINEAR_API_KEY")

    # --- workspace root (expand ~/$ , fallback to tempdir) ---
    root = w.get("root")
    if root:
        if isinstance(root, str) and root.startswith("$"):
            root = _env(root)
        if root and str(root).startswith("~"):
            root = str(Path(root).expanduser())
    if not root:
        root = str(Path(tempfile.gettempdir()) / "symphony_workspaces")

    # --- workspace repo (expand ~/$ for local git repo path) ---
    repo = w.get("repo")
    if repo:
        if isinstance(repo, str) and repo.startswith("$"):
            repo = _env(repo)
        if repo and str(repo).startswith("~"):
            repo = str(Path(repo).expanduser())

    # --- hook timeout (must be positive) ---
    hook_timeout = _int(h.get("timeout_ms"), 60000)
    if hook_timeout <= 0:
        hook_timeout = 60000

    # --- by-state concurrency limits (lowercase keys, positive ints only) ---
    by_state: dict[str, int] = {}
    if isinstance(a.get("max_concurrent_agents_by_state"), dict):
        for k, v in a["max_concurrent_agents_by_state"].items():
            try:
                iv = int(v)
                if iv > 0:
                    by_state[k.strip().lower()] = iv
            except (ValueError, TypeError):
                pass

    # --- claude optional overrides ---
    claude_extra: dict[str, Any] = {}
    if (at := _tool_list(c.get("allowed_tools"))) is not None:
        claude_extra["allowed_tools"] = at
    if (dt := _tool_list(c.get("disallowed_tools"))) is not None:
        claude_extra["disallowed_tools"] = dt
    if c.get("model"):
        claude_extra["model"] = c["model"]
    if c.get("max_turns") is not None:
        claude_extra["max_turns"] = _int(c["max_turns"], None)
    if c.get("system_prompt"):
        claude_extra["system_prompt"] = c["system_prompt"]

    return ServiceConfig(
        tracker=TrackerConfig(
            kind=t.get("kind"),
            endpoint=t.get("endpoint", "https://api.linear.app/graphql"),
            api_key=api_key,
            project_slug=t.get("project_slug"),
            active_states=_states(t.get("active_states"), ["Todo", "In Progress"]),
            terminal_states=_states(
                t.get("terminal_states"),
                ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"],
            ),
        ),
        polling=PollingConfig(interval_ms=_int(p.get("interval_ms"), 30000)),
        workspace=WorkspaceConfig(root=root, repo=repo),
        hooks=HooksConfig(
            after_create=h.get("after_create"),
            before_run=h.get("before_run"),
            after_run=h.get("after_run"),
            before_remove=h.get("before_remove"),
            timeout_ms=hook_timeout,
        ),
        agent=AgentConfig(
            max_concurrent_agents=_int(a.get("max_concurrent_agents"), 10),
            max_turns=_int(a.get("max_turns"), 200),
            max_runs=_int(a.get("max_runs"), 1),
            max_retry_backoff_ms=_int(a.get("max_retry_backoff_ms"), 300000),
            max_concurrent_agents_by_state=by_state,
        ),
        claude=ClaudeConfig(
            command=c.get("command", "claude") or "claude",
            permission_mode=c.get("permission_mode", "bypassPermissions"),
            turn_timeout_ms=_int(c.get("turn_timeout_ms"), 3600000),
            stall_timeout_ms=_int(c.get("stall_timeout_ms"), 300000),
            **claude_extra,
        ),
        automerge=AutomergeConfig(
            parse_transcript_prs=bool(am.get("parse_transcript_prs", False)),
        ),
        server=ServerConfig(
            port=s.get("port") if isinstance(s.get("port"), int) else None,
        ),
        supervisor_restart=bool(config.get("supervisor_restart", False)),
    )


def validate_dispatch_config(config: ServiceConfig) -> list[str]:
    errors: list[str] = []
    if not config.tracker.kind:
        errors.append("tracker.kind is required")
    elif config.tracker.kind != "linear":
        errors.append(f"Unsupported tracker.kind: {config.tracker.kind}")
    if not config.tracker.api_key:
        errors.append("tracker.api_key is required (or set LINEAR_API_KEY)")
    if config.tracker.kind == "linear" and not config.tracker.project_slug:
        errors.append("tracker.project_slug is required for Linear")
    if not config.claude.command:
        errors.append("claude.command is required")
    return errors
