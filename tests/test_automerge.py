"""Tests for the automerge module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pyphony.automerge import _parse_pr_ref, try_automerge_pr


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


class TestTryAutomergePr:
    @pytest.mark.asyncio
    async def test_successful_merge(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"Merged", b""))

        with patch("pyphony.automerge.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")

        assert result is True
        mock_exec.assert_called_once_with(
            "gh", "pr", "merge", "42",
            "--squash",
            "--delete-branch",
            "--repo", "owner/repo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_merge_failure(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"merge conflict"))

        with patch("pyphony.automerge.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")

        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_url_returns_false(self):
        result = await try_automerge_pr("https://example.com/not-a-pr")
        assert result is False

    @pytest.mark.asyncio
    async def test_gh_not_found(self):
        with patch("pyphony.automerge.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=FileNotFoundError):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")

        assert result is False

    @pytest.mark.asyncio
    async def test_unexpected_exception(self):
        with patch("pyphony.automerge.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await try_automerge_pr("https://github.com/owner/repo/pull/42")

        assert result is False
