from __future__ import annotations

import pytest

from pyphony.errors import TemplateParseError, TemplateRenderError
from pyphony.models import Issue
from pyphony.prompt import render_prompt


def _make_issue(**overrides) -> Issue:
    defaults = {
        "id": "issue-1",
        "identifier": "ENG-123",
        "title": "Fix the bug",
        "state": "Todo",
        "labels": ["backend", "urgent"],
    }
    defaults.update(overrides)
    return Issue(**defaults)


class TestRenderPrompt:
    def test_render_issue_identifier(self) -> None:
        issue = _make_issue()
        result = render_prompt("{{ issue.identifier }}", issue)
        assert result == "ENG-123"

    def test_attempt_none(self) -> None:
        issue = _make_issue()
        result = render_prompt("{{ attempt }}", issue, attempt=None)
        assert result == "None"

    def test_attempt_integer(self) -> None:
        issue = _make_issue()
        result = render_prompt("{{ attempt }}", issue, attempt=3)
        assert result == "3"

    def test_nested_labels(self) -> None:
        issue = _make_issue(labels=["backend", "urgent"])
        result = render_prompt("{{ issue.labels }}", issue)
        assert result == "['backend', 'urgent']"

    def test_unknown_variable_raises_render_error(self) -> None:
        issue = _make_issue()
        with pytest.raises(TemplateRenderError):
            render_prompt("{{ unknown }}", issue)

    def test_unknown_filter_raises_render_error(self) -> None:
        issue = _make_issue()
        with pytest.raises(TemplateRenderError):
            render_prompt("{{ issue.title | badfilter }}", issue)

    def test_empty_body_returns_default(self) -> None:
        issue = _make_issue()
        result = render_prompt("", issue)
        assert result == "You are working on an issue from Linear."

    def test_whitespace_only_body_returns_default(self) -> None:
        issue = _make_issue()
        result = render_prompt("   \n  ", issue)
        assert result == "You are working on an issue from Linear."

    def test_malformed_syntax_raises_parse_error(self) -> None:
        issue = _make_issue()
        with pytest.raises(TemplateParseError):
            render_prompt("{{ unclosed", issue)

    def test_comments_appended_to_prompt(self) -> None:
        issue = _make_issue()
        comments = [
            {"user": "Alice", "created_at": "2025-01-01T00:00:00Z", "body": "First comment"},
            {"user": "Bob", "created_at": "2025-01-02T00:00:00Z", "body": "Second comment"},
        ]
        result = render_prompt("Hello {{ issue.identifier }}", issue, comments=comments)
        assert "Hello ENG-123" in result
        assert "Comments on this issue" in result
        assert "Alice" in result
        assert "First comment" in result
        assert "Bob" in result
        assert "Second comment" in result

    def test_no_comments_section_when_empty(self) -> None:
        issue = _make_issue()
        result = render_prompt("Hello {{ issue.identifier }}", issue, comments=[])
        assert "Comments on this issue" not in result

    def test_no_comments_section_when_none(self) -> None:
        issue = _make_issue()
        result = render_prompt("Hello {{ issue.identifier }}", issue, comments=None)
        assert "Comments on this issue" not in result

    def test_comments_contain_chronological_instruction(self) -> None:
        issue = _make_issue()
        comments = [
            {"user": "Alice", "created_at": "2025-01-01T00:00:00Z", "body": "First"},
        ]
        result = render_prompt("Hello", issue, comments=comments)
        assert "Comments are in chronological order" in result
        assert "Pay special attention to the latest comment" in result

    def test_latest_comment_marker_when_multiple_comments(self) -> None:
        issue = _make_issue()
        comments = [
            {"user": "Alice", "created_at": "2025-01-01T00:00:00Z", "body": "First"},
            {"user": "Bob", "created_at": "2025-01-02T00:00:00Z", "body": "Second"},
            {"user": "Charlie", "created_at": "2025-01-03T00:00:00Z", "body": "Third"},
        ]
        result = render_prompt("Hello", issue, comments=comments)
        # "### Latest comment" should appear before the last comment
        marker_pos = result.index("### Latest comment")
        charlie_pos = result.index("**Charlie**")
        bob_pos = result.index("**Bob**")
        assert marker_pos > bob_pos
        assert marker_pos < charlie_pos

    def test_no_latest_comment_marker_when_single_comment(self) -> None:
        issue = _make_issue()
        comments = [
            {"user": "Alice", "created_at": "2025-01-01T00:00:00Z", "body": "Only comment"},
        ]
        result = render_prompt("Hello", issue, comments=comments)
        assert "### Latest comment" not in result

    def test_plan_required_label_appends_plan_instructions(self) -> None:
        issue = _make_issue(labels=["plan required"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "Work on ENG-123" in result
        assert "plan required" in result
        assert "НЕ" in result  # "Do NOT write code"
        assert "[DONE]" in result

    def test_plan_required_case_insensitive(self) -> None:
        issue = _make_issue(labels=["Plan Required"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "plan required" in result

    def test_plan_required_hyphenated_label(self) -> None:
        """'plan-required' (with hyphen) should also trigger plan mode."""
        issue = _make_issue(labels=["plan-required"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "plan required" in result
        assert "НЕ" in result
        assert "[DONE]" in result

    def test_no_plan_suffix_without_label(self) -> None:
        issue = _make_issue(labels=["backend", "urgent"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "plan required" not in result

    def test_plan_required_with_comments(self) -> None:
        issue = _make_issue(labels=["plan required"])
        comments = [{"user": "Alice", "created_at": "2025-01-01", "body": "context"}]
        result = render_prompt("{{ issue.identifier }}", issue, comments=comments)
        assert "Alice" in result
        assert "plan required" in result

    def test_resolve_conflict_label_appends_conflict_instructions(self) -> None:
        issue = _make_issue(labels=["resolve-conflict"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "Work on ENG-123" in result
        assert "resolve-conflict" in result
        assert "rebase" in result.lower() or "merge" in result.lower()
        assert "[DONE]" in result

    def test_resolve_conflict_case_insensitive(self) -> None:
        issue = _make_issue(labels=["Resolve-Conflict"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "resolve-conflict" in result

    def test_plan_required_takes_priority_over_resolve_conflict(self) -> None:
        """When both labels present, plan required should take priority."""
        issue = _make_issue(labels=["plan required", "resolve-conflict"])
        result = render_prompt("Work on {{ issue.identifier }}", issue)
        assert "plan required" in result
        assert "rebase" not in result.lower()
