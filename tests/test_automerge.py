"""Tests for the automerge module."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch, MagicMock, call

import pytest

from pyphony.automerge import (
    _parse_pr_ref,
    extract_pr_urls_from_transcript,
    try_automerge_pr,
    _gh_update_branch,
    _gh_merge,
)


class TestParsePrRef:
    def test_valid_url(self):
        assert _parse_pr_ref("https://github.com/owner/repo/pull/42") == ("owner/repo", "42")

    def test_valid_url_with_trailing_path(self):
        result = _parse_pr_ref("https://github.com/owner/repo/pull/123")
        assert result == ("owner/repo", "123")

    def test_http_url(self):
        assert _parse_pr_ref("http://github.com/org/project/pull/7") == ("org/project", "7")

    def test_invalid_url_no_pull(self):
        assert _parse_pr_ref("https://github.com/owner/repo/issues/42") is None

    def test_invalid_url_not_github(self):
        assert _parse_pr_ref("https://gitlab.com/owner/repo/pull/42") is None

    def test_empty_string(self):
        assert _parse_pr_ref("") is None


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Create a mock subprocess with the given return code and outputs."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestGhUpdateBranch:
    @pytest.mark.asyncio
    async def test_successful_update(self):
        proc = _make_proc(0)
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc):
            result = await _gh_update_branch("owner/repo", "42")
        assert result is True

    @pytest.mark.asyncio
    async def test_already_up_to_date(self):
        proc = _make_proc(1, stderr=b"already up-to-date")
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc):
            result = await _gh_update_branch("owner/repo", "42")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_failure(self):
        proc = _make_proc(1, stderr=b"merge conflict")
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc):
            result = await _gh_update_branch("owner/repo", "42")
        assert result is False


class TestGhMerge:
    @pytest.mark.asyncio
    async def test_successful_merge(self):
        proc = _make_proc(0, stdout=b"Merged")
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc) as mock_exec:
            ok, stderr = await _gh_merge("owner/repo", "42")
        assert ok is True
        assert stderr == ""
        mock_exec.assert_called_once_with(
            "gh", "pr", "merge", "42",
            "--squash", "--delete-branch",
            "--repo", "owner/repo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_merge_failure(self):
        proc = _make_proc(1, stderr=b"conflict")
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc):
            ok, stderr = await _gh_merge("owner/repo", "42")
        assert ok is False
        assert stderr == "conflict"


class TestTryAutomergePr:
    @pytest.mark.asyncio
    async def test_direct_merge_succeeds(self):
        """When direct merge works, no branch update needed."""
        proc = _make_proc(0, stdout=b"Merged")
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=proc):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is True

    @pytest.mark.asyncio
    async def test_merge_after_branch_update(self):
        """When direct merge fails but branch update + retry succeeds."""
        fail_proc = _make_proc(1, stderr=b"branch is behind")
        update_proc = _make_proc(0)
        success_proc = _make_proc(0, stdout=b"Merged")

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_proc  # first merge attempt fails
            elif call_count == 2:
                return update_proc  # branch update succeeds
            else:
                return success_proc  # retry merge succeeds

        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=mock_exec), \
             patch("pyphony.automerge.asyncio.sleep", new_callable=AsyncMock):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is True

    @pytest.mark.asyncio
    async def test_branch_update_fails_gives_up(self):
        """When branch update itself fails, gives up immediately."""
        fail_merge = _make_proc(1, stderr=b"cannot merge")
        fail_update = _make_proc(1, stderr=b"merge conflict in update")

        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fail_merge
            else:
                return fail_update

        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=mock_exec), \
             patch("pyphony.automerge.asyncio.sleep", new_callable=AsyncMock):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is False

    @pytest.mark.asyncio
    async def test_exhausted_retries(self):
        """When merge keeps failing after updates, exhausts retries."""
        fail_proc = _make_proc(1, stderr=b"cannot merge")
        update_proc = _make_proc(0)

        async def mock_exec(*args, **kwargs):
            # Check if this is an update-branch call or a merge call
            if "update-branch" in args:
                return update_proc
            return fail_proc

        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=mock_exec), \
             patch("pyphony.automerge.asyncio.sleep", new_callable=AsyncMock):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_url_returns_false(self):
        result = await try_automerge_pr("https://example.com/not-a-pr")
        assert result is False

    @pytest.mark.asyncio
    async def test_gh_not_found(self):
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=FileNotFoundError):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is False

    @pytest.mark.asyncio
    async def test_unexpected_exception(self):
        with patch("pyphony.automerge.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")
        assert result is False


def _write_transcript(lines: list[dict]) -> str:
    """Write transcript JSONL to a temp file, return path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return path


class TestExtractPrUrlsFromTranscript:
    def test_none_path(self):
        assert extract_pr_urls_from_transcript(None) == []

    def test_missing_file(self):
        assert extract_pr_urls_from_transcript("/nonexistent/path.jsonl") == []

    def test_pr_url_in_tool_result(self):
        """PR URL found in a tool_result entry (e.g. gh pr create output)."""
        path = _write_transcript([
            {"type": "tool_result", "content": "https://github.com/toloka-partners/taiga-examples/pull/2\n"},
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/toloka-partners/taiga-examples/pull/2"]
        finally:
            os.unlink(path)

    def test_pr_url_in_assistant_text(self):
        """PR URL found in an assistant message text block."""
        path = _write_transcript([
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I created PR https://github.com/owner/repo/pull/42 for you."},
                    ],
                },
            },
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/owner/repo/pull/42"]
        finally:
            os.unlink(path)

    def test_pr_url_in_result_field(self):
        """PR URL found in a ResultMessage-style entry."""
        path = _write_transcript([
            {"result": "[DONE] PR: https://github.com/owner/repo/pull/99"},
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/owner/repo/pull/99"]
        finally:
            os.unlink(path)

    def test_deduplication(self):
        """Same URL appearing multiple times is returned once."""
        path = _write_transcript([
            {"type": "tool_result", "content": "https://github.com/o/r/pull/1\n"},
            {"result": "Created https://github.com/o/r/pull/1"},
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/o/r/pull/1"]
        finally:
            os.unlink(path)

    def test_multiple_prs(self):
        """Multiple different PR URLs are all returned."""
        path = _write_transcript([
            {"type": "tool_result", "content": "https://github.com/o/r/pull/1\n"},
            {"type": "tool_result", "content": "https://github.com/o/r2/pull/2\n"},
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/o/r/pull/1", "https://github.com/o/r2/pull/2"]
        finally:
            os.unlink(path)

    def test_no_pr_urls(self):
        """Transcript with no PR URLs returns empty list."""
        path = _write_transcript([
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done!"}]}},
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == []
        finally:
            os.unlink(path)

    def test_nested_tool_result_content(self):
        """PR URL in nested tool_result content list."""
        path = _write_transcript([
            {
                "type": "tool_result",
                "content": [
                    {"type": "text", "text": "https://github.com/org/proj/pull/5"},
                ],
            },
        ])
        try:
            urls = extract_pr_urls_from_transcript(path)
            assert urls == ["https://github.com/org/proj/pull/5"]
        finally:
            os.unlink(path)
