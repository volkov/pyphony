from datetime import datetime, timezone

from pyphony.models import Issue
from pyphony.normalization import normalize_label, normalize_state, sanitize_workspace_key, sort_issues_for_dispatch


class TestSanitizeWorkspaceKey:
    def test_already_safe(self):
        assert sanitize_workspace_key("ABC-123") == "ABC-123"

    def test_dots_and_underscores(self):
        assert sanitize_workspace_key("my.project_v2") == "my.project_v2"

    def test_spaces_replaced(self):
        assert sanitize_workspace_key("foo bar baz") == "foo_bar_baz"

    def test_slashes_replaced(self):
        assert sanitize_workspace_key("foo/bar") == "foo_bar"

    def test_mixed_special_chars(self):
        assert sanitize_workspace_key("foo/bar baz@qux#1") == "foo_bar_baz_qux_1"

    def test_empty_string(self):
        assert sanitize_workspace_key("") == ""

    def test_all_unsafe(self):
        assert sanitize_workspace_key("!@#$%") == "_____"


class TestNormalizeState:
    def test_simple(self):
        assert normalize_state("Todo") == "todo"

    def test_with_spaces(self):
        assert normalize_state("  In Progress  ") == "in progress"

    def test_mixed_case(self):
        assert normalize_state("IN PROGRESS") == "in progress"

    def test_already_normalized(self):
        assert normalize_state("done") == "done"


class TestNormalizeLabel:
    def test_lowercase(self):
        assert normalize_label("Plan Required") == "plan required"

    def test_hyphen_to_space(self):
        assert normalize_label("plan-required") == "plan required"

    def test_underscore_to_space(self):
        assert normalize_label("plan_required") == "plan required"

    def test_mixed(self):
        assert normalize_label("Plan-Required") == "plan required"

    def test_already_normalized(self):
        assert normalize_label("plan required") == "plan required"

    def test_strips_whitespace(self):
        assert normalize_label("  plan required  ") == "plan required"

    def test_review_required_hyphenated(self):
        assert normalize_label("review-required") == "review required"


class TestSortIssuesForDispatch:
    def _make_issue(
        self,
        identifier: str,
        priority: int | None = None,
        created_at: datetime | None = None,
    ) -> Issue:
        return Issue(
            id=identifier.lower(),
            identifier=identifier,
            title=f"Issue {identifier}",
            state="Todo",
            priority=priority,
            created_at=created_at,
        )

    def test_priority_ascending(self):
        issues = [
            self._make_issue("C", priority=3),
            self._make_issue("A", priority=1),
            self._make_issue("B", priority=2),
        ]
        sorted_issues = sort_issues_for_dispatch(issues)
        assert [i.identifier for i in sorted_issues] == ["A", "B", "C"]

    def test_null_priority_sorts_last(self):
        issues = [
            self._make_issue("B", priority=None),
            self._make_issue("A", priority=1),
        ]
        sorted_issues = sort_issues_for_dispatch(issues)
        assert [i.identifier for i in sorted_issues] == ["A", "B"]

    def test_created_at_oldest_first(self):
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        issues = [
            self._make_issue("B", priority=1, created_at=t2),
            self._make_issue("A", priority=1, created_at=t1),
        ]
        sorted_issues = sort_issues_for_dispatch(issues)
        assert [i.identifier for i in sorted_issues] == ["A", "B"]

    def test_identifier_tiebreak(self):
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        issues = [
            self._make_issue("ZZZ-1", priority=1, created_at=t),
            self._make_issue("AAA-1", priority=1, created_at=t),
        ]
        sorted_issues = sort_issues_for_dispatch(issues)
        assert [i.identifier for i in sorted_issues] == ["AAA-1", "ZZZ-1"]

    def test_empty_list(self):
        assert sort_issues_for_dispatch([]) == []

    def test_combined_sorting(self):
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
        issues = [
            self._make_issue("D", priority=None),
            self._make_issue("C", priority=2, created_at=t2),
            self._make_issue("B", priority=2, created_at=t1),
            self._make_issue("A", priority=1, created_at=t1),
        ]
        sorted_issues = sort_issues_for_dispatch(issues)
        assert [i.identifier for i in sorted_issues] == ["A", "B", "C", "D"]
