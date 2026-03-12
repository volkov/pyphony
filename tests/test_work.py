"""Tests for the ``pyphony work`` CLI subcommand."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyphony.cli import parse_args
from pyphony.work import (
    _extract_last_assistant_message,
    _find_latest_transcript,
)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseArgsWork:
    def test_work_subcommand(self):
        args = parse_args(["work", "SER-11"])
        assert args.command == "work"
        assert args.issue_identifier == "SER-11"
        assert args.workflow_file == "WORKFLOW.md"

    def test_work_with_custom_workflow(self):
        args = parse_args(["work", "SER-42", "custom.md"])
        assert args.command == "work"
        assert args.issue_identifier == "SER-42"
        assert args.workflow_file == "custom.md"

    def test_work_with_log_level(self):
        args = parse_args(["work", "SER-5", "--log-level", "DEBUG"])
        assert args.command == "work"
        assert args.issue_identifier == "SER-5"
        assert args.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


class TestFindLatestTranscript:
    def test_no_projects_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
        result = _find_latest_transcript("/some/workspace", 0.0)
        assert result is None

    def test_finds_newest_transcript(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        # Sanitize workspace path the same way as production code
        workspace_path = "/home/user/workspaces/SER-11"
        sanitized = workspace_path.replace("/", "-").replace("_", "-")
        projects_dir = tmp_path / "projects" / sanitized
        projects_dir.mkdir(parents=True)

        # Create two transcripts
        old_file = projects_dir / "old-session.jsonl"
        old_file.write_text("{}")
        os.utime(old_file, (100.0, 100.0))

        new_file = projects_dir / "new-session.jsonl"
        new_file.write_text("{}")
        os.utime(new_file, (200.0, 200.0))

        result = _find_latest_transcript(workspace_path, 50.0)
        assert result == str(new_file)

    def test_ignores_transcripts_before_start(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        workspace_path = "/workspace/test"
        sanitized = workspace_path.replace("/", "-").replace("_", "-")
        projects_dir = tmp_path / "projects" / sanitized
        projects_dir.mkdir(parents=True)

        old_file = projects_dir / "old.jsonl"
        old_file.write_text("{}")
        os.utime(old_file, (100.0, 100.0))

        result = _find_latest_transcript(workspace_path, 200.0)
        assert result is None


class TestExtractLastAssistantMessage:
    def test_extracts_last_assistant_text(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "First message from assistant with enough text to pass the threshold."}
                    ]
                },
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "This is the final summary of all work done in this session, including commits and PR."}
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = _extract_last_assistant_message(str(transcript))
        assert result is not None
        assert "final summary" in result

    def test_skips_short_messages(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "ok"}
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = _extract_last_assistant_message(str(transcript))
        assert result is None

    def test_missing_file_returns_none(self):
        result = _extract_last_assistant_message("/nonexistent/path.jsonl")
        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("")
        result = _extract_last_assistant_message(str(transcript))
        assert result is None

    def test_concatenates_multiple_text_blocks(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Part one of the response."},
                        {"type": "text", "text": "Part two of the response with more detail about the work."},
                    ]
                },
            }),
        ]
        transcript.write_text("\n".join(lines))

        result = _extract_last_assistant_message(str(transcript))
        assert result is not None
        assert "Part one" in result
        assert "Part two" in result
