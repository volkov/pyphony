from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BlockerRef(BaseModel):
    id: str | None = None
    identifier: str | None = None
    state: str | None = None


class Issue(BaseModel):
    id: str
    identifier: str
    title: str
    description: str | None = None
    priority: int | None = None
    state: str
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = Field(default_factory=list)
    blocked_by: list[BlockerRef] = Field(default_factory=list)
    assignee: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkflowDefinition(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    prompt_template: str = ""


class TrackerConfig(BaseModel):
    kind: str | None = None
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str | None = None
    project_slug: str | None = None
    active_states: list[str] = Field(default_factory=lambda: ["Todo", "In Progress"])
    terminal_states: list[str] = Field(
        default_factory=lambda: ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
    )


class PollingConfig(BaseModel):
    interval_ms: int = 30000


class WorkspaceConfig(BaseModel):
    root: str | None = None
    repo: str | None = None  # Path to local git repo; enables worktree mode


class HooksConfig(BaseModel):
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    timeout_ms: int = 60000


class AgentConfig(BaseModel):
    max_concurrent_agents: int = 10
    max_turns: int = 100
    max_runs: int = 1
    max_retry_backoff_ms: int = 300000
    max_concurrent_agents_by_state: dict[str, int] = Field(default_factory=dict)


class CodexConfig(BaseModel):
    command: str = "claude"
    permission_mode: str = "bypassPermissions"
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    )
    disallowed_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    max_turns: int | None = None
    turn_timeout_ms: int = 3600000
    stall_timeout_ms: int = 300000
    system_prompt: str | None = None


class AutomergeConfig(BaseModel):
    parse_transcript_prs: bool = False


class ServerConfig(BaseModel):
    port: int | None = None
    explorer_base_url: str = "http://localhost:3939"


class ServiceConfig(BaseModel):
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    automerge: AutomergeConfig = Field(default_factory=AutomergeConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    supervisor_restart: bool = False


class MergeInfo(BaseModel):
    """Details about a direct merge (rebase onto main, no PR)."""

    commit_sha: str
    diffstat: str  # raw ``git diff --stat`` output


class Workspace(BaseModel):
    path: str
    workspace_key: str
    created_now: bool = False


class RunAttempt(BaseModel):
    issue_id: str
    issue_identifier: str
    attempt: int | None = None
    workspace_path: str = ""
    started_at: datetime | None = None
    status: str = "pending"
    error: str | None = None
    result: str | None = None
    plan_text: str | None = None
    transcript_path: str | None = None


class LiveSession(BaseModel):
    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    agent_pid: str | None = None
    last_agent_event: str | None = None
    last_agent_timestamp: datetime | None = None
    last_agent_message: str = ""
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    agent_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


class RetryEntry(BaseModel):
    issue_id: str
    identifier: str
    attempt: int = 1
    due_at_ms: float = 0.0
    timer_handle: Any = None
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class RunningEntry(BaseModel):
    issue: Issue
    attempt: RunAttempt
    session: LiveSession = Field(default_factory=LiveSession)
    worker_task: Any = None

    model_config = {"arbitrary_types_allowed": True}


class AgentTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0


class OrchestratorRuntimeState(BaseModel):
    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: dict[str, RunningEntry] = Field(default_factory=dict)
    claimed: set[str] = Field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = Field(default_factory=dict)
    completed: set[str] = Field(default_factory=set)
    agent_totals: AgentTotals = Field(default_factory=AgentTotals)
    agent_rate_limits: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}
