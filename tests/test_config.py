import os
import tempfile

import pytest

from pyphony.config import service_config_from_workflow, validate_dispatch_config


class TestServiceConfigFromWorkflow:
    def test_all_defaults(self):
        cfg = service_config_from_workflow({})
        assert cfg.tracker.kind is None
        assert cfg.tracker.endpoint == "https://api.linear.app/graphql"
        assert cfg.tracker.active_states == ["Todo", "In Progress"]
        assert cfg.tracker.terminal_states == ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
        assert cfg.polling.interval_ms == 30000
        assert cfg.hooks.timeout_ms == 60000
        assert cfg.agent.max_concurrent_agents == 10
        assert cfg.agent.max_turns == 200
        assert cfg.agent.max_retry_backoff_ms == 300000
        assert cfg.claude.command == "claude"
        assert cfg.claude.turn_timeout_ms == 3600000
        assert cfg.claude.permission_mode == "bypassPermissions"
        assert cfg.claude.stall_timeout_ms == 300000

    def test_default_workspace_root(self):
        cfg = service_config_from_workflow({})
        expected = str(tempfile.gettempdir()) + "/symphony_workspaces"
        assert cfg.workspace.root == expected

    def test_env_var_resolution(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret-token")
        cfg = service_config_from_workflow({"tracker": {"api_key": "$MY_KEY", "kind": "linear"}})
        assert cfg.tracker.api_key == "secret-token"

    def test_env_var_empty_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "")
        cfg = service_config_from_workflow({"tracker": {"api_key": "$MY_KEY"}})
        assert cfg.tracker.api_key is None

    def test_env_var_not_set(self):
        cfg = service_config_from_workflow({"tracker": {"api_key": "$NONEXISTENT_VAR_12345"}})
        assert cfg.tracker.api_key is None

    def test_linear_api_key_fallback(self, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "fallback-key")
        cfg = service_config_from_workflow({"tracker": {"kind": "linear"}})
        assert cfg.tracker.api_key == "fallback-key"

    def test_tilde_expansion(self):
        cfg = service_config_from_workflow({"workspace": {"root": "~/my_workspaces"}})
        assert "~" not in cfg.workspace.root
        assert cfg.workspace.root.endswith("/my_workspaces")

    def test_comma_separated_states(self):
        cfg = service_config_from_workflow({
            "tracker": {"active_states": "Todo, In Progress, Review"}
        })
        assert cfg.tracker.active_states == ["Todo", "In Progress", "Review"]

    def test_list_states(self):
        cfg = service_config_from_workflow({
            "tracker": {"active_states": ["Todo", "Review"]}
        })
        assert cfg.tracker.active_states == ["Todo", "Review"]

    def test_integer_string_coercion(self):
        cfg = service_config_from_workflow({"polling": {"interval_ms": "5000"}})
        assert cfg.polling.interval_ms == 5000

    def test_invalid_integer_uses_default(self):
        cfg = service_config_from_workflow({"polling": {"interval_ms": "not_a_number"}})
        assert cfg.polling.interval_ms == 30000

    def test_by_state_parsing(self):
        cfg = service_config_from_workflow({
            "agent": {"max_concurrent_agents_by_state": {"Todo": 3, "In Progress": 5}}
        })
        assert cfg.agent.max_concurrent_agents_by_state == {"todo": 3, "in progress": 5}

    def test_by_state_invalid_entries_ignored(self):
        cfg = service_config_from_workflow({
            "agent": {"max_concurrent_agents_by_state": {"Todo": -1, "Review": "bad", "Done": 2}}
        })
        assert cfg.agent.max_concurrent_agents_by_state == {"done": 2}

    def test_hook_timeout_non_positive_uses_default(self):
        cfg = service_config_from_workflow({"hooks": {"timeout_ms": 0}})
        assert cfg.hooks.timeout_ms == 60000

        cfg = service_config_from_workflow({"hooks": {"timeout_ms": -100}})
        assert cfg.hooks.timeout_ms == 60000

    def test_server_port(self):
        cfg = service_config_from_workflow({"server": {"port": 8080}})
        assert cfg.server.port == 8080

    def test_server_port_non_int_ignored(self):
        cfg = service_config_from_workflow({"server": {"port": "abc"}})
        assert cfg.server.port is None


