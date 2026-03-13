"""Tests for pyphony.url_handler."""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from pyphony.url_handler import (
    _escape_for_applescript,
    _is_app_installed,
    open_in_iterm,
    open_in_terminal_app,
    handle_url,
    parse_pyphony_url,
    _build_command,
)


# ---------------------------------------------------------------------------
# _escape_for_applescript
# ---------------------------------------------------------------------------


class TestEscapeForApplescript:
    def test_plain_string(self):
        assert _escape_for_applescript("hello") == "hello"

    def test_double_quotes(self):
        assert _escape_for_applescript('say "hi"') == 'say \\"hi\\"'

    def test_backslashes(self):
        assert _escape_for_applescript("path\\to\\file") == "path\\\\to\\\\file"

    def test_mixed(self):
        assert _escape_for_applescript('"a\\b"') == '\\"a\\\\b\\"'

    def test_empty_string(self):
        assert _escape_for_applescript("") == ""


# ---------------------------------------------------------------------------
# _is_app_installed
# ---------------------------------------------------------------------------


class TestIsAppInstalled:
    @patch("pyphony.url_handler.subprocess.run")
    def test_iterm_installed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="/Applications/iTerm.app\n")
        assert _is_app_installed("iTerm2") is True

    @patch("pyphony.url_handler.subprocess.run")
    def test_iterm_not_installed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        assert _is_app_installed("iTerm2") is False

    @patch("pyphony.url_handler.subprocess.run")
    def test_mdfind_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert _is_app_installed("iTerm2") is False

    @patch("pyphony.url_handler.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mdfind", timeout=5)
        assert _is_app_installed("iTerm2") is False


# ---------------------------------------------------------------------------
# open_in_iterm
# ---------------------------------------------------------------------------


class TestOpenInIterm:
    @patch("pyphony.url_handler.subprocess.run")
    @patch("pyphony.url_handler._is_app_installed", return_value=True)
    def test_success(self, mock_installed, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert open_in_iterm("pyphony work SER-1") is True
        # osascript should be called
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0][0] == "osascript"

    @patch("pyphony.url_handler._is_app_installed", return_value=False)
    def test_iterm_not_installed(self, mock_installed):
        assert open_in_iterm("pyphony work SER-1") is False

    @patch("pyphony.url_handler.subprocess.run")
    @patch("pyphony.url_handler._is_app_installed", return_value=True)
    def test_osascript_failure(self, mock_installed, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "osascript")
        assert open_in_iterm("pyphony work SER-1") is False

    @patch("pyphony.url_handler.subprocess.run")
    @patch("pyphony.url_handler._is_app_installed", return_value=True)
    def test_script_creates_window_or_tab(self, mock_installed, mock_run):
        """The AppleScript should handle both cases: no windows and existing windows."""
        mock_run.return_value = MagicMock(returncode=0)
        open_in_iterm("pyphony work SER-1", title="test")
        script = mock_run.call_args[0][0][2]
        assert "create window with default profile" in script
        assert "create tab with default profile" in script
        assert "(count of windows) = 0" in script

    @patch("pyphony.url_handler.subprocess.run")
    @patch("pyphony.url_handler._is_app_installed", return_value=True)
    def test_command_escaping_in_script(self, mock_installed, mock_run):
        """Commands with quotes should be escaped properly."""
        mock_run.return_value = MagicMock(returncode=0)
        open_in_iterm('echo "hello"', title='my "title"')
        script = mock_run.call_args[0][0][2]
        assert 'echo \\"hello\\"' in script
        assert 'my \\"title\\"' in script


# ---------------------------------------------------------------------------
# open_in_terminal_app
# ---------------------------------------------------------------------------


class TestOpenInTerminalApp:
    @patch("pyphony.url_handler.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert open_in_terminal_app("pyphony work SER-1") is True

    @patch("pyphony.url_handler.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "osascript")
        assert open_in_terminal_app("pyphony work SER-1") is False

    @patch("pyphony.url_handler.subprocess.run")
    def test_command_escaping(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        open_in_terminal_app('echo "hello"')
        script = mock_run.call_args[0][0][2]
        assert 'echo \\"hello\\"' in script


# ---------------------------------------------------------------------------
# parse_pyphony_url
# ---------------------------------------------------------------------------


class TestParsePyphonyUrl:
    def test_basic_url(self):
        result = parse_pyphony_url("pyphony://SER-123/work")
        assert result["identifier"] == "SER-123"
        assert result["action"] == "work"

    def test_url_with_query(self):
        result = parse_pyphony_url("pyphony://SER-123/work?interactive=true")
        assert result["identifier"] == "SER-123"
        assert result["interactive"] == "true"

    def test_triple_slash(self):
        result = parse_pyphony_url("pyphony:///SER-123/work")
        assert result["identifier"] == "SER-123"
        assert result["action"] == "work"

    def test_default_action(self):
        result = parse_pyphony_url("pyphony://SER-45")
        assert result["action"] == "work"


# ---------------------------------------------------------------------------
# handle_url
# ---------------------------------------------------------------------------


class TestHandleUrl:
    @patch("pyphony.url_handler.open_in_iterm", return_value=True)
    @patch("pyphony.url_handler._build_command", return_value="pyphony work SER-1 --main")
    def test_opens_iterm_first(self, mock_cmd, mock_iterm):
        handle_url("pyphony://SER-1/work")
        mock_iterm.assert_called_once()

    @patch("pyphony.url_handler.open_in_terminal_app", return_value=True)
    @patch("pyphony.url_handler.open_in_iterm", return_value=False)
    @patch("pyphony.url_handler._build_command", return_value="pyphony work SER-1 --main")
    def test_falls_back_to_terminal(self, mock_cmd, mock_iterm, mock_terminal):
        handle_url("pyphony://SER-1/work")
        mock_iterm.assert_called_once()
        mock_terminal.assert_called_once()

    @patch("pyphony.url_handler.open_in_terminal_app", return_value=False)
    @patch("pyphony.url_handler.open_in_iterm", return_value=False)
    @patch("pyphony.url_handler._build_command", return_value="pyphony work SER-1 --main")
    def test_exits_when_no_terminal(self, mock_cmd, mock_iterm, mock_terminal):
        with pytest.raises(SystemExit):
            handle_url("pyphony://SER-1/work")

    def test_exits_on_invalid_url(self):
        with pytest.raises(SystemExit):
            handle_url("pyphony://")
