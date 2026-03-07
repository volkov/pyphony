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