class TestDotenvSupport:
    """Tests for .env file loading via python-dotenv."""

    def test_var_resolved_from_dotenv_file(self, tmp_path, monkeypatch):
        """$VAR resolves from .env file when not set in real environment."""
        monkeypatch.chdir(tmp_path)
        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\n---\n")
        env_file = tmp_path / ".env"
        env_file.write_text("MY_DOTENV_SECRET=from-dotenv\n")

        # Make sure variable is NOT in real env
        os.environ.pop("MY_DOTENV_SECRET", None)

        cfg = service_config_from_workflow(
            {"tracker": {"api_key": "$MY_DOTENV_SECRET", "kind": "linear"}},
            workflow_path=str(workflow),
        )
        assert cfg.tracker.api_key == "from-dotenv"

        # Cleanup
        os.environ.pop("MY_DOTENV_SECRET", None)

    def test_real_env_var_overrides_dotenv(self, tmp_path, monkeypatch):
        """Real environment variable takes priority over .env file value."""
        monkeypatch.chdir(tmp_path)
        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\n---\n")
        env_file = tmp_path / ".env"
        env_file.write_text("PRIORITY_TEST_VAR=from-dotenv\n")

        monkeypatch.setenv("PRIORITY_TEST_VAR", "from-real-env")

        cfg = service_config_from_workflow(
            {"tracker": {"api_key": "$PRIORITY_TEST_VAR", "kind": "linear"}},
            workflow_path=str(workflow),
        )
        assert cfg.tracker.api_key == "from-real-env"

    def test_missing_dotenv_file_does_not_raise(self, tmp_path):
        """Absent .env file should not cause any error."""
        workflow = tmp_path / "WORKFLOW.md"
        workflow.write_text("---\n---\n")
        # No .env file created

        cfg = service_config_from_workflow(
            {"tracker": {"kind": "linear"}},
            workflow_path=str(workflow),
        )
        assert cfg.tracker.kind == "linear"

    def test_missing_workflow_path_does_not_raise(self):
        """workflow_path=None should not cause any error."""
        cfg = service_config_from_workflow({"tracker": {"kind": "linear"}}, workflow_path=None)
        assert cfg.tracker.kind == "linear"


class TestValidateDispatchConfig:
    def _valid_config(self, **overrides):
        from pyphony.models import ServiceConfig, TrackerConfig, ClaudeConfig
        tracker = TrackerConfig(kind="linear", api_key="key", project_slug="slug")
        claude_cfg = ClaudeConfig(command="claude")
        cfg = ServiceConfig(tracker=tracker, claude=claude_cfg)
        return cfg

    def test_valid_config(self):
        errors = validate_dispatch_config(self._valid_config())
        assert errors == []

    def test_missing_kind(self):
        cfg = self._valid_config()
        cfg.tracker.kind = None
        errors = validate_dispatch_config(cfg)
        assert any("tracker.kind" in e for e in errors)

    def test_unsupported_kind(self):
        cfg = self._valid_config()
        cfg.tracker.kind = "jira"
        errors = validate_dispatch_config(cfg)
        assert any("Unsupported" in e for e in errors)

    def test_missing_api_key(self):
        cfg = self._valid_config()
        cfg.tracker.api_key = None
        errors = validate_dispatch_config(cfg)
        assert any("api_key" in e for e in errors)

    def test_missing_project_slug(self):
        cfg = self._valid_config()
        cfg.tracker.project_slug = None
        errors = validate_dispatch_config(cfg)
        assert any("project_slug" in e for e in errors)

    def test_missing_command(self):
        cfg = self._valid_config()
        cfg.claude.command = ""
        errors = validate_dispatch_config(cfg)
        assert any("claude.command" in e for e in errors)
